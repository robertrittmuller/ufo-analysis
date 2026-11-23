# UAP Files Research Dashboard

An interactive research dashboard for the U.S. Department of War [PURSUE](https://www.war.gov/ufo/) UAP/UFO file releases. The pipeline downloads every released document, image, video and audio file, transcribes it, writes a page-cited deep analysis report for each file, and publishes a static site:

- **Overview** – corpus synthesis, incident timeline and map, explanation and evidence breakdowns, data-quality panel.
- **Reports** – one deep report per released file: summary and key points with page citations, every incident in the file, the record's own conclusion kept separate from a machine assessment, provenance and redaction details, people and entities, related reports, and a page-by-page source-text reader.
- **Incidents** – every extracted observation event, filterable and exportable (CSV/JSON).
- **People & orgs**, **full-text search** over all page text, and a **methods** page.

View the [live dashboard](https://ufo-analysis.rittmuller.com/).

This is an independent project. It is not affiliated with, endorsed by, or operated by any government agency. Summaries, classifications and assessments are machine-generated research aids; check the cited page of the original file before quoting or relying on a claim.

## How it works

```
download ─▶ extract ─▶ analyze ─▶ embed ─▶ build
war.gov     OCR,        deep       related    static site data
manifest    frames,     report     reports,   (dashboard/data)
            audio       per file   same event
```

| Stage | What it does | Models |
|---|---|---|
| `download` | Reads the war.gov CSV manifest and DVIDS metadata, downloads files to a temp name, validates them (size, file signature) and records a checksum. One bad row never aborts the run. | – |
| `extract` | Every scanned PDF page and image is transcribed by a vision model, which also notes handwriting, redactions, markings and pictures. Born-digital pages use their native text. Video is sampled into frames that are described; audio is transcribed in full in 60-second chunks, plus official captions where available. | Remote → local → Tesseract → embedded text layer (OCR). Local → remote (audio). |
| `analyze` | Reads the whole text in page-aligned parts (nothing is sampled), extracts constituent records, incidents, entities and key passages, then writes the file-level report. Quotes are checked against page text; dates and places are normalised. | Remote → local |
| `embed` | Embeds reports and incidents to link related reports and the same event described in different files. | Local |
| `build` | Writes `dashboard/data/` – a compact index, one JSON per report (loaded on demand), full-text search shards, frame thumbnails – and a corpus synthesis. | Remote → local |

Every stage is **resumable**. Results are cached by file content and settings, a failed page or part is never cached as if it had succeeded, and re-running a stage redoes only missing or failed work. Each result records which model or fallback produced it; the published dashboard shows only the type of model (for example, remote vision-language model or local speech-recognition model), never the specific model name, per page and per report.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg tesseract        # macOS; required for video/audio and the OCR fallback
cp .env.example .env                 # then fill in endpoints and keys
```

### Model configuration (`.env`)

The pipeline uses three types of model, all served through OpenAI-compatible APIs: **vision-language models** (multimodal LLMs) for OCR, analysis and report writing, a **speech-recognition model** for audio transcription, and a **text-embedding model** for linking related reports. Any models of these types can be configured.

| Variable | Role |
|---|---|
| `REMOTE_UFO_REVIEW_BASE_URL`, `REMOTE_UFO_REVIEW_API_KEY`, `REMOTE_UFO_REVIEW_MODEL` | Primary model for OCR, analysis and report generation (OpenAI-compatible, vision-capable). |
| `REMOTE_UFO_AUDIO_TRANSCRIPT_MODEL` | Remote backup for audio transcription. Optional. |
| `LOCAL_UFO_REVIEW_BASE_URL`, `LOCAL_UFO_REVIEW_API_KEY` | Local OpenAI-compatible server. |
| `LOCAL_UFO_REVIEW_MODEL` | Local backup for OCR and analysis. Optional but recommended. |
| `LOCAL_UFO_AUDIO_TRANSCRIPT_MODEL` | Primary audio transcription model (local). |
| `LOCAL_UFO_EMBEDDING_MODEL` | Embedding model (local). |
| `REMOTE_UFO_OCR_PARAMS`, `REMOTE_UFO_EXTRACT_PARAMS`, `REMOTE_UFO_SYNTHESIS_PARAMS` | Optional JSON merged into the request for each task, e.g. to switch off hidden reasoning for transcription. If the server rejects them the request is retried without. |
| `UFO_OCR_CONCURRENCY`, `UFO_ANALYSIS_CONCURRENCY`, `UFO_REQUEST_TIMEOUT`, `UFO_SITE_URL` | Optional tuning. |

A preflight check runs before any stage. It verifies `ffmpeg`, `ffprobe`, `tesseract` and every configured endpoint and model, stops if a stage could not produce valid output, and warns when a backup is unavailable:

```sh
python -m ufo_pipeline check
```

## Running

```sh
python -m ufo_pipeline all        # download + extract + analyze + embed + build
python -m ufo_pipeline process    # everything except download
tools/update_dashboard.sh         # download, then process (continues if some downloads fail)
```

Individual stages: `download`, `extract`, `analyze`, `embed`, `build`.

Useful options:

| Option | Purpose |
|---|---|
| `--only TEXT` | Only sources whose filename contains TEXT (repeatable). |
| `--limit N`, `--page-limit N` | Scratch runs on a subset. |
| `--force` | Ignore caches for the selected stage and sources. |
| `--site-dir DIR` | Write the site data somewhere else. Required for partial builds – a subset is never published into `dashboard/`. |
| `--no-corpus-summary` | Skip the model-written corpus synthesis in `build`. |
| `--prune-orphans` | `download`: move files that are not in the manifest to `data/orphaned/`. |

A full first run over the whole corpus is long (many hours): it is bounded by how fast the remote model generates text. It can be interrupted and restarted at any time.

Scratch build of a few files:

```sh
python -m ufo_pipeline process --only CIA-UAP-D027 --only DOW-UAP-PR055 --site-dir data/scratch-site
cp -R dashboard/index.html dashboard/favicon.svg dashboard/assets data/scratch-site/
python3 -m http.server 8766 --directory data/scratch-site
```

## Viewing and deploying

The site loads its data with `fetch`, so serve the folder rather than opening the file directly:

```sh
python3 -m http.server 8000 --directory dashboard
```

Deployment is Cloudflare Workers Static Assets (`wrangler.toml`); Cloudflare uploads `dashboard/` as built and does not run the pipeline:

```sh
npx wrangler deploy
```

## Repository layout

| Path | Purpose |
|---|---|
| `ufo_pipeline/` | The pipeline package: `download`, `extract`, `analyze`, `embed`, `site`, plus `llm` (client, retries, fallback chains), `geo` (place resolution), `config`, `cli`. |
| `dashboard/index.html`, `dashboard/assets/` | Hand-written front end (no build step). `app.js` is the overview and explorers, `report.js` the deep-report view. |
| `dashboard/data/` | Generated site data (index, reports, search shards, frame thumbnails). |
| `data/sources/` | Downloaded source media. Ignored by Git. |
| `data/processed/source_manifest.json` | Source manifest with checksums. |
| `data/cache/` | Stage caches (`pages/`, `media/`, `analysis/`, embeddings, corpus summary) and `run_issues.json`, the report of the last run. |
| `tools/update_dashboard.sh` | One-command refresh. |

## What the reports contain

The report design follows what UAP researchers, archivists and FOIA practitioners ask of a record:

- **Three kinds of date kept apart** – incident date, document date and release date.
- **Incident facts** modelled on the Air Force's own reporting format (AFR 200-2): time and duration, observer location, witnesses and their role, manner of observation and sensors, object description, weather, official action.
- **The record's conclusion vs. the analyst assessment**, always shown separately. Candidate conventional explanations carry supporting and contradicting evidence and an intelligence-community likelihood band.
- **Standard scales** – Hynek class, Vallée class and SVP rating, NATO Admiralty source-reliability and information-credibility codes (rated separately).
- **Documentation strength** – a transparent, computed 0–100 score for how well an incident is documented (sensing, witnesses, specificity, documentation). It is not a measure of how anomalous the event is.
- **Provenance** – originating office, file numbers, classification markings as printed, declassification, redaction level and exemption codes, items referenced but missing.
- **Verification** – every quote is labelled as an exact match, close match or not found in the page text; every page shows how its text was obtained.

"Unexplained in the record" means the file offers no resolution. It does not mean the event is anomalous.

## Data and publishing notes

- `data/` is ignored by Git; the full source corpus is tens of gigabytes.
- `dashboard/data/` contains the generated analysis and extracted page text. Review it before publishing.
- Directories from the previous pipeline (`data/ocr_markdown/`, `data/processed/documents/`, `data/processed/page_text/`, `data/processed/transcripts/`, `data/reviewed/`) are no longer read and can be deleted.

## License

See [LICENSE](LICENSE).
