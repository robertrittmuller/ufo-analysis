"""OpenAI-compatible client with retries, truncation detection and endpoint fallback chains.

Design notes (each fixes a defect found in the pre-refactor pipeline):
- every transport error is caught (socket timeouts are not TimeoutError on Python 3.9);
- transient failures are retried with backoff before an endpoint is given up on;
- finish_reason == "length" is an error, never a silently short answer;
- a chain records which endpoint produced each result, and a failing endpoint is
  rested for a cooldown rather than disabled for the rest of the run.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Endpoint
from .util import log

USER_AGENT = "ufo-pipeline/2.0"
TRANSPORT_ERRORS = (urllib.error.URLError, http.client.HTTPException, socket.timeout, OSError, ValueError)


class LLMError(Exception):
    """A request failed after retries."""


class TruncatedError(LLMError):
    """The model hit max_tokens before finishing. `partial` holds what it wrote."""

    def __init__(self, message: str, partial: str = "") -> None:
        super().__init__(message)
        self.partial = partial


class AllEndpointsFailed(LLMError):
    pass


def _headers(endpoint: Endpoint, content_type: Optional[str] = "application/json") -> Dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    return headers


def _request(endpoint: Endpoint, path: str, body: Optional[bytes], headers: Dict[str, str], timeout: int) -> Any:
    request = urllib.request.Request(endpoint.base_url.rstrip("/") + path, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _post_with_retries(
    endpoint: Endpoint, path: str, body: bytes, headers: Dict[str, str], timeout: int, retries: int
) -> Any:
    last = ""
    for attempt in range(retries + 1):
        try:
            return _request(endpoint, path, body, headers, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            last = f"HTTP {exc.code}: {detail}"
            # 4xx other than rate limiting/timeouts will not improve on retry.
            if exc.code not in (408, 409, 425, 429) and exc.code < 500:
                raise LLMError(last)
        except TRANSPORT_ERRORS as exc:
            last = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, socket.timeout) or "timed out" in str(exc):
                # A request that ran out the clock will usually do so again; let the
                # caller fall back to another endpoint instead of waiting three times.
                raise LLMError(f"{endpoint.label} {path} timed out after {timeout}s")
        if attempt < retries:
            time.sleep(min(60, 3 * (2 ** attempt)))
    raise LLMError(f"{endpoint.label} {path} failed after {retries + 1} attempts: {last}")


def list_models(endpoint: Endpoint) -> Any:
    """Return the list of served model ids, or an error string."""
    try:
        payload = _request(endpoint, "/models", None, _headers(endpoint, None), 20)
        return [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)]
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except TRANSPORT_ERRORS as exc:
        return f"{type(exc).__name__}: {exc}"


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_NO_JSON_MODE: set = set()  # endpoint labels that rejected response_format


def chat(
    endpoint: Endpoint,
    messages: List[Dict[str, Any]],
    max_tokens: int = 8000,
    json_mode: bool = False,
    timeout: int = 300,
    retries: int = 2,
    temperature: float = 0.0,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """One chat completion. Returns (text, usage). Raises LLMError / TruncatedError."""
    body: Dict[str, Any] = {
        "model": endpoint.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if extra:
        body.update(extra)
    use_json_mode = json_mode and endpoint.label not in _NO_JSON_MODE
    if use_json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        payload = None
        for attempt in range(3):
            payload = _post_with_retries(
                endpoint, "/chat/completions", json.dumps(body).encode("utf-8"), _headers(endpoint), timeout, retries
            )
            # Some gateways report a transient failure as an error object with HTTP 200.
            if isinstance(payload, dict) and payload.get("error") and not payload.get("choices") and attempt < 2:
                time.sleep(5 * (3 ** attempt))
                continue
            break
    except LLMError as exc:
        if use_json_mode and "HTTP 4" in str(exc) and "response_format" in str(exc):
            _NO_JSON_MODE.add(endpoint.label)
            return chat(endpoint, messages, max_tokens, json_mode, timeout, retries, temperature, extra)
        if extra and "HTTP 4" in str(exc) and "HTTP 429" not in str(exc) and "HTTP 408" not in str(exc):
            # The server rejected the request; the optional tuning parameters are the likely cause.
            log(f"{endpoint.label} rejected the request with extra parameters {list(extra)}; retrying without them")
            return chat(endpoint, messages, max_tokens, json_mode, timeout, retries, temperature, None)
        raise
    try:
        choice = payload["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMError(f"{endpoint.label} returned an unexpected payload: {str(payload)[:300]}")
    if isinstance(text, list):  # some servers return content parts
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    text = _THINK_RE.sub("", text).strip()
    if choice.get("finish_reason") == "length":
        raise TruncatedError(f"{endpoint.label} hit max_tokens={max_tokens}", text)
    if not text:
        raise LLMError(f"{endpoint.label} returned empty content (finish_reason={choice.get('finish_reason')})")
    return text, payload.get("usage") or {}


def parse_json_object(text: str) -> Dict[str, Any]:
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    candidate = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    try:
        value = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model output")
        try:
            value = json.loads(candidate[start : end + 1])
        except ValueError as exc:
            raise ValueError(f"invalid JSON in model output: {exc}")
    # Some models wrap the requested object in a one-element list.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    # ...or flatten it into ["key", value, "key", value, ...].
    if isinstance(value, list) and len(value) >= 2 and len(value) % 2 == 0 and all(isinstance(k, str) for k in value[0::2]) \
            and len(set(value[0::2])) == len(value) // 2 and all(len(k) <= 40 for k in value[0::2]) \
            and not all(isinstance(v, str) and re.fullmatch(r"[a-z_]{2,24}", v) for v in value[1::2]):  # not a bare list of key names
        value = dict(zip(value[0::2], value[1::2]))
    if not isinstance(value, dict):
        raise ValueError("model output was JSON but not an object")
    return value


def image_part(jpeg_bytes: bytes) -> Dict[str, Any]:
    data = base64.b64encode(jpeg_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


class Usage:
    """Thread-safe token/request counters per endpoint, reported at the end of a run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data: Dict[str, Dict[str, int]] = {}

    def add(self, label: str, usage: Dict[str, Any], ok: bool = True) -> None:
        with self._lock:
            row = self.data.setdefault(label, {"requests": 0, "failures": 0, "prompt_tokens": 0, "completion_tokens": 0})
            if ok:
                row["requests"] += 1
                row["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                row["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            else:
                row["failures"] += 1


USAGE = Usage()


class Chain:
    """Ordered endpoints tried in turn (e.g. remote then local) with a per-endpoint cooldown."""

    FAILURES_BEFORE_REST = 4
    REST_SECONDS = 180

    def __init__(self, endpoints: Sequence[Endpoint], timeout: int = 300) -> None:
        self.endpoints = [e for e in endpoints if e and e.configured]
        self.timeout = timeout
        self._lock = threading.Lock()
        self._consecutive: Dict[str, int] = {}
        self._rest_until: Dict[str, float] = {}

    def __bool__(self) -> bool:
        return bool(self.endpoints)

    def _available(self) -> List[Endpoint]:
        now = time.time()
        with self._lock:
            ready = [e for e in self.endpoints if self._rest_until.get(e.label, 0) <= now]
        # If everything is resting, still try: better slow than dropping data.
        return ready or list(self.endpoints)

    def _record(self, endpoint: Endpoint, ok: bool) -> None:
        with self._lock:
            if ok:
                self._consecutive[endpoint.label] = 0
                return
            count = self._consecutive.get(endpoint.label, 0) + 1
            self._consecutive[endpoint.label] = count
            if count >= self.FAILURES_BEFORE_REST:
                self._rest_until[endpoint.label] = time.time() + self.REST_SECONDS
                self._consecutive[endpoint.label] = 0
                log(f"{endpoint.label} failed {count} times in a row; resting it for {self.REST_SECONDS}s")

    def json(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 8000,
        max_tokens_ceiling: int = 32000,
        required_keys: Sequence[str] = (),
        validate: Optional[Any] = None,
        task: str = "synthesis",
        salvage: Optional[Any] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Return (parsed JSON object, endpoint label).

        Per endpoint: a truncated answer is retried once with a doubled budget, an
        unparseable answer is retried once. TruncatedError is re-raised if every
        endpoint truncates, so callers can split their input instead of losing it.
        """
        errors: List[str] = []
        truncated_everywhere = True
        for endpoint in self._available():
            budget = max_tokens
            for attempt in range(3):
                try:
                    text, usage = chat(endpoint, messages, max_tokens=budget, json_mode=True, timeout=self.timeout,
                                       extra=endpoint.params.get(task) or None)
                    data = parse_json_object(text)
                    missing = [key for key in required_keys if key not in data]
                    if missing:
                        raise ValueError(f"missing keys {missing}")
                    if validate is not None:
                        validate(data)  # raises ValueError to reject unusable output
                    USAGE.add(endpoint.label, usage)
                    self._record(endpoint, True)
                    return data, endpoint.label
                except TruncatedError as exc:
                    USAGE.add(endpoint.label, {}, ok=False)
                    errors.append(str(exc))
                    if salvage is not None:
                        # The caller may be able to use a cut-off answer (e.g. a long page transcription).
                        rescued = salvage(exc.partial)
                        if rescued is not None:
                            self._record(endpoint, True)
                            return rescued, endpoint.label
                    if budget >= max_tokens_ceiling:
                        break
                    budget = min(max_tokens_ceiling, budget * 2)
                except ValueError as exc:  # bad JSON: one more try, it is usually transient
                    USAGE.add(endpoint.label, {}, ok=False)
                    errors.append(f"{endpoint.label} bad JSON: {exc}")
                    truncated_everywhere = False
                    if attempt >= 1:
                        self._record(endpoint, False)
                        break
                except LLMError as exc:
                    USAGE.add(endpoint.label, {}, ok=False)
                    errors.append(str(exc))
                    truncated_everywhere = False
                    self._record(endpoint, False)
                    break
        if not self.endpoints:
            raise AllEndpointsFailed("no review endpoint configured")
        if truncated_everywhere and errors:
            raise TruncatedError("; ".join(errors[-2:]))
        raise AllEndpointsFailed("; ".join(errors[-3:]))


def embed(endpoint: Endpoint, texts: List[str], timeout: int = 120) -> List[List[float]]:
    body = json.dumps({"model": endpoint.model, "input": texts}).encode("utf-8")
    payload = _post_with_retries(endpoint, "/embeddings", body, _headers(endpoint), timeout, retries=2)
    try:
        rows = sorted(payload["data"], key=lambda item: item.get("index", 0))
        vectors = [row["embedding"] for row in rows]
    except (KeyError, TypeError):
        raise LLMError(f"{endpoint.label} returned an unexpected embeddings payload")
    if len(vectors) != len(texts):
        raise LLMError(f"{endpoint.label} returned {len(vectors)} embeddings for {len(texts)} inputs")
    return vectors


def transcribe(endpoint: Endpoint, wav_bytes: bytes, timeout: int = 180, language: str = "en") -> str:
    """Transcribe a WAV clip. Tries /audio/transcriptions, then chat with input_audio."""
    boundary = uuid.uuid4().hex
    parts: List[bytes] = []
    for name, value in (("model", endpoint.model), ("language", language), ("response_format", "json")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
    )
    parts.append(wav_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    headers = _headers(endpoint, f"multipart/form-data; boundary={boundary}")
    first_error = ""
    try:
        payload = _post_with_retries(endpoint, "/audio/transcriptions", b"".join(parts), headers, timeout, retries=1)
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return payload["text"].strip()
        first_error = f"unexpected payload {str(payload)[:200]}"
    except LLMError as exc:
        first_error = str(exc)
    # Fallback: chat completion with an input_audio part. Dedicated ASR gateways reject
    # text parts, general audio models need an instruction, so try both shapes.
    audio_part = {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav_bytes).decode("ascii"), "format": "wav"}}
    instruction = {
        "type": "text",
        "text": "Transcribe the speech in this audio verbatim. Output only the transcript. "
        "If there is no intelligible speech, output exactly: [NO SPEECH]",
    }
    errors = [first_error]
    for content in ([audio_part], [instruction, audio_part]):
        try:
            text, _usage = chat(endpoint, [{"role": "user", "content": content}], max_tokens=4000, timeout=timeout, retries=1)
        except LLMError as exc:
            if "empty content" in str(exc):
                return ""  # an ASR model answering nothing means no speech
            errors.append(str(exc))
            continue
        return "" if "[NO SPEECH]" in text else text.strip()
    raise LLMError(f"{endpoint.label} transcription failed: " + " | ".join(errors))
