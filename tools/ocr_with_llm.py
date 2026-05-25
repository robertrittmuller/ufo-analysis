#!/usr/bin/env python3
"""Render document pages and OCR them with the local multimodal LLM."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import http.client
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib import error as urllib_error
from urllib import request as urllib_request


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "ocr_markdown"
DEFAULT_BASE_URL = "http://localhost:4321/v1"
DEFAULT_MODEL = "Qwen3.6-35B-A3B-UD-MLX-4bit"
DEFAULT_API_KEY = "REDACTED"
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class PageImage:
  page_number: int
  data_url: str
  image_path: Path | None = None
  compact_hint: bool = False
  source_path: Path | None = None
  source_page_index: int | None = None


@dataclass(frozen=True)
class PageOcr:
  page_number: int
  markdown: str


class LengthLimitError(RuntimeError):
  pass


@dataclass(frozen=True)
class OcrClient:
  base_url: str
  model_name: str
  api_key: str | None
  timeout_seconds: int
  max_tokens: int
  compact_first: bool = False

  def chat(
      self,
      content: str | list[dict[str, object]],
      response_format: dict[str, object] | None = None,
  ) -> str:
    payload = {
      "model": self.model_name,
      "messages": [
        {
          "role": "system",
          "content": (
            "You are a careful document OCR engine. Return only the requested final JSON answer. "
            "Never include reasoning, analysis, drafts, plans, or commentary about the task."
          ),
        },
        {"role": "user", "content": content},
      ],
      "temperature": 0,
      "max_tokens": self.max_tokens,
      "enable_thinking": False,
      "chat_template_kwargs": {"enable_thinking": False},
    }
    if response_format is not None:
      payload["response_format"] = response_format
    headers = {"Content-Type": "application/json"}
    if self.api_key:
      headers["Authorization"] = f"Bearer {self.api_key}"

    base_url = self.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
      base_url = f"{base_url}/v1"
    request = urllib_request.Request(
      url=f"{base_url}/chat/completions",
      data=json.dumps(payload).encode("utf-8"),
      headers=headers,
      method="POST",
    )
    with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
      try:
        response_payload = json.loads(response.read().decode("utf-8"))
      except http.client.IncompleteRead as error:
        raise RuntimeError(f"incomplete model response: {error}") from error

    choices = response_payload.get("choices") or []
    if not choices:
      raise RuntimeError("model response did not include any choices")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
      raise LengthLimitError("model stopped before returning final JSON because max_tokens was reached")
    message = choice.get("message") or {}
    content_text = message.get("content")
    if isinstance(content_text, str) and content_text.strip():
      return content_text.strip()
    raise RuntimeError("model response did not include final content")


def unwrap_markdown_fence(text: str) -> str:
  cleaned = text.strip()
  match = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
  return match.group(1).strip() if match else cleaned


def extract_json_object(text: str) -> dict[str, object] | None:
  candidate = text.strip()
  if not candidate:
    return None

  fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
  if fenced_match:
    candidate = fenced_match.group(1).strip()
  elif not candidate.startswith("{"):
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
      return None
    candidate = candidate[start : end + 1]

  try:
    payload = json.loads(candidate)
  except json.JSONDecodeError:
    return None
  return payload if isinstance(payload, dict) else None


def parse_page_selection(value: str | None, page_count: int) -> list[int]:
  if not value:
    return list(range(page_count))

  selected: list[int] = []
  for part in value.split(","):
    token = part.strip()
    if not token:
      continue
    if "-" in token:
      start_text, end_text = token.split("-", 1)
      start = int(start_text) if start_text else 1
      end = int(end_text) if end_text else page_count
      if start > end:
        raise ValueError(f"invalid descending page range: {token}")
      selected.extend(range(start - 1, end))
    else:
      selected.append(int(token) - 1)

  unique = sorted(dict.fromkeys(selected))
  invalid = [index + 1 for index in unique if index < 0 or index >= page_count]
  if invalid:
    raise ValueError(f"page selection outside document range 1-{page_count}: {invalid}")
  return unique


def image_bytes_to_data_url(
    image_bytes: bytes,
    max_dimension: int,
    jpeg_quality: int,
) -> str:
  try:
    from PIL import Image
  except ImportError as error:
    raise RuntimeError("Pillow is required. Install dependencies with `pip install -r requirements.txt`.") from error

  with Image.open(io.BytesIO(image_bytes)) as image:
    image = image.convert("RGB")
    if max_dimension > 0:
      image.thumbnail((max_dimension, max_dimension))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=jpeg_quality, optimize=True)
  encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
  return f"data:image/jpeg;base64,{encoded}"


def page_has_dotted_form_leaders(page: object) -> bool:
  try:
    text_dictionary = page.get_text("dict")
  except Exception:
    return False

  dot_clusters = 0
  text_spans = 0
  for block in text_dictionary.get("blocks", []):
    for line in block.get("lines", []):
      for span in line.get("spans", []):
        text = str(span.get("text") or "")
        if text.strip():
          text_spans += 1
        if re.search(r"(?:\.\s*){8,}", text):
          dot_clusters += 1
  return dot_clusters >= 8 or (dot_clusters >= 4 and text_spans <= 80)


def render_pdf_pages(
    path: Path,
    pages: str | None,
    scale: float,
    max_dimension: int,
    jpeg_quality: int,
    page_image_dir: Path | None,
) -> list[PageImage]:
  try:
    import fitz
  except ImportError as error:
    raise RuntimeError("PyMuPDF is required. Install dependencies with `pip install -r requirements.txt`.") from error

  rendered: list[PageImage] = []
  with fitz.open(path) as doc:
    page_indices = parse_page_selection(pages, doc.page_count)
    for page_index in page_indices:
      page = doc[page_index]
      compact_hint = page_has_dotted_form_leaders(page)
      pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
      png_bytes = pixmap.tobytes("png")
      data_url = image_bytes_to_data_url(png_bytes, max_dimension, jpeg_quality)
      image_path = None
      if page_image_dir is not None:
        page_image_dir.mkdir(parents=True, exist_ok=True)
        image_path = page_image_dir / f"{path.stem}-page-{page_index + 1:04d}.jpg"
        image_path.write_bytes(base64.b64decode(data_url.partition(",")[2]))
      rendered.append(
        PageImage(
          page_number=page_index + 1,
          data_url=data_url,
          image_path=image_path,
          compact_hint=compact_hint,
          source_path=path,
          source_page_index=page_index,
        )
      )
  return rendered


def render_image_document(
    path: Path,
    max_dimension: int,
    jpeg_quality: int,
    page_image_dir: Path | None,
) -> list[PageImage]:
  image_bytes = path.read_bytes()
  data_url = image_bytes_to_data_url(image_bytes, max_dimension, jpeg_quality)
  image_path = None
  if page_image_dir is not None:
    page_image_dir.mkdir(parents=True, exist_ok=True)
    image_path = page_image_dir / f"{path.stem}-page-0001.jpg"
    image_path.write_bytes(base64.b64decode(data_url.partition(",")[2]))
  return [PageImage(page_number=1, data_url=data_url, image_path=image_path, source_path=path, source_page_index=0)]


def render_document_pages(
    path: Path,
    pages: str | None,
    scale: float,
    max_dimension: int,
    jpeg_quality: int,
    page_image_dir: Path | None,
) -> list[PageImage]:
  suffix = path.suffix.lower()
  if suffix == ".pdf":
    return render_pdf_pages(path, pages, scale, max_dimension, jpeg_quality, page_image_dir)
  if suffix in IMAGE_SUFFIXES:
    if pages and pages.strip() not in {"1", "1-1"}:
      raise ValueError("image inputs only support page selection 1")
    return render_image_document(path, max_dimension, jpeg_quality, page_image_dir)
  raise ValueError(f"unsupported input type: {path.suffix}")


def batched(items: list[PageImage], size: int) -> Iterable[list[PageImage]]:
  for index in range(0, len(items), size):
    yield items[index : index + size]


def build_ocr_prompt(input_path: Path, pages: list[PageImage], compact: bool = False) -> list[dict[str, object]]:
  page_numbers = ", ".join(str(page.page_number) for page in pages)
  compact_instruction = (
    "- COMPACT FORM MODE: for forms, output one concise labeled line per visible field. Omit blank dotted leaders, empty continuation lines, and repeated fill characters entirely. Keep only filled-in values and meaningful empty-field markers.\n"
    if compact
    else ""
  )
  prompt = (
    "Convert the attached document page image(s) into Markdown OCR and return a JSON object.\n\n"
    "Output requirements:\n"
    "- Return only valid JSON with one top-level key named pages.\n"
    "- pages must be an array with one object for each input page, in input order.\n"
    "- Each page object must have page_number and markdown keys.\n"
    "- page_number must be the provided source PDF page number, not a visible handwritten page number or incident page number.\n"
    "- markdown must contain only the OCR Markdown body for that page. Do not include `## Page N` headings; the script will add those.\n"
    "- Preserve the basic source structure: headings, paragraph breaks, lists, tables, labels, signatures, stamps, and redactions where visible.\n"
    "- Keep page order exactly as provided.\n"
    "- Transcribe readable text faithfully. Use `[illegible]` for text that cannot be read and `[redacted]` for visible redaction blocks.\n"
    "- Never transcribe dotted leader lines or repeated filler characters used for blank form space. For forms, keep the field label and filled-in value only.\n"
    "- Treat dotted leader lines as visual noise, not document text. Do not mention or represent them with dots, ellipses, descriptions, or placeholder text.\n"
    "- Omit blank dotted continuation lines entirely.\n"
    "- If a field has only dotted leaders and no filled-in value, leave the value blank or write `N/S` only when that is visibly written.\n"
    f"{compact_instruction}"
    "- For tables and forms, use Markdown tables when practical; otherwise use compact labeled lines that preserve row/field relationships.\n"
    "- Clearly tag visual material with `[VISUAL: ...]`, including photos, diagrams, stamps, letterhead, handwriting, layout cues, marks, and other non-text evidence.\n"
    "- Include `[VISUAL: ...]` lines for visible classification stamps, declassification boxes, handwritten marks, exhibit labels, punched holes, diagrams, photos, sketches, and unusual layout cues even when some of their text is also transcribed.\n"
    "- Use `[VISUAL: ...]` as the only annotation tag for non-text visual descriptions; do not create alternate tags such as `[Handwritten]` or `[Stamp]`.\n"
    "- Do not use source-visible page numbers, incident numbers, exhibit numbers, or handwritten numbers as page_number values.\n"
    "- Do not summarize or invent missing text. Do not omit meaningful visual details.\n"
    "- Do not include reasoning, analysis, self-correction, drafts, or notes about how you are doing the OCR.\n\n"
    "JSON shape:\n"
    '{"pages":[{"page_number":1,"markdown":"OCR markdown body only"}]}\n\n'
    f"Source filename: {input_path.name}\n"
    f"Pages in this request: {page_numbers}\n\n"
    "/no_think"
  )
  content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
  for page in pages:
    content.append({"type": "text", "text": f"Page {page.page_number} image:"})
    content.append({"type": "image_url", "image_url": {"url": page.data_url}})
  return content


def high_resolution_page_data_url(page: PageImage, max_dimension: int = 2600, jpeg_quality: int = 92) -> str:
  if page.source_path and page.source_path.suffix.lower() == ".pdf" and page.source_page_index is not None:
    try:
      import fitz
    except ImportError as error:
      raise RuntimeError("PyMuPDF is required for high-resolution fallback rendering.") from error
    with fitz.open(page.source_path) as doc:
      pixmap = doc[page.source_page_index].get_pixmap(matrix=fitz.Matrix(3.0, 3.0), alpha=False)
      return image_bytes_to_data_url(pixmap.tobytes("png"), max_dimension, jpeg_quality)

  if page.source_path and page.source_path.exists():
    return image_bytes_to_data_url(page.source_path.read_bytes(), max_dimension, jpeg_quality)

  return page.data_url


def build_tesseract_hint_prompt(input_path: Path, page: PageImage, tesseract_text: str, high_res_data_url: str) -> list[dict[str, object]]:
  prompt = (
    "Use the attached high-resolution page image and the Tesseract OCR text as a hint to produce better Markdown OCR. "
    "Return a JSON object.\n\n"
    "Output requirements:\n"
    "- Return only valid JSON with one top-level key named pages.\n"
    "- pages must contain exactly one object with page_number and markdown keys.\n"
    "- page_number must be the provided source PDF page number.\n"
    "- markdown must contain only the OCR Markdown body for that page. Do not include a `## Page N` heading.\n"
    "- Use the Tesseract text as a line-order and word-shape hint. Correct obvious OCR errors using the image, but do not invent text that is not supported by the image.\n"
    "- Keep the output compact. If a line cannot be resolved, write `[illegible]` instead of exploring alternate readings.\n"
    "- Do not copy Tesseract punctuation noise, repeated dots, repeated dashes, repeated underscores, pipes, or filler marks from blank form fields.\n"
    "- Never transcribe dotted leader lines or repeated filler characters used for blank form space. Keep field labels and filled-in values only.\n"
    "- Stop immediately after the final JSON object.\n"
    "- Preserve meaningful visual information with `[VISUAL: ...]` tags.\n"
    "- Do not include reasoning, analysis, drafts, or commentary about the task.\n\n"
    "JSON shape:\n"
    '{"pages":[{"page_number":1,"markdown":"OCR markdown body only"}]}\n\n'
    f"Source filename: {input_path.name}\n"
    f"Page in this request: {page.page_number}\n\n"
    "Tesseract OCR hint:\n"
    f"{tesseract_text.strip()}\n\n"
    "/no_think"
  )
  return [
    {"type": "text", "text": prompt},
    {"type": "image_url", "image_url": {"url": high_res_data_url}},
  ]


THINKING_LINE_PATTERNS = (
  re.compile(r"^\s*The user wants\b", re.IGNORECASE),
  re.compile(r"^\s*\d+\.\s+\*\*(?:Analyze|Transcribe|Refine|Final)\b", re.IGNORECASE),
  re.compile(r"^\s*[*-]\s+(?:I will|I'll|I need|We need|Let's|Wait,|Actually,|Okay,|Final decision|Final plan)\b", re.IGNORECASE),
  re.compile(r"^\s*[*-]\s+`## Page \d+`\s*$", re.IGNORECASE),
)


def remove_thinking_lines(markdown: str) -> str:
  cleaned = re.sub(r"<think>.*?</think>", "", markdown, flags=re.DOTALL | re.IGNORECASE)
  output_lines: list[str] = []
  previous_line = ""
  repeated_count = 0
  for line in cleaned.splitlines():
    stripped = line.strip()
    if any(pattern.search(line) for pattern in THINKING_LINE_PATTERNS):
      continue
    if stripped and stripped == previous_line:
      repeated_count += 1
      if repeated_count >= 2:
        continue
    else:
      previous_line = stripped
      repeated_count = 0
    output_lines.append(line.rstrip())
  return "\n".join(output_lines).strip()


def remove_page_headings(markdown: str) -> str:
  return re.sub(r"(?m)^\s*#{1,6}\s*Page\s+\d+\s*$\n?", "", markdown).strip()


def remove_filler_runs(markdown: str) -> str:
  cleaned = re.sub(r"(?:\s*\.\s*){8,}", " ", markdown)
  cleaned = re.sub(r"(?:\s*[_-]\s*){8,}", " ", cleaned)
  return cleaned


def sanitize_page_markdown(markdown: str) -> str:
  cleaned = unwrap_markdown_fence(markdown)
  cleaned = remove_thinking_lines(cleaned)
  cleaned = remove_page_headings(cleaned)
  cleaned = remove_filler_runs(cleaned)
  return cleaned.strip()


def parse_ocr_response(raw_text: str, expected_pages: list[PageImage]) -> list[PageOcr]:
  payload = extract_json_object(raw_text)
  if not payload:
    raise RuntimeError("model response did not contain a JSON object")

  pages_value = payload.get("pages")
  if not isinstance(pages_value, list):
    raise RuntimeError("model response JSON did not contain a pages array")

  expected_numbers = [page.page_number for page in expected_pages]
  parsed_by_page: dict[int, PageOcr] = {}
  for index, page_payload in enumerate(pages_value):
    if not isinstance(page_payload, dict):
      continue
    page_number_value = page_payload.get("page_number")
    page_number = page_number_value if isinstance(page_number_value, int) else None
    if page_number not in expected_numbers and index < len(expected_numbers):
      page_number = expected_numbers[index]
    if page_number not in expected_numbers:
      continue
    markdown = page_payload.get("markdown")
    if not isinstance(markdown, str):
      continue
    if page_number in parsed_by_page:
      continue
    parsed_by_page[page_number] = PageOcr(page_number=page_number, markdown=sanitize_page_markdown(markdown))

  found_numbers = set(parsed_by_page)
  missing_numbers = [page_number for page_number in expected_numbers if page_number not in found_numbers]
  if missing_numbers:
    raise RuntimeError(f"model response omitted page(s): {missing_numbers}")
  return [parsed_by_page[page_number] for page_number in expected_numbers]


def output_path_for_input(input_path: Path, output_dir: Path, output_file: Path | None) -> Path:
  if output_file is not None:
    return output_file
  return output_dir / f"{input_path.stem}.md"


def format_page_numbers(page_numbers: list[int]) -> str:
  if not page_numbers:
    return ""

  ranges: list[str] = []
  start = page_numbers[0]
  previous = page_numbers[0]
  for page_number in page_numbers[1:]:
    if page_number == previous + 1:
      previous = page_number
      continue
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    start = previous = page_number
  ranges.append(str(start) if start == previous else f"{start}-{previous}")
  return ", ".join(ranges)


def build_document_markdown(input_path: Path, page_markdown: list[PageOcr], model_name: str) -> str:
  page_range = format_page_numbers([page.page_number for page in page_markdown])
  header = [
    f"# {input_path.stem}",
    "",
    "<!--",
    f"Source: {input_path}",
    f"Model: {model_name}",
    f"OCR pages: {page_range}",
    "-->",
    "",
  ]
  sections: list[str] = []
  for page in page_markdown:
    sections.append(f"## Page {page.page_number}")
    if page.markdown.strip():
      sections.append("")
      sections.append(page.markdown.strip())
    sections.append("")
  return "\n".join(header + sections).rstrip() + "\n"


def write_markdown_output(output_path: Path, input_path: Path, sections: list[PageOcr], model_name: str) -> None:
  output_path.parent.mkdir(parents=True, exist_ok=True)
  output_path.write_text(
    build_document_markdown(input_path, sorted(sections, key=lambda page: page.page_number), model_name),
    encoding="utf-8",
  )


def failed_page_ocr(page: PageImage, error: Exception) -> PageOcr:
  return PageOcr(
    page_number=page.page_number,
    markdown=f"[OCR FAILED: {error}]",
  )


def tesseract_page_text(page: PageImage) -> str:
  try:
    from PIL import Image
    import pytesseract
  except ImportError as error:
    raise RuntimeError("Tesseract fallback requires Pillow and pytesseract. Install dependencies with `pip install -r requirements.txt`.") from error

  try:
    encoded_image = page.data_url.partition(",")[2]
    image_bytes = base64.b64decode(encoded_image)
    with Image.open(io.BytesIO(image_bytes)) as image:
      text = pytesseract.image_to_string(image.convert("RGB"), config="--psm 6")
  except Exception as error:
    raise RuntimeError(f"Tesseract fallback failed: {error}") from error

  return sanitize_page_markdown(text)


def tesseract_page_ocr(page: PageImage, text: str) -> PageOcr:
  cleaned = sanitize_page_markdown(text)
  if not cleaned:
    cleaned = "[OCR FALLBACK: Tesseract produced no readable text]"
  return PageOcr(
    page_number=page.page_number,
    markdown=f"[OCR FALLBACK: Tesseract]\n\n{cleaned}",
  )


def llm_refine_tesseract_ocr(client: OcrClient, input_path: Path, page: PageImage, tesseract_text: str) -> PageOcr:
  high_res_data_url = high_resolution_page_data_url(page)
  response_text = client.chat(
    build_tesseract_hint_prompt(input_path, page, tesseract_text, high_res_data_url),
    response_format={"type": "json_object"},
  )
  parsed = parse_ocr_response(response_text, [page])
  if not parsed:
    raise RuntimeError("LLM Tesseract-refinement response did not include OCR text")
  return PageOcr(
    page_number=page.page_number,
    markdown=f"[OCR FALLBACK: Tesseract + LLM refinement]\n\n{parsed[0].markdown}",
  )


def fallback_or_failed_page_ocr(client: OcrClient, input_path: Path, page: PageImage, llm_error: Exception) -> PageOcr:
  try:
    tesseract_text = tesseract_page_text(page)
  except RuntimeError as tesseract_error:
    combined_error = RuntimeError(f"LLM OCR failed: {llm_error}; Tesseract fallback failed: {tesseract_error}")
    print(f"Skipping failed page {page.page_number}: {combined_error}", file=sys.stderr)
    return failed_page_ocr(page, combined_error)

  if not tesseract_text.strip():
    return tesseract_page_ocr(page, tesseract_text)

  try:
    return llm_refine_tesseract_ocr(client, input_path, page, tesseract_text)
  except (RuntimeError, urllib_error.URLError, TimeoutError, json.JSONDecodeError, http.client.IncompleteRead) as refine_error:
    print(f"LLM refinement failed for page {page.page_number}; using Tesseract output: {refine_error}", file=sys.stderr)
    return tesseract_page_ocr(page, tesseract_text)


def ocr_page_batch(client: OcrClient, input_path: Path, page_batch: list[PageImage], retries: int) -> list[PageOcr]:
  last_error: Exception | None = None
  compact = client.compact_first or any(page.compact_hint for page in page_batch)
  for attempt in range(1, retries + 2):
    try:
      response_text = client.chat(
        build_ocr_prompt(input_path, page_batch, compact=compact),
        response_format={"type": "json_object"},
      )
      return parse_ocr_response(response_text, page_batch)
    except LengthLimitError as error:
      last_error = error
      page_label = ", ".join(str(page.page_number) for page in page_batch)
      if compact:
        break
      compact = True
      if attempt <= retries:
        print(f"Retry {attempt}/{retries} for page(s) {page_label} in compact form mode: {error}", file=sys.stderr)
    except (RuntimeError, urllib_error.URLError, TimeoutError, json.JSONDecodeError, http.client.IncompleteRead) as error:
      last_error = error
      page_label = ", ".join(str(page.page_number) for page in page_batch)
      if attempt <= retries:
        print(f"Retry {attempt}/{retries} for page(s) {page_label}: {error}", file=sys.stderr)
  page_label = ", ".join(str(page.page_number) for page in page_batch)
  raise RuntimeError(f"OCR failed for page(s) {page_label}: {last_error}")


async def process_page_batch(
    semaphore: asyncio.Semaphore,
    client: OcrClient,
    input_path: Path,
    page_batch: list[PageImage],
    retries: int,
) -> list[PageOcr]:
  async with semaphore:
    batch_label = ", ".join(str(page.page_number) for page in page_batch)
    print(f"OCR {input_path.name}: page(s) {batch_label}", file=sys.stderr)
    try:
      return await asyncio.to_thread(ocr_page_batch, client, input_path, page_batch, retries)
    except RuntimeError as batch_error:
      if len(page_batch) <= 1:
        print(f"LLM OCR failed for page {page_batch[0].page_number}; trying Tesseract fallback: {batch_error}", file=sys.stderr)
        return [await asyncio.to_thread(fallback_or_failed_page_ocr, client, input_path, page_batch[0], batch_error)]

      results: list[PageOcr] = []
      print(f"Batch failed for page(s) {batch_label}; retrying pages individually: {batch_error}", file=sys.stderr)
      for single_page in page_batch:
        print(f"OCR {input_path.name}: page(s) {single_page.page_number}", file=sys.stderr)
        try:
          results.extend(await asyncio.to_thread(ocr_page_batch, client, input_path, [single_page], retries))
        except RuntimeError as error:
          print(f"LLM OCR failed for page {single_page.page_number}; trying Tesseract fallback: {error}", file=sys.stderr)
          results.append(await asyncio.to_thread(fallback_or_failed_page_ocr, client, input_path, single_page, error))
      return results


async def ocr_document(
    input_path: Path,
    output_path: Path,
    client: OcrClient,
    pages_per_request: int,
    concurrency: int,
    retries: int,
    pages: str | None,
    scale: float,
    max_dimension: int,
    jpeg_quality: int,
    page_image_dir: Path | None,
) -> None:
  rendered_pages = render_document_pages(
    input_path,
    pages=pages,
    scale=scale,
    max_dimension=max_dimension,
    jpeg_quality=jpeg_quality,
    page_image_dir=page_image_dir,
  )
  if not rendered_pages:
    raise RuntimeError(f"no pages rendered for {input_path}")

  sections: list[PageOcr] = []
  semaphore = asyncio.Semaphore(concurrency)
  tasks = [
    asyncio.create_task(process_page_batch(semaphore, client, input_path, page_batch, retries))
    for page_batch in batched(rendered_pages, pages_per_request)
  ]

  for task in asyncio.as_completed(tasks):
    sections.extend(await task)
    write_markdown_output(output_path, input_path, sections, client.model_name)


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Render document pages and OCR them to Markdown with the local multimodal review model.",
  )
  parser.add_argument("inputs", nargs="+", type=Path, help="Input PDF or image document(s).")
  parser.add_argument(
    "--output-dir",
    type=Path,
    default=DEFAULT_OUTPUT_DIR,
    help="Directory for generated Markdown files when --output-file is not used.",
  )
  parser.add_argument(
    "--output-file",
    type=Path,
    help="Exact Markdown output path. Only valid with one input.",
  )
  parser.add_argument(
    "--overwrite",
    action="store_true",
    help="Recreate Markdown output files even when they already exist.",
  )
  parser.add_argument(
    "--page-image-dir",
    type=Path,
    help="Optional directory where rendered page JPEGs are saved for inspection.",
  )
  parser.add_argument(
    "--pages",
    help="Optional 1-based page selection such as '1', '1-3', or '1,4,8-10'.",
  )
  parser.add_argument(
    "--pages-per-request",
    type=int,
    default=1,
    help="Number of page images to send to the LLM in each request. Increase if your context window can handle it.",
  )
  parser.add_argument(
    "--concurrency",
    type=int,
    default=4,
    help="Maximum number of simultaneous LLM requests.",
  )
  parser.add_argument(
    "--retries",
    type=int,
    default=2,
    help="Number of retry attempts for a page or page batch before stopping.",
  )
  parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible local model base URL.")
  parser.add_argument("--model", default=DEFAULT_MODEL, help="Local multimodal model name.")
  parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key for the local model endpoint.")
  parser.add_argument("--timeout", type=int, default=90, help="Timeout in seconds for each LLM request.")
  parser.add_argument("--max-tokens", type=int, default=5000, help="Maximum output tokens per LLM request.")
  parser.add_argument(
    "--compact-first",
    action=argparse.BooleanOptionalAction,
    default=True,
    help="Use compact form OCR from the first attempt to avoid long dotted-form outputs.",
  )
  parser.add_argument("--render-scale", type=float, default=2.0, help="PDF render scale before image downsampling.")
  parser.add_argument(
    "--max-image-dimension",
    type=int,
    default=1800,
    help="Largest image side sent to the LLM after rendering. Use 0 to disable downsampling.",
  )
  parser.add_argument("--jpeg-quality", type=int, default=88, help="JPEG quality for rendered page images.")
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  if args.output_file is not None and len(args.inputs) != 1:
    print("--output-file can only be used with exactly one input", file=sys.stderr)
    return 2
  if args.pages_per_request < 1:
    print("--pages-per-request must be at least 1", file=sys.stderr)
    return 2
  if args.concurrency < 1:
    print("--concurrency must be at least 1", file=sys.stderr)
    return 2
  if args.retries < 0:
    print("--retries must be 0 or greater", file=sys.stderr)
    return 2
  if not 1 <= args.jpeg_quality <= 100:
    print("--jpeg-quality must be between 1 and 100", file=sys.stderr)
    return 2

  client = OcrClient(
    base_url=args.base_url,
    model_name=args.model,
    api_key=args.api_key or None,
    timeout_seconds=args.timeout,
    max_tokens=args.max_tokens,
    compact_first=args.compact_first,
  )

  try:
    with tempfile.TemporaryDirectory(prefix="ufo-llm-ocr-pages-") as temp_dir:
      for input_path in args.inputs:
        input_path = input_path.resolve()
        if not input_path.exists():
          raise FileNotFoundError(input_path)
        image_dir = None
        if args.page_image_dir is not None:
          image_dir = args.page_image_dir.resolve() / input_path.stem
        else:
          image_dir = Path(temp_dir) / input_path.stem
        output_path = output_path_for_input(input_path, args.output_dir, args.output_file).resolve()
        if output_path.exists() and not args.overwrite:
          print(f"Skipping {input_path}: output already exists at {output_path} (use --overwrite to recreate)", file=sys.stderr)
          continue
        asyncio.run(
          ocr_document(
            input_path=input_path,
            output_path=output_path,
            client=client,
            pages_per_request=args.pages_per_request,
            concurrency=args.concurrency,
            retries=args.retries,
            pages=args.pages,
            scale=args.render_scale,
            max_dimension=args.max_image_dimension,
            jpeg_quality=args.jpeg_quality,
            page_image_dir=image_dir,
          )
        )
        print(f"Wrote {output_path}", file=sys.stderr)
  except (OSError, ValueError, RuntimeError, urllib_error.URLError, TimeoutError, json.JSONDecodeError) as error:
    print(f"error: {error}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
