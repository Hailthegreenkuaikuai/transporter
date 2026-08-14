"""Video Transporter - paste share text, strip URLs, download via yt-dlp."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
BASE_DIR = "D:/Transport"
DEFAULT_DIRS = [BASE_DIR]

URL_RE = re.compile(r"https?://[^\s<>\"'，。；、\)\]]+")
JOBS = {}
JOBS_LOCK = threading.Lock()
UI_CONTEXT = None  # live Playwright browser context of the UI window (set by _open_ui_browser)
UI_LOCK = threading.Lock()  # serialize Playwright calls across threads
# Playwright's sync API is thread-bound: every call must run on the thread that
# created the context. Requests from Flask threads are queued here and serviced by
# the _open_ui_browser loop on the browser's own thread.
UI_CMD_COND = threading.Condition()
_ui_export_reqs = []  # list of {"ready": Event, "result": cookies | Exception}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# our --progress-template lines: "4751360 10485760 2424832.0 5 45.2%"
# (unknown numeric fields render as "NA"; percent empty when total is unknown)
TPL_RE = re.compile(r"^(\d+)\s+([\d.]+|NA)\s+([\d.]+|NA)\s+([\d.]+|NA)\s?(.*)$")


class CookieStaleError(Exception):
    pass


def load_config():
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    else:
        cfg = {}
    cfg.setdefault("base_dir", BASE_DIR)
    cfg.setdefault("dirs", list(DEFAULT_DIRS))
    cfg.setdefault("history", [])
    return cfg


def cookies_path():
    return Path(load_config()["base_dir"]) / "cookies.txt"


def ui_profile_dir():
    """Persistent profile for the app's own Playwright browser (cookies survive restarts)."""
    return str(Path(load_config()["base_dir"]) / ".ui-profile")


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_urls(text):
    """Pull bare URLs out of share text (e.g. douyin's 复制此链接 spam)."""
    return list(dict.fromkeys(URL_RE.findall(text or "")))


def resolve_dir(d):
    """Absolute paths pass through; bare names nest under the configured base dir."""
    d = (d or "").strip().strip('"').strip("'")
    return d if os.path.isabs(d) else os.path.join(load_config()["base_dir"], d)


def ytdlp_cmd(url, out_dir, cookie_args):
    exe = shutil.which("yt-dlp")
    cmd = [exe] if exe else [sys.executable, "-m", "yt_dlp"]
    cmd += cookie_args
    cmd += ["--newline", "--progress", "--no-warnings", "--no-playlist",
            "--print", "after_move:filepath",
            "--progress-template", ("download:%(progress.downloaded_bytes)s "
                                     "%(progress.total_bytes)s %(progress.speed)s "
                                     "%(progress.eta)s %(progress._percent_str)s"),
            "-o", os.path.join(out_dir, "%(title)s.%(ext)s")]
    if "douyin" in url.lower():
        # also capture the creator's 抖音號 (author unique_id); parsed as a DYID: line
        cmd += ["--print", "DYID:%(uploader)s"]
    return cmd + [url]


WEBTIME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def _normalize_expiry(exp):
    """Chromium stores expiries as microseconds since 1601 (WebTime) - convert to Unix seconds."""
    if not exp or exp <= 0:
        return 0
    if exp > 1e12:  # raw WebTime, not Unix seconds
        return int(exp / 1e6 - WEBTIME_EPOCH_OFFSET)
    return int(exp)


def _read_cookies_txt():
    """Parse Netscape cookies.txt -> list of {domain, expires, name}."""
    if not cookies_path().exists():
        return []
    cookies = []
    for line in cookies_path().read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        expires = parts[4]
        cookies.append({"domain": parts[0], "name": parts[5],
                        "expires": _normalize_expiry(int(expires)) if expires.lstrip("-").isdigit() else 0})
    return cookies


