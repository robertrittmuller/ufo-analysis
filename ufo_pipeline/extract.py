"""Stage 2: turn every source file into page text / transcripts / frame descriptions.

PDF and image OCR goes remote model -> local model -> Tesseract -> embedded text layer.
Audio goes captions + local ASR -> remote ASR. Every result records which path produced it,
failed pages are never cached as text, and caches are keyed on file content + settings.
"""

from __future__ import annotations

import array
import io
import json
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import llm
from .config import Settings
from .util import FileHasher, atomic_write_json, clean_str, log, normalize_space, read_json, stable_hash, str_list

PAGE_CACHE_VERSION = 1
MEDIA_CACHE_VERSION = 1
OCR_PROMPT_VERSION = 3
FRAME_PROMPT_VERSION = 2

OCR_PROMPT = """You are transcribing one page of a declassified U.S. government record (scan, typescript, teletype, form, handwriting or photograph).

Return ONE JSON object with exactly these keys:
- "text": complete, faithful transcription of ALL text on the page in reading order, as Markdown. Keep original wording, spelling, numbers, dates, names, file numbers, stamps, routing slips, letterhead, marginalia and handwritten notes (mark handwriting as [handwritten: ...]). Use Markdown tables for tabular data and forms ("Label: value"). Write [illegible] for unreadable words and [REDACTED] for each blacked-out or whited-out passage. Do not summarize, translate, correct or invent text. If the page is rotated, still transcribe it. If the page has no text, use "". NEVER transcribe filler: rows of dots, dot leaders after form fields (". . . . ."), ruled lines, underscores or dashes are omitted entirely - write the form label, then the typed or written entry, and stop the line there; a blank form field is written as "Label:" with nothing after it. If the page is the blank reverse of a sheet showing only faint mirror-image show-through of the other side, do NOT transcribe the mirrored text: transcribe only right-reading stamps or notes and describe the show-through in "visual".
- "visual": one or two sentences describing non-text content (photographs, sketches, diagrams, maps, charts, signatures, seals), or "" if none.
- "handwriting": true if the page contains handwriting beyond a signature.
- "redactions": number of distinct redacted passages (0 if none).
- "markings": list of classification / control / declassification markings and FOIA exemption codes exactly as printed (e.g. "SECRET", "UNCLASSIFIED", "(b)(6)", "DECLASSIFIED Authority NND 813016").
- "legibility": one of "good", "fair", "poor", "blank"."""

FRAME_PROMPT = """These are {count} frames sampled in time order from one declassified U.S. government video clip (timestamps: {stamps}). Published title: "{title}".

Describe only what is visible. Return ONE JSON object with these keys:
- "summary": 2-4 sentences describing what the clip shows across the frames.
- "sensor_type": one of "infrared", "electro-optical", "night-vision", "radar-display", "sonar-display", "handheld-camera", "cockpit-display", "film", "other", "unclear".
- "overlay_text": all readable on-screen / HUD text (ranges, bearings, coordinates, times, modes), verbatim, or "".
- "object_description": shape, relative size, contrast/colour and count of the object(s) of interest, or "" if none is discernible.
- "object_motion": apparent motion between frames (and whether it could be camera/platform motion), or "".
- "background": terrain / sea / sky / cloud context, or "".
- "frames": list of {{"t": seconds, "description": one sentence}} for each frame.
- "image_quality": one of "good", "fair", "poor".
- "limits": what cannot be determined from these frames alone (range, size, speed, etc.)."""

DESCRIBE_PROMPT = """Look at this scanned page from a government file and classify it. Return ONE short JSON object:
{"kind": "readable-text" | "mirror-show-through" | "blank" | "image-only", "visual": "one or two sentences describing the page"}
- "readable-text": the page carries right-reading typed, printed or handwritten text meant to be read.
- "mirror-show-through": the blank back of a sheet where only faint reversed (mirror-image) text from the other side shows, possibly with a few stamps.
- "blank": no meaningful content.
- "image-only": a photograph, drawing or map with no text to transcribe."""

