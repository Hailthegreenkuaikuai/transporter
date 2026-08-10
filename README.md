# Video Transporter

Paste share text (douyin, bilibili, …), strip URLs, download with yt-dlp. Single-file Flask app (`app.py`) plus one HTML template.

## Prerequisites

- **Python 3.10+** (developed on 3.14)
- **`uv`** (used to create the venv) — or any Python + venv/pip
- **yt-dlp**, **Flask**, **Playwright** (see install below)
- **A Chromium browser** for the UI: uses Microsoft Edge via Playwright if installed, else bundled Chromium (`uv run playwright install chromium`)
- **tkinter** (Windows Python builds include it) for the native folder picker
- The browser whose cookies you download with must be **closed** during cookie export (Chromium locks its cookie DB)

## Install

```bash
uv venv
uv pip install flask yt-dlp playwright
# optional: use the bundled browser instead of Edge
uv run playwright install chromium
```

## Run

```bash
uv run python app.py
```

Opens the UI in a dedicated Playwright browser window and serves the app at `http://127.0.0.1:5000`. The UI browser dies with the app; if Playwright fails it falls back to your system browser. Config is saved to `config.json` (base download dir, pinned dirs, recent history).

## Usage

1. Paste share text → **Add URLs from text** (accumulates into a queue).
2. Pick a target folder, browser, and whether to use cookies.
3. **Download**. Progress and errors show live.

Downloads land in the base dir (`D:/Transport` by default, change via **Choose base folder…**). Download history per folder is written to `<dir>/.transporter.json`.

### Cookie notes

- If `D:/Transport/cookies.txt` exists and is fresh, it's reused.
- If stale/missing, it auto-exports from the selected browser (must be closed).
- Export manually: **Export cookies** button, or:

```bash
uv run python app.py --export-cookies <chrome|edge|opera>
```

## CLI flags

| Flag | Effect |
|------|--------|
| `--selftest` | Run built-in assertions, then exit |
| `--export-cookies <browser>` | Export cookies to `cookies.txt`, then exit |
