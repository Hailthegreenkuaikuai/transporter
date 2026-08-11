# AGENT.md

Guidance for coding agents working in this repo.

## What this is

**Video Transporter** — paste share text, extract URLs, download videos with yt-dlp, driven from a local Flask web UI. One file of server logic, one HTML template, zero build step.

## Layout

| Path | Role |
|------|------|
| `app.py` | All server logic: Flask routes, yt-dlp job orchestration, cookie handling, config |
| `templates/index.html` | Single-page UI, vanilla JS (no framework), talks to `/api/*` |
| `config.json` | Runtime state: `base_dir`, `dirs` (pinned folders), `history` (last 100) |
| `.venv/` | Virtualenv (Python 3.14, uv-managed). No requirements.txt — deps are flask, yt-dlp, playwright |

## How it works

- **Jobs**: `POST /api/download` → `start_job()` spawns one daemon thread per batch of URLs; progress lives in the in-memory `JOBS` dict, polled by the UI (`GET /api/jobs`, prunes finished jobs).
- **Download**: shells out to `yt-dlp` (falls back to `python -m yt_dlp` if the exe is missing) with `--newline --progress --progress-template` and `--print after_move:filepath`. One merged stdout stream is parsed per line: progress lines match `TPL_RE`, error/warning/`[` lines go to the job log, everything else is a downloaded filepath.
- **Cookies**: a real Chromium browser must be **closed** to export its cookie DB. The `playwright` source is the app's own UI browser — exported live via `ctx.cookies()` (no DB lock, immune to Edge/Chrome v20 app-bound encryption), falling back to reading the persistent profile's cookie DB when the window is closed (dead context). `_cookie_hint()` routes export failures (DPAPI/v20, locked, missing) to the right advice. Logic in `_resolve_cookies()`: reuse `D:/Transport/cookies.txt` if valid, else auto-export from the selected browser via `yt_dlp.cookies.extract_cookies_from_browser()` (or `export_playwright_cookies()` for `playwright`), else fall back to `--cookies-from-browser`. On Windows, Chromium stores expiries as WebTime (µs since 1601) — `_normalize_expiry()` converts.
- **Dirs**: `resolve_dir()` treats absolute paths as-is, bare names nest under `base_dir`. `GET /api/dirs` scans top-level subfolders of the base dir (mirrors the filesystem) plus manually-pinned dirs from config.
- **History**: each download appends to `<dir>/.transporter.json` and the global `config.json` history.
- **UI browser**: `_open_ui_browser()` launches a dedicated Playwright Edge/Chromium window so its profile never locks real browser cookies. Dies with the process; falls back to the system browser.

## Running

```bash
uv run python app.py          # start server + UI window at http://127.0.0.1:5000
uv run python app.py --selftest   # run built-in assertions (pure logic, no network)
```

## Conventions

- **Single-file, stdlib-first**: keep logic in `app.py`. Don't add modules, frameworks, or abstraction layers for things a few lines of Python can do.
- **Pony-tail style**: shortest working change. Prefer deletion and reuse over new structure.
- **Self-check**: non-trivial logic gets one `assert`-based `_selftest()` case (no test framework, no pytest).
- **Don't touch**: `.venv/`, `__pycache__/`, `config.json` (user state), downloaded media.
- `config.json` and history paths are user-specific (Windows `D:/Transport`) — don't hardcode assumptions in tests or new code.

## API surface (for reference)

`GET /` · `POST /api/extract` · `GET|POST|DELETE /api/dirs` · `POST /api/download` · `GET /api/jobs` · `GET /api/history` · `GET /api/base` · `POST /api/choose-dir` · `GET /api/cookies/status` · `POST /api/cookies/export`