ASR_BOILERPLATE = re.compile(
    r"^\W*(thank you( very much)?|thanks for watching|please subscribe|you|bye|okay|music|applause|\[.*?\])\W*$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- PDF / image OCR

def _looks_degenerate(text: str) -> bool:
    """Detect runaway repetition, a common vision-model failure on noisy scans.

    Placeholder lines ([REDACTED], [illegible]) and table rules legitimately repeat, so
    they are ignored.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not re.fullmatch(r"[\[\]|:\-\s*_.]*|\[[^\]]{0,40}\]", line)]
    if len(lines) < 25:
        return False
    run = longest = 1
    for previous, current in zip(lines, lines[1:]):
        run = run + 1 if current == previous and len(current) > 3 else 1
        longest = max(longest, run)
    return longest >= 20


def _tail_loops(text: str) -> bool:
    """True when the text ends in one short unit repeated back-to-back many times."""
    tail = re.sub(r"\s+", " ", text[-2000:]).strip()
    for size in range(1, 81):
        unit = tail[-size:]
        repeats = max(8, 320 // size)
        if len(tail) >= size * repeats and unit.strip(" |-_.") and tail.endswith(unit * repeats):
            return True
    return False


def _strip_tail_loop(text: str) -> str:
    """Remove a back-to-back repeated unit from the end of the text."""
    for _round in range(4):
        tail_start = max(0, len(text) - 6000)
        tail = text[tail_start:]
        best = None
        for size in range(1, 81):
            unit = tail[-size:]
            if not unit.strip():
                continue
            count = 0
            while tail.endswith(unit * (count + 1)) and (count + 1) * size <= len(tail):
                count += 1
            if count >= max(8, 320 // size) and (best is None or count * size > best):
                best = count * size
        if best is None:
            break
        text = text[: len(text) - best].rstrip()
    return text


_DOT_LEADER = re.compile(r"(?:[ \t]*\.){5,}[ \t]*")


def clean_ocr_text(text: str) -> str:
    """Drop dot leaders and ruled filler that carry no information."""
    text = _DOT_LEADER.sub(" ", text)
    text = re.sub(r"_{6,}", " ", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _alnum(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def _tesseract(jpeg: bytes) -> str:
    if not shutil.which("tesseract"):
        return ""
    try:
        import pytesseract
        from PIL import Image

        return pytesseract.image_to_string(Image.open(io.BytesIO(jpeg)), config="--psm 3").strip()
    except Exception as exc:  # pytesseract raises a variety of errors
        log(f"WARNING: tesseract failed: {exc}")
        return ""


def ocr_image_bytes(jpeg: bytes, chain: llm.Chain, text_layer: str = "") -> Dict[str, Any]:
    """OCR one rendered page. Returns a page record (without page number)."""
    record: Dict[str, Any] = {"flags": []}
    messages = [{"role": "user", "content": [{"type": "text", "text": OCR_PROMPT}, llm.image_part(jpeg)]}]
    error = ""
    if chain:
        try:
            def usable(data: Dict[str, Any]) -> None:
                if isinstance(data.get("text"), str) and (_looks_degenerate(data["text"]) or _tail_loops(data["text"])):
                    raise ValueError("degenerate repeated output")

            def salvage(partial: str) -> Optional[Dict[str, Any]]:
                # A cut-off answer is either a very dense page (keep what was read, flagged)
                # or a repetition loop (reject, so the next OCR path is tried).
                match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)', partial, re.DOTALL)
                if not match:
                    return None
                body = match.group(1)
                body = body[: len(body) - 1] if body.endswith("\\") else body
                try:
                    text = json.loads('"' + body + '"')
                except ValueError:
                    return None
                if _tail_loops(text):
                    # Cut the runaway repetition off and keep what was read before it.
                    text = _strip_tail_loop(text)
                    closed = False
                    if _alnum(text) < 200 or _looks_degenerate(text):
                        return None
                    return {"text": text, "_truncated": True}
                if _alnum(text) < 200 or _looks_degenerate(text):
                    return None
                # If the text string was closed, the transcription is complete and only a later
                # metadata field ran away; otherwise the page text itself was cut off.
                closed = partial[match.end():match.end() + 1] == '"'
                return {"text": text, "_truncated": not closed}

            # One page rarely needs more than ~3k tokens; a higher cap only prolongs runaway output.
            data, provider = chain.json(messages, max_tokens=6000, max_tokens_ceiling=6000, required_keys=("text",),
                                        validate=usable, task="ocr", salvage=salvage)
            text = data.get("text")
            text = clean_ocr_text(text) if isinstance(text, str) else ""
            legibility = clean_str(data.get("legibility")).lower()
            record.update({
                "text": text,
                "method": "llm_ocr",
                "provider": provider,
                "visual": clean_str(data.get("visual")),
                "handwriting": bool(data.get("handwriting")),
                "redactions": int(data.get("redactions") or 0) if str(data.get("redactions") or "0").isdigit() else 0,
                "markings": str_list(data.get("markings"), 20),
                "legibility": legibility if legibility in {"good", "fair", "poor", "blank"} else "",
            })
            if data.get("_truncated"):
                record["flags"].append("ocr_truncated")
            # A model that returns far less than the embedded text layer may have skipped content.
            if _alnum(text_layer) > 400 and _alnum(text) < 0.3 * _alnum(text_layer):
                record["flags"].append("ocr_much_shorter_than_text_layer")
                record["alt_text"] = text_layer
            return record
        except llm.LLMError as exc:
            error = str(exc)[:300]
    else:
        error = "no review model available"

    # Both models failed to transcribe. Before trusting Tesseract, ask what kind of page this
    # is: on a blank reverse side with mirror-image show-through, Tesseract returns gibberish.
    if chain:
        try:
            kind, _provider = chain.json(
                [{"role": "user", "content": [{"type": "text", "text": DESCRIBE_PROMPT}, llm.image_part(jpeg)]}],
                max_tokens=700, max_tokens_ceiling=700, required_keys=("kind",), task="ocr")
            label = clean_str(kind.get("kind")).lower()
            if label in ("mirror-show-through", "blank", "image-only"):
                record.update({"text": "", "method": "llm_ocr", "provider": _provider, "visual": clean_str(kind.get("visual"), 600),
                               "handwriting": False, "redactions": 0, "markings": [], "legibility": "blank",
                               "flags": ["no_readable_text:" + label], "llm_error": error})
                return record
        except llm.LLMError:
            pass

    tess = _tesseract(jpeg)
    if _alnum(tess) >= max(20, 0.5 * _alnum(text_layer)):
        record.update({"text": tess, "method": "tesseract", "provider": "tesseract", "flags": ["llm_ocr_failed"], "llm_error": error})
        return record
    if _alnum(text_layer) >= 20:
        record.update({"text": text_layer, "method": "text_layer", "provider": "embedded", "flags": ["llm_ocr_failed"], "llm_error": error})
        return record
    # Nothing usable: record the failure so the page is retried next run instead of cached as text.
    record.update({"text": "", "method": "failed", "provider": "", "error": error or "no OCR path produced text"})
    return record


def _page_cache_key(settings: Settings) -> str:
    return stable_hash([PAGE_CACHE_VERSION, OCR_PROMPT_VERSION, settings.render_dpi, settings.render_max_dimension])


MAX_OCR_ATTEMPTS = 3


def _page_done(record: Optional[Dict[str, Any]]) -> bool:
    """A page is done when it has text from the vision model or is born-digital.

    Pages that only got a fallback (Tesseract / embedded layer) are retried on later runs,
    up to MAX_OCR_ATTEMPTS, so a temporary model problem does not become permanent.
    """
    if not record or record.get("method") in (None, "failed"):
        return False
    if "llm_ocr_failed" in (record.get("flags") or []) and int(record.get("attempts") or 1) < MAX_OCR_ATTEMPTS:
        return False
    return True


def extract_pdf(path: Path, file_sha: str, settings: Settings, chain: llm.Chain, pool: ThreadPoolExecutor,
                page_limit: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
    import fitz

    cache_path = settings.pages_dir / f"{path.stem}.json"
    key = _page_cache_key(settings)
    cached = read_json(cache_path) or {}
    pages: Dict[str, Dict[str, Any]] = {}
    if not force and cached.get("file_sha") == file_sha and cached.get("key") == key:
        pages = {k: v for k, v in (cached.get("pages") or {}).items() if _page_done(v)}
    earlier = (cached.get("pages") or {}) if cached.get("file_sha") == file_sha and cached.get("key") == key else {}

    document = fitz.open(str(path))
    try:
        if document.needs_pass:
            raise RuntimeError("PDF is password protected")
        page_count = document.page_count
        wanted = range(1, (min(page_count, page_limit) if page_limit else page_count) + 1)
        todo = [n for n in wanted if str(n) not in pages]
        lock = threading.Lock()
        gate = threading.Semaphore(settings.ocr_concurrency * 3)  # bound rendered pages held in memory
        completed = [0]

        def save() -> None:
            atomic_write_json(cache_path, {
                "version": PAGE_CACHE_VERSION, "file": path.name, "file_sha": file_sha, "key": key,
                "page_count": page_count, "pages": pages,
            })

        def finish(number: int, record: Dict[str, Any]) -> None:
            with lock:
                pages[str(number)] = record
                completed[0] += 1
                if completed[0] % 20 == 0:
                    save()
                    log(f"  {path.name}: {completed[0]}/{len(todo)} pages")

        def job(number: int, jpeg: bytes, text_layer: str, base: Dict[str, Any]) -> None:
            try:
                record = ocr_image_bytes(jpeg, chain, text_layer)
            except Exception as exc:  # never let one page kill the document
                record = {"text": "", "method": "failed", "provider": "", "error": f"{type(exc).__name__}: {exc}"[:300], "flags": []}
            finally:
                gate.release()
            record.update(base)
            record["attempts"] = int((earlier.get(str(number)) or {}).get("attempts") or 0) + 1
            finish(number, record)

        futures = []
        for number in todo:
            page = document[number - 1]
            native = page.get_text("text").strip()
            page_area = max(1.0, abs(page.rect))
            image_area = 0.0
            try:
                for info in page.get_image_info():
                    x0, y0, x1, y1 = info["bbox"]
                    image_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            except Exception:
                image_area = page_area  # be conservative: treat as a scan
            coverage = min(1.0, image_area / page_area)
            base = {"native_chars": _alnum(native), "image_coverage": round(coverage, 2)}
            # Born-digital page: real text and no significant imagery. Everything else is a
            # scan (its text layer, if any, is somebody else's OCR and is not trusted) or
            # carries pictures worth describing, so it goes to the vision model.
            if _alnum(native) >= 120 and coverage < 0.15:
                finish(number, dict(base, text=native, method="native", provider="pdf", flags=[]))
                continue
            zoom = settings.render_dpi / 72.0
            longest = max(page.rect.width, page.rect.height) * zoom
            if longest > settings.render_max_dimension:
                zoom *= settings.render_max_dimension / longest
            try:
                jpeg = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("jpeg", jpg_quality=85)
            except Exception as exc:
                if _alnum(native) >= 20:
                    finish(number, dict(base, text=native, method="text_layer", provider="embedded", flags=["render_failed"]))
                else:
                    finish(number, dict(base, text="", method="failed", provider="", error=f"render failed: {exc}"[:300], flags=[]))
                continue
            gate.acquire()
            futures.append(pool.submit(job, number, jpeg, native, base))
        for future in futures:
            future.result()
        save()
    finally:
        document.close()
    return {"page_count": page_count, "pages": pages}


def extract_image(path: Path, file_sha: str, settings: Settings, chain: llm.Chain, force: bool = False) -> Dict[str, Any]:
    from PIL import Image

    cache_path = settings.pages_dir / f"{path.stem}.json"
    key = _page_cache_key(settings)
    cached = read_json(cache_path) or {}
    if not force and cached.get("file_sha") == file_sha and cached.get("key") == key and _page_done((cached.get("pages") or {}).get("1")):
        return {"page_count": 1, "pages": cached["pages"]}
    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size
        image.thumbnail((settings.render_max_dimension, settings.render_max_dimension))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88)
    record = ocr_image_bytes(buffer.getvalue(), chain)
    record.update({"native_chars": 0, "image_coverage": 1.0, "width": width, "height": height})
    pages = {"1": record}
    atomic_write_json(cache_path, {"version": PAGE_CACHE_VERSION, "file": path.name, "file_sha": file_sha, "key": key, "page_count": 1, "pages": pages})
    return {"page_count": 1, "pages": pages}


# --------------------------------------------------------------------------- audio / video

def _run(args: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def probe(path: Path) -> Dict[str, Any]:
    result = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.decode('utf-8', 'replace')[:200]}")
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    info: Dict[str, Any] = {"duration": None, "has_video": False, "has_audio": False}
    try:
        info["duration"] = round(float(data.get("format", {}).get("duration")), 2)
    except (TypeError, ValueError):
        pass
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not (stream.get("disposition") or {}).get("attached_pic"):
            info.update({"has_video": True, "video_codec": stream.get("codec_name"), "width": stream.get("width"),
                         "height": stream.get("height"), "frame_rate": stream.get("avg_frame_rate")})
        elif kind == "audio":
            info.update({"has_audio": True, "audio_codec": stream.get("codec_name"), "sample_rate": stream.get("sample_rate"),
                         "channels": stream.get("channels")})
    return info


def sample_frames(path: Path, duration: float, count: int, out_dir: Path) -> List[Dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: List[Dict[str, Any]] = []
    duration = max(duration or 0.0, 0.1)
    count = max(1, min(count, int(duration) + 1))
    for index in range(count):
        stamp = round(duration * (index + 0.5) / count, 2)
        target = out_dir / f"f{index:02d}.jpg"
        result = _run(["ffmpeg", "-v", "error", "-y", "-ss", str(stamp), "-i", str(path), "-frames:v", "1",
                       "-vf", "scale='min(1280,iw)':-2", "-q:v", "4", str(target)], 180)
        if result.returncode == 0 and target.exists() and target.stat().st_size > 500:
            frames.append({"t": stamp, "file": target.name})
    return frames


def review_frames(frames: List[Dict[str, Any]], frame_dir: Path, title: str, chain: llm.Chain) -> Dict[str, Any]:
    """Describe sampled frames. Retries with fewer frames if the request is rejected."""
    selection = frames
    last_error = ""
    while selection:
        content: List[Dict[str, Any]] = [{"type": "text", "text": FRAME_PROMPT.format(
            count=len(selection), stamps=", ".join(f"{f['t']}s" for f in selection), title=title)}]
        content += [llm.image_part((frame_dir / f["file"]).read_bytes()) for f in selection]
        try:
            data, provider = chain.json([{"role": "user", "content": content}], max_tokens=6000, required_keys=("summary",), task="ocr")
            return {
                "summary": clean_str(data.get("summary")),
                "sensor_type": clean_str(data.get("sensor_type")).lower() or "unclear",
                "overlay_text": clean_str(data.get("overlay_text")),
                "object_description": clean_str(data.get("object_description")),
                "object_motion": clean_str(data.get("object_motion")),
                "background": clean_str(data.get("background")),
                "image_quality": clean_str(data.get("image_quality")).lower(),
                "limits": clean_str(data.get("limits")),
                "frames": [{"t": f.get("t"), "description": clean_str(f.get("description"))}
                           for f in data.get("frames", []) if isinstance(f, dict)][:20],
                "frames_reviewed": len(selection),
                "provider": provider,
            }
        except llm.LLMError as exc:
            last_error = str(exc)[:300]
            if len(selection) <= 2:
                break
            selection = selection[::2]
    raise llm.LLMError(last_error or "no frames")


def fetch_captions(urls: Any) -> Tuple[str, str]:
    """Return (plain caption text with timestamps, source url) or ("", reason)."""
    if not isinstance(urls, dict):
        return "", "no caption url"
    url = urls.get("srt") or urls.get("webvtt")
    if not url:
        return "", "no caption url"
    raw = ""
    reason = ""
    try:
        from curl_cffi import requests  # war.gov / DVIDS block default Python user agents

        response = requests.get(url, impersonate="chrome124", timeout=60)
        if response.status_code == 200:
            raw = response.text
        else:
            reason = f"HTTP {response.status_code}"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:200]
    if not raw.strip():
        return "", reason or "empty caption file"
    lines: List[str] = []
    stamp = ""
    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.isdigit() or line.upper().startswith("WEBVTT"):
            continue
        match = re.match(r"(\d{2}:\d{2}:\d{2})[.,]\d{3}\s*-->", line)
        if match:
            stamp = match.group(1)
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text:
            lines.append(f"[{stamp}] {text}" if stamp else text)
            stamp = ""
    text = "\n".join(lines)
    if _alnum(text) < 8:
        return "", "empty"  # fetched fine; the caption track simply has no words
    return text, url


def _rms_dbfs(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes)) as handle:
        samples = array.array("h", handle.readframes(handle.getnframes()))
    if not samples:
        return -120.0
    step = max(1, len(samples) // 60000)
    subset = samples[::step]
    mean_square = sum(s * s for s in subset) / len(subset)
    if mean_square <= 0:
        return -120.0
    import math

    return 10 * math.log10(mean_square / (32768.0 ** 2))


def _asr_text_ok(text: str) -> Tuple[bool, str]:
    """Reject common ASR hallucinations on noise: wrong script, boilerplate, loops."""
    clean = normalize_space(text)
    if _alnum(clean) < 3:
        return False, "empty"
    letters = [ch for ch in clean if ch.isalpha()]
    if letters and sum(1 for ch in letters if ord(ch) > 0x24F) / len(letters) > 0.15:
        return False, "non-latin script in an English recording (likely hallucination)"
    if ASR_BOILERPLATE.match(clean):
        return False, "boilerplate phrase"
    words = clean.lower().split()
    if len(words) >= 12 and len(set(words)) / len(words) < 0.2:
        return False, "repetition loop"
    return True, ""


def transcribe_audio(path: Path, duration: float, settings: Settings) -> Dict[str, Any]:
    """Transcribe the whole audio track in fixed chunks; local model first, remote as backup."""
    chain = settings.audio_chain()
    result: Dict[str, Any] = {"text": "", "chunks": [], "status": "", "providers": []}
    if not chain:
        result["status"] = "no transcription model available"
        return result
    seconds = settings.audio_chunk_seconds
    with tempfile.TemporaryDirectory() as tmp:
        pattern = str(Path(tmp) / "c%05d.wav")
        split = _run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                      "-f", "segment", "-segment_time", str(seconds), "-c:a", "pcm_s16le", pattern], 3600)
        files = sorted(Path(tmp).glob("c*.wav"))
        if split.returncode != 0 or not files:
            result["status"] = f"audio extraction failed: {split.stderr.decode('utf-8', 'replace')[:200]}"
            return result
        down: set = set()
        fails: Dict[str, int] = {}
        for index, chunk_path in enumerate(files):
            start = index * seconds
            chunk: Dict[str, Any] = {"start": start, "end": min(start + seconds, int(duration or start + seconds))}
            wav = chunk_path.read_bytes()
            level = _rms_dbfs(wav)
            chunk["level_db"] = round(level, 1)
            if level < -55:
                chunk["status"] = "silent"
                result["chunks"].append(chunk)
                continue
            errors: List[str] = []
            for endpoint in chain:
                if endpoint.label in down:
                    continue
                try:
                    text = llm.transcribe(endpoint, wav)
                except llm.LLMError as exc:
                    errors.append(str(exc)[:200])
                    # Repeated hard failures: stop trying this endpoint for this file only.
                    fails[endpoint.label] = fails.get(endpoint.label, 0) + 1
                    if fails[endpoint.label] >= 3:
                        down.add(endpoint.label)
                    continue
                fails[endpoint.label] = 0
                ok, why = _asr_text_ok(text)
                chunk.update({"provider": endpoint.label, "status": "ok" if ok else "rejected", "text": text if ok else "",
                              "rejected_text": "" if ok else text[:200], "reason": why})
                break
            else:
                chunk.update({"status": "failed", "error": " | ".join(errors)[:400]})
            result["chunks"].append(chunk)
    spoken = [c for c in result["chunks"] if c.get("status") == "ok"]
    result["text"] = "\n".join(f"[{c['start'] // 60:02d}:{c['start'] % 60:02d}] {c['text']}" for c in spoken)
    result["providers"] = sorted({c["provider"] for c in spoken})
    failed = sum(1 for c in result["chunks"] if c.get("status") == "failed")
    if failed:
        result["status"] = f"{failed} of {len(result['chunks'])} chunks failed"
    elif spoken:
        result["status"] = "ok"
    else:
        result["status"] = "no intelligible speech detected"
    result["complete"] = failed == 0
    return result


def _media_cache_key(settings: Settings) -> str:
    return stable_hash([MEDIA_CACHE_VERSION, FRAME_PROMPT_VERSION, settings.video_frames, settings.audio_chunk_seconds,
                        [e.model for e in settings.audio_chain()]])


def extract_media(path: Path, file_sha: str, entry: Dict[str, Any], settings: Settings, chain: llm.Chain,
                  force: bool = False) -> Dict[str, Any]:
    cache_path = settings.media_dir / f"{path.stem}.json"
    key = _media_cache_key(settings)
    cached = read_json(cache_path) or {}
    record: Dict[str, Any] = {}
    if not force and cached.get("file_sha") == file_sha and cached.get("key") == key:
        record = cached
    record.update({"version": MEDIA_CACHE_VERSION, "file": path.name, "file_sha": file_sha, "key": key})
    issues: List[str] = []

    if not record.get("probe"):
        record["probe"] = probe(path)
    info = record["probe"]
    duration = info.get("duration") or float(entry.get("duration_seconds") or 0)

    frame_dir = settings.media_dir / "frames" / path.stem
    if info.get("has_video") and str(entry.get("media_type")) != "audio":
        frames = record.get("frames") or []
        if not frames or not all((frame_dir / f["file"]).exists() for f in frames):
            frames = sample_frames(path, duration, settings.video_frames, frame_dir)
            record["frames"] = frames
            record.pop("frame_review", None)
        if not frames:
            issues.append("no video frames could be extracted")
        elif not record.get("frame_review"):
            try:
                record["frame_review"] = review_frames(frames, frame_dir, str(entry.get("title") or path.stem), chain)
            except llm.LLMError as exc:
                issues.append(f"frame review failed: {exc}")

    if "captions" not in record or (record["captions"] or {}).get("retry"):
        text, source = fetch_captions(entry.get("closed_caption_urls"))
        if text:
            record["captions"] = {"text": text, "source": source}
        elif source == "empty":
            record["captions"] = {"text": "", "note": "upstream caption track contains no text"}
        elif entry.get("closed_caption_urls"):
            record["captions"] = {"text": "", "retry": True, "error": source}
            issues.append(f"captions listed upstream but not retrieved: {source}")
        else:
            record["captions"] = None

    if info.get("has_audio"):
        asr = record.get("asr")
        if not asr or not asr.get("complete"):
            record["asr"] = transcribe_audio(path, duration, settings)
            if not record["asr"].get("complete"):
                issues.append(f"transcription incomplete: {record['asr'].get('status')}")
    else:
        record["asr"] = {"text": "", "chunks": [], "status": "no audio track", "complete": True, "providers": []}

    record["issues"] = issues
    atomic_write_json(cache_path, record)
    return record


# --------------------------------------------------------------------------- stage driver

def list_sources(settings: Settings, only: Optional[List[str]] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Manifest entries whose file exists. Files outside the manifest are reported, not ingested."""
    from .download import find_orphans, load_manifest

    manifest = load_manifest(settings)
    if not manifest:
        raise SystemExit(f"No source manifest at {settings.manifest_path}. Run: python -m ufo_pipeline download")
    entries = []
    for entry in manifest:
        if not (settings.sources_dir / str(entry["filename"])).exists():
            log(f"WARNING: {entry['filename']} is in the manifest but missing on disk; run the download stage")
            continue
        if only and not any(token.lower() in str(entry["filename"]).lower() for token in only):
            continue
        entries.append(entry)
    orphans = find_orphans(settings, manifest)
    if orphans and not only:
        log(f"NOTE: {len(orphans)} files in data/sources are not in the manifest and are ignored")
    entries.sort(key=lambda e: str(e["filename"]).lower())
    return entries[:limit] if limit else entries


def run(settings: Settings, only: Optional[List[str]] = None, limit: Optional[int] = None,
        page_limit: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
    entries = list_sources(settings, only, limit)
    chain = llm.Chain(settings.review_chain(), timeout=settings.request_timeout)
    hasher = FileHasher(settings.cache_dir / "file_hashes.json")
    issues: Dict[str, List[str]] = {}
    stats = {"sources": len(entries), "pages": 0, "failed_pages": 0, "methods": {}}
    lock = threading.Lock()
    done = [0]

    def work(entry: Dict[str, Any]) -> None:
        name = str(entry["filename"])
        path = settings.sources_dir / name
        media_type = str(entry.get("media_type"))
        problems: List[str] = []
        result = None
        try:
            sha = hasher.sha256(path)
            if media_type == "pdf":
                result = extract_pdf(path, sha, settings, chain, pool, page_limit, force)
            elif media_type == "image":
                result = extract_image(path, sha, settings, chain, force)
            else:
                problems = list(extract_media(path, sha, entry, settings, chain, force).get("issues") or [])
        except Exception as exc:
            problems = [f"extraction failed: {type(exc).__name__}: {exc}"]
            log(f"WARNING: {name}: {type(exc).__name__}: {exc}")
        with lock:
            done[0] += 1
            if result:
                failed = [n for n, page in result["pages"].items() if page.get("method") == "failed"]
                for page in result["pages"].values():
                    stats["pages"] += 1
                    method = page.get("method") or "failed"
                    stats["methods"][method] = stats["methods"].get(method, 0) + 1
                if failed:
                    stats["failed_pages"] += len(failed)
                    problems.append(f"{len(failed)} page(s) have no text after every OCR path failed: {', '.join(sorted(failed, key=int)[:20])}")
            if problems:
                issues[name] = problems
            log(f"[extract {done[0]}/{len(entries)}] {name}" + (f" - {len(problems)} issue(s)" if problems else ""))
            if done[0] % 10 == 0:
                hasher.save()

    # Several documents are in flight at once and share one page pool, so a few slow
    # pages at the end of one document do not leave the other workers idle.
    with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool, \
            ThreadPoolExecutor(max_workers=settings.extract_documents) as doc_pool:
        list(doc_pool.map(work, entries))
    hasher.save()
    stats["issues"] = issues
    log(f"Extract finished: {stats['sources']} sources, {stats['pages']} pages {stats['methods']}, "
        f"{stats['failed_pages']} failed pages, {len(issues)} sources with issues")
    return stats
