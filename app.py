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
    return cmd + ["--newline", "--progress", "--no-warnings", "--no-playlist",
                  "--print", "after_move:filepath",
                  "--progress-template", ("download:%(progress.downloaded_bytes)s "
                                           "%(progress.total_bytes)s %(progress.speed)s "
                                           "%(progress.eta)s %(progress._percent_str)s"),
                  "-o", os.path.join(out_dir, "%(title)s.%(ext)s"), url]


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


def export_cookies(browser):
    """Reuse yt-dlp's own browser-cookie decryption (incl. Chromium app-bound keys),
    dump the result to D:/Transport/cookies.txt. Fails while the browser is open."""
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


def _resolve_cookies(job):
    """Use cookies.txt if fresh; auto re-export when stale/missing and browser is closed."""
    status = cookies_status()
    if status["exists"] and status["valid"] > 0:
        return ["--cookies", str(cookies_path())], "cookies.txt"
    try:
        n = export_cookies(job["browser"])
        return ["--cookies", str(cookies_path())], f"cookies.txt (auto-exported {n})"
    except Exception as e:
        if status["exists"]:
            raise CookieStaleError(
                f"Saved cookies are expired and auto re-export failed ({_short_err(e)}). "
                "Close the browser and click 'Export cookies', or untick 'use cookies'.")
        return ["--cookies-from-browser", job["browser"]], job["browser"]


def start_job(urls, out_dir, browser, use_cookies):
    with JOBS_LOCK:
        job = {
            "id": f"{time.time_ns()}",
            "urls": urls, "dir": out_dir, "browser": browser, "use_cookies": use_cookies,
            "status": "running", "percent": 0, "index": 0, "total": len(urls),
            "message": "starting...", "files": [], "error": None, "progress": None,
            "cookie_source": "..." if use_cookies else "none",
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
            job["index"], job["percent"], job["message"], job["progress"] = i, 0, f"downloading {i}/{job['total']}", None
            # streams are merged: yt-dlp routes progress/logs/paths to different streams
            # depending on binary and flags, so one loop classifies every line
            proc = subprocess.Popen(
                ytdlp_cmd(url, job["dir"], cookie_args),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
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
                elif line.startswith(("WARNING:", "ERROR:", "[")):
                    job["lines"].append(line)
                    job["lines"] = job["lines"][-200:]
                    job["message"] = line[-120:]
                else:  # --print after_move:filepath output (absolute paths never start with "[")
                    job["files"].append(line)
            proc.wait()
            if proc.returncode != 0:
                err = "\n".join(dict.fromkeys(job["lines"][-12:]))  # dedupe yt-dlp's retry repeats
                if "could not find" in err.lower():
                    err += ("\n\nNo cookie database found for this browser on this PC. "
                            "Pick another browser in the dropdown, or close it and click 'Export cookies'.")
                elif "cookie" in err.lower():
                    err += ("\n\nCookie access failed - this browser's cookies are locked while it is open "
                            "(the 'Chrome cookie database' message is yt-dlp's generic name for Chromium, "
                            "Opera/Edge/Chrome alike). Close the browser and retry - cookies are exported "
                            "automatically and reused for later downloads.")
                job["status"], job["error"] = "error", err
                return
        job["status"] = "done"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)
    finally:
        _record_history(job)


def _record_dir_history(job):
    """Append {time, filename, url} to <dir>/.transporter.json for each downloaded file."""
    if not job.get("files"):
        return
    with open(os.path.join(job["dir"], ".transporter.json"), "a", encoding="utf-8") as f:
        for file, url in zip(job["files"], job["urls"]):
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M"),
                                "file": os.path.basename(file), "url": url},
                               ensure_ascii=False) + "\n")


def _record_history(job):
    if not job.get("files"):
        return
    _record_dir_history(job)
    cfg = load_config()
    cfg["history"] = [{"time": time.strftime("%Y-%m-%d %H:%M"),
                       "dir": job["dir"], "files": job["files"]}] + cfg["history"]
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
    """Top-level subfolders of the base dir, so the dropdown mirrors the filesystem."""
    out = []
    try:
        for name in sorted(os.listdir(base)):
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
                                    "message", "files", "error", "dir", "cookie_source", "progress")}


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
    return jsonify(load_config()["history"][:20])


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
                        "message": f"{_short_err(e)} — close the browser and try again, "
                                   "or run: python app.py --export-cookies {browser}"}), 400


def _selftest():
    sample = ("5.30 :5pm MWZ:/ 03/16 E@u.SY 无语  "
              "https://v.douyin.com/Oa6WLk8qJuc/ 复制此链接，打开Dou音搜索，直接观看视频！")
    assert extract_urls(sample) == ["https://v.douyin.com/Oa6WLk8qJuc/"], extract_urls(sample)
    assert extract_urls("no urls here") == []
    assert extract_urls("a https://x.com/1 b https://x.com/1 c") == ["https://x.com/1"]
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
    print("selftest ok")


def _open_ui_browser(url="http://127.0.0.1:5000"):
    """Open the UI in a dedicated Playwright browser, isolated from your real browsers
    (so its profile never locks the cookies you export). The browser is a child of
    Playwright's driver, which exits with this process - it dies when app.py dies.
    Falls back to the system browser if Playwright/browsers are unavailable."""

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
                    browser = p.chromium.launch(channel="msedge")  # reuse installed Edge, no download
                except Exception:
                    browser = p.chromium.launch()  # bundled chromium (needs: uv run playwright install chromium)
                page = browser.new_page(viewport={"width": 700, "height": 960})
                for _ in range(30):  # the Flask server may still be starting
                    try:
                        page.goto(url, timeout=3000)
                        break
                    except Exception:
                        time.sleep(1)
                while True:
                    time.sleep(60)  # keep the browser referenced until the process exits
        except Exception as e:
            print(f"[transporter] could not open UI browser ({e}) - open {url} in any browser instead")

    threading.Thread(target=run, daemon=True).start()


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
            sys.exit(1)
        sys.exit(0)
    _open_ui_browser()
    app.run(host="127.0.0.1", port=5000)