def cookies_status():
    now = time.time()
    cookies = _read_cookies_txt()
    expired = [c for c in cookies if 0 < c["expires"] <= now]
    session = [c for c in cookies if c["expires"] == 0]
    # clamp the display window; anything beyond ~50 years is meaningless (and localtime can't handle it)
    future = sorted(e for e in (c["expires"] for c in cookies)
                    if now < e < now + 50 * 365 * 86400)
    domains = {}
    for c in cookies:
        d = c["domain"].lstrip(".")
        domains[d] = domains.get(d, 0) + 1
    return {
        "exists": bool(cookies) or cookies_path().exists(),
        "count": len(cookies), "session": len(session), "expired": len(expired),
        "valid": len(cookies) - len(expired),
        "next_expiry": time.strftime("%Y-%m-%d", time.localtime(future[0])) if future else None,
        "domains": [d for d, _ in sorted(domains.items(), key=lambda kv: -kv[1])[:5]],
    }


def _cookie_line(c):
    """One Netscape-format line, None if the cookie is expired."""
    if c.is_expired():
        return None
    dom = c.domain
    return "\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE",
                      c.path or "/", "TRUE" if c.secure else "FALSE",
                      str(_normalize_expiry(c.expires)),
                      (c.name or "").replace("\t", " ").replace("\n", " "),
                      (c.value or "").replace("\t", " ").replace("\n", " ")])


def _playwright_cookie_line(c):
    """Playwright cookie dict -> one Netscape line (expires is already Unix seconds)."""
    dom = c["domain"]
    return "\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE",
                      c.get("path") or "/", "TRUE" if c.get("secure") else "FALSE",
                      str(int(c.get("expires") or 0)),
                      (c["name"] or "").replace("\t", " ").replace("\n", " "),
                      (c["value"] or "").replace("\t", " ").replace("\n", " ")])


def export_playwright_cookies():
    """Pull cookies from the app's live UI browser via the Playwright API - no DB lock,
    and immune to Edge/Chrome v20 app-bound encryption. Requires the window to be open.
    Playwright's sync API is thread-bound, so the read is queued to the browser's own
    thread (see UI_CMD_COND) rather than called from this Flask thread."""
    with UI_LOCK:
        global UI_CONTEXT
        if UI_CONTEXT is None:
            raise CookieStaleError(
                "the app window is closed - restart the app to reopen it (your douyin "
                "session is saved in its profile), then retry")
        req = {"ready": threading.Event(), "result": None}
        with UI_CMD_COND:
            _ui_export_reqs.append(req)
            UI_CMD_COND.notify()
        if not req["ready"].wait(timeout=15):
            UI_CONTEXT = None  # the browser thread is gone; don't keep probing a dead context
            raise CookieStaleError("the app window is not responding - restart the app and retry")
        res = req["result"]
    if isinstance(res, Exception):
        UI_CONTEXT = None  # window closed while we were waiting
        raise CookieStaleError(
            "the app window is closed - restart the app to reopen it (your douyin "
            "session is saved in its profile), then retry")
    cookies = res
    lines = ["# Netscape HTTP Cookie File"] + [_playwright_cookie_line(c) for c in cookies]
    cookies_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def export_cookies(browser):
    """Reuse yt-dlp's own browser-cookie decryption (incl. Chromium app-bound keys),
    dump the result to D:/Transport/cookies.txt. Fails while the browser is open."""
    if browser == "playwright":
        return export_playwright_cookies()
    from yt_dlp.cookies import extract_cookies_from_browser

    cj = extract_cookies_from_browser(browser)
    lines = ["# Netscape HTTP Cookie File"]
    for c in cj:
        line = _cookie_line(c)
        if line:
            lines.append(line)
    cookies_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def _short_err(e):
    s = str(e).strip().splitlines()
    return s[0][:160] if s else type(e).__name__


def _cookie_hint(e, browser):
    """Actionable fix for a cookie export failure, by failure class."""
    if "DPAPI" in str(e):
        return ("Edge/Chrome app-bound encryption (v20) blocks cookie-DB decryption. "
                "Use 'playwright (app window)' instead: open www.douyin.com in the app "
                "window, then download - cookies are read live, no decryption needed.")
    if browser == "playwright":
        return ("Restart the app to reopen its window, open a douyin video page there "
                "(no login needed; ttwid + passport_csrf_token are set on video pages), "
                "then click 'Export cookies' and retry.")
    return "Close the browser and click 'Export cookies', or untick 'use cookies'."


