# UFO Analysis Dashboard

This repository builds a static HTML dashboard at `dashboard/index.html`.

## Build the Dashboard Locally

Install the Python dependencies and run the dashboard builder from the repo root:

```sh
pip install -r requirements.txt
python3 tools/build_dashboard.py
```

The generated dashboard is written to `dashboard/index.html`.

## OCR a Document with the Local LLM

Render PDF pages to images and ask the same local multimodal model used by the dashboard review flow to produce one Markdown OCR file:

```sh
python3 tools/ocr_with_llm.py data/sources/38_143685_box7_Incident_Summaries_1-100.pdf --pages 1 --output-dir data/ocr_markdown
```

Use `--concurrency` to control simultaneous LLM requests; it defaults to 4. Use `--pages-per-request` to control how many page images are sent in each LLM call.

## Deploy to Cloudflare Workers

This project is configured for Cloudflare Workers Static Assets. Cloudflare only uploads the already-built files in `dashboard/`; it does not run the Python dashboard build.

Deploy the current dashboard with:

```sh
npx wrangler deploy
```

For a local Wrangler preview:

```sh
npx wrangler dev
```

The Worker configuration lives in `wrangler.toml`.
