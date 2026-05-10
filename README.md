# UFO Analysis Dashboard

This repository builds a static HTML dashboard at `dashboard/index.html`.

## Build the Dashboard Locally

Install the Python dependencies and run the dashboard builder from the repo root:

```sh
pip install -r requirements.txt
python3 tools/build_dashboard.py
```

The generated dashboard is written to `dashboard/index.html`.

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