def _enrich_error(err, use_cookies):
    """Append actionable advice for known yt-dlp failure classes.
    'cookies are needed' (site demands a session) is NOT a cookie-access failure."""
    low = err.lower()
    if "could not find" in low:
        return err + ("\n\nNo cookie database found for this browser on this PC. "
                      "Pick another browser in the dropdown, or close it and click 'Export cookies'.")
    if "fresh cookies" in low:
        return err + ("\n\nDouyin needs fresh cookies, not a login: in the app window open an "
                      "actual douyin VIDEO page (ttwid + passport_csrf_token are only set there, "
                      "not on the homepage), wait for it to load, then click 'Export cookies' "
                      "(or delete cookies.txt) to write them, and retry. "
                      + ("tick 'use cookies' first." if not use_cookies else ""))
    if any(k in low for k in ("cookies are needed", "cookies are required", "requires login",
                              "login required", "not logged in", "logged in", "logged-in")):
        return err + ("\n\nThis site needs cookies: " + (
            "tick 'use cookies' and pick a logged-in browser (close it once so its cookies "
            "can be exported, then it can stay open)."
            if not use_cookies else
            "your saved cookies aren't accepted - re-export from a logged-in browser "
            "(close it, then click 'Export cookies')."))
    if "cookie" in low:
        return err + ("\n\nCookie access failed - this browser's cookies are locked while it is open "
                      "(the 'Chrome cookie database' message is yt-dlp's generic name for Chromium, "
                      "Opera/Edge/Chrome alike). Close the browser and retry - cookies are exported "
                      "automatically and reused for later downloads.")
    return err


def _resolve_cookies(job):
    """Use cookies.txt if fresh; auto re-export when stale/missing and browser is closed."""
    status = cookies_status()
    if status["exists"] and status["valid"] > 0:
        return ["--cookies", str(cookies_path())], "cookies.txt"
    try:
        n = export_cookies(job["browser"])
        src = f"cookies.txt (auto-exported {n})"
        if job["browser"] == "playwright" and n == 0:
            src += " - 0 cookies: open www.douyin.com in the app window first (no login needed)"
        return ["--cookies", str(cookies_path())], src
    except Exception as e:
        if status["exists"] or job["browser"] == "playwright":
            raise CookieStaleError(
                f"Saved cookies are expired and auto re-export failed ({_short_err(e)}). "
                f"{_cookie_hint(e, job['browser'])}")
        return ["--cookies-from-browser", job["browser"]], job["browser"]


def start_job(urls, out_dir, browser, use_cookies):
    with JOBS_LOCK:
        job = {
            "id": f"{time.time_ns()}",
            "urls": urls, "dir": out_dir, "browser": browser, "use_cookies": use_cookies,
            "status": "running", "percent": 0, "index": 0, "total": len(urls),
            "message": "starting...", "files": [], "file_dyids": [], "error": None, "progress": None,
            "dyid": None, "cookie_source": "..." if use_cookies else "none",
        }
        JOBS[job["id"]] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


def _run_job(job):
    job["lines"] = []
    try:
        os.makedirs(job["dir"], exist_ok=True)
        cookie_args, job["cookie_source"] = _resolve_cookies(job) if job["use_cookies"] else ([], "none")
        for i, url in enumerate(job["urls"], 1):
            job["index"], job["percent"], job["message"], job["progress"], job["dyid"] = i, 0, f"downloading {i}/{job['total']}", None, None
            # streams are merged: yt-dlp routes progress/logs/paths to different streams
            # depending on binary and flags, so one loop classifies every line
            # yt-dlp prints file paths (titles) in the locale encoding on Windows
            # (here cp950), which we read as UTF-8 -> mojibake in history/recent
            # downloads. Force the child to emit UTF-8.
            proc = subprocess.Popen(
                ytdlp_cmd(url, job["dir"], cookie_args),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                m = TPL_RE.match(line)
                if m:
                    pct = m.group(5).strip()
                    if pct.endswith("%"):
                        job["percent"] = float(pct[:-1])
                    job["progress"] = {"pct": job["percent"], "down": m.group(1), "total": m.group(2),
                                       "speed": m.group(3), "eta": m.group(4)}
                elif line.startswith("DYID:"):
                    job["dyid"] = None if line == "DYID:NA" else line[5:]  # 抖音號 (author unique_id)
                elif line.startswith(("WARNING:", "ERROR:", "[")):
                    job["lines"].append(line)
                    job["lines"] = job["lines"][-200:]
                    job["message"] = line[-120:]
                else:  # --print after_move:filepath output (absolute paths never start with "[")
                    job["files"].append(line)
                    job["file_dyids"].append(job["dyid"])
            proc.wait()
            if proc.returncode != 0:
                err = "\n".join(dict.fromkeys(job["lines"][-12:]))  # dedupe yt-dlp's retry repeats
                job["status"], job["error"] = "error", _enrich_error(err, job["use_cookies"])
                return
        job["status"] = "done"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)
    finally:
        _record_history(job)


def _record_dir_history(job):
    """Append {time, filename, url, dyid} to <dir>/.transporter.json for each downloaded file."""
    if not job.get("files"):
        return
    with open(os.path.join(job["dir"], ".transporter.json"), "a", encoding="utf-8") as f:
        for file, url, dyid in zip(job["files"], job["urls"], job.get("file_dyids") or []):
            rec = {"time": time.strftime("%Y-%m-%d %H:%M"),
                   "file": os.path.basename(file), "url": url}
            if dyid:
                rec["dyid"] = dyid  # 抖音號 of the video's creator (douyin links only)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _record_history(job):
    if not job.get("files"):
        return
    _record_dir_history(job)
    cfg = load_config()
    cfg["history"] = [{"time": time.strftime("%Y-%m-%d %H:%M"),
                       "dir": job["dir"], "files": job["files"],
                       "dyids": [d for d in job.get("file_dyids") or [] if d]}] + cfg["history"]
    cfg["history"] = cfg["history"][:100]
    save_config(cfg)


app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html", dirs=load_config()["dirs"])


@app.post("/api/extract")
def api_extract():
    data = request.get_json(silent=True) or {}
    return jsonify({"urls": extract_urls(data.get("text", ""))})


def _scan_dirs(base):
    """Top-level subfolders of the base dir, so the dropdown mirrors the filesystem.
    Dot-dirs (.ui-profile = the app's own browser profile) are not download folders."""
    out = []
    try:
        for name in sorted(os.listdir(base)):
            if name.startswith("."):
                continue
            p = os.path.join(base, name)
            if os.path.isdir(p):
                out.append(p)
    except OSError:
        pass
    return out


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _dirs_payload():
    cfg = load_config()
    base = cfg["base_dir"]
    pins = cfg["dirs"]
    dirs = list(dict.fromkeys([base] + _scan_dirs(base) + pins))
    return {"dirs": dirs, "pins": pins}


@app.get("/api/dirs")
def api_get_dirs():
    return jsonify(_dirs_payload())


@app.post("/api/dirs")
def api_add_dir():
    d = (request.get_json(silent=True) or {}).get("dir", "").strip()
    if not d:
        return jsonify({"error": "empty path"}), 400
    resolved = resolve_dir(d)
    try:
        os.makedirs(resolved, exist_ok=True)  # create now so it shows up in the scan
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    cfg = load_config()
    cfg["dirs"] = list(dict.fromkeys(cfg["dirs"] + [resolved]))
    save_config(cfg)
    return jsonify(_dirs_payload())


@app.delete("/api/dirs")
def api_del_dir():
    d = _norm((request.get_json(silent=True) or {}).get("dir", ""))
    cfg = load_config()
    cfg["dirs"] = [x for x in cfg["dirs"] if _norm(x) != d]
    save_config(cfg)
    return jsonify(_dirs_payload())


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    urls = list(dict.fromkeys(u.strip() for u in (data.get("urls") or []) if u.strip()))
    if not urls:
        return jsonify({"error": "no urls"}), 400
    job = start_job(urls, resolve_dir(data.get("dir", BASE_DIR)),
                    data.get("browser", "opera"), bool(data.get("use_cookies", True)))
    return jsonify({"job": _job_view(job)})


def _job_view(job):
    return {k: job.get(k) for k in ("id", "status", "index", "total", "percent",
                                    "message", "files", "error", "dir", "cookie_source", "progress", "dyid")}


@app.get("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["id"], reverse=True)[:10]
        # prune finished jobs
        for j in [x for x in jobs if x["status"] != "running"][5:]:
            JOBS.pop(j["id"], None)
    return jsonify({"jobs": [_job_view(j) for j in jobs]})


@app.get("/api/history")
def api_history():
    """Per-folder download log (<dir>/.transporter.json), newest first.
    Optional ?date=YYYY-MM-DD filters to records whose download time matches."""
    d = request.args.get("dir")
    date = request.args.get("date")
    recs = []
    if d:
        p = Path(d) / ".transporter.json"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not date or rec.get("time", "").startswith(date):
                    recs.append(rec)
    return jsonify(list(reversed(recs)))


@app.get("/api/base")
def api_base():
    return jsonify({"base_dir": load_config()["base_dir"]})


@app.post("/api/choose-dir")
def api_choose_dir():
    """Native folder picker for the base directory (tkinter, stdlib)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return jsonify({"ok": False,
                        "message": "tkinter unavailable - set base_dir in config.json"}), 400
    cfg = load_config()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        d = filedialog.askdirectory(title="Choose base download directory", initialdir=cfg["base_dir"])
    finally:
        root.destroy()
    if d:
        cfg["base_dir"] = d
        cfg["dirs"] = list(dict.fromkeys(cfg["dirs"] + [d]))
        save_config(cfg)
    return jsonify({"ok": True, "base_dir": cfg["base_dir"]})


@app.get("/api/cookies/status")
def api_cookies_status():
    return jsonify(cookies_status())


@app.post("/api/cookies/export")
def api_cookies_export():
    browser = (request.get_json(silent=True) or {}).get("browser", "opera")
    try:
        n = export_cookies(browser)
        return jsonify({"ok": True, "message": f"Exported {n} cookies from {browser}"})
    except Exception as e:
        return jsonify({"ok": False,
                        "message": f"{_short_err(e)} — {_cookie_hint(e, browser)}"}), 400


def _selftest():
    sample = ("5.30 :5pm MWZ:/ 03/16 E@u.SY 无语  "
              "https://v.douyin.com/Oa6WLk8qJuc/ 复制此链接，打开Dou音搜索，直接观看视频！")
    assert extract_urls(sample) == ["https://v.douyin.com/Oa6WLk8qJuc/"], extract_urls(sample)
    assert extract_urls("no urls here") == []
    assert extract_urls("a https://x.com/1 b https://x.com/1 c") == ["https://x.com/1"]
    dy = ytdlp_cmd("https://v.douyin.com/QKv-Jhc3Me0/", "D:/o", [])
    assert "DYID:%(uploader)s" in dy, dy  # douyin links also capture the creator's 抖音號
    assert not any("DYID" in c for c in ytdlp_cmd("https://x.com/1", "D:/o", [])), dy
    assert resolve_dir("Music") == os.path.join("D:/Transport", "Music")
    assert resolve_dir("D:/Videos/x") == "D:/Videos/x"
    from http.cookiejar import Cookie
    c = Cookie(0, "sid", "abc", None, False, ".douyin.com", True, True,
               "/", True, False, None, True, None, None, {})
    line = _cookie_line(c)
    assert line == ".douyin.com\tTRUE\t/\tFALSE\t0\tsid\tabc", line
    assert _normalize_expiry(13448366773549492) == 1803893173  # WebTime -> Unix
    assert _normalize_expiry(1800000000) == 1800000000          # already seconds
    assert _normalize_expiry(0) == 0 and _normalize_expiry(-5) == 0
    pw = {"name": "sid", "value": "abc", "domain": ".douyin.com", "path": "/",
          "expires": -1, "secure": False, "httpOnly": True}
    assert _playwright_cookie_line(pw) == ".douyin.com\tTRUE\t/\tFALSE\t-1\tsid\tabc"
    site_err = _enrich_error("ERROR: [Douyin] x: Fresh cookies (not necessarily logged in) are needed", False)
    assert "tick 'use cookies'" in site_err and "locked while it is open" not in site_err, site_err
    locked = _enrich_error("ERROR: Chrome cookie database is locked", True)
    assert "locked while it is open" in locked and "tick 'use cookies'" not in locked, locked
    assert _enrich_error("ERROR: something unrelated", True) == "ERROR: something unrelated"
    import tempfile
    _t = tempfile.mkdtemp()
    os.mkdir(os.path.join(_t, ".ui-profile")); os.mkdir(os.path.join(_t, "Music"))
    assert _scan_dirs(_t) == [os.path.join(_t, "Music")], _scan_dirs(_t)  # dot-dirs excluded
    dpapi = _cookie_hint(Exception("Failed to decrypt with DPAPI. See .../10927"), "edge")
    assert "app-bound" in dpapi and "playwright" in dpapi, dpapi
    assert "Close the browser" in _cookie_hint(Exception("x"), "edge")
    assert "Restart the app" in _cookie_hint(Exception("x"), "playwright")
    print("selftest ok")


def _open_ui_browser(url="http://127.0.0.1:5000"):
    """Open the UI in a dedicated Playwright browser, isolated from your real browsers.
    A persistent profile (base_dir/.ui-profile) keeps logins across restarts; its cookies
    can be exported while it's open, since Playwright reads them via its API, not the DB.
    The browser is a child of Playwright's driver, which exits with this process - it dies
    when app.py dies. Falls back to the system browser if Playwright/browsers are unavailable."""

    def run():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            import webbrowser
            webbrowser.open(url)
            return
        try:
            with sync_playwright() as p:
                try:
                    ctx = p.chromium.launch_persistent_context(
                        ui_profile_dir(), channel="msedge",
                        viewport={"width": 700, "height": 960},
                        ignore_default_args=["--enable-automation"],
                        args=["--disable-blink-features=AutomationControlled"])  # look less automated to site risk control
                except Exception:
                    ctx = p.chromium.launch_persistent_context(
                        ui_profile_dir(),
                        viewport={"width": 700, "height": 960},
                        ignore_default_args=["--enable-automation"],
                        args=["--disable-blink-features=AutomationControlled"])  # bundled chromium fallback
                global UI_CONTEXT
                UI_CONTEXT = ctx
                page = ctx.new_page()
                with UI_LOCK:  # serialize with cookie export while the server is still starting
                    for _ in range(30):  # the Flask server may still be starting
                        try:
                            page.goto(url, timeout=3000)
                            break
                        except Exception:
                            time.sleep(1)
                while True:
                    # service cookie exports on THIS thread (sync API is thread-bound)
                    with UI_CMD_COND:
                        while not _ui_export_reqs:
                            UI_CMD_COND.wait(60)  # keep the browser referenced until the process exits
                        reqs = _ui_export_reqs[:]
                        _ui_export_reqs.clear()
                    for req in reqs:
                        try:
                            req["result"] = ctx.cookies()
                        except Exception as e:
                            req["result"] = e
                        finally:
                            req["ready"].set()
        except Exception as e:
            print(f"[transporter] could not open UI browser ({e}) - open {url} in any browser instead")

    threading.Thread(target=run, daemon=True).start()


def _already_running():
    """True if another instance already serves 127.0.0.1:5000.

    Two instances each launch their own Playwright window on the same .ui-profile,
    so cookie export ends up reading the wrong window. Refuse to start instead."""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 5000))  # fails if the port is taken
        return False
    except OSError:
        return True
    finally:
        s.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    if "--export-cookies" in sys.argv:
        browser = sys.argv[sys.argv.index("--export-cookies") + 1] if len(sys.argv) > sys.argv.index("--export-cookies") + 1 else "opera"
        try:
            n = export_cookies(browser)
            print(f"Exported {n} cookies from {browser} to {cookies_path()}")
        except Exception as e:
            print(f"Export failed: {_short_err(e)}")
            print(_cookie_hint(e, browser))
            sys.exit(1)
        sys.exit(0)
    if _already_running():
        print("[transporter] another instance is already running at http://127.0.0.1:5000 - "
              "close it first, then start one app only")
        sys.exit(1)
    _open_ui_browser()
    app.run(host="127.0.0.1", port=5000)
