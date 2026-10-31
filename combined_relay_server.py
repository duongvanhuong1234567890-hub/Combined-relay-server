import array
import base64
import bisect
import collections
import concurrent.futures
import contextlib
import fcntl
import gc
import hmac
import html
import http.client
import http.cookiejar
import io
import ipaddress
import itertools
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

from flask import Flask, request, Response, jsonify, g
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler

if not os.environ.get("MALLOC_ARENA_MAX"):
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").mallopt(-8, 2)
    except Exception:
        pass


def _env_str(name, default=""):
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return bool(default)
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name, sep=r"[,;\s]+"):
    return [x for x in re.split(sep, os.environ.get(name, "")) if x.strip()]


def _log(*a):
    print("[br]", *a, flush=True)


def _say(tag, msg):
    print(f"[{tag}] {msg}", flush=True)


_ALL_ROLES = {"video", "music", "browser"}
_ROLE_ALIAS = {"youtube": "video", "yt": "video", "video": "video", "ytm": "music", "music": "music",
               "youtubemusic": "music", "google": "browser", "gg": "browser", "web": "browser", "br": "browser",
               "browser": "browser"}


def _parse_roles(raw):
    raw = (raw or "all").strip().lower()
    if raw in ("video", "youtube", "yt"):
        return {"video", "music"}
    if raw in ("video-only", "youtube-only", "yt-only"):
        return {"video"}
    if raw in ("music-only", "ytm-only"):
        return {"music"}
    parts = [p for p in re.split(r"[,+/\s]+", raw) if p]
    if not parts or "all" in parts:
        return set(_ALL_ROLES)
    out = {_ROLE_ALIAS[p] for p in parts if p in _ROLE_ALIAS}
    return out or set(_ALL_ROLES)


ROLES = _parse_roles(os.environ.get("ROLE", "all"))
ROLE = "all" if ROLES == _ALL_ROLES else ",".join(sorted(ROLES))
HAS_VIDEO = "video" in ROLES
HAS_MUSIC = "music" in ROLES
HAS_BROWSER = "browser" in ROLES
HAS_YT = HAS_VIDEO or HAS_MUSIC

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Mobile Safari/537.36")
_GEN_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/130.0.0.0 Safari/537.36")
PORT = _env_int("PORT", 8000)
RELAY_URL = os.environ.get("RELAY_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
_BOOT_MONO = time.monotonic()
WARP_ON = os.environ.get("WARP", "1") == "1"
WARP_PORT = int(os.environ.get("WARP_PORT", "8118"))
_WARP_DIR = "/tmp/warp"

def _warp_log(msg):
    print("[warp] " + msg, flush=True)

def _warp_fetch(url, dest, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)

def _wg_parse(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "[Interface]" not in raw:
        try:
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return ""
    if "[Interface]" not in raw or "[Peer]" not in raw:
        return ""

    drop = ("postup", "postdown", "preup", "predown", "table", "saveconfig", "fwmark")
    out = []
    for ln in raw.replace("\r", "").split("\n"):
        k = ln.split("=", 1)[0].strip().lower()
        if k in drop:
            continue
        out.append(ln)
    return "\n".join(out).strip() + "\n"

def _wg_custom_confs():
    """Doc WG_CONF, WG_CONF_2 ... WG_CONF_7 -> [(ten_bien, noi_dung_conf)]."""
    res, seen = [], set()
    for key in ["WG_CONF"] + [f"WG_CONF_{n}" for n in range(2, 8)]:
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        conf = _wg_parse(raw)
        if not conf:
            _warp_log(f"canh bao: {key} khong hop le (thieu [Interface]/[Peer]) -> bo qua")
            continue
        if conf in seen:
            continue
        seen.add(conf)
        res.append((key, conf))
    return res

def _warp_check(port, need_warp=True):
    try:
        px = f"http://127.0.0.1:{port}"
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": px, "https": px}))
        with op.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=12) as r:
            txt = r.read(4000).decode("utf-8", "replace")
        if not need_warp:
            m = re.search(r"^ip=(\S+)", txt, re.M)
            if m:
                _warp_log(f"IP ra ngoai (cong {port}): {m.group(1)}")
            return bool(m)
        return "warp=on" in txt or "warp=plus" in txt
    except Exception:
        return False

def _wg_run(wp, conf, port, need_warp, label):
    """Chay 1 wireproxy tren 127.0.0.1:port. True neu proxy len va ra duoc internet."""
    os.makedirs(_WARP_DIR, exist_ok=True)
    cpath = os.path.join(_WARP_DIR, f"wireproxy_{port}.conf")
    lpath = os.path.join(_WARP_DIR, f"wireproxy_{port}.log")
    with open(cpath, "w", encoding="utf-8") as fh:
        fh.write(conf.rstrip() + f"\n\n[http]\nBindAddress = 127.0.0.1:{port}\n")
    logf = open(lpath, "wb")
    proc = subprocess.Popen([wp, "-c", cpath], stdout=logf, stderr=logf, stdin=subprocess.DEVNULL)
    ok = False
    for _ in range(10):
        time.sleep(1)
        if proc.poll() is not None:
            break
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            continue
        for attempt in range(2):
            ok = _warp_check(port, need_warp=need_warp)
            if ok:
                break
            time.sleep(2)
        break
    if not ok:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            with open(lpath, "rb") as fh:
                tail = fh.read()[-200:].decode("utf-8", "replace").replace("\n", " ")
        except Exception:
            tail = ""
        _warp_log(f"loi: {label} (cong {port}) khong len | {tail}")
    return ok

def _warp_start():
    if not WARP_ON or os.environ.get("YT_PROXY", "").strip():
        return
    if not HAS_YT:
        return
    try:
        import platform
        import tarfile
        confs = _wg_custom_confs()
        os.makedirs(_WARP_DIR, exist_ok=True)
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if not arch:
            _warp_log(f"loi: kien truc khong ho tro: {platform.machine()}")
            return

        wp = shutil.which("wireproxy") or os.path.join(_WARP_DIR, "wireproxy")
        if not os.path.exists(wp):
            tgz = wp + ".tgz"
            _warp_fetch(f"https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_{arch}.tar.gz", tgz)
            with tarfile.open(tgz) as t:
                mem = next((m for m in t.getmembers() if m.isfile() and os.path.basename(m.name) == "wireproxy"), None)
                if mem is None:
                    _warp_log("loi: khong thay file wireproxy trong goi tai ve")
                    return
                with t.extractfile(mem) as src, open(wp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.chmod(wp, 0o755)

        # ---- VPN WireGuard tu bien moi truong: moi cai 1 wireproxy / 1 cong rieng ----
        if confs:
            ups = []
            for idx, (key, conf) in enumerate(confs):
                port = WARP_PORT + idx
                px = f"http://127.0.0.1:{port}"
                if _warp_check(port, need_warp=False):
                    _warp_log(f"{key}: dung lai proxy dang chay {px}")
                    ups.append(px)
                    continue
                if _wg_run(wp, conf, port, False, key):
                    _warp_log(f"{key}: OK -> {px}")
                    ups.append(px)
            if ups:
                os.environ["YT_PROXY"] = ups[0]
                rest = ups[1:] + [p for p in _env_list("YT_PROXIES") if p not in ups]
                if rest:
                    os.environ["YT_PROXIES"] = ",".join(rest)
                _warp_log(f"{len(ups)}/{len(confs)} VPN san sang (bi chan se tu doi sang cai khac)")
                return
            _warp_log("canh bao: khong VPN WG nao len -> thu WARP")

        # ---- WARP (Cloudflare) khi khong co/khong chay duoc VPN WG ----
        px = f"http://127.0.0.1:{WARP_PORT}"
        if _warp_check(WARP_PORT, need_warp=True):
            os.environ["YT_PROXY"] = px
            _warp_log(f"OK (dung lai proxy dang chay) -> YT_PROXY={px}")
            return
        wg = shutil.which("wgcf") or os.path.join(_WARP_DIR, "wgcf")
        if not os.path.exists(wg):
            tags = []
            try:
                req = urllib.request.Request("https://github.com/ViRb3/wgcf/releases/latest", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    m = re.search(r"/tag/(v[0-9][0-9.]*)", r.geturl())
                    if m:
                        tags.append(m.group(1))
            except Exception as e:
                _warp_log(f"canh bao: khong lay duoc phien ban wgcf moi nhat ({type(e).__name__}), dung ban ghim")
            tags.append("v2.2.22")
            got = False
            for tag in tags:
                link = f"https://github.com/ViRb3/wgcf/releases/download/{tag}/wgcf_{tag[1:]}_linux_{arch}"
                try:
                    _warp_fetch(link, wg)
                    got = True
                    break
                except Exception as e:
                    _warp_log(f"canh bao: tai wgcf {tag} that bai: {type(e).__name__}: {str(e)[:80]}")
                    try:
                        os.remove(wg)
                    except OSError:
                        pass
            if not got:
                _warp_log("loi: khong tai duoc wgcf")
                return
            os.chmod(wg, 0o755)
        prof = os.path.join(_WARP_DIR, "wgcf-profile.conf")
        if not os.path.exists(prof):
            for args in (["register", "--accept-tos"], ["generate"]):
                subprocess.run([wg] + args, cwd=_WARP_DIR, capture_output=True, timeout=45, stdin=subprocess.DEVNULL)
        if not os.path.exists(prof):
            _warp_log("loi: khong tao duoc profile WARP (wgcf register that bai)")
            return
        with open(prof, "r", encoding="utf-8") as fh:
            conf = fh.read()
        if _wg_run(wp, conf, WARP_PORT, True, "WARP"):
            os.environ["YT_PROXY"] = px
            _warp_log(f"OK -> YT_PROXY={px}")
        else:
            _warp_log("loi: proxy khong len, chay khong proxy")
    except Exception as e:
        _warp_log(f"loi: {type(e).__name__}: {str(e)[:200]}")

# Khoi dong VPN o luong nen: khong bao gio chan viec mo cong cua server (Render "Port scan timeout").
_WARP_THREAD = threading.Thread(target=_warp_start, name="warp-boot", daemon=True)
_WARP_THREAD.start()
_WARP_THREAD.join(_env_float("WARP_WAIT", 45))
if _WARP_THREAD.is_alive():
    _warp_log("VPN con dang khoi dong ngam, server chay truoc (se tu nap proxy khi xong)")

ALLOW_PRIVATE_HOSTS = os.environ.get("ALLOW_PRIVATE_HOSTS", "") == "1"
_SAFE_HOST_SUFFIX = (".googlevideo.com", ".youtube.com", ".ytimg.com", ".googleusercontent.com")
_host_cache = {}
_host_lock = threading.Lock()

def _host_public(host):
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    if ALLOW_PRIVATE_HOSTS or host in ("youtube.com", "youtu.be") or host.endswith(_SAFE_HOST_SUFFIX):
        return True
    now = time.monotonic()
    with _host_lock:
        hit = _host_cache.get(host)
    if hit and now < hit[0]:
        return hit[1]
    ok = False
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        ok = bool(ips)
        for ip in ips:
            a = ipaddress.ip_address(ip.split("%")[0])
            a = getattr(a, "ipv4_mapped", None) or a
            if not a.is_global:
                ok = False
                break
    except Exception:
        ok = False
    with _host_lock:
        if len(_host_cache) > 150:
            _host_cache.clear()
        _host_cache[host] = (now + (300 if ok else 10), ok)
    return ok

def _url_is_public(url):
    try:
        p = urllib.parse.urlparse(url or "")
        return p.scheme in ("http", "https") and _host_public(p.hostname)
    except ValueError:
        return False

def _clip_pct(s, n):
    s = s[:n]
    i = s.rfind("%")
    return s[:i] if i != -1 and i > len(s) - 3 else s


class ProxyPool:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = []
        self.bad = {}
        self.i = 0
        self.rotations = 0

    def load(self):
        items = []
        base = _env_str("YT_PROXY")
        if base:
            items.append(base)
        for p in _env_list("YT_PROXIES"):
            if p not in items:
                items.append(p)
        with self.lock:
            self.items = items
        if len(items) > 1:
            _say("proxy", f"{len(items)} proxy (xoay vong khi bi chan): " +
                 ", ".join(self._mask(p) for p in items))

    @staticmethod
    def _mask(p):
        try:
            u = urllib.parse.urlsplit(p)
            return f"{u.scheme}://{u.hostname}:{u.port}" if u.hostname else p
        except Exception:
            return "?"

    @staticmethod
    def _real(p):
        return "" if p == "direct" else p

    def current(self):
        with self.lock:
            if not self.items:
                return ""
            now = time.monotonic()
            n = len(self.items)
            for k in range(n):
                p = self.items[(self.i + k) % n]
                if now >= self.bad.get(p, 0.0):
                    return self._real(p)
            return self._real(min(self.items, key=lambda x: self.bad.get(x, 0.0)))

    def penalize(self, px, why="", cool=None):
        key = px or "direct"
        with self.lock:
            if len(self.items) < 2 or key not in self.items:
                return False
            cool = _env_float("YT_PROXY_COOL", 600) if cool is None else cool
            self.bad[key] = time.monotonic() + cool
            self.i = (self.items.index(key) + 1) % len(self.items)
            self.rotations += 1
        _say("proxy", f"doi proxy: {self._mask(key)} nghi {cool:.0f}s ({why[:80]})")
        return True

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            return [{"proxy": self._mask(p), "rest": max(0, int(self.bad.get(p, 0) - now))} for p in self.items]


PROXIES = ProxyPool()
PROXIES.load()


def _reload_proxies_after_warp():
    _WARP_THREAD.join()
    PROXIES.load()

if _WARP_THREAD.is_alive():
    threading.Thread(target=_reload_proxies_after_warp, name="warp-reload", daemon=True).start()


def _proxy_opener(px=""):
    if px:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": px, "https": px}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 700_000


@app.errorhandler(Exception)
def _on_error(e):
    if isinstance(e, HTTPException):
        return e
    print("[error]", traceback.format_exc(), flush=True)
    return "loi may chu", 500


STREAM_TOKEN = _env_str("STREAM_TOKEN")
ALLOW_OPEN_STREAM = _env_bool("ALLOW_OPEN_STREAM", False)
BR_TOKEN = os.environ.get("BR_TOKEN", "")
if not STREAM_TOKEN and not ALLOW_OPEN_STREAM:
    print("[config] canh bao: chua dat STREAM_TOKEN -> /stream /search /ytm* /tr bi khoa (503)", flush=True)


def _tok_ok(given, expected):
    try:
        return bool(expected) and hmac.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


_stat = {"since": time.time(), "streams": 0, "last": 0.0}
_stat_lock = threading.Lock()
_last_req = time.monotonic()
_real_req_seen = False
_reqbusy = 0
_reqbusy_lock = threading.Lock()
LAST_SVC_REQ = {"video": 0.0, "music": 0.0, "browser": 0.0}


def _reqbusy_add(d):
    global _reqbusy
    with _reqbusy_lock:
        _reqbusy = max(0, _reqbusy + d)


@app.teardown_request
def _reqbusy_done(exc):
    if getattr(g, "_reqbusy_on", False):
        g._reqbusy_on = False
        _reqbusy_add(-1)


_TOKEN_GATED = ("/stream", "/search", "/tr", "/gs", "/ytm", "/app", "/yt/")
_BR_GATED = ("/br", "/cookies")


def _svc_of(path):
    if path.startswith(("/stream", "/search", "/_src/", "/yt/")):
        return "video"
    if path.startswith("/ytm"):
        return "music"
    if path.startswith("/br"):
        return "browser"
    return None


@app.before_request
def _gate():
    global _last_req, _real_req_seen
    p = request.path
    if not p.startswith(_TOKEN_GATED + _BR_GATED + ("/_src/",)):
        return None
    svc = _svc_of(p)
    if svc and svc not in ROLES:
        return "disabled", 404
    if p.startswith(_TOKEN_GATED):
        sid = request.args.get("sid", "")
        resume = p == "/stream" and bool(sid) and sid in _sessions
        if not resume:
            if not STREAM_TOKEN and not ALLOW_OPEN_STREAM:
                return "chua dat STREAM_TOKEN", 503
            if STREAM_TOKEN and not _tok_ok(request.args.get("tk", ""), STREAM_TOKEN):
                return "forbidden", 403
    elif p.startswith(_BR_GATED) and not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return "forbidden", 403
    if not p.startswith("/_src/"):
        _last_req = time.monotonic()
        _real_req_seen = True
        if svc:
            LAST_SVC_REQ[svc] = _last_req
        elif p.startswith(("/tr", "/gs")):
            LAST_SVC_REQ["browser"] = _last_req
        if HAS_YT:
            _maybe_update_on_use()
        g._reqbusy_on = True
        _reqbusy_add(1)
    return None


_NOT_ALLOWED = ("<!doctype html>\n<html lang=en>\n<title>405 Method Not Allowed</title>\n"
                "<h1>Method Not Allowed</h1>\n<p>The method is not allowed for the requested URL.</p>\n")


def _405():
    return Response(_NOT_ALLOWED, status=405, mimetype="text/html", headers={"Allow": "POST"})


class HTTP10RequestHandler(WSGIRequestHandler):
    protocol_version = "HTTP/1.0"
    disable_nagle_algorithm = True


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return max(lo, min(hi, int(default)))


_YT_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YTM_ID_ONLY = _YT_BARE_ID
_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/|v/))([A-Za-z0-9_-]{11})")
_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_youtube(url):
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _YT_HOSTS)


def _canon_url(url):
    m = _YT_ID_RE.search(url or "")
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url


def _normalize_input_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if _YT_BARE_ID.match(u):
        return f"https://www.youtube.com/watch?v={u}"
    if "://" not in u:
        u = "https://" + u
    if not re.match(r"^https?://", u, re.I):
        return ""
    return u if _url_is_public(u) else ""


def _who():
    try:
        ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
        return f"{request.path} tu {ip}"
    except Exception:
        return "?"


def _tk_q():
    return ("&tk=" + urllib.parse.quote(STREAM_TOKEN, safe="")) if STREAM_TOKEN else ""


def _base_url():
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip() or "http"
    return f"{scheme}://{request.host}"


def _rd(path):
    with open(path) as fh:
        return fh.read()


def _mem_info():
    try:
        for cur, lim, stat, keys in (
                ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.stat", ("anon",)),
                ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes",
                 "/sys/fs/cgroup/memory/memory.stat", ("total_rss", "rss"))):
            if not (os.path.exists(cur) and os.path.exists(lim)):
                continue
            limit_s = _rd(lim).strip()
            if not limit_s.isdigit() or int(limit_s) >= (1 << 40):
                continue
            used = int(_rd(cur).strip())
            try:
                for line in _rd(stat).splitlines():
                    k, v = line.split()[:2]
                    if k in keys:
                        used = int(v)
                        break
            except Exception:
                pass
            return used / 1048576.0, int(limit_s) / 1048576.0
    except Exception:
        pass
    return None


def _mem_str():
    mi = _mem_info()
    return f"{mi[0]:.0f}/{mi[1]:.0f}MB" if mi else "?"


def _mem_frac():
    mi = _mem_info()
    return (mi[0] / mi[1]) if mi else 0.0


_LIBC = None


def _free_ram():
    global _LIBC
    gc.collect()
    try:
        if _LIBC is None:
            import ctypes
            _LIBC = ctypes.CDLL("libc.so.6")
        _LIBC.malloc_trim(0)
    except Exception:
        pass


def _proc_rss_list():
    out = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/status") as fh:
                    txt = fh.read()
                nm = re.search(r"^Name:\s*(\S+)", txt, re.M)
                rs = re.search(r"^VmRSS:\s*(\d+)", txt, re.M)
                if nm and rs:
                    out.append((int(rs.group(1)) // 1024, nm.group(1), int(pid)))
            except Exception:
                continue
    except Exception:
        pass
    out.sort(reverse=True)
    return out


def _ram_report(tag):
    try:
        top = _proc_rss_list()[:6]
        print(f"[ram] {tag} | {_mem_str()} | " + ", ".join(f"{n}({p})={mb}MB" for mb, n, p in top), flush=True)
    except Exception:
        pass


class Mark:
    def __init__(self, kind):
        self.kind = kind
        self.path = "/tmp/relay_%s_%d" % (kind, os.getpid())

    def touch(self, on=True):
        try:
            if on:
                with open(self.path, "a"):
                    os.utime(self.path, None)
            elif os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass

    def elsewhere(self, max_age=60):
        try:
            import glob
            now = time.time()
            for f in glob.glob("/tmp/relay_%s_*" % self.kind):
                if f != self.path and now - os.path.getmtime(f) < max_age:
                    return True
        except OSError:
            pass
        return False


class RecentFails:

    def __init__(self, window):
        self.window = window
        self.d = {}
        self.lock = threading.Lock()

    def _prune(self, now):
        for k in [k for k, t in self.d.items() if now - t > self.window]:
            self.d.pop(k, None)

    def add(self, key):
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            self.d[key] = now
            return len(self.d)

    def count(self):
        with self.lock:
            self._prune(time.monotonic())
            return len(self.d)

    def clear(self):
        with self.lock:
            self.d.clear()


class Breaker:

    def __init__(self, base, cap):
        self.base, self.cap = float(base), float(cap)
        self.level = 0
        self.until = 0.0
        self.trips = 0
        self.lock = threading.Lock()

    def left(self):
        return max(0.0, self.until - time.monotonic())

    def trip(self):
        with self.lock:
            wait = min(self.cap, self.base * 2 ** min(self.level, 5)) * random.uniform(0.9, 1.3)
            self.level += 1
            self.trips += 1
            self.until = time.monotonic() + wait
            return wait

    def ok(self):
        with self.lock:
            self.level = 0
            self.until = 0.0

    reset = ok


class Heat:
    HALF = 150.0

    def __init__(self):
        self.v = 0.0
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def _decay(self, now):
        self.v *= 0.5 ** ((now - self.t) / self.HALF)
        self.t = now

    def bump(self, w=1.0):
        with self.lock:
            self._decay(time.monotonic())
            self.v = min(20.0, self.v + w)

    def level(self):
        with self.lock:
            self._decay(time.monotonic())
            return self.v

    def factor(self):
        return 1.0 + min(2.0, self.level() / 4.0)


HEAT = Heat()


class Lane:
    def __init__(self, name, gap):
        self.name, self.gap = name, float(gap)
        self.last = 0.0
        self.lock = threading.Lock()


class Governor:

    def __init__(self):
        self.g_lock = threading.Lock()
        self.g_next = 0.0
        self.gap_global = _env_float("YT_GLOBAL_GAP", 0.35)
        self.lanes = {}

    def lane(self, name, gap):
        ln = self.lanes.get(name)
        if ln is None:
            ln = self.lanes.setdefault(name, Lane(name, gap))
        return ln

    def pace(self, name, jitter=(1.0, 1.5)):
        ln = self.lanes[name]
        with ln.lock:
            gap = ln.gap * random.uniform(*jitter) * HEAT.factor()
            wait = ln.last + gap - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, 12.0))
            with self.g_lock:
                w2 = self.g_next - time.monotonic()
                if w2 > 0:
                    time.sleep(min(w2, 2.0))
                self.g_next = time.monotonic() + self.gap_global
            ln.last = time.monotonic()


GOV = Governor()
GOV.lane("video", _env_float("YT_MIN_GAP", 2.5))
GOV.lane("video_direct", _env_float("YT_DIRECT_GAP", 0.25))
GOV.lane("music", _env_float("YTM_PACE", 0.8))
GOV.lane("music_direct", _env_float("YTM_DIRECT_GAP", 0.25))
GOV.lane("search", _env_float("YT_SEARCH_GAP", 0.4))
GOV.lane("bg", _env_float("YT_BG_GAP", 3.0))


_SECRET_COOKIES = "/etc/secrets/cookies.txt"


def _looks_like_cookies(txt):
    return len(txt) >= 200 and "youtube.com" in txt and "\t" in txt


class _CookieLease:
    def __init__(self):
        self.path = None
        self.keep = False


class CookieStore:
    def __init__(self):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
        if os.path.exists(_SECRET_COOKIES):
            if not os.path.exists("/tmp/cookies.txt"):
                shutil.copyfile(_SECRET_COOKIES, "/tmp/cookies.txt")
            self.path = "/tmp/cookies.txt"
        self.lock = threading.Lock()
        self.bad_until = 0.0
        self.bad_why = ""
        self.writebacks = 0
        self._seq = itertools.count(1)
        if os.path.exists(self.path):
            sz = os.path.getsize(self.path)
            print(f"[cookies] TIM THAY {self.path} - kich thuoc {sz} bytes")
            if sz < 200:
                print("[cookies] CANH BAO: file qua nho, co the dan thieu noi dung hoac rong")
        else:
            print(f"[cookies] KHONG TIM THAY {self.path} - Secret File chua duoc tao dung, hoac sai ten file")

    def exists(self):
        return os.path.exists(self.path)

    def usable(self):
        return self.exists() and time.monotonic() >= self.bad_until

    def mark_bad(self, why, secs=1800.0):
        if time.monotonic() >= self.bad_until:
            _say("cookies", f"cookies co van de ({why[:100]}) -> tam bo qua {secs:.0f}s, dung cach khong cookies")
        self.bad_until = time.monotonic() + secs
        self.bad_why = why[:120]

    def info(self):
        if not self.exists():
            return "chua co"
        age = int(time.time() - os.path.getmtime(self.path))
        s = f"{os.path.getsize(self.path)}B, cap nhat {age}s truoc"
        if time.monotonic() < self.bad_until:
            s += f", TAM BO QUA ({self.bad_why})"
        if self.writebacks:
            s += f", ghi-nguoc {self.writebacks} lan"
        return s

    def update(self, txt):
        target = self.path if self.path.startswith("/tmp/") or not os.path.exists(_SECRET_COOKIES) else "/tmp/cookies.txt"
        tmp = target + ".new"
        with self.lock:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(txt if txt.endswith("\n") else txt + "\n")
            os.replace(tmp, target)
            self.path = target
            self.bad_until = 0.0
            self.bad_why = ""

    @contextlib.contextmanager
    def lease(self, use=True):
        L = _CookieLease()
        if not use or not self.usable():
            yield L
            return
        tmp = "/tmp/ytck_%d_%d.txt" % (os.getpid(), next(self._seq))
        try:
            shutil.copyfile(self.path, tmp)
            m0 = os.path.getmtime(self.path)
            L.path = tmp
        except OSError:
            yield L
            return
        try:
            yield L
        finally:
            try:
                if L.keep:
                    self._writeback(tmp, m0)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _writeback(self, tmp, m0):
        try:
            with open(tmp, "r", encoding="utf-8", errors="replace") as fh:
                txt = fh.read(700000)
            if not _looks_like_cookies(txt):
                return
            with self.lock:
                if os.path.getmtime(self.path) != m0:
                    return
                with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                    cur = fh.read(700000)
                if cur == txt:
                    return
                nt = self.path + ".new"
                with open(nt, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(txt)
                os.replace(nt, self.path)
                self.writebacks += 1
        except Exception:
            pass


COOKIES = CookieStore()


YT_SUBPROC = _env_bool("YT_SUBPROC", True)
YT_SUBPROC_TIMEOUT = _env_float("YT_SUBPROC_TIMEOUT", 150)
YT_DENO_HEAP_MB = _env_int("YT_DENO_HEAP_MB", 128)
YT_SPAWN_RAM_MB = _env_float("YT_SPAWN_RAM_MB", 250)

if HAS_YT:
    import yt_dlp
else:
    class _LazyYT:
        def __getattr__(self, name):
            import yt_dlp as _real
            globals()["yt_dlp"] = _real
            return getattr(_real, name)
    yt_dlp = _LazyYT()

_YDL_WORKER = r"""
import sys, json
def _out(o):
    sys.stdout.write("\n@@YDL@@" + json.dumps(o, default=str) + "\n")
    sys.stdout.flush()
_DROP = ("formats", "thumbnails", "storyboards", "heatmap", "chapters")
try:
    req = json.loads(sys.stdin.read())
    import yt_dlp
    with yt_dlp.YoutubeDL(req["opts"]) as ydl:
        info = ydl.extract_info(req["url"], download=False)
        info = ydl.sanitize_info(info)
    if req.get("slim") and isinstance(info, dict):
        if info.get("requested_formats") or info.get("url"):
            for k in _DROP:
                info.pop(k, None)
        for e in (info.get("entries") or []):
            if isinstance(e, dict):
                for k in _DROP:
                    e.pop(k, None)
    _out({"ok": True, "info": info})
except BaseException as e:
    ei = getattr(e, "exc_info", None)
    cls = type(e).__name__
    if ei and ei[0] is not None and getattr(ei[0], "__name__", "") == "GeoRestrictedError":
        cls = "GeoRestrictedError"
    _out({"ok": False, "err": str(e), "cls": cls})
"""


def _ram_guard_before_ydl():
    if YT_SPAWN_RAM_MB <= 0:
        return
    mi = _mem_info()
    if not mi or mi[0] < YT_SPAWN_RAM_MB:
        return
    try:
        ARB.relieve("chuan bi yt-dlp")
    except Exception:
        pass
    end = time.monotonic() + 15
    while time.monotonic() < end:
        mi = _mem_info()
        if not mi or mi[0] < YT_SPAWN_RAM_MB:
            return
        time.sleep(1)
    mi = _mem_info()
    if mi and mi[0] >= YT_SPAWN_RAM_MB + 60:
        raise RuntimeError(f"RAM cao {mi[0]:.0f}MB, khong mo yt-dlp de tranh OOM")


def _ydl_info(url, opts, slim=False):
    if not YT_SUBPROC:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    payload = json.dumps({"url": url, "opts": opts, "slim": slim})
    _ram_guard_before_ydl()
    _env = dict(os.environ)
    _env["PYTHONDONTWRITEBYTECODE"] = "1"
    if YT_DENO_HEAP_MB > 0 and "DENO_V8_FLAGS" not in _env:
        _env["DENO_V8_FLAGS"] = f"--max-old-space-size={YT_DENO_HEAP_MB}"
    try:
        proc = subprocess.Popen(_oom_prefix() + [sys.executable, "-c", _YDL_WORKER], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                env=_env)
    except OSError as e:
        print(f"[ydl] khong chay duoc tien trinh con ({e}) -> dung trong tien trinh chinh", flush=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        out, err = proc.communicate(payload, timeout=YT_SUBPROC_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise RuntimeError(f"yt-dlp qua {YT_SUBPROC_TIMEOUT:.0f}s, da dung")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    _i = (out or "").rfind("@@YDL@@")
    if _i < 0:
        tail = (err or "").strip()[-300:]
        raise RuntimeError(f"yt-dlp tien trinh con loi rc={proc.returncode}: {tail}")
    try:
        res = json.loads(out[_i + 7:].strip())
    except ValueError:
        raise RuntimeError("yt-dlp tra ket qua hong")
    if not res.get("ok"):
        raise type(str(res.get("cls") or "YtDlpError"), (Exception,), {})(res.get("err") or "yt-dlp loi")
    return res.get("info")


def format_ffmpeg_headers(fmt):
    headers = fmt.get("http_headers") or {}
    if not headers:
        return ""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


class ResolveError(RuntimeError):
    def __init__(self, msg, permanent=False):
        super().__init__(msg)
        self.permanent = permanent


class DirectFail(Exception):
    def __init__(self, msg, hard=False, kind="other"):
        super().__init__(msg)
        self.hard = hard
        self.kind = kind


_NO_RETRY_STRONG = ("Private video", "has been removed", "not available in your country", "This video is private",
                    "members-only", "Join this channel", "live event will begin", "Premieres in",
                    "account associated with this video has been terminated")
_NO_RETRY_WEAK = ("Video unavailable", "This video is not available")
_GEO_HINTS = ("not made this video available in your country", "not available in your country")
_GEO_RE = re.compile(r"available in your (?:country|region|location)|blocked it in your country|"
                     r"geo[- ]?restrict|not (?:available|accessible) in your (?:country|region)|from your location", re.I)
_BLOCK_HINTS = ("not a bot", "player response", "HTTP Error 429", "Too Many Requests", "HTTP Error 403")
_AGE_HINTS = ("confirm your age", "age-restricted", "inappropriate for some users")
_OUTDATED_HINTS = ("player response", "Unable to extract", "nsig", "Signature extraction")
_HARD_BLOCK_HINTS = ("not a bot", "HTTP Error 429", "Too Many Requests")
_COOKIE_BAD_HINTS = ("cookies are no longer valid", "account cookies are no longer valid", "cookies have been rotated")
_NET_HINTS = ("timed out", "timeout", "connection reset", "connection refused", "name or service not known",
              "temporary failure in name resolution", "network is unreachable", "remote end closed")


def _is_geo_error(e, msg):
    if type(e).__name__ == "GeoRestrictedError" or any(h in msg for h in _GEO_HINTS) or _GEO_RE.search(msg or ""):
        return True
    ei = getattr(e, "exc_info", None)
    return bool(ei and ei[0] is not None and getattr(ei[0], "__name__", "") == "GeoRestrictedError")


def classify_error(msg, e=None):
    m = msg or ""
    if _is_geo_error(e, m):
        return "geo"
    if any(h in m for h in _NO_RETRY_STRONG):
        return "gone"
    if any(h in m for h in _AGE_HINTS):
        return "age"
    if any(h in m for h in _HARD_BLOCK_HINTS):
        return "bot"
    if any(h in m for h in ("player response", "HTTP Error 403")):
        return "block"
    if any(h in m for h in _OUTDATED_HINTS):
        return "outdated"
    low = m.lower()
    if any(h in low for h in _NET_HINTS):
        return "net"
    if any(h in m for h in _NO_RETRY_WEAK):
        return "weak"
    return "other"


INNERTUBE_BASE = _env_str("YT_INNERTUBE_BASE", "https://www.youtube.com").rstrip("/")
YTM_INNERTUBE_BASE = _env_str("YTM_INNERTUBE_BASE", "https://music.youtube.com").rstrip("/")
YT_DIRECT_VERSION = _env_str("YT_DIRECT_VERSION", "1.65.10")
YT_DIRECT_TIMEOUT = _env_float("YT_DIRECT_TIMEOUT", 8)
YT_DIRECT_CLIENT_DEAD = _env_float("YT_DIRECT_CLIENT_DEAD", 180)
YT_DIRECT_PROBE = _env_bool("YT_DIRECT_PROBE", True)
YT_AUDIO_MAX_BPS = _env_int("YT_AUDIO_MAX_BPS", 72000)
YTM_AUDIO_MAX_BPS = _env_int("YTM_AUDIO_MAX_BPS", 96000)
YT_DIRECT_CLIENTS = [c.lower() for c in _env_list("YT_DIRECT_CLIENTS") or ["visionos", "android_vr", "ios", "android"]]

_DC_STATIC = {
    "visionos": {"cid": 101, "client": {
        "clientName": "VISIONOS", "clientVersion": "0.1", "deviceMake": "Apple",
        "deviceModel": "RealityDevice14,1", "osName": "visionOS", "osVersion": "1.3.21O771"}},
    "android_vr": {"cid": 28, "client": {
        "clientName": "ANDROID_VR", "clientVersion": YT_DIRECT_VERSION, "deviceMake": "Oculus",
        "deviceModel": "Quest 3", "osName": "Android", "osVersion": "12L", "androidSdkVersion": 32,
        "userAgent": f"com.google.android.apps.youtube.vr.oculus/{YT_DIRECT_VERSION} "
                     f"(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"}},
    "ios": {"cid": 5, "client": {
        "clientName": "IOS", "clientVersion": "20.10.4", "deviceMake": "Apple", "deviceModel": "iPhone16,2",
        "osName": "iPhone", "osVersion": "18.3.2.22D82",
        "userAgent": "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"}},
    "android": {"cid": 3, "client": {
        "clientName": "ANDROID", "clientVersion": "20.10.38", "osName": "Android", "osVersion": "11",
        "androidSdkVersion": 30, "userAgent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"}},
}
_POT_CLIENTS = {"web", "web_safari", "mweb", "ios", "web_creator", "web_embedded", "tv", "tv_simply"}

_client_table_cache = [False, None]


def _ytdlp_client_table():
    if not _client_table_cache[0]:
        tbl = None
        for mod in ("yt_dlp.extractor.youtube._base", "yt_dlp.extractor.youtube"):
            try:
                m = __import__(mod, fromlist=["INNERTUBE_CLIENTS"])
                t = getattr(m, "INNERTUBE_CLIENTS", None)
                if t:
                    tbl = t
                    break
            except Exception:
                continue
        _client_table_cache[0], _client_table_cache[1] = True, tbl
    return _client_table_cache[1]


def dc_cfg(key):
    base = _DC_STATIC.get(key)
    c = (_ytdlp_client_table() or {}).get(key)
    if not base and not c:
        return None
    cfg = {"cid": base["cid"] if base else 0, "client": dict(base["client"]) if base else {}}
    if c:
        try:
            for k, v in ((c.get("INNERTUBE_CONTEXT") or {}).get("client") or {}).items():
                if v not in (None, ""):
                    cfg["client"][k] = v
            cfg["cid"] = c.get("INNERTUBE_CONTEXT_CLIENT_NAME") or cfg["cid"]
        except Exception:
            pass
    if not cfg["client"].get("clientName"):
        return None
    return cfg


class Visitor:

    def __init__(self, ttl=21600.0):
        self.v, self.t, self.ttl = "", 0.0, ttl

    def get(self):
        return self.v if self.v and time.monotonic() - self.t < self.ttl else ""

    def offer(self, v):
        if isinstance(v, str) and v and not self.get():
            self.v, self.t = v, time.monotonic()

    def clear(self):
        self.v, self.t = "", 0.0


class PotClient:

    def __init__(self):
        self.base = _env_str("YT_POT_URL").rstrip("/")
        self.cache = {}
        self.lock = threading.Lock()
        self.fail_until = 0.0
        self.fails = 0

    def enabled(self):
        return bool(self.base) and time.monotonic() >= self.fail_until

    def get(self, binding, px=""):
        if not self.enabled() or not binding:
            return ""
        now = time.monotonic()
        with self.lock:
            hit = self.cache.get(binding)
            if hit and hit[0] > now:
                return hit[1]
        body = {"content_binding": binding, "visitor_data": binding}
        if px:
            body["proxy"] = px
        try:
            req = urllib.request.Request(self.base + "/get_pot", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read(200000).decode("utf-8", "replace"))
            tok = data.get("poToken") or data.get("po_token") or ""
            if not tok:
                raise ValueError("khong co poToken")
        except Exception as e:
            self.fails += 1
            self.fail_until = time.monotonic() + min(600, 30 * self.fails)
            _say("pot", f"khong lay duoc PO token ({str(e)[:80]}), nghi {min(600, 30 * self.fails)}s")
            return ""
        self.fails = 0
        with self.lock:
            if len(self.cache) > 20:
                self.cache.clear()
            self.cache[binding] = (time.monotonic() + 3 * 3600, tok)
        return tok


POT = PotClient()


class ClientBoard:

    def __init__(self, dead_secs=YT_DIRECT_CLIENT_DEAD):
        self.ema, self.dead, self.fails = {}, {}, {}
        self.last_ok = ""
        self.dead_secs = dead_secs
        self.lock = threading.Lock()

    def order(self, names, first=""):
        now = time.monotonic()
        with self.lock:
            live = [n for n in names if now >= self.dead.get(n, 0.0)] or list(names)
            idx = {n: i for i, n in enumerate(names)}
            live.sort(key=lambda n: (0 if n == self.last_ok else 1, -self.ema.get(n, 0.5), idx[n]))
            if first and not self.last_ok and first in live:
                live.remove(first)
                live.insert(0, first)
            return live

    def ok(self, n):
        with self.lock:
            self.ema[n] = 0.7 * self.ema.get(n, 0.5) + 0.3
            self.fails[n] = 0
            self.last_ok = n
            self.dead.pop(n, None)

    def fail(self, n, hard=False, secs=None):
        with self.lock:
            self.ema[n] = 0.7 * self.ema.get(n, 0.5)
            self.fails[n] = self.fails.get(n, 0) + 1
            if hard or self.fails[n] >= 2:
                self.dead[n] = time.monotonic() + (self.dead_secs if secs is None else secs)
            if self.last_ok == n:
                self.last_ok = ""

    def kill(self, n, secs=None):
        with self.lock:
            self.dead[n] = time.monotonic() + (self.dead_secs if secs is None else secs)
            if self.last_ok == n:
                self.last_ok = ""

    def reset(self):
        with self.lock:
            self.dead.clear()
            self.fails.clear()
            self.last_ok = ""

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            return {n: {"ema": round(v, 2), "rest": max(0, int(self.dead.get(n, 0) - now))} for n, v in self.ema.items()}


def _mime(f):
    return str(f.get("mimeType", ""))


def _usable(lst):
    return [f for f in (lst or []) if isinstance(f, dict) and isinstance(f.get("url"), str) and f["url"].startswith("http")]


def _abr(f):
    return f.get("averageBitrate") or f.get("bitrate") or 0


def pick_video_formats(data, height_cap, want=("", "")):
    sd = (data or {}).get("streamingData") or {}
    cap = int(height_cap)
    ada = _usable(sd.get("adaptiveFormats"))
    vids = [f for f in ada if _mime(f).startswith("video/")]
    auds = [f for f in ada if _mime(f).startswith("audio/")]

    def fps_ok(f):
        return (f.get("fps") or 30) <= 30

    pool = ([f for f in vids if "avc1" in _mime(f) and fps_ok(f) and (f.get("height") or 0) <= cap]
            or [f for f in vids if fps_ok(f) and (f.get("height") or 0) <= cap]
            or [f for f in vids if (f.get("height") or 0) <= cap])
    if pool:
        v = max(pool, key=lambda f: (f.get("height") or 0, "avc1" in _mime(f), -(f.get("bitrate") or 0)))
    elif vids:
        v = min(vids, key=lambda f: (f.get("height") or 99999, f.get("bitrate") or 0))
    else:
        v = None
    if v is not None:
        a = None
        if auds:
            low = [f for f in auds if _abr(f) <= YT_AUDIO_MAX_BPS]
            a = (max(low, key=lambda f: ("mp4a" in _mime(f), _abr(f))) if low else min(auds, key=_abr))
        if want and want[0]:
            _w = [f for f in vids if str(f.get("itag")) == want[0]]
            if _w:
                v = _w[0]
        if want and len(want) > 1 and want[1]:
            _w = [f for f in auds if str(f.get("itag")) == want[1]]
            if _w:
                a = _w[0]
        return v["url"], (a["url"] if a else None)
    comb = [f for f in _usable(sd.get("formats")) if _mime(f).startswith("video/")]
    comb = [f for f in comb if (f.get("height") or 0) <= cap] or comb
    if comb:
        return max(comb, key=lambda f: -abs((f.get("height") or 0) - cap))["url"], None
    return None


def pick_audio_format(data, max_bps):
    sd = (data or {}).get("streamingData") or {}
    auds = [f for f in _usable(sd.get("adaptiveFormats")) if _mime(f).startswith("audio/")]
    if auds:
        low = [f for f in auds if _abr(f) <= max_bps]
        a = max(low, key=lambda f: ("mp4a" in _mime(f), _abr(f))) if low else min(auds, key=_abr)
        return a["url"]
    comb = [f for f in _usable(sd.get("formats")) if _mime(f).startswith("video/")]
    if comb:
        return min(comb, key=lambda f: f.get("height") or 99999)["url"]
    return None


def hls_pick(master, ua, opener, cap):
    req = urllib.request.Request(master, headers={"User-Agent": ua})
    try:
        with opener.open(req, timeout=YT_DIRECT_TIMEOUT) as r:
            txt = r.read(600000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise DirectFail(f"hls HTTP {e.code}", hard=True) from None
    except (urllib.error.URLError, OSError) as e:
        raise DirectFail(f"hls loi mang: {str(e)[:50]}", hard=True) from None
    if "#EXT-X-STREAM-INF" not in txt:
        return master
    lines = txt.splitlines()
    cands = []
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = next((x.strip() for x in lines[i + 1:i + 3] if x.strip() and not x.startswith("#")), "")
        if not uri or not ("mp4a" in ln or "AUDIO=" not in ln):
            continue
        mh = re.search(r"RESOLUTION=\d+x(\d+)", ln)
        mb = re.search(r"BANDWIDTH=(\d+)", ln)
        cands.append((int(mh.group(1)) if mh else 0, int(mb.group(1)) if mb else 0, uri))
    if not cands:
        return master
    ok = [c for c in cands if 0 < c[0] <= cap]
    pick = max(ok, key=lambda c: (c[0], -c[1])) if ok else min(cands, key=lambda c: (c[0] or 99999, c[1]))
    return urllib.parse.urljoin(master, pick[2])


def dc_probe(url, ua, opener, key):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Range": "bytes=0-1"})
    try:
        with opener.open(req, timeout=6) as r:
            r.read(2)
    except urllib.error.HTTPError as e:
        raise DirectFail(f"{key}: googlevideo HTTP {e.code}", hard=True, kind="block" if e.code in (403, 429) else "other") from None
    except (urllib.error.URLError, OSError) as e:
        raise DirectFail(f"{key}: probe loi {str(e)[:50]}", hard=True, kind="net") from None


def _add_pot(url, pot):
    if not pot or "pot=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "pot=" + urllib.parse.quote(pot, safe="")


def innertube_player(base, key, video_id, opener, visitor, px="", timeout=None):
    cfg = dc_cfg(key)
    if not cfg:
        raise DirectFail(f"{key}: client khong ton tai", hard=True)
    cl = dict(cfg["client"])
    cl.setdefault("hl", "en")
    cl.setdefault("gl", "US")
    ua = cl.get("userAgent") or _GEN_UA
    vd = visitor.get()
    if vd:
        cl["visitorData"] = vd
    body = {
        "context": {"client": cl},
        "videoId": video_id,
        "contentCheckOk": True, "racyCheckOk": True,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
    }
    pot = ""
    if key in _POT_CLIENTS and POT.enabled():
        pot = POT.get(vd, px)
        if pot:
            body["serviceIntegrityDimensions"] = {"poToken": pot}
    hd = {"Content-Type": "application/json", "User-Agent": ua, "Origin": base,
          "Accept-Language": "en-US,en;q=0.9",
          "X-YouTube-Client-Name": str(cfg["cid"]), "X-YouTube-Client-Version": str(cl.get("clientVersion", ""))}
    if vd:
        hd["X-Goog-Visitor-Id"] = vd
    req = urllib.request.Request(base + "/youtubei/v1/player?prettyPrint=false",
                                 data=json.dumps(body).encode("utf-8"), method="POST", headers=hd)
    try:
        with opener.open(req, timeout=timeout or YT_DIRECT_TIMEOUT) as r:
            data = json.loads(r.read(3_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            HEAT.bump(1.0 if e.code == 403 else 2.0)
        raise DirectFail(f"{key}: HTTP {e.code}", hard=True, kind="bot" if e.code == 429 else "block") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DirectFail(f"{key}: loi mang/JSON: {str(e)[:60]}", hard=True, kind="net") from None
    if not isinstance(data, dict):
        raise DirectFail(f"{key}: phan hoi khong phai JSON object", hard=True)
    visitor.offer((data.get("responseContext") or {}).get("visitorData"))
    ps = data.get("playabilityStatus") or {}
    if ps.get("status") != "OK":
        why = f"{ps.get('status', '?')}: {str(ps.get('reason') or '')[:100]}"
        kind = classify_error(why)
        if kind in ("gone",):
            raise ResolveError(why, permanent=True)
        if kind == "geo":
            raise ResolveError(why, permanent=True)
        if kind == "bot" or "not a bot" in why or "Sign in to confirm" in why and "age" not in why:
            HEAT.bump(1.0)
            raise DirectFail(f"{key}: {why}", hard=True, kind="bot")
        raise DirectFail(f"{key}: {why}", hard=(ps.get("status") == "LOGIN_REQUIRED"), kind=kind)
    return data, ua, pot


def _url_itag(u):
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(u or "").query).get("itag", [""])[0]
    except Exception:
        return ""


def direct_resolve_video(base, key, video_id, cap, opener, visitor, px="", want=("", "")):
    data, ua, pot = innertube_player(base, key, video_id, opener, visitor, px)
    hdr = f"User-Agent: {ua}\r\n"
    pick = pick_video_formats(data, cap, want)
    if pick:
        v, a = _add_pot(pick[0], pot), (_add_pot(pick[1], pot) if pick[1] else None)
        if YT_DIRECT_PROBE:
            dc_probe(v, ua, opener, key)
        return v, a, hdr, (hdr if a else "")
    hls = (data.get("streamingData") or {}).get("hlsManifestUrl")
    if isinstance(hls, str) and hls.startswith("http"):
        return hls_pick(hls, ua, opener, int(cap)), None, hdr, ""
    raise DirectFail(f"{key}: khong co luong co URL (can PO token / SABR?)", kind="pot")


def direct_resolve_audio(base, key, video_id, opener, visitor, px=""):
    data, ua, pot = innertube_player(base, key, video_id, opener, visitor, px)
    hdr = f"User-Agent: {ua}\r\n"
    src = pick_audio_format(data, YTM_AUDIO_MAX_BPS)
    if src:
        src = _add_pot(src, pot)
        if YT_DIRECT_PROBE:
            dc_probe(src, ua, opener, key)
        return src, hdr
    hls = (data.get("streamingData") or {}).get("hlsManifestUrl")
    if isinstance(hls, str) and hls.startswith("http"):
        return hls_pick(hls, ua, opener, 240), hdr
    raise DirectFail(f"{key}: khong co luong audio co URL (can PO token / SABR?)", kind="pot")


def _res_expiry(urls):
    best = None
    for u in urls:
        if not u:
            continue
        try:
            exp = int(urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("expire", ["0"])[0])
        except Exception:
            continue
        if exp:
            m = time.monotonic() + (exp - time.time()) - 120
            best = m if best is None else min(best, m)
    return best


Streams = collections.namedtuple("Streams", "v a vh ah px cl", defaults=("", ""))

RAPID_ENABLED = _env_bool("RAPID_ENABLED", False)  # RapidAPI tat mac dinh; bat bang RAPID_ENABLED=1
RAPID_KEY = _env_str("RAPIDAPI_KEY") if RAPID_ENABLED else ""
RAPID_URL = _env_str("RAPID_URL", "https://youtube-media-downloader.p.rapidapi.com/v2/video/details")
RAPID_HOST = urllib.parse.urlparse(RAPID_URL).netloc
RAPID_ONLY = _env_bool("RAPID_ONLY", False)
RAPID_FAST_HOST = _env_str("RAPID_FAST_HOST", "youtube-video-fast-downloader-24-7.p.rapidapi.com")
RAPID_FAST_QUALITY = _env_str("RAPID_FAST_QUALITY", "18")
RAPID_FAST_QUALITY_LOW = _env_str("RAPID_FAST_QUALITY_LOW", "17")
RAPID_FILE_WAIT = _env_float("RAPID_FILE_WAIT", 45)
RAPID_POLL = max(0.5, _env_float("RAPID_POLL", 1.5))
RAPID_API_TIMEOUT = _env_float("RAPID_API_TIMEOUT", 40)
RAPID_SKIP_413 = _env_float("RAPID_SKIP_413", 1800)
RAPID_SKIP_OTHER = _env_float("RAPID_SKIP_OTHER", 90)
RAPID_SKIP_QUOTA = _env_float("RAPID_SKIP_QUOTA", 600)
RAPID_STREAMS_ON = _env_bool("RAPID_STREAMS", True)
WARM_RAPID = _env_bool("WARM_RAPID", False)
YTM_RAPID = _env_bool("YTM_RAPID", True)
ALT_PIPED = [u.rstrip("/") for u in _env_list("YT_PIPED")]
ALT_INVIDIOUS = [u.rstrip("/") for u in _env_list("YT_INVIDIOUS")]
ALT_COOL = _env_float("YT_ALT_COOL", 300)


def _http_json(url, headers=None, timeout=10, limit=3_000_000):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA, "Accept": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(limit).decode("utf-8", "replace"))


def _h_from_quality(q):
    m = re.match(r"\s*(\d{3,4})", str(q or ""))
    return int(m.group(1)) if m else 0


class AltSources:
    def __init__(self):
        self.lock = threading.Lock()
        self.skip_all = 0.0
        self.skip_vid = {}
        self.big = {}
        self.streams_off_until = 0.0
        self.inst_dead = {}

    def rapid_on(self):
        return bool(RAPID_KEY)

    def should_skip(self, vid):
        now = time.monotonic()
        with self.lock:
            return now < self.skip_all or now < self.skip_vid.get(vid, 0.0)

    def note_failure(self, vid, msg):
        now = time.monotonic()
        with self.lock:
            if "HTTP 413" in msg:
                self.skip_vid[vid] = now + RAPID_SKIP_413
            elif any(f"HTTP {c}" in msg for c in (401, 402, 403, 429)):
                self.skip_all = now + RAPID_SKIP_QUOTA
            else:
                self.skip_vid[vid] = now + RAPID_SKIP_OTHER
            if len(self.skip_vid) > 300:
                for k in [k for k, t in self.skip_vid.items() if now >= t]:
                    self.skip_vid.pop(k, None)

    def _rapid_details(self, vid):
        url = RAPID_URL + "?" + urllib.parse.urlencode({"videoId": vid, "urlAccess": "normal"})
        req = urllib.request.Request(url, headers={"x-rapidapi-host": RAPID_HOST, "x-rapidapi-key": RAPID_KEY,
                                                   "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read(5_000_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read(200).decode("utf-8", "replace")
            raise ResolveError(f"RapidAPI HTTP {e.code}: {body}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ResolveError(f"RapidAPI loi mang: {str(e)[:100]}") from None

    def rapid_streams(self, vid, cap):
        if not RAPID_KEY:
            raise ResolveError("chua dat RAPIDAPI_KEY")
        data = self._rapid_details(vid)
        vitems = [v for v in ((data.get("videos") or {}).get("items") or []) if isinstance(v, dict) and v.get("url")]
        aitems = [a for a in ((data.get("audios") or {}).get("items") or []) if isinstance(a, dict) and a.get("url")]
        cap = int(cap)
        hdr = f"User-Agent: {UA}\r\n"
        combined = [v for v in vitems if v.get("hasAudio") and (v.get("height") or 0) <= cap]
        if combined:
            best = max(combined, key=lambda v: (v.get("height") or 0, "avc1" in str(v.get("mimeType", ""))))
            return best["url"], None, hdr, ""
        vids = [v for v in vitems if str(v.get("mimeType", "")).startswith("video/") and (v.get("height") or 0) <= cap]
        if not vids:
            raise ResolveError("RapidAPI khong co luong video phu hop")
        v = max(vids, key=lambda v: ("avc1" in str(v.get("mimeType", "")), v.get("height") or 0))
        if not aitems:
            return v["url"], None, hdr, ""
        a = min(aitems, key=lambda x: x.get("size") or x.get("bitrate") or 0)
        return v["url"], a["url"], hdr, hdr

    def rapid_audio(self, vid):
        if not RAPID_KEY:
            raise ResolveError("chua dat RAPIDAPI_KEY")
        data = self._rapid_details(vid)
        aitems = [a for a in ((data.get("audios") or {}).get("items") or []) if isinstance(a, dict) and a.get("url")]
        if not aitems:
            raise ResolveError("RapidAPI khong co luong audio")

        def br(x):
            return x.get("bitrate") or x.get("size") or 0
        low = [x for x in aitems if (x.get("bitrate") or 0) and x.get("bitrate") <= YTM_AUDIO_MAX_BPS]
        a = max(low, key=br) if low else min(aitems, key=br)
        return a["url"], f"User-Agent: {UA}\r\n"

    def rapid_file(self, vid):
        if not RAPID_KEY:
            raise ResolveError("chua dat RAPIDAPI_KEY")
        qualities = [RAPID_FAST_QUALITY]
        if RAPID_FAST_QUALITY_LOW and RAPID_FAST_QUALITY_LOW not in qualities:
            qualities.append(RAPID_FAST_QUALITY_LOW)
        if len(qualities) > 1 and time.monotonic() < self.big.get(vid, 0.0):
            qualities = qualities[1:]
        data = None
        for qi, qual in enumerate(qualities):
            api = (f"https://{RAPID_FAST_HOST}/download_video/{vid}?" + urllib.parse.urlencode({"quality": qual}))
            req = urllib.request.Request(api, headers={"x-rapidapi-host": RAPID_FAST_HOST, "x-rapidapi-key": RAPID_KEY,
                                                       "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=RAPID_API_TIMEOUT) as r:
                    data = json.loads(r.read(200_000).decode("utf-8", "replace"))
                if qi:
                    print(f"[rapid] chat luong {qualities[0]} qua lon -> dung chat luong thap {qual}", flush=True)
                break
            except urllib.error.HTTPError as e:
                body = e.read(200).decode("utf-8", "replace")
                if e.code == 413 and qi + 1 < len(qualities):
                    print(f"[rapid] HTTP 413 o chat luong {qual} -> thu chat luong thap hon", flush=True)
                    if len(self.big) > 300:
                        self.big.clear()
                    self.big[vid] = time.monotonic() + RAPID_SKIP_413
                    continue
                raise ResolveError(f"RapidAPI HTTP {e.code}: {body}") from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise ResolveError(f"RapidAPI loi mang: {str(e)[:100]}") from None
        file_url = data.get("file") if isinstance(data, dict) else None
        if not file_url or not _url_is_public(file_url):
            raise ResolveError("RapidAPI khong tra file hop le: " + str(data)[:200])
        deadline = time.monotonic() + RAPID_FILE_WAIT
        while True:
            try:
                rq = urllib.request.Request(file_url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
                with urllib.request.urlopen(rq, timeout=15) as r:
                    if r.status in (200, 206):
                        break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise ResolveError(f"file tu RapidAPI loi HTTP {e.code}") from None
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > deadline:
                raise ResolveError(f"file chua san sang sau {RAPID_FILE_WAIT:.0f}s")
            time.sleep(RAPID_POLL)
        print("[rapid] file san sang, bat dau phat", flush=True)
        return file_url, None, "", ""

    @staticmethod
    def urls_usable(urls):
        for u in urls:
            if not u:
                continue
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Range": "bytes=0-1"})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    r.read(2)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 410):
                    return False
            except Exception:
                pass
        return True

    def _inst_ok(self, inst):
        return time.monotonic() >= self.inst_dead.get(inst, 0.0)

    def _inst_fail(self, inst):
        self.inst_dead[inst] = time.monotonic() + ALT_COOL

    def piped(self, inst, vid, cap, audio_only):
        data = _http_json(f"{inst}/streams/{vid}", timeout=10)
        hdr = f"User-Agent: {UA}\r\n"
        vs = [s for s in (data.get("videoStreams") or []) if isinstance(s, dict) and s.get("url")]
        as_ = [s for s in (data.get("audioStreams") or []) if isinstance(s, dict) and s.get("url")]
        if audio_only:
            if not as_:
                raise ResolveError("piped: khong co audio")
            low = [s for s in as_ if 0 < (s.get("bitrate") or 0) <= YTM_AUDIO_MAX_BPS]
            a = max(low, key=lambda s: s.get("bitrate") or 0) if low else min(as_, key=lambda s: s.get("bitrate") or 1 << 30)
            return Streams(a["url"], None, hdr, "")
        cap = int(cap)

        def hh(s):
            return s.get("height") or _h_from_quality(s.get("quality"))
        comb = [s for s in vs if not s.get("videoOnly") and 0 < hh(s) <= cap]
        if comb:
            b = max(comb, key=lambda s: (hh(s), "avc" in str(s.get("codec", "")) or "avc1" in str(s.get("mimeType", ""))))
            return Streams(b["url"], None, hdr, "")
        vo = [s for s in vs if s.get("videoOnly") and 0 < hh(s) <= cap]
        if vo and as_:
            v = max(vo, key=lambda s: ("avc" in str(s.get("codec", "")) or "avc1" in str(s.get("mimeType", "")), hh(s)))
            a = min(as_, key=lambda s: s.get("bitrate") or 1 << 30)
            return Streams(v["url"], a["url"], hdr, hdr)
        if data.get("hls"):
            return Streams(data["hls"], None, hdr, "")
        raise ResolveError("piped: khong co luong phu hop")

    def invidious(self, inst, vid, cap, audio_only):
        data = _http_json(f"{inst}/api/v1/videos/{vid}?local=true", timeout=10)
        hdr = f"User-Agent: {UA}\r\n"
        ada = [f for f in (data.get("adaptiveFormats") or []) if isinstance(f, dict) and f.get("url")]
        fmt = [f for f in (data.get("formatStreams") or []) if isinstance(f, dict) and f.get("url")]

        def br(f):
            try:
                return int(f.get("bitrate") or 0)
            except (TypeError, ValueError):
                return 0
        if audio_only:
            auds = [f for f in ada if str(f.get("type", "")).startswith("audio/")]
            if not auds:
                raise ResolveError("invidious: khong co audio")
            low = [f for f in auds if 0 < br(f) <= YTM_AUDIO_MAX_BPS]
            a = max(low, key=br) if low else min(auds, key=lambda f: br(f) or 1 << 30)
            return Streams(a["url"], None, hdr, "")
        cap = int(cap)

        def hh(f):
            return _h_from_quality(f.get("qualityLabel") or f.get("resolution") or f.get("quality"))
        comb = [f for f in fmt if 0 < hh(f) <= cap]
        if comb:
            b = max(comb, key=hh)
            return Streams(b["url"], None, hdr, "")
        if fmt:
            b = min(fmt, key=lambda f: hh(f) or 99999)
            return Streams(b["url"], None, hdr, "")
        raise ResolveError("invidious: khong co luong phu hop")

    def configured(self, audio):
        if audio:
            return bool((RAPID_KEY and YTM_RAPID) or ALT_PIPED or ALT_INVIDIOUS)
        return bool(RAPID_KEY or ALT_PIPED or ALT_INVIDIOUS)

    def try_all(self, vid, cap, audio, background):
        errs = []
        if RAPID_KEY and (not audio or YTM_RAPID) and (WARM_RAPID or not background) and not self.should_skip(vid):
            if RAPID_STREAMS_ON and time.monotonic() >= self.streams_off_until:
                try:
                    if audio:
                        src, hdr = self.rapid_audio(vid)
                        res = (src, None, hdr, "")
                    else:
                        res = self.rapid_streams(vid, cap)
                    if not self.urls_usable(res[:2]):
                        self.streams_off_until = time.monotonic() + RAPID_SKIP_QUOTA
                        raise ResolveError("RapidAPI streams: URL bi khoa theo IP (googlevideo 403)")
                    print(f"[resolve] RapidAPI (streams) OK cho {vid}", flush=True)
                    return Streams(res[0], res[1], res[2], res[3], "", "rapid-streams")
                except ResolveError as e:
                    if any(f"HTTP {c}" in str(e) for c in (401, 402, 403, 429)):
                        self.streams_off_until = time.monotonic() + RAPID_SKIP_QUOTA
                    errs.append(str(e)[:100])
                except Exception as e:
                    errs.append(f"rapid: {type(e).__name__}")
        for kind, insts in (("piped", ALT_PIPED), ("invidious", ALT_INVIDIOUS)):
            if background:
                break
            for inst in insts:
                if not self._inst_ok(inst):
                    continue
                try:
                    fn = self.piped if kind == "piped" else self.invidious
                    res = fn(inst, vid, cap, audio)
                    print(f"[resolve] {kind} OK ({urllib.parse.urlsplit(inst).hostname}) cho {vid}", flush=True)
                    return res._replace(px="", cl=f"{kind}")
                except Exception as e:
                    self._inst_fail(inst)
                    errs.append(f"{kind}:{urllib.parse.urlsplit(inst).hostname}: {str(e)[:60]}")
        if (RAPID_KEY and not audio and (WARM_RAPID or not background) and not self.should_skip(vid)):
            try:
                f = self.rapid_file(vid)
                return Streams(f[0], f[1], f[2], f[3], "", "rapid-file")
            except ResolveError as e:
                self.note_failure(vid, str(e))
                errs.append(str(e)[:100])
        raise ResolveError(" | ".join(errs)[:400] or "khong co nguon du phong")


ALT = AltSources()


YT_DIRECT = _env_bool("YT_DIRECT", True)
YTM_DIRECT = _env_bool("YTM_DIRECT", True)
YT_DIRECT_FIRST = _env_bool("YT_DIRECT_FIRST", True)
YT_DIRECT_MAX_FAILS = _env_int("YT_DIRECT_MAX_FAILS", 3)
YT_DIRECT_COOLDOWN = _env_float("YT_DIRECT_COOLDOWN", 600)
YTM_DIRECT_MAX_FAILS = max(2, _env_int("YTM_DIRECT_MAX_FAILS", 3))
YTM_DIRECT_COOLDOWN = _env_float("YTM_DIRECT_COOLDOWN", 600)
YT_BLOCK_VIDEOS_HARD = _env_int("YT_BLOCK_VIDEOS_HARD", 2)
YT_BLOCK_VIDEOS_SOFT = _env_int("YT_BLOCK_VIDEOS_SOFT", 3)
YT_BLOCK_WINDOW = _env_float("YT_BLOCK_WINDOW", 180)
YT_FAIL_TTL = _env_float("YT_FAIL_TTL", 20)
YT_NEG_TTL = _env_float("YT_NEG_TTL", 90)
GEO_TTL = _env_float("GEO_TTL", 3600)
RESOLVE_TTL = _env_int("RESOLVE_TTL", 5400)
YTM_RESOLVE_TTL = _env_int("YTM_RESOLVE_TTL", 3600)
YT_NOCOOKIE_MAX_FAILS = _env_int("YT_NOCOOKIE_MAX_FAILS", 2)
YT_NOCOOKIE_COOLDOWN = _env_float("YT_NOCOOKIE_COOLDOWN", 1800)
YT_FMT_LEAN = _env_bool("YT_FMT_LEAN", True)
YT_LIGHT = _env_bool("YT_LIGHT", True)
YT_FORCE_IPV4 = _env_bool("YT_FORCE_IPV4", False)
YT_RACE_FIRST = _env_str("YT_RACE_FIRST", "mweb")
YT_FIRST_BUDGET = _env_float("YT_FIRST_BUDGET", 25)
YT_FIRST_GATE = _env_float("YT_FIRST_GATE", 5)
YT_WARM_BUDGET = _env_float("YT_WARM_BUDGET", 20)
RACE_RAPID = _env_bool("RACE_RAPID", True)
TURBO_REFRESH_YTDLP = _env_bool("TURBO_REFRESH_YTDLP", False)

_YT_ATTEMPTS_ALL = [
    ("visionos", ["visionos"], False, False),
    ("android_vr", ["android_vr"], False, False),
    ("web_embedded", ["web_embedded"], False, False),
    ("tv_downgraded", ["tv_downgraded"], True, True),
    ("tv", ["tv"], True, True),
    ("mweb", ["mweb"], True, True),
    ("web_safari", ["web_safari"], True, True),
    ("android", ["android"], False, False),
    ("runtime+ejs", None, True, True),
]
_attempts_cache = []


def _installed_attempts():
    if _attempts_cache:
        return _attempts_cache
    t = _ytdlp_client_table() if HAS_YT else None
    out = [a for a in _YT_ATTEMPTS_ALL if not (t is not None and a[1] and any(c not in t for c in a[1]))]
    _attempts_cache.extend(out or _YT_ATTEMPTS_ALL)
    return _attempts_cache


def _extract_with(video_url, ydl_opts):
    info = _ydl_info(video_url, ydl_opts, slim=True)
    if info is None:
        raise RuntimeError("yt-dlp khong tra thong tin")
    if "entries" in info:
        _ents = [e for e in (info.get("entries") or []) if e]
        if not _ents:
            raise RuntimeError("Video unavailable (khong co entries)")
        info = _ents[0]
    formats = info.get("requested_formats")
    if formats:
        video_fmt = next((f for f in formats if f.get("vcodec") not in (None, "none")), formats[0])
        audio_fmt = next((f for f in formats if f.get("acodec") not in (None, "none")), None)
        if audio_fmt and audio_fmt is video_fmt:
            audio_fmt = None
        return (video_fmt["url"], audio_fmt["url"] if audio_fmt else None,
                format_ffmpeg_headers(video_fmt), format_ffmpeg_headers(audio_fmt) if audio_fmt else "")
    if not info.get("url"):
        raise RuntimeError("khong lay duoc URL luong tu yt-dlp")
    return info["url"], None, format_ffmpeg_headers(info), ""


class Resolver:
    def __init__(self, name, audio):
        self.name = name
        self.audio = audio
        self.label = "YT Music" if audio else "YouTube"
        self.cache, self.neg, self.fail, self.locks = {}, {}, {}, {}
        self.guard = threading.Lock()
        self.gate = threading.Semaphore(max(1, _env_int("YTM_RESOLVE_PAR" if audio else "YT_RESOLVE_PAR", 2 if audio else 1)))
        self.gate_wait = _env_float("YTM_GATE_WAIT", 40) if audio else _env_float("YT_GATE_WAIT", 90)
        self.budget = _env_float("YTM_RESOLVE_BUDGET", 40) if audio else _env_float("YT_RESOLVE_BUDGET", 45)
        pre = "YTM_" if audio else "YT_"
        self.block = Breaker(_env_float(pre + "BLOCK_BACKOFF", 30), _env_float(pre + "BLOCK_MAX", 900))
        self.blockfails = RecentFails(YT_BLOCK_WINDOW)
        self.nocookie_fails = 0
        self.nocookie_skip_until = 0.0
        self.nocookie_vids = RecentFails(YT_BLOCK_WINDOW)
        self.board = ClientBoard(dead_secs=120.0)
        self.dboard = ClientBoard()
        self.visitor = Visitor()
        self.direct_on = YTM_DIRECT if audio else YT_DIRECT
        self.direct_max = YTM_DIRECT_MAX_FAILS if audio else YT_DIRECT_MAX_FAILS
        self.direct_cool = YTM_DIRECT_COOLDOWN if audio else YT_DIRECT_COOLDOWN
        self.direct_vids = RecentFails(float(_env_float("YT_DIRECT_FAIL_WINDOW", 900)))
        self.direct_off_until = 0.0
        self.geo = {}
        self.geo_lock = threading.Lock()
        self.ema = 0.0
        self.ttl = YTM_RESOLVE_TTL if audio else RESOLVE_TTL
        self.stats = collections.Counter()
        self.itb = YTM_INNERTUBE_BASE if audio else INNERTUBE_BASE
        if not _env_bool("YTM_USE_MUSIC_HOST", False):
            self.itb = INNERTUBE_BASE

    def reset(self, why=""):
        self.block.reset()
        self.blockfails.clear()
        self.nocookie_fails = 0
        self.nocookie_skip_until = 0.0
        self.nocookie_vids.clear()
        self.board.reset()
        self.dboard.reset()
        self.visitor.clear()
        self.direct_vids.clear()
        self.direct_off_until = 0.0
        with self.guard:
            self.neg.clear()
            self.fail.clear()

    def geo_mark(self, vid):
        now = time.monotonic()
        with self.geo_lock:
            if len(self.geo) > 300:
                for k in [k for k, t in self.geo.items() if now >= t]:
                    self.geo.pop(k, None)
            self.geo[vid] = now + GEO_TTL

    def geo_known(self, vid):
        with self.geo_lock:
            return time.monotonic() < self.geo.get(vid, 0.0)

    def cached(self, url, cap="0"):
        with self.guard:
            hit = self.cache.get((_canon_url(url), str(cap)))
            return bool(hit and time.monotonic() < hit[2])

    def drop(self, url, cap="0"):
        with self.guard:
            self.cache.pop((_canon_url(url), str(cap)), None)

    def snapshot(self):
        return {"block_left": int(self.block.left()), "block_level": self.block.level, "trips": self.block.trips,
                "direct_off": max(0, int(self.direct_off_until - time.monotonic())),
                "ytdlp_clients": self.board.snapshot(), "direct_clients": self.dboard.snapshot(),
                "cache": len(self.cache), "ema_s": round(self.ema, 1), "stats": dict(self.stats)}

    def resolve(self, url, cap="360", background=False, force=False):
        url = _canon_url(url)
        cap = str(cap)
        key = (url, "0" if self.audio else cap)
        with self.guard:
            if force:
                self.cache.pop(key, None)
            now = time.monotonic()
            hit = self.cache.get(key)
            if hit and now < hit[2]:
                self.stats["cache_hit"] += 1
                return hit[1]
            neg = self.neg.get(url)
            if neg and now < neg[0]:
                raise ResolveError(neg[1], permanent=True)
            fc = self.fail.get(url)
            if fc and now < fc[0] and not force and not background:
                raise ResolveError(fc[1] + f" (vua loi {now - fc[2]:.0f}s truoc, thu lai sau it giay)", permanent=False)
            if len(self.locks) > 100:
                for k in [k for k, l in self.locks.items() if not l.locked()]:
                    self.locks.pop(k, None)
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            with self.guard:
                hit = self.cache.get(key)
                if hit and time.monotonic() < hit[2]:
                    return hit[1]
            t0 = time.monotonic()
            try:
                res = self._uncached(url, cap, background)
            except ResolveError as e:
                self.stats["fail"] += 1
                if e.permanent:
                    with self.guard:
                        self.neg[url] = (time.monotonic() + YT_NEG_TTL, str(e)[:300])
                        if len(self.neg) > 200:
                            _n = time.monotonic()
                            for k in [k for k, v in self.neg.items() if _n >= v[0]]:
                                self.neg.pop(k, None)
                elif not background and YT_FAIL_TTL > 0 and "dang chan IP" not in str(e) and "dang ban" not in str(e) \
                        and "nghi them" not in str(e):
                    with self.guard:
                        _t = time.monotonic()
                        self.fail[url] = (_t + YT_FAIL_TTL, str(e)[:300], _t)
                        if len(self.fail) > 100:
                            for k in [k for k, v in self.fail.items() if _t >= v[0]]:
                                self.fail.pop(k, None)
                print(f"[resolve:{self.name}] that bai ({str(e)[:120]})", flush=True)
                raise
            except Exception as e:
                self.stats["fail"] += 1
                print(f"[resolve:{self.name}] loi ({str(e)[:120]})", flush=True)
                raise
            dt = time.monotonic() - t0
            self.ema = dt if not self.ema else 0.6 * self.ema + 0.4 * dt
            self.stats["ok"] += 1
            print(f"[resolve:{self.name}] OK {res.cl or '?'} mat {dt:.1f}s cho {url} (tb {self.ema:.0f}s)", flush=True)
            with self.guard:
                ts = time.monotonic()
                until = ts + self.ttl
                exp = _res_expiry((res.v, res.a))
                if exp is not None:
                    until = min(until, exp)
                elif res.v and "googlevideo.com" not in res.v:
                    until = min(until, ts + 480)
                if until > ts + 30:
                    self.cache[key] = (ts, res, until)
                for k in [k for k, v in self.cache.items() if ts >= v[2]]:
                    self.cache.pop(k, None)
                if len(self.cache) > (200 if self.audio else 60):
                    for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:20]:
                        self.cache.pop(k, None)
            return res

    def _uncached(self, url, cap, background):
        m = _YT_ID_RE.search(url or "")
        if not (m and _is_youtube(url)):
            return self._generic(url, cap, background)
        vid = m.group(1)
        px = PROXIES.current()
        geo = self.geo_known(vid)
        err_direct = None
        if self.direct_on and YT_DIRECT_FIRST and not geo:
            r = self._direct(vid, cap, px, background)
            if r is not None:
                return r
        if geo:
            print(f"[resolve:{self.name}] {vid} bi chan theo vung (da biet) -> bo qua yt-dlp", flush=True)
            if self.direct_on and not YT_DIRECT_FIRST:
                r = self._direct(vid, cap, px, background)
                if r is not None:
                    return r
            err = ResolveError("video bi chan theo vung (khong phat duoc o vung may chu)", permanent=False)
            if ALT.configured(self.audio) and not RAPID_ONLY:
                try:
                    return ALT.try_all(vid, cap, self.audio, background)
                except ResolveError:
                    pass
            raise err
        alt_ok = ALT.configured(self.audio) and (not background or WARM_RAPID) and not (background and self.audio)
        race = (RACE_RAPID and alt_ok and not self.audio and bool(RAPID_KEY) and not background and not RAPID_ONLY)
        if race:
            box = self._race(url, vid, cap, px)
            if box["res"] is not None:
                return box["res"]
            ye = box["yt_err"]
            if isinstance(ye, ResolveError) and ye.permanent:
                raise ye
            raise ResolveError(str(box["alt_err"] or ye or "ca 2 nguon that bai")[:600])
        if not RAPID_ONLY:
            ye = None
            try:
                return self._ytdlp(url, vid, cap, px, background,
                                   budget=(YT_WARM_BUDGET if background else (YT_FIRST_BUDGET if alt_ok else None)),
                                   wait=(0.0 if background else (YT_FIRST_GATE if alt_ok else self.gate_wait)))
            except ResolveError as e:
                if e.permanent:
                    raise
                ye = e
            if not alt_ok:
                raise ye
            try:
                return ALT.try_all(vid, cap, self.audio, background)
            except ResolveError as ae:
                raise ResolveError(f"{str(ye)[:300]} | {str(ae)[:300]}")
        if alt_ok:
            return ALT.try_all(vid, cap, self.audio, background)
        raise ResolveError("RAPID_ONLY nhung khong co nguon du phong nao")

    def _race(self, url, vid, cap, px):
        box = {"res": None, "src": None, "yt_err": None, "alt_err": None, "yt_done": False, "alt_done": False}
        cv = threading.Condition()
        t0 = time.monotonic()

        def finish(done_key, err_key, err):
            with cv:
                box[err_key] = err
                box[done_key] = True
                cv.notify_all()

        def _yt():
            err = None
            try:
                r = self._ytdlp(url, vid, cap, px, False, budget=YT_FIRST_BUDGET, wait=YT_FIRST_GATE)
                with cv:
                    if box["res"] is None:
                        box["res"], box["src"] = r, "yt-dlp"
                        cv.notify_all()
            except Exception as e:
                err = e
            finish("yt_done", "yt_err", err)

        def _alt():
            err = None
            try:
                r = ALT.try_all(vid, cap, False, False)
                with cv:
                    if box["res"] is None:
                        box["res"], box["src"] = r, r.cl or "alt"
                        cv.notify_all()
            except Exception as e:
                err = e
            finish("alt_done", "alt_err", err)

        threading.Thread(target=_yt, daemon=True).start()
        threading.Thread(target=_alt, daemon=True).start()
        deadline = t0 + max(YT_FIRST_BUDGET + 60.0, RAPID_FILE_WAIT + RAPID_API_TIMEOUT + 10.0)
        with cv:
            while box["res"] is None and not (box["yt_done"] and box["alt_done"]):
                ye = box["yt_err"]
                if isinstance(ye, ResolveError) and ye.permanent:
                    break
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                cv.wait(min(rem, 1.0))
        if box["res"] is not None:
            print(f"[race:{self.name}] {box['src']} thang sau {time.monotonic() - t0:.1f}s cho {vid}", flush=True)
        else:
            print(f"[race:{self.name}] ca 2 nguon that bai sau {time.monotonic() - t0:.1f}s cho {vid}", flush=True)
        return box

    def _generic(self, url, cap, background):
        if background:
            if not self.gate.acquire(blocking=False):
                raise ResolveError("busy (dang co resolve khac)")
        elif not self.gate.acquire(timeout=self.gate_wait):
            raise ResolveError(f"server dang ban, cho qua {self.gate_wait:.0f}s")
        try:
            fmt = self._fmt(cap)
            with COOKIES.lease(True) as ck:
                opts = self._opts(fmt, None, False, "", ck.path)
                r = _extract_with(url, opts)
                ck.keep = bool(ck.path)
            return Streams(r[0], r[1], r[2], r[3], "", "generic")
        finally:
            self.gate.release()

    def _fmt(self, cap):
        if self.audio:
            return "bestaudio[abr<=96]/bestaudio/best"
        fmt = (f"bestvideo[height<={cap}][vcodec^=avc1]+bestaudio"
               f"/bestvideo[height<={cap}]+bestaudio"
               f"/best[height<={cap}]"
               f"/best")
        if YT_FMT_LEAN:
            fmt = (f"bestvideo[height<={cap}][vcodec^=avc1][fps<=30]+bestaudio[abr<=72]"
                   f"/bestvideo[height<={cap}][vcodec^=avc1][fps<=30]+bestaudio/") + fmt
        return fmt

    def _opts(self, fmt, client, runtimes, px, cookiefile):
        opts = {"quiet": True, "no_warnings": True, "format": fmt, "skip_download": True, "geo_bypass": True,
                "socket_timeout": 10, "noplaylist": True, "retries": 1, "extractor_retries": 1}
        yt_args = {}
        if client:
            yt_args["player_client"] = list(client)
        if YT_LIGHT:
            yt_args["skip"] = ["dash", "translated_subs"]
        if yt_args:
            opts["extractor_args"] = {"youtube": yt_args}
        _pot = os.environ.get("YT_POT_URL", "").strip()
        if _pot:
            opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {"base_url": [_pot]}
        if runtimes:
            opts["js_runtimes"] = {"deno": {}, "node": {}}
            opts["remote_components"] = ["ejs:github"] if _env_bool("YT_REMOTE_EJS", True) else []
        if px:
            opts["proxy"] = px
        if cookiefile:
            opts["cookiefile"] = cookiefile
        if YT_FORCE_IPV4:
            opts["source_address"] = "0.0.0.0"
        return opts

    def _attempts(self, background):
        attempts = list(_installed_attempts())
        pc = _env_list("YT_PLAYER_CLIENTS")
        if pc:
            attempts.insert(0, ("env", pc, True, True))
        if not COOKIES.usable():
            attempts = [a for a in attempts if not a[2] or a[1] is None]
        elif time.monotonic() < self.nocookie_skip_until or background:
            wc = [a for a in attempts if a[2]]
            if wc:
                attempts = wc
                print(f"[resolve:{self.name}] tam bo cac cach khong cookies, thu cach co cookies", flush=True)
        if self.nocookie_skip_until and time.monotonic() >= self.nocookie_skip_until:
            self.nocookie_skip_until = 0.0
            self.nocookie_fails = 0
            self.nocookie_vids.clear()
        names = [a[0] for a in attempts]
        first = _env_str("YTM_FIRST", YT_RACE_FIRST) if self.audio else YT_RACE_FIRST
        order = self.board.order(names, first=first)
        by = {a[0]: a for a in attempts}
        return [by[n] for n in order]

    def _ytdlp(self, url, vid, cap, px, background, budget=None, wait=None):
        left = self.block.left()
        if left > 0:
            raise ResolveError(f"{self.label} dang chan IP - tam nghi them {left:.0f}s de khong bi chan nang hon")
        wait = self.gate_wait if wait is None else wait
        if wait <= 0:
            if not self.gate.acquire(blocking=False):
                raise ResolveError("busy (dang co resolve khac)")
        elif not self.gate.acquire(timeout=wait):
            raise ResolveError(f"server dang ban (yt-dlp dang chay), cho qua {wait:.0f}s")
        try:
            return self._ytdlp_locked(url, vid, cap, px, background, budget)
        finally:
            self.gate.release()

    def _ytdlp_locked(self, url, vid, cap, px, background, budget):
        fmt = self._fmt(cap)
        budget = self.budget if budget is None else budget
        t_start = time.monotonic()
        errors = []
        permanent = False
        weak = 0
        attempts = self._attempts(background)
        for name, client, use_cookies, runtimes in attempts:
            if errors and time.monotonic() - t_start > budget:
                break
            if self.block.left() > 0 and errors:
                break
            GOV.pace("bg" if background else self.name)
            with COOKIES.lease(use_cookies) as ck:
                opts = self._opts(fmt, client, runtimes, px, ck.path)
                try:
                    r = _extract_with(url, opts)
                    if self.audio:
                        src = r[1] or r[0]
                        hdr = r[3] if r[1] else r[2]
                        if not src:
                            raise RuntimeError("khong lay duoc URL audio")
                        res = Streams(src, None, hdr, "", px, "ytdlp:" + name)
                    else:
                        res = Streams(r[0], r[1], r[2], r[3], px, "ytdlp:" + name)
                    ck.keep = bool(ck.path)
                except Exception as e:
                    msg = str(e)
                    kind = classify_error(msg, e)
                    print(f"[resolve:{self.name}] cach '{name}' that bai ({kind}): {msg[:140]}", flush=True)
                    errors.append(f"{name}: {msg[:110]}")
                    if any(h in msg for h in _COOKIE_BAD_HINTS):
                        COOKIES.mark_bad(msg)
                    if kind == "outdated" or "player response" in msg:
                        trigger_update_async()
                    if not background and not use_cookies and ("player response" in msg or "not a bot" in msg):
                        self.nocookie_fails = self.nocookie_vids.add(vid)
                        if self.nocookie_fails >= YT_NOCOOKIE_MAX_FAILS:
                            self.nocookie_skip_until = time.monotonic() + YT_NOCOOKIE_COOLDOWN
                    if kind == "geo":
                        print(f"[resolve:{self.name}] video bi chan theo vung -> bo cac client con lai", flush=True)
                        self.geo_mark(vid)
                        break
                    if kind == "gone":
                        permanent = True
                        break
                    if kind == "weak":
                        weak += 1
                        if weak >= 2:
                            permanent = True
                            break
                    elif kind != "age":
                        self.board.fail(name)
                    continue
            if self.board.last_ok != name:
                print(f"[resolve:{self.name}] cach '{name}' thanh cong", flush=True)
            self.board.ok(name)
            self.block.ok()
            self.blockfails.clear()
            if not use_cookies:
                self.nocookie_fails = 0
                self.nocookie_skip_until = 0.0
                self.nocookie_vids.clear()
            self.stats["ytdlp_ok"] += 1
            print(f"[resolve:{self.name}] yt-dlp xong sau {time.monotonic() - t_start:.1f}s bang '{name}'", flush=True)
            return res
        joined = " | ".join(errors)
        if not permanent and not background:
            if any(h in joined for h in _BLOCK_HINTS) and not any(h in joined for h in _AGE_HINTS):
                n_vids = self.blockfails.add(vid)
                need = YT_BLOCK_VIDEOS_HARD if any(h in joined for h in _HARD_BLOCK_HINTS) else YT_BLOCK_VIDEOS_SOFT
                if n_vids >= need:
                    wait_s = self.block.trip()
                    HEAT.bump(3.0)
                    self.visitor.clear()
                    PROXIES.penalize(px, f"{self.name} bi chan")
                    print(f"[resolve:{self.name}] {n_vids} video khac nhau that bai lien tiep -> nghi {wait_s:.0f}s "
                          f"(IP co the bi chan, lan {self.block.level})", flush=True)
                else:
                    print(f"[resolve:{self.name}] video {vid} that bai ({n_vids}/{need} video khac nhau) - coi la loi RIENG "
                          f"cua video, khong khoa dich vu", flush=True)
        raise ResolveError(joined[:700] or "khong cach nao chay", permanent=permanent)

    def _direct(self, vid, cap, px, background):
        if not self.direct_on or time.monotonic() < self.direct_off_until:
            return None
        keys = [k for k in YT_DIRECT_CLIENTS if dc_cfg(k)]
        if not keys:
            return None
        t0 = time.monotonic()
        opener = _proxy_opener(px)
        lane = "bg" if background else self.name + "_direct"
        errs = []
        bad_net = 0
        for k in self.dboard.order(keys):
            GOV.pace(lane, jitter=(1.0, 1.3))
            try:
                if self.audio:
                    src, hdr = direct_resolve_audio(self.itb, k, vid, opener, self.visitor, px)
                    res = Streams(src, None, hdr, "", px, "direct:" + k)
                else:
                    v, a, vh, ah = direct_resolve_video(self.itb, k, vid, cap, opener, self.visitor, px)
                    res = Streams(v, a, vh, ah, px, "direct:" + k)
            except DirectFail as e:
                errs.append(str(e)[:90])
                self.dboard.fail(k, hard=e.hard)
                if e.kind == "net":
                    bad_net += 1
                continue
            except ResolveError:
                raise
            except Exception as e:
                errs.append(f"{k}: {type(e).__name__}: {str(e)[:60]}")
                self.dboard.fail(k, hard=False)
                continue
            self.dboard.ok(k)
            self.direct_vids.clear()
            self.blockfails.clear()
            self.block.ok()
            self.stats["direct_ok"] += 1
            print(f"[direct:{self.name}] {vid} OK sau {time.monotonic() - t0:.1f}s (client {k})", flush=True)
            return res
        n = self.direct_vids.add(vid)
        print(f"[direct:{self.name}] {vid} that bai: {' | '.join(errs)[:300]} ({n} video khac nhau lien tiep)", flush=True)
        if n >= self.direct_max and not (bad_net and bad_net == len(errs)):
            self.direct_off_until = time.monotonic() + self.direct_cool
            print(f"[direct:{self.name}] that bai tren {n} video -> tat duong truc tiep {self.direct_cool:.0f}s (dung yt-dlp)",
                  flush=True)
        return None

    def refresh(self, url, cap, px, bad_cl="", want=("", "")):
        url = _canon_url(url)
        m = _YT_ID_RE.search(url or "")
        if m and self.direct_on:
            if bad_cl.startswith("direct:"):
                self.dboard.kill(bad_cl[7:])
            elif self.dboard.last_ok:
                self.dboard.kill(self.dboard.last_ok)
            keys = [k for k in YT_DIRECT_CLIENTS if dc_cfg(k)]
            opener = _proxy_opener(px)
            _alt = None
            for k in self.dboard.order(keys):
                GOV.pace(self.name + "_direct", jitter=(1.0, 1.2))
                try:
                    if self.audio:
                        src, hdr = direct_resolve_audio(self.itb, k, m.group(1), opener, self.visitor, px)
                        r = Streams(src, None, hdr, "", px, "direct:" + k)
                    else:
                        v, a, vh, ah = direct_resolve_video(self.itb, k, m.group(1), cap, opener, self.visitor, px, want)
                        r = Streams(v, a, vh, ah, px, "direct:" + k)
                except Exception as e:
                    self.dboard.fail(k, hard=getattr(e, "hard", False))
                    continue
                if not self.audio and ((want[0] and _url_itag(r.v) != want[0]) or (want[1] and r.a and _url_itag(r.a) != want[1])):
                    print(f"[turbo] client '{k}' khong co dung itag {want} (v={_url_itag(r.v)} a={_url_itag(r.a)}) -> thu client khac", flush=True)
                    _alt = _alt or r
                    continue
                self.dboard.ok(k)
                print(f"[turbo] lay lai link direct bang client '{k}' (client loi: '{bad_cl or '?'}')", flush=True)
                return r
            print("[turbo] direct doi client that bai (khong client nao co dung itag)", flush=True)
        if not (TURBO_REFRESH_YTDLP and m):
            raise RuntimeError("khong lay duoc link moi (yt-dlp tat cho refresh)")
        if not self.gate.acquire(timeout=20):
            raise RuntimeError("dang co resolve khac")
        try:
            _ram_guard_before_ydl()
            t0 = time.monotonic()
            r = self._ytdlp_locked(url, m.group(1), cap, px, False, max(YT_FIRST_BUDGET, 25.0))
            print(f"[turbo] lay lai link bang yt-dlp sau {time.monotonic() - t0:.1f}s", flush=True)
            return r
        finally:
            self.gate.release()


VIDEO = Resolver("video", audio=False)
MUSIC = Resolver("music", audio=True)


YT_API_KEY = _env_str("YOUTUBE_API_KEY")
YT_API_LANG = _env_str("YT_API_LANG", "vi")
YT_API_REGION = _env_str("YT_API_REGION", "VN")
YT_API_COOLDOWN = _env_int("YT_API_COOLDOWN", 3600)
SEARCH_TTL = _env_int("SEARCH_TTL", 600)
YTM_SEARCH_TTL = _env_int("YTM_SEARCH_TTL", 600)
YT_SEARCH_INNERTUBE = _env_bool("YT_SEARCH_INNERTUBE", True)
_YTM_PARAMS_SONGS = _env_str("YTM_SEARCH_PARAMS", "EgWKAQIIAWoMEA4QChADEAQQCRAF")
_WEB_VERSION_FALLBACK = _env_str("YT_WEB_VERSION", "2.20260101.00.00")
_WEB_REMIX_VERSION_FALLBACK = _env_str("YTM_WEB_VERSION", "1.20260101.01.00")


class YTApiError(Exception):
    def __init__(self, status, reason, message=""):
        super().__init__(f"HTTP {status} {reason} {message}".strip())
        self.status = status
        self.reason = reason


def _next_pacific_midnight():
    try:
        import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime.datetime.now(tz)
        nxt = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return max(60.0, (nxt - now).total_seconds())
    except Exception:
        return None


class ApiKeyState:

    def __init__(self):
        self.block_until = 0.0
        self.why = ""

    def ok(self):
        return bool(YT_API_KEY) and time.monotonic() >= self.block_until

    def note(self, reason):
        if reason == "rateLimitExceeded":
            secs = min(60, YT_API_COOLDOWN)
        elif reason in ("quotaExceeded", "dailyLimitExceeded"):
            secs = _next_pacific_midnight() or YT_API_COOLDOWN
        elif reason in ("keyInvalid", "keyExpired", "accessNotConfigured", "forbidden", "ipRefererBlocked"):
            secs = YT_API_COOLDOWN
        elif reason == "network":
            secs = min(30, YT_API_COOLDOWN)
        else:
            return
        self.block_until = time.monotonic() + secs
        self.why = reason


API_STATE = ApiKeyState()


def _api_get(endpoint, params):
    params = dict(params, key=YT_API_KEY)
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reason, msg = "", ""
        try:
            err = json.loads(e.read().decode("utf-8", "replace")).get("error", {})
            msg = err.get("message", "")[:120]
            reason = (err.get("errors") or [{}])[0].get("reason", "")
        except Exception:
            pass
        API_STATE.note(reason)
        raise YTApiError(e.code, reason, msg) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        API_STATE.note("network")
        raise YTApiError(0, "network", str(e)[:100]) from None


def _iso_duration_to_seconds(d):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", d or "")
    if not m:
        return 0
    days, hrs, mins, secs = (int(x) if x else 0 for x in m.groups())
    return days * 86400 + hrs * 3600 + mins * 60 + secs


def search_via_api(query, n, music=False):
    sp = {"part": "snippet", "type": "video", "maxResults": n, "q": query, "safeSearch": "moderate",
          "fields": "items(id/videoId,snippet(title,channelTitle,liveBroadcastContent))"}
    if music:
        sp["videoCategoryId"] = "10"
    else:
        sp["videoEmbeddable"] = "any"
    if YT_API_LANG:
        sp["relevanceLanguage"] = YT_API_LANG
    if YT_API_REGION:
        sp["regionCode"] = YT_API_REGION
    data = _api_get("search", sp)
    items = data.get("items", [])
    ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    durations = {}
    view_counts = {}
    if ids:
        try:
            vids = _api_get("videos", {"part": "contentDetails,statistics", "id": ",".join(ids),
                                       "fields": "items(id,contentDetails/duration,statistics/viewCount)"})
            for v in vids.get("items", []):
                durations[v["id"]] = _iso_duration_to_seconds(v["contentDetails"].get("duration"))
                try:
                    view_counts[v["id"]] = int((v.get("statistics") or {}).get("viewCount") or 0)
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            print(f"[search] bo qua thoi luong ({str(e)[:80]})", flush=True)
    out = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        if not vid:
            continue
        sn = it.get("snippet") or {}
        out.append({"id": vid, "title": html.unescape(sn.get("title", "")),
                    "channel": html.unescape(sn.get("channelTitle", "")),
                    "duration": ("LIVE" if sn.get("liveBroadcastContent") == "live" else format_duration(durations.get(vid))),
                    "views": view_counts.get(vid, 0)})
    return out


def _walk(o):
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _txt(x):
    if not isinstance(x, dict):
        return ""
    if "simpleText" in x:
        return str(x["simpleText"])
    return "".join(str(r.get("text", "")) for r in (x.get("runs") or []) if isinstance(r, dict))


_DUR_RE = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")


def _views_from_text(x):
    t = _txt(x)
    d = re.sub(r"\D", "", t or "")
    if not d or len(d) > 10:
        return 0
    return int(d)


def parse_video_search(data, n):
    out, seen = [], set()
    for d in _walk(data):
        vr = d.get("videoRenderer")
        if not isinstance(vr, dict):
            continue
        vid = vr.get("videoId")
        if not (isinstance(vid, str) and _YT_BARE_ID.match(vid)) or vid in seen:
            continue
        seen.add(vid)
        live = any("LIVE" in json.dumps(b.get("metadataBadgeRenderer", {}).get("style", ""))
                   for b in (vr.get("badges") or []) if isinstance(b, dict)) or \
            any(isinstance(o, dict) and o.get("thumbnailOverlayTimeStatusRenderer", {}).get("style") == "LIVE"
                for o in (vr.get("thumbnailOverlays") or []))
        dur = _txt(vr.get("lengthText"))
        out.append({"id": vid, "title": _txt(vr.get("title")),
                    "channel": _txt(vr.get("ownerText")) or _txt(vr.get("longBylineText")),
                    "duration": "LIVE" if (live and not dur) else dur,
                    "views": _views_from_text(vr.get("viewCountText"))})
        if len(out) >= n:
            break
    return out


_MUSIC_TYPE_WORDS = {"song", "video", "single", "ep", "album", "playlist", "artist", "bai hat", "bài hát"}


def parse_music_search(data, n):
    out, seen = [], set()
    for d in _walk(data):
        it = d.get("musicResponsiveListItemRenderer")
        if not isinstance(it, dict):
            continue
        vid = (it.get("playlistItemData") or {}).get("videoId")
        if not vid:
            for x in _walk(it):
                w = x.get("watchEndpoint")
                if isinstance(w, dict) and w.get("videoId"):
                    vid = w["videoId"]
                    break
        if not (isinstance(vid, str) and _YT_BARE_ID.match(vid)) or vid in seen:
            continue
        cols = it.get("flexColumns") or []

        def col(i):
            try:
                return (cols[i].get("musicResponsiveListItemFlexColumnRenderer") or {}).get("text") or {}
            except (IndexError, AttributeError):
                return {}
        title = _txt(col(0))
        parts = [str(r.get("text", "")).strip() for r in (col(1).get("runs") or []) if isinstance(r, dict)]
        parts = [p for p in parts if p and p not in ("•", "·", "-")]
        dur = next((p for p in reversed(parts) if _DUR_RE.match(p)), "")
        names = [p for p in parts if p.lower() not in _MUSIC_TYPE_WORDS and not _DUR_RE.match(p)]
        if not title:
            continue
        seen.add(vid)
        out.append({"id": vid, "title": title, "channel": names[0] if names else "", "duration": dur})
        if len(out) >= n:
            break
    return out


def _web_context(music):
    key = "web_music" if music else "web"
    cfg = dc_cfg(key) or dc_cfg("web_remix" if music else "web")
    cl = dict(cfg["client"]) if cfg else {}
    if not cl.get("clientName"):
        cl["clientName"] = "WEB_REMIX" if music else "WEB"
    if not cl.get("clientVersion"):
        cl["clientVersion"] = _WEB_REMIX_VERSION_FALLBACK if music else _WEB_VERSION_FALLBACK
    cl["hl"] = YT_API_LANG or "en"
    cl["gl"] = YT_API_REGION or "US"
    return cl, (cfg["cid"] if cfg and cfg.get("cid") else (67 if music else 1))


def innertube_search(query, n, music, visitor, px=""):
    base = YTM_INNERTUBE_BASE if music else INNERTUBE_BASE
    cl, cid = _web_context(music)
    vd = visitor.get()
    if vd:
        cl["visitorData"] = vd
    body = {"context": {"client": cl}, "query": query}
    if music:
        if _YTM_PARAMS_SONGS:
            body["params"] = _YTM_PARAMS_SONGS
    else:
        body["params"] = "EgIQAQ%3D%3D"
    hd = {"Content-Type": "application/json", "User-Agent": _GEN_UA, "Origin": base,
          "Accept-Language": "vi,en;q=0.8", "X-YouTube-Client-Name": str(cid),
          "X-YouTube-Client-Version": str(cl.get("clientVersion", ""))}
    if vd:
        hd["X-Goog-Visitor-Id"] = vd
    req = urllib.request.Request(base + "/youtubei/v1/search?prettyPrint=false", data=json.dumps(body).encode(),
                                 method="POST", headers=hd)
    try:
        with _proxy_opener(px).open(req, timeout=10) as r:
            data = json.loads(r.read(4_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            HEAT.bump(1.0)
        raise RuntimeError(f"innertube search HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"innertube search loi: {str(e)[:80]}") from None
    visitor.offer((data.get("responseContext") or {}).get("visitorData") if isinstance(data, dict) else None)
    res = parse_music_search(data, n) if music else parse_video_search(data, n)
    if not res and music and body.get("params"):
        body.pop("params")
        req = urllib.request.Request(base + "/youtubei/v1/search?prettyPrint=false", data=json.dumps(body).encode(),
                                     method="POST", headers=hd)
        with _proxy_opener(px).open(req, timeout=10) as r:
            data = json.loads(r.read(4_000_000).decode("utf-8", "replace"))
        res = parse_music_search(data, n)
    return res


class SearchEngine:
    def __init__(self, name, resolver, music):
        self.name = name
        self.res = resolver
        self.music = music
        self.ttl = YTM_SEARCH_TTL if music else SEARCH_TTL
        self.cache = {}
        self.lock = threading.Lock()
        self.it_break = Breaker(60, 900)
        self.visitor = Visitor()

    def clear(self):
        with self.lock:
            self.cache.clear()

    def _get(self, ck):
        with self.lock:
            hit = self.cache.get(ck)
        return hit[1] if hit and time.monotonic() - hit[0] < self.ttl else None

    def _put(self, ck, res):
        now = time.monotonic()
        with self.lock:
            for k in [k for k, v in self.cache.items() if now - v[0] >= self.ttl]:
                self.cache.pop(k, None)
            if len(self.cache) >= 100:
                for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:30]:
                    self.cache.pop(k, None)
            self.cache[ck] = (now, res)

    def _ytdlp(self, query, n):
        r = self.res
        if r.block.left() > 0:
            raise RuntimeError("dang bi chan IP, bo qua tim kiem yt-dlp")
        if not r.gate.acquire(timeout=20):
            raise RuntimeError("yt-dlp dang ban (resolve khac)")
        try:
            GOV.pace("search")
            with COOKIES.lease(True) as ck:
                opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
                        "socket_timeout": 10}
                px = PROXIES.current()
                if px:
                    opts["proxy"] = px
                if ck.path:
                    opts["cookiefile"] = ck.path
                info = _ydl_info(f"ytsearch{n}:{query}", opts, slim=True)
                ck.keep = bool(ck.path)
        finally:
            r.gate.release()
        out = []
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id"):
                continue
            out.append({"id": e["id"], "title": e.get("title") or "",
                        "channel": e.get("channel") or e.get("uploader") or "",
                        "duration": "LIVE" if e.get("live_status") == "is_live" else format_duration(e.get("duration")),
                        "views": int(e.get("view_count") or 0)})
        return out

    def search(self, query, n):
        ck = (query.lower(), n)
        hit = self._get(ck)
        if hit is not None:
            print(f"[{self.name}] tim kiem CACHE HIT {query!r}", flush=True)
            return hit
        order = ["innertube", "api", "ytdlp"] if self.music else ["api", "innertube", "ytdlp"]
        res = []
        for src in order:
            try:
                if src == "api" and API_STATE.ok():
                    res = search_via_api(query, n, self.music)
                elif src == "innertube" and YT_SEARCH_INNERTUBE and self.it_break.left() <= 0:
                    GOV.pace("search")
                    res = innertube_search(query, n, self.music, self.visitor, PROXIES.current())
                    if not res:
                        raise RuntimeError("innertube khong co ket qua (doi cau truc?)")
                    self.it_break.ok()
                elif src == "ytdlp":
                    res = self._ytdlp(query, n)
                else:
                    continue
                if res:
                    print(f"[{self.name}] tim kiem {src} OK: {len(res)} ket qua", flush=True)
                    break
            except Exception as e:
                if src == "innertube":
                    w = self.it_break.trip()
                    print(f"[{self.name}] tim kiem innertube loi ({str(e)[:100]}) nghi {w:.0f}s", flush=True)
                else:
                    print(f"[{self.name}] tim kiem {src} loi ({str(e)[:100]})", flush=True)
                res = []
        if res:
            self._put(ck, res)
        return res


VIDEO_SEARCH = SearchEngine("search", VIDEO, music=False)
MUSIC_SEARCH = SearchEngine("ytm", MUSIC, music=True)


MJPEG_Q = os.environ.get("MJPEG_Q", "6")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "1")
STREAM_FAST_DECODE = _env_bool("STREAM_FAST_DECODE", True)
STREAM_SRC_AUTO = _env_bool("STREAM_SRC_AUTO", True)
STREAM_NICE = _env_int("STREAM_NICE", 5)
STREAM_READRATE = _env_float("STREAM_READRATE", 1.4)
STREAM_READ_BURST = _env_float("STREAM_READ_BURST", 4)
STREAM_MAX_LEAD = _env_float("STREAM_MAX_LEAD", 12)
STREAM_SKIP_NOREF = _env_bool("STREAM_SKIP_NOREF", True)
STREAM_FPS_MAX = _env_int("STREAM_FPS_MAX", 12)
AUDIO_RATE = os.environ.get("AUDIO_RATE", "16000")
STREAM_FIT = _env_bool("STREAM_FIT", True)
KILL_ON_LEAVE = _env_bool("KILL_ON_LEAVE", True)
BR_KILL_ON_LEAVE = _env_bool("BR_KILL_ON_LEAVE", False)
SESS_HOLD_SEC = _env_float("SESS_HOLD_SEC", 600)
SESS_CONVERT_SEC = _env_float("SESS_CONVERT_SEC", 180)
SESS_BUF_BYTES = int(_env_float("SESS_BUF_MB", 16) * 1048576)
SESS_BACK_BYTES = int(_env_float("SESS_BACK_KB", 6144) * 1024)
FIRST_BYTE_WAIT = _env_float("FIRST_BYTE_WAIT", 25)
RESUME_REALIGN = _env_bool("RESUME_REALIGN", True)
print(f"[config] role={ROLE} AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS}", flush=True)
_ff_caps = None


def _ffmpeg_caps():
    global _ff_caps
    if _ff_caps is None:
        txt = ""
        try:
            txt = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True, text=True, timeout=20).stdout
        except Exception:
            pass
        _ff_caps = ("-readrate " in txt or "-readrate\t" in txt, "readrate_initial_burst" in txt)
        print(f"[config] ffmpeg readrate={_ff_caps[0]} initial_burst={_ff_caps[1]} (STREAM_READRATE={STREAM_READRATE})", flush=True)
    return _ff_caps


_sessions = {}
_sess_lock = threading.Lock()
_cur_sess = None
_active_streams = 0
_active_lock = threading.Lock()
_cur_proc = None
_cur_lock = threading.Lock()
_last_height_cap = ""
_VIDEO_MARK = Mark("stream")


def _streams_add(d):
    global _active_streams
    with _active_lock:
        _active_streams = max(0, _active_streams + d)


def _mark_stream(on=True):
    _VIDEO_MARK.touch(on)


def _stream_elsewhere(max_age=60):
    return _VIDEO_MARK.elsewhere(max_age)


def _kill_quiet(p):
    try:
        p.kill()
    except Exception:
        pass


def _kill_stream(why=""):
    global _cur_proc
    if _cur_sess is not None:
        _cur_sess.kill(why or "kill")
        return
    with _cur_lock:
        p, _cur_proc = _cur_proc, None
    if p is not None and p.poll() is None:
        print(f"[stream] roi video -> tat ffmpeg cu (boi {why or '?'})", flush=True)
        try:
            p.kill()
        except Exception:
            pass


def video_active():
    return any((not s.dead) and s.detached_at is None and not s.paused for s in list(_sessions.values()))


def video_release_idle(why):
    n = 0
    for s_ in list(_sessions.values()):
        if not s_.dead and s_.detached_at is not None:
            s_.kill(why)
            n += 1
    return n


def video_kill_all(why):
    for s_ in list(_sessions.values()):
        s_.kill(why)


def _sess_drop(sid, why):
    s_ = _sessions.get(sid)
    if s_ is not None:
        s_.kill(why)


def _sess_drop_all(why):
    for s_ in list(_sessions.values()):
        s_.kill(why)

TURBO = os.environ.get("TURBO", "1") == "1"
TURBO_WORKERS = max(1, _env_int("TURBO_WORKERS", 3))
TURBO_CHUNK = max(64, _env_int("TURBO_CHUNK_KB", 512)) * 1024
TURBO_HEAD = 128 * 1024
TURBO_MAX_FAIL = max(2, _env_int("TURBO_MAX_FAIL", 12))
TURBO_HEAD_N = 8
TURBO_REFRESH_MAX = max(3, _env_int("TURBO_REFRESH_MAX", 10))
TURBO_AHEAD = max(4, _env_int("TURBO_AHEAD_MB", 4) * 1024 * 1024 // TURBO_CHUNK + TURBO_HEAD_N)
_LOCAL_IPS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
_fetchers = {}

def _hdr_dict(s):
    d = {}
    for line in (s or "").split("\r\n"):
        if ": " in line:
            k, v = line.split(": ", 1)
            if k.lower() not in ("range", "host", "connection", "accept-encoding"):
                d[k] = v
    d["Accept-Encoding"] = "identity"
    return d

def _turbo_size(i):
    if i < 4:
        return TURBO_HEAD
    if i < TURBO_HEAD_N:
        return 2 * TURBO_HEAD
    return TURBO_CHUNK

class _ChunkError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status

class _Fetcher:
    def __init__(self, url, hdr, proxy="", refresh=None):
        self.hdr = hdr
        self.proxy = proxy
        self.refresh = refresh
        self._refreshing, self._last_refresh, self.refreshes = False, 0.0, 0
        self.size, self.url = self._probe(url)
        self.starts = [0]
        while self.starts[-1] < self.size:
            self.starts.append(self.starts[-1] + _turbo_size(len(self.starts) - 1))
        self.starts[-1] = self.size
        self.n = len(self.starts) - 1
        self.buf, self.inflight = {}, set()
        self.cond = threading.Condition()
        self.cursor, self.closed, self.err = 0, False, None
        self.fails = {}
        self.t0, self.dl_bytes, self.wait_s = time.monotonic(), 0, 0.0
        for _ in range(TURBO_WORKERS):
            threading.Thread(target=self._work, daemon=True).start()

    def _probe(self, url, hdr=None):
        h = dict(self.hdr if hdr is None else hdr)
        h["Range"] = "bytes=0-0"
        px = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(px))
        with opener.open(urllib.request.Request(url, headers=h), timeout=10) as r:
            m = re.match(r"bytes 0-0/(\d+)$", r.headers.get("Content-Range", ""))
            if r.status != 206 or not m:
                raise RuntimeError("nguon khong ho tro Range")
            r.read()
            final = r.geturl()
        if not _url_is_public(final):
            raise RuntimeError("nguon tro toi dia chi noi bo")
        return int(m.group(1)), final

    def _try_refresh(self):
        if not self.refresh:
            return False
        with self.cond:
            if self._refreshing or self.closed or self.refreshes >= TURBO_REFRESH_MAX or time.monotonic() - self._last_refresh < 4:
                return False
            self._refreshing = True
        ok = False
        try:
            res = self.refresh()
            if res and res[0]:
                nu, nh = res
                sz, final = self._probe(nu, nh)
                if sz == self.size:
                    self.url, self.hdr = final, nh
                    ok = True
                    print(f"[turbo] 403 giua chung -> da doi sang link moi ({urllib.parse.urlsplit(final).hostname}), tiep tuc tu byte hien tai", flush=True)
                else:
                    print(f"[turbo] link moi khac dinh dang ({sz} != {self.size}) -> khong doi", flush=True)
        except Exception as e:
            print(f"[turbo] lay link moi that bai: {str(e)[:100]}", flush=True)
        finally:
            with self.cond:
                self._refreshing = False
                self._last_refresh = time.monotonic()
                self.refreshes += 1
                if ok:
                    self.fails.clear()
                    self.refreshes = max(0, self.refreshes - 2)
                self.cond.notify_all()
        return ok

    def _conn(self):
        u = urllib.parse.urlsplit(self.url)
        https = u.scheme == "https"
        port = u.port or (443 if https else 80)
        if self.proxy.startswith("http"):
            pp = urllib.parse.urlsplit(self.proxy)
            if https:
                c = http.client.HTTPSConnection(pp.hostname, pp.port or 80, timeout=15)
                th = {}
                if pp.username:
                    cred = f"{urllib.parse.unquote(pp.username)}:{urllib.parse.unquote(pp.password or '')}"
                    th["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
                c.set_tunnel(u.hostname, port, headers=th)
                return c
        return (http.client.HTTPSConnection if https else http.client.HTTPConnection)(u.hostname, port, timeout=15)

    def _request(self, c, a, b):
        r = None
        for _hop in range(5):
            if c is None:
                c = self._conn()
            u = urllib.parse.urlsplit(self.url)
            h = dict(self.hdr)
            h["Range"] = f"bytes={a}-{b}"
            try:
                c.request("GET", u.path + ("?" + u.query if u.query else ""), headers=h)
                r = c.getresponse()
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
                raise
            if r.status in (301, 302, 303, 307, 308):
                loc = r.getheader("Location", "")
                try:
                    r.read(512)
                except Exception:
                    pass
                try:
                    c.close()
                except Exception:
                    pass
                c = None
                if not loc:
                    raise _ChunkError(r.status, f"HTTP {r.status} khong co Location")
                new = urllib.parse.urljoin(self.url, loc)
                if not new.startswith(("http://", "https://")) or not _url_is_public(new):
                    raise _ChunkError(r.status, f"HTTP {r.status} redirect khong hop le")
                if new != self.url:
                    print(f"[turbo] theo redirect {r.status} -> {urllib.parse.urlsplit(new).hostname}", flush=True)
                    self.url = new
                continue
            return c, r
        raise _ChunkError(r.status if r is not None else 0, "qua nhieu redirect")

    def _fetch(self, c, idx):
        a = self.starts[idx]
        b = self.starts[idx + 1] - 1
        c, r = self._request(c, a, b)
        if r.status != 206:

            try:
                r.read(512)
            except Exception:
                pass
            raise _ChunkError(r.status, f"HTTP {r.status}")
        data = r.read()
        if len(data) != b - a + 1:
            raise _ChunkError(r.status, f"thieu du lieu {len(data)}/{b - a + 1}B")
        return data, c

    def _next(self):
        for i in range(self.cursor, min(self.n, self.cursor + TURBO_AHEAD)):
            if i not in self.buf and i not in self.inflight:
                return i
        return None

    def _work(self):
        c, last = None, None
        while True:
            with self.cond:
                if self.closed:
                    break
                idx = self._next()
                if idx is None:
                    self.cond.wait(0.5)
                    continue
                self.inflight.add(idx)
            data = None
            last = None
            for attempt in range(3):
                try:
                    data, c = self._fetch(c, idx)
                    break
                except Exception as e:
                    last = e
                    try:
                        c and c.close()
                    except Exception:
                        pass
                    c = None
                    if getattr(e, "status", 0) == 403 and self.refresh and self.refreshes < TURBO_REFRESH_MAX:
                        break
                    _slow = getattr(e, "status", 0) in (403, 429, 503)
                    time.sleep((1.0 if _slow else 0.2) * (attempt + 1))
            with self.cond:
                self.inflight.discard(idx)
                if data is not None:
                    if not self.closed:
                        self.buf[idx] = data
                        self.dl_bytes += len(data)
                        self.fails.pop(idx, None)
                else:
                    n = self.fails.get(idx, (0, None))[0] + 1
                    self.fails[idx] = (n, last)
                self.cond.notify_all()
            if data is None and getattr(last, "status", 0) == 403:
                self._try_refresh()
            if data is None:

                time.sleep(min(3.0, 0.4 * self.fails.get(idx, (1, None))[0]))
        try:
            c and c.close()
        except Exception:
            pass

    def get(self, pos, n):
        idx = bisect.bisect_right(self.starts, pos) - 1
        deadline = time.monotonic() + 30
        with self.cond:
            if idx != self.cursor:
                self.cursor = idx
                for k in [k for k in self.buf if k < idx - 2 or k >= idx + TURBO_AHEAD]:
                    del self.buf[k]
                self.cond.notify_all()
            _tw = time.monotonic()
            while idx not in self.buf:
                if self.closed:
                    raise RuntimeError("fetcher da dong")
                nf, le = self.fails.get(idx, (0, None))
                if nf >= TURBO_MAX_FAIL and not self._refreshing and not (self.refresh and self.refreshes < TURBO_REFRESH_MAX and time.monotonic() - self._last_refresh < 20):
                    raise RuntimeError(f"tai loi chunk {idx} {nf} lan: {le}")
                if time.monotonic() > deadline:
                    raise TimeoutError("het gio cho chunk")
                self.cond.wait(0.5)
            self.wait_s += time.monotonic() - _tw
            d = self.buf[idx]
        off = pos - self.starts[idx]
        return d[off: off + n]

    def close(self):
        with self.cond:
            self.closed = True
            self.buf.clear()
            self.cond.notify_all()

_src_app = Flask("relay_src")
_src_port = 0
_src_port_lock = threading.Lock()

def _src_server_port():
    global _src_port
    with _src_port_lock:
        if _src_port:
            return _src_port
        from werkzeug.serving import make_server
        srv = make_server("127.0.0.1", 0, _src_app, threaded=True)
        _src_port = srv.server_port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[turbo] may chu nguon cuc bo 127.0.0.1:{_src_port} (pid {os.getpid()})", flush=True)
        return _src_port

def _turbo(url, hdr, proxy, refresh=None):
    if not (TURBO and url and url.startswith(("http://", "https://"))) or re.search(r"\.m3u8|\.mpd|/manifest|/playlist", url, re.I):
        return None
    try:
        port = _src_server_port()
        f = _Fetcher(url, _hdr_dict(hdr), proxy, refresh)
    except Exception as e:
        print(f"[turbo] bo qua ({str(e)[:100]}) -> doc truc tiep", flush=True)
        return None
    sid = secrets.token_hex(16)
    _fetchers[sid] = f
    return f"http://127.0.0.1:{port}/_src/{sid}", sid

def _drop_fetchers(sids):
    for sid in sids or []:
        f = _fetchers.pop(sid, None)
        if f:
            try:
                el = max(0.1, time.monotonic() - f.t0)
                if f.wait_s > 3.0:
                    print(f"[dl-slow] nguon tai cham: ffmpeg phai cho {f.wait_s:.1f}s/{el:.0f}s | "
                          f"tai {f.dl_bytes // 1024}KB = {f.dl_bytes / el / 1024:.0f}KB/s | workers={TURBO_WORKERS}", flush=True)
            except Exception:
                pass
            f.close()

@app.route("/_src/<sid>", methods=["GET", "HEAD"])
def turbo_src(sid):
    if request.remote_addr not in _LOCAL_IPS:
        return "", 404
    f = _fetchers.get(sid)
    if not f:
        return "", 404
    size = f.size
    m = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("Range", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            a, b = int(m.group(1)), (int(m.group(2)) if m.group(2) else size - 1)
        else:
            a, b = max(0, size - int(m.group(2))), size - 1
        b = min(b, size - 1)
        if a > b or a >= size:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    else:
        a, b, status = 0, size - 1, 200
    hd = {"Accept-Ranges": "bytes", "Content-Length": str(b - a + 1), "Content-Type": "application/octet-stream"}
    if status == 206:
        hd["Content-Range"] = f"bytes {a}-{b}/{size}"
    if request.method == "HEAD":
        return Response(status=status, headers=hd)

    def gen():
        pos = a
        try:
            while pos <= b:
                piece = f.get(pos, min(b - pos + 1, 256 * 1024))
                pos += len(piece)
                yield piece
        except Exception as e:
            print(f"[turbo] loi nguon tai byte {pos}/{size}: {str(e)[:120]} -> chuyen sang tai 1 ket noi", flush=True)
        if pos <= b and not f.closed:
            yield from _turbo_fallback(f, pos, b)
    return Response(gen(), status=status, headers=hd)

def _turbo_fallback(f, pos, end):
    c, tries = None, 0
    try:
        while pos <= end and not f.closed and tries < 6:
            start = pos
            try:
                c, r = f._request(c, pos, end)
                if r.status != 206:
                    raise _ChunkError(r.status, f"HTTP {r.status}")
                while pos <= end:
                    piece = r.read(65536)
                    if not piece:
                        break
                    piece = piece[:end - pos + 1]
                    pos += len(piece)
                    yield piece
                if pos <= end:
                    raise RuntimeError("nguon cat ngang")
            except Exception as e:
                if getattr(e, "status", 0) == 403:
                    f._try_refresh()
                try:
                    c and c.close()
                except Exception:
                    pass
                c = None
                tries = 0 if pos > start else tries + 1
                print(f"[turbo] loi tai du phong tai byte {pos}: {str(e)[:100]} (lan {tries})", flush=True)
                time.sleep(0.5 * (tries + 1))
        if pos <= end:
            print(f"[turbo] loi: tai du phong that bai, cat luong tai byte {pos}/{end + 1}", flush=True)
    finally:
        try:
            c and c.close()
        except Exception:
            pass

_src_app.add_url_rule("/_src/<sid>", "turbo_src", turbo_src, methods=["GET", "HEAD"])

_OOM_CHILD = os.environ.get("OOM_CHILD_FIRST", "1") == "1"
_OOM_PREFIX = ["sh", "-c", 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$@"', "sh"]

def _oom_prefix():
    if _OOM_CHILD and os.path.exists("/proc/self/oom_score_adj") and shutil.which("sh"):
        return list(_OOM_PREFIX)
    return []

def _nice_prefix():
    pre = _oom_prefix()
    if STREAM_NICE > 0 and shutil.which("nice"):
        return pre + ["nice", "-n", str(STREAM_NICE)]
    return pre

def _sock_diag(sock):
    if sock is None:
        return "sock=n/a"
    out = []
    try:
        d = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        out.append("peek=FIN(ben kia da dong)" if d == b"" else "peek=con_du_lieu")
    except BlockingIOError:
        out.append("peek=ket_noi_con_mo")
    except ConnectionResetError:
        out.append("peek=RST(bi reset)")
    except OSError as e:
        out.append(f"peek=loi:{type(e).__name__}")
    try:
        ti = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
        st, _ca, retr = struct.unpack_from("3B", ti, 0)
        unacked, _sacked, lost, retrans = struct.unpack_from("4I", ti, 24)
        out.append(f"tcp_state={st} retrans={retr} unacked={unacked} lost={lost}")
    except Exception as e:
        out.append(f"tcpinfo=loi:{type(e).__name__}")
    return " ".join(out)

class _AviCounter:
    def __init__(self):
        self.buf = b""
        self.hdr = b""
        self.state = 0
        self.skip = 0
        self.frames = 0
        self.audio = 0
        self.ok = True

    def feed(self, data):
        if not self.ok or not data:
            return
        try:
            if self.state == 0:
                self.buf += data
                i = self.buf.find(b"movi")
                if i < 0:
                    if len(self.buf) > 300000:
                        self.ok = False
                    return
                data, self.buf, self.state = self.buf[i + 4:], b"", 1
            pos, n = 0, len(data)
            while pos < n:
                if self.skip:
                    t = min(self.skip, n - pos)
                    self.skip -= t
                    pos += t
                    continue
                take = data[pos:pos + 8 - len(self.hdr)]
                self.hdr += take
                pos += len(take)
                if len(self.hdr) < 8:
                    break
                fcc, size = self.hdr[:4], int.from_bytes(self.hdr[4:8], "little")
                self.hdr = b""
                if fcc[2:] == b"dc" and fcc[:2].isdigit():
                    self.frames += 1
                elif fcc[2:] == b"wb" and fcc[:2].isdigit():
                    self.audio += size
                elif not (fcc[:2] == b"ix" or fcc == b"JUNK" or fcc[2:] == b"tx"):
                    self.ok = False
                    return
                self.skip = size + (size & 1)
        except Exception:
            self.ok = False

def _terminate(proc):
    try:
        proc.stdout.close()
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

class _AviMap:

    def __init__(self):
        self.ok = True
        self.state = 0
        self.buf = b""
        self.hdr = b""
        self.hdr_start = 0
        self.skip = 0
        self.pos = 0
        self.starts = array.array("Q")
        self.sizes = array.array("I")
        self.fcc = bytearray()

    def feed(self, data):
        if not data:
            return
        base_abs = self.pos
        self.pos += len(data)
        if not self.ok:
            return
        try:
            if self.state == 0:
                self.buf += data
                i = self.buf.find(b"movi")
                if i < 0:
                    if len(self.buf) > 300000:
                        self.ok = False
                    return
                base_abs = (self.pos - len(self.buf)) + i + 4
                data, self.buf, self.state = self.buf[i + 4:], b"", 1
            p, n = 0, len(data)
            while p < n:
                if self.skip:
                    t = min(self.skip, n - p)
                    self.skip -= t
                    p += t
                    continue
                if not self.hdr:
                    self.hdr_start = base_abs + p
                take = data[p:p + 8 - len(self.hdr)]
                self.hdr += take
                p += len(take)
                if len(self.hdr) < 8:
                    break
                fcc, size = self.hdr[:4], int.from_bytes(self.hdr[4:8], "little")
                self.hdr = b""
                if not ((fcc[:2].isdigit() and fcc[2:] in (b"dc", b"wb")) or fcc[:2] == b"ix" or fcc == b"JUNK"
                        or fcc[2:] == b"tx"):
                    self.ok = False
                    return
                self.starts.append(self.hdr_start)
                self.sizes.append(size)
                self.fcc += fcc
                self.skip = size + (size & 1)
        except Exception:
            self.ok = False

    def realign(self, internal, base):
        if not self.ok or len(self.starts) < 2:
            return None
        i = bisect.bisect_right(self.starts, internal) - 1
        if i < 0 or i + 1 >= len(self.starts):
            return None
        st, en = self.starts[i], self.starts[i + 1]
        if not (st <= internal < en):
            return None
        size = self.sizes[i]
        fcc = bytes(self.fcc[4 * i:4 * i + 4])
        full = bytearray(fcc + size.to_bytes(4, "little") + bytes(size + (size & 1)))
        if len(full) != en - st:
            return None
        if fcc[2:] == b"dc" and size >= 2:
            full[8 + size - 2:8 + size] = b"\xff\xd9"
        j = bisect.bisect_left(self.starts, max(base, en))
        if j >= len(self.starts):
            return None

class _Sess:

    def __init__(self, proc, sids, first, target_bps, burst, fps):
        self.sid = secrets.token_hex(16)
        self.proc, self.sids = proc, sids
        self.target_bps, self.burst, self.fps = target_bps, burst, fps
        self.buf = bytearray(first)
        self.skew = 0
        self.amap = _AviMap()
        self.amap.feed(bytes(first))
        self.base = 0
        self.end = len(first)
        self.sent = 0
        self.eof = False
        self.dead = False
        self.gen = 0
        self.detached_at = None
        self.paused = False
        self.counted = True
        self.cv = threading.Condition()
        global _cur_sess
        with _sess_lock:
            old, _cur_sess = _cur_sess, self
            _sessions[self.sid] = self
        if old is not None and old is not self:
            old.kill("co video moi")
        _mark_stream()
        self._rt = threading.Thread(target=self._reader, daemon=True)
        self._rt.start()
        _sess_start_reaper()

    def _reader(self):
        p = self.proc
        try:
            while True:
                with self.cv:
                    while not self.dead and len(self.buf) >= SESS_BUF_BYTES:
                        self.cv.wait(1.0)
                    if self.dead:
                        break
                data = p.stdout.read1(65536)
                if not data:
                    break
                    if not data:
                        continue
                with self.cv:
                    self.buf += data
                    self.end += len(data)
                    self.amap.feed(data)
                    self.cv.notify_all()
        except Exception:
            pass
        finally:
            with self.cv:
                self.eof = True
                self.cv.notify_all()

    def pause(self):
        if self.paused or self.dead or self.eof or self.proc.poll() is not None:
            return
        try:
            os.kill(self.proc.pid, signal.SIGSTOP)
        except Exception:
            return
        self.paused = True
        if self.counted:
            self.counted = False
            _streams_add(-1)
            if _active_streams == 0:
                _mark_stream(False)
        print(f"[sess] {self.sid[:6]} nghi sau {SESS_CONVERT_SEC:.0f}s khong co ESP (ffmpeg tam dung, cho lenh noi lai)", flush=True)

    def resume(self):
        if not self.paused:
            return
        self.paused = False
        try:
            os.kill(self.proc.pid, signal.SIGCONT)
        except Exception:
            pass
        if not self.counted:
            self.counted = True
            _streams_add(1)
        print(f"[sess] {self.sid[:6]} ESP quay lai -> ffmpeg chay tiep", flush=True)

    def kill(self, why=""):
        with self.cv:
            if self.dead:
                return
            self.dead = True
            self.cv.notify_all()
        with _sess_lock:
            _sessions.pop(self.sid, None)
            global _cur_sess
            if _cur_sess is self:
                _cur_sess = None
        _kill_quiet(self.proc)
        self._rt.join(timeout=3)
        _terminate(self.proc)
        try:
            _drop_fetchers(self.sids)
        except Exception:
            pass
        with self.cv:
            self.buf = bytearray()
        if self.counted:
            self.counted = False
            _streams_add(-1)
        if _active_streams == 0:
            _mark_stream(False)
        print(f"[sess] {self.sid[:6]} giai phong ({why or '?'}) | RAM {_mem_str()}", flush=True)
        _free_ram()
        _ram_report("sau giai phong phien")

    def _trim(self):
        keep = max(self.base, self.sent - min(SESS_BACK_BYTES, SESS_BUF_BYTES // 2))
        if keep - self.base >= min(1 << 20, max(4096, SESS_BUF_BYTES // 8)):
            del self.buf[:keep - self.base]
            self.base = keep
            self.cv.notify_all()


_reaper_on = False

def _sess_start_reaper():
    global _reaper_on
    if _reaper_on:
        return
    _reaper_on = True
    threading.Thread(target=_sess_reaper, daemon=True).start()

def _sess_reaper():
    while True:
        time.sleep(5)
        try:
            now = time.monotonic()
            for s_ in list(_sessions.values()):
                if s_.dead:
                    continue
                if s_.detached_at is None:
                    _mark_stream()
                    continue
                if now - s_.detached_at > SESS_CONVERT_SEC:
                    s_.pause()
                hold = 60.0 if (s_.eof and s_.sent >= s_.end) else SESS_HOLD_SEC
                if _mem_frac() > 0.70:
                    hold = min(hold, 90.0)
                if now - s_.detached_at > hold:
                    s_.kill(f"khong con ESP sau {hold:.0f}s")
        except Exception:
            traceback.print_exc()

def _sess_response(sess, off, prefix=b""):
    gen_id = None
    with sess.cv:
        sess.gen += 1
        gen_id = sess.gen
        sess.detached_at = None
    sess.resume()
    csock = request.environ.get("gunicorn.socket")

    def generate():
        t0 = time.monotonic()
        pos = off
        sent_here = 0
        avi = _AviCounter() if off == 0 else None
        reason = "ffmpeg het du lieu (EOF)"
        last_mark = 0.0
        try:
            if prefix:
                yield prefix
            while True:
                with sess.cv:
                    while pos >= sess.end and not sess.eof and not sess.dead and sess.gen == gen_id:
                        sess.cv.wait(1.0)
                    if sess.gen != gen_id:
                        reason = "ESP noi lai tren ket noi moi"
                        return
                    if sess.dead:
                        reason = "phien da bi giai phong"
                        return
                    if pos >= sess.end:
                        break
                    n = min(65536, sess.end - pos)
                    lo = pos - sess.base
                    if lo < 0:
                        reason = "offset da bi cat"
                        return
                    chunk = bytes(sess.buf[lo:lo + n])
                if avi is not None:
                    avi.feed(chunk)
                    if STREAM_MAX_LEAD > 0 and avi.ok and avi.frames:
                        over = avi.frames / sess.fps - (time.monotonic() - t0) - STREAM_MAX_LEAD
                        if over > 0:
                            time.sleep(min(over, 2.0))
                sent_here += len(chunk)
                expected = max(0, sent_here - sess.burst) / sess.target_bps
                actual = time.monotonic() - t0
                if expected > actual:
                    time.sleep(expected - actual)
                yield chunk
                pos += len(chunk)
                with sess.cv:
                    if sess.gen == gen_id:
                        sess.sent = max(sess.sent, pos)
                        sess._trim()
                t = time.monotonic()
                if t - last_mark >= 5:
                    last_mark = t
                    _mark_stream()
        except GeneratorExit:
            reason = "THIET BI DONG KET NOI (client thoat/ngat WiFi/timeout phia ESP)"
            raise
        except BaseException as e:
            reason = f"LOI {type(e).__name__}: {str(e)[:120]}"
            raise
        finally:
            print(f"[stream] {sess.sid[:6]} ket noi dong sau {time.monotonic() - t0:.0f}s, gui {sent_here // 1024}KB "
                  f"(off={off} -> {pos}) | ly do: {reason} | RAM {_mem_str()} | {_sock_diag(csock)}", flush=True)
            with sess.cv:
                if sess.gen == gen_id and not sess.dead:
                    sess.detached_at = time.monotonic()
            if not reason.startswith(("ESP noi lai", "phien da")):
                print(f"[sess] {sess.sid[:6]} giu toi da {SESS_HOLD_SEC:.0f}s cho ESP noi lai", flush=True)

    return Response(generate(), mimetype="video/avi",
                    headers={"Cache-Control": "no-store", "X-Sid": sess.sid,
                             "X-Relay-Purpose": "transcode-only-for-LilyGo-T-Display-S3"})

RESUME_REALIGN = os.environ.get("RESUME_REALIGN", "1") == "1"

def _stream_resume(sid, off):
    sess = _sessions.get(sid)
    if sess is None or sess.dead:
        return jsonify({"error": "session_gone", "detail": "phien het han hoac da bi thay the"}), 410
    prefix = b""
    with sess.cv:
        internal = off + sess.skew
        if internal < sess.base:
            fix = sess.amap.realign(internal, sess.base) if RESUME_REALIGN else None
            if fix is None:
                print(f"[sess] {sid[:6]} 410 offset_trimmed: ESP xin off={off} (noi bo {internal}) nhung bo dem chi con tu "
                      f"{sess.base} (da gui {sess.sent}, cua so lui {SESS_BACK_BYTES // 1024}KB)", flush=True)
                return jsonify({"error": "offset_trimmed", "detail": "offset nam ngoai bo dem"}), 410
            prefix, newpos = fix
            gap = newpos - (internal + len(prefix))
            sess.skew += gap
            print(f"[sess] {sid[:6]} ESP xin off={off} da roi khoi bo dem ({sess.base}) -> bu {len(prefix)}B va nhay qua {gap}B "
                  f"toi {newpos} (khong tra 410, ESP khong bi da)", flush=True)
            internal = newpos
        if internal > sess.end:
            return jsonify({"error": "offset_ahead"}), 416
        if internal == sess.end and sess.eof and not prefix:
            sess.detached_at = time.monotonic()
            sess.sent = max(sess.sent, internal)
            return Response("", 204)
    print(f"[sess] {sid[:6]} ESP noi lai tai off={off} (noi bo {internal}, server da co toi {sess.end})", flush=True)
    return _sess_response(sess, internal, prefix)


def _inputs(res, url, cap, is_yt):
    px = res.px if is_yt else ""
    _want = (_url_itag(res.v), _url_itag(res.a) if res.a else "") if is_yt else ("", "")
    _sh = {"t": 0.0, "r": None, "cl": res.cl}
    _shl = threading.Lock()

    def _shared():
        with _shl:
            if _sh["r"] is not None and time.monotonic() - _sh["t"] < 20:
                return _sh["r"]
            r = VIDEO.refresh(url, cap, px, _sh["cl"], _want)
            _sh["r"], _sh["t"], _sh["cl"] = r, time.monotonic(), (r.cl or _sh["cl"])
            return r

    def _mk(i):
        if not is_yt:
            return None

        def _r():
            r = _shared()
            u, hh = (r.v, r.vh) if i == 0 else (r.a, r.ah)
            return (u, _hdr_dict(hh)) if u else None
        return _r
    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        fv = ex.submit(_turbo, res.v, res.vh, px, _mk(0))
        fa = ex.submit(_turbo, res.a, res.ah, px, _mk(1)) if res.a else None
        tv, ta = fv.result(), (fa.result() if fa else None)
    sids = [t[1] for t in (tv, ta) if t]
    vi = (tv[0], "", True) if tv else (res.v, res.vh, False)
    ai = ((ta[0], "", True) if ta else (res.a, res.ah, False)) if res.a else None
    return vi, ai, sids


def _ffmpeg_cmd(v_url, a_url, v_hdr, a_hdr, px, w, h, fps, q, ar, v_local=False, a_local=False, ss=0):
    def inp(url, hdr, vid=False, local=False):
        a = ["-protocol_whitelist", "http,https,tcp,tls,crypto,data,httpproxy",
             "-reconnect", "1", "-reconnect_at_eof", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "5", "-rw_timeout", "20000000"]
        if px and not local:
            a += ["-http_proxy", px]
        if hdr:
            a += ["-headers", hdr]
        a += ["-probesize", "500000", "-analyzeduration", "500000", "-fflags", "+nobuffer",
              "-threads", str(FFMPEG_THREADS)]
        if vid and STREAM_FAST_DECODE:
            a += ["-skip_loop_filter", "all"]
        if vid and STREAM_SKIP_NOREF and fps <= 12:
            a += ["-skip_frame", "noref"]
        if STREAM_READRATE > 0:
            rr, burst = _ffmpeg_caps()
            if rr:
                a += ["-readrate", str(STREAM_READRATE)]
                if burst and STREAM_READ_BURST > 0:
                    a += ["-readrate_initial_burst", str(STREAM_READ_BURST)]
            else:
                a += ["-re"]
        if ss > 0:
            a += ["-ss", str(int(ss))]
        a += ["-i", url]
        return a

    if STREAM_FIT:
        vf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    else:
        vf = f"fps={fps},scale={w}:{h}:flags=fast_bilinear"
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + inp(v_url, v_hdr, vid=True, local=v_local)
    if a_url:
        cmd += inp(a_url, a_hdr, local=a_local) + ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-threads", str(FFMPEG_THREADS), "-filter_threads", str(FFMPEG_THREADS),
            "-vf", vf, "-pix_fmt", "yuvj420p", "-c:v", "mjpeg", "-q:v", str(q),
            "-c:a", "pcm_s16le", "-ar", str(ar), "-ac", "1",
            "-flush_packets", "1", "-f", "avi", "pipe:1"]
    return cmd


def _stream_impl(video_url):
    global _last_height_cap, _cur_proc
    t_req = time.monotonic()
    if not (video_url or "").strip():
        return Response(status=400)
    video_url = _normalize_input_url(video_url)
    if not video_url:
        return jsonify({"error": "bad_url", "detail": "chi nhan http/https hoac ID YouTube"}), 400

    w = _int_arg("w", 320, 16, 640)
    h = _int_arg("h", 170, 16, 480)
    w -= w % 2
    h -= h % 2
    _cap = _int_arg("height_cap", 360, 1, 2160)
    if STREAM_SRC_AUTO:
        _cap = min(_cap, max(h, int(w * 9 / 16)))
    height_cap = str(max(_cap, _env_int("MIN_HEIGHT", 240)))
    _last_height_cap = height_cap
    _start_ss = _int_arg("ss", 0, 0, 86400)
    fps = _int_arg("fps", 15, 1, 30)
    _fmin = _env_int("STREAM_FPS_MIN", 10)
    if _fmin:
        fps = min(30, max(fps, _fmin))
    fps = max(1, min(fps, STREAM_FPS_MAX))
    q = min(31, max(_int_arg("q", _env_int("MJPEG_Q", 6), 2, 31), _env_int("STREAM_Q_MIN", 2)))
    ar = _int_arg("ar", _env_int("AUDIO_RATE", 16000), 4000, 22050)
    bps_cap = _env_int("STREAM_MAX_BPS_CAP", 3000000)
    target_bps = _int_arg("bps", _env_int("STREAM_MAX_BPS", 1000000), 25000, max(25000, bps_cap))
    target_bps = max(target_bps, _env_int("STREAM_MIN_BPS", 160000))
    burst_bytes = _env_int("STREAM_BURST_BYTES", 512 * 1024)
    is_yt = _is_youtube(video_url)

    ARB.begin_play("video")
    if _mem_frac() > 0.70:
        print(f"[stream] RAM {_mem_str()} cao -> don cache truoc khi resolve", flush=True)
        ARB.trim_caches()

    if KILL_ON_LEAVE:
        _kill_stream(_who())

    can_retry = VIDEO.cached(video_url, height_cap)
    force = False
    proc = None
    first = b""
    sids = []
    for attempt in (1, 2):
        try:
            res = VIDEO.resolve(video_url, height_cap, force=force)
        except Exception as e:
            print(f"[stream] resolve error: {e}", flush=True)
            if not isinstance(e, ResolveError):
                traceback.print_exc()
            msg = str(e)
            if any(k in msg for k in _OUTDATED_HINTS):
                trigger_update_async()
            return jsonify({"error": "resolve_failed", "detail": msg[:300],
                            "permanent": bool(getattr(e, "permanent", False))}), 502

        _free_ram()
        _ram_report("sau resolve")
        if not all(_url_is_public(u) for u in (res.v, res.a) if u):
            print("[stream] URL luong tro vao host noi bo -> chan", flush=True)
            VIDEO.drop(video_url, height_cap)
            return jsonify({"error": "blocked_host", "detail": "nguon tro toi dia chi noi bo"}), 403

        vi, ai, sids = _inputs(res, video_url, height_cap, is_yt)
        cmd = _ffmpeg_cmd(vi[0], ai[0] if ai else None, vi[1], ai[1] if ai else "", res.px if is_yt else "",
                          w, h, fps, q, ar, vi[2], ai[2] if ai else False, ss=_start_ss)
        with _cur_lock:
            _old = _cur_proc
            if _old is not None and _old.poll() is None:
                print(f"[stream] co yeu cau moi -> tat ffmpeg cu de nhuong CPU (boi {_who()})", flush=True)
                try:
                    _old.kill()
                except Exception:
                    pass
            try:
                proc = subprocess.Popen(_nice_prefix() + cmd, stdout=subprocess.PIPE, bufsize=1 << 20)
            except OSError as e:
                print(f"[stream] khong chay duoc ffmpeg: {e}", flush=True)
                _drop_fetchers(sids)
                return jsonify({"error": "ffmpeg_missing", "detail": str(e)[:120]}), 500
            _cur_proc = proc
        _streams_add(1)
        try:
            fcntl.fcntl(proc.stdout.fileno(), 1031, 1 << 20)
        except Exception:
            pass
        wd = threading.Timer(FIRST_BYTE_WAIT, _kill_quiet, (proc,))
        wd.daemon = True
        wd.start()
        try:
            first = proc.stdout.read1(65536)
        except Exception:
            first = b""
        finally:
            wd.cancel()
        if first:
            print(f"[stream] byte dau tien sau {time.monotonic() - t_req:.1f}s ke tu luc nhan yeu cau", flush=True)
            break
        with _cur_lock:
            superseded = _cur_proc is not proc
        _terminate(proc)
        _streams_add(-1)
        _drop_fetchers(sids)
        proc = None
        if superseded:
            print("[stream] bi yeu cau moi thay the trong luc cho byte dau tien -> dung", flush=True)
            return jsonify({"error": "superseded", "detail": "co yeu cau moi hon"}), 409
        VIDEO.drop(video_url, height_cap)
        print("[stream] ffmpeg khong ra du lieu (URL het han / nguon cat ket noi?)", flush=True)
        if attempt == 1 and can_retry:
            force = True
            continue
        break
    if proc is None:
        return jsonify({"error": "ffmpeg_no_output", "detail": "ffmpeg khong nhan duoc luong tu nguon"}), 502

    sess = _Sess(proc, sids, first, target_bps, burst_bytes, fps)
    with _stat_lock:
        _stat["streams"] += 1
        _stat["last"] = time.time()
        _n = _stat["streams"]
    print(f"[proof] giai ma cho LilyGo #{_n} sid={sess.sid} | {_who()} | {w}x{h}@{fps}fps | khong luu o dia", flush=True)
    return _sess_response(sess, 0)


WARM_TOP_N = _env_int("WARM_TOP_N", 3 if YT_DIRECT else 1)
YT_WARM_MAX_COST = _env_float("YT_WARM_MAX_COST", 25)


def _warm_height():
    try:
        floor = int(os.environ.get("MIN_HEIGHT", "240"))
        return str(max(int(_last_height_cap or os.environ.get("WARM_HEIGHT_CAP", "240")), floor))
    except ValueError:
        return "240"


def _warm_worker(video_ids, height_cap):
    if VIDEO.ema > YT_WARM_MAX_COST:
        print(f"[warm] resolve trung binh {VIDEO.ema:.0f}s > {YT_WARM_MAX_COST:.0f}s, bo qua lam nong", flush=True)
        return
    if _mem_frac() > 0.75 or HEAT.level() > 2.0:
        print("[warm] RAM cao / IP dang nong, bo qua lam nong", flush=True)
        return
    for vid in video_ids:
        if ARB.any_active() or VIDEO.block.left() > 0 or not VIDEO.gate._value:
            print("[warm] may dang ban / dang phat, bo qua lam nong", flush=True)
            return
        try:
            VIDEO.resolve(f"https://www.youtube.com/watch?v={vid}", height_cap, background=True)
        except Exception as e:
            print(f"[warm] loi {vid}: {str(e)[:120]}", flush=True)


def warm_results(results):
    ids = [r["id"] for r in results if r.get("id")][:WARM_TOP_N]
    if ids:
        threading.Thread(target=_warm_worker, args=(ids, _warm_height()), daemon=True).start()


@app.route("/stream")
def stream():
    sid = (request.args.get("sid") or "").strip()
    if sid:
        try:
            off = max(0, int(request.args.get("off", "0")))
        except ValueError:
            return Response("off sai", 400)
        return _stream_resume(sid, off)
    return _stream_impl(request.args.get("url", ""))


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()[:200]
    print(f"[search] yeu cau q={query!r}", flush=True)
    try:
        n = max(1, min(25, int(request.args.get("n", 5))))
    except ValueError:
        n = 5
    if not query:
        return jsonify([])
    res = VIDEO_SEARCH.search(query, n)
    if res:
        warm_results(res)
    return jsonify(res)


# ---------------- YouTube: lien quan / phu de ----------------
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _yt_id():
    vid = (request.args.get("id") or "").strip()
    return vid if _YT_ID_RE.match(vid) else ""


@app.route("/yt/related")
def yt_related():
    vid = _yt_id()
    n = _int_arg("n", 8, 1, 20)
    title = (request.args.get("t") or "").strip()
    chan = (request.args.get("c") or "").strip()
    q = re.sub(r"[\[\](){}|#]+", " ", title)
    q = " ".join(q.split()[:8]).strip()
    if not q:
        q = chan
    if not q:
        return jsonify([])
    try:
        res = VIDEO_SEARCH.search(q, n + 4) or []
    except Exception as e:
        print(f"[yt/related] loi: {str(e)[:100]}", flush=True)
        return jsonify([])
    out = [r for r in res if r.get("id") and r.get("id") != vid][:n]
    if out:
        warm_results(out)
    return jsonify(out)


# ---------------- YouTube: xu huong + thumbnail 16:9 cho man Home ----------------
_yt_trend_cache = {}
_yt_trend_lock = threading.Lock()
_YT_TREND_TTL = 900


_yt_trend_next = {}
_yt_next_busy = set()
_YT_TREND_QS = {
    "vi": ["video xu huong hom nay", "nhac moi hot", "video hay nhat tuan nay", "phim ngan hot", "nhac tre moi nhat",
           "review cong nghe moi", "hai huoc moi nhat", "du lich vlog moi"],
    "en": ["trending videos today", "popular now", "viral videos this week", "new music videos", "tech reviews new",
           "funny moments new", "travel vlog new", "short films popular"],
}


def _yt_trend_build(key, count=24):
    qs = list(_YT_TREND_QS.get(key) or _YT_TREND_QS["vi"])
    random.shuffle(qs)
    out, seen = [], set()
    for q in qs[:4]:
        try:
            res = VIDEO_SEARCH.search(q, 12) or []
        except Exception as e:
            print(f"[yt/trending] loi: {str(e)[:100]}", flush=True)
            res = []
        for r in res:
            vid = r.get("id")
            if vid and vid not in seen and r.get("duration") != "LIVE":
                seen.add(vid)
                out.append(r)
        if len(out) >= count:
            break
    random.shuffle(out)
    return out[:count]


@app.route("/yt/trending")
def yt_trending():
    n = _int_arg("n", 6, 1, 24)
    vi = (request.args.get("lang") or "vi").lower().startswith("vi")
    key = "vi" if vi else "en"
    fresh = request.args.get("fresh") == "1"
    now = time.time()
    if fresh:
        with _yt_trend_lock:
            nx = _yt_trend_next.pop(key, None)
        if nx:
            with _yt_trend_lock:
                _yt_trend_cache[key] = (now, nx)
            threading.Thread(target=_yt_thumb_prefetch, args=([r["id"] for r in nx[:12]], 150, 70), daemon=True).start()
            return jsonify(nx[:n])
    with _yt_trend_lock:
        hit = _yt_trend_cache.get(key)
    if not fresh and hit and now - hit[0] < _YT_TREND_TTL and len(hit[1]) >= n:
        return jsonify(hit[1][:n])
    out = _yt_trend_build(key, 24)
    if out:
        with _yt_trend_lock:
            _yt_trend_cache[key] = (now, out)
        threading.Thread(target=_yt_thumb_prefetch, args=([r["id"] for r in out[:12]], 150, 70), daemon=True).start()
        return jsonify(out[:n])
    if hit:
        return jsonify(hit[1][:n])
    return jsonify([])


def _yt_next_job(key):
    try:
        out = _yt_trend_build(key, 24)
        if out:
            with _yt_trend_lock:
                _yt_trend_next[key] = out
            _yt_thumb_prefetch([r["id"] for r in out[:12]], 150, 70)
    finally:
        with _yt_trend_lock:
            _yt_next_busy.discard(key)


@app.route("/yt/closed")
def yt_closed():
    vi = (request.args.get("lang") or "vi").lower().startswith("vi")
    key = "vi" if vi else "en"
    with _yt_trend_lock:
        if key in _yt_next_busy:
            return jsonify({"ok": True, "busy": True})
        _yt_next_busy.add(key)
    threading.Thread(target=_yt_next_job, args=(key,), daemon=True).start()
    return jsonify({"ok": True})


_yt_thumb_cache = {}
_yt_thumb_lock = threading.Lock()
_yt_thumb_gen_lock = threading.Lock()


def _yt_thumb_get(vid, w, h):
    key = (vid, w, h)
    with _yt_thumb_lock:
        data = _yt_thumb_cache.get(key)
    if data:
        return data
    with _yt_thumb_gen_lock:
        with _yt_thumb_lock:
            data = _yt_thumb_cache.get(key)
        if data:
            return data
        src = None
        for name in ("mqdefault", "hqdefault", "default"):
            try:
                rq = urllib.request.Request(f"https://i.ytimg.com/vi/{vid}/{name}.jpg", headers={"User-Agent": UA})
                with urllib.request.urlopen(rq, timeout=8) as r:
                    src = r.read(600_000)
                if src:
                    break
            except Exception as e:
                print(f"[yt/thumb] {name} loi: {type(e).__name__}", flush=True)
        if not src:
            return None
        data = b""
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(src))
            im.draft("RGB", (w * 2, h * 2))
            im = im.convert("RGB")
            s = max(w / im.width, h / im.height)
            nw, nh = max(w, int(round(im.width * s))), max(h, int(round(im.height * s)))
            im = im.resize((nw, nh), Image.BILINEAR)
            l, t = (nw - w) // 2, (nh - h) // 2
            im = im.crop((l, t, l + w, t + h))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=68, optimize=True)
            data = buf.getvalue()
        except Exception:
            data = b""
        if not data:
            try:
                vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
                pr = subprocess.run(_nice_prefix() + ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-vf", vf,
                                                      "-frames:v", "1", "-q:v", "6", "-f", "mjpeg", "pipe:1"],
                                    input=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
                data = pr.stdout
            except Exception as e:
                print(f"[yt/thumb] loi cat anh: {type(e).__name__}", flush=True)
        if not data:
            return None
        with _yt_thumb_lock:
            if len(_yt_thumb_cache) > 300:
                for k in list(_yt_thumb_cache)[:120]:
                    _yt_thumb_cache.pop(k, None)
            _yt_thumb_cache[key] = data
        return data


def _yt_thumb_prefetch(ids, w, h):
    for v in ids:
        try:
            _yt_thumb_get(v, w, h)
        except Exception:
            pass


@app.route("/yt/thumb")
def yt_thumb():
    vid = _yt_id()
    if not vid:
        return Response("id khong hop le", 400)
    w = _int_arg("w", 150, 32, 320)
    h = _int_arg("h", 84, 18, 180)
    data = _yt_thumb_get(vid, w, h)
    if not data:
        return Response("khong lay duoc anh", 502)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


_yt_subs_cache = {}
_yt_subs_lock = threading.Lock()


def _yt_pick_sub(info, lang):
    order = [x for x in (lang, "vi", "en") if x]
    for table in ("subtitles", "automatic_captions"):
        tab = info.get(table) or {}
        for lg in order:
            for key in tab:
                if key == lg or key.startswith(lg + "-"):
                    for f in tab[key]:
                        if f.get("ext") == "json3" and f.get("url"):
                            return f["url"]
    return ""


@app.route("/yt/subs")
def yt_subs():
    """Phu de dang text gon: moi dong 'startMs<TAB>endMs<TAB>noi dung'."""
    vid = _yt_id()
    lang = (request.args.get("lang") or "vi").strip()[:8]
    if not vid:
        return Response("", 200, mimetype="text/plain")
    ck = (vid, lang)
    now = time.monotonic()
    with _yt_subs_lock:
        hit = _yt_subs_cache.get(ck)
        if hit and now - hit[0] < 1800:
            return Response(hit[1], 200, mimetype="text/plain; charset=utf-8")
    text = ""
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 10}
        px = PROXIES.current()
        if px:
            opts["proxy"] = px
        info = _ydl_info("https://www.youtube.com/watch?v=" + vid, opts) or {}
        u = _yt_pick_sub(info, lang)
        if u:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read(3 * 1024 * 1024).decode("utf-8", "replace"))
            lines = []
            for ev in data.get("events") or []:
                segs = ev.get("segs") or []
                t = "".join(sg.get("utf8", "") for sg in segs).replace("\n", " ").replace("\t", " ").strip()
                if not t:
                    continue
                s0 = int(ev.get("tStartMs", 0))
                e0 = s0 + int(ev.get("dDurationMs", 2000))
                lines.append(f"{s0}\t{e0}\t{t[:90]}")
                if len(lines) >= 700:
                    break
            text = "\n".join(lines)
    except Exception as e:
        print(f"[yt/subs] loi: {str(e)[:120]}", flush=True)
    with _yt_subs_lock:
        if len(_yt_subs_cache) > 20:
            _yt_subs_cache.clear()
        _yt_subs_cache[ck] = (now, text)
    return Response(text, 200, mimetype="text/plain; charset=utf-8")


YTM_BITRATE = os.environ.get("YTM_BITRATE", "64k")
YTM_RATE = _env_int("YTM_RATE", 22050)
YTM_MAX_STREAMS = max(1, _env_int("YTM_MAX_STREAMS", 3))
YTM_IDLE_SEC = _env_float("YTM_IDLE_SEC", 45)
YTM_COVER_PX_DEFAULT = _env_int("YTM_COVER_PX", 96)
YTM_COVER_PREFETCH_PX = _env_int("YTM_COVER_PREFETCH_PX", 64)
_MUSIC_MARK = Mark("ytm")


class _MusicProc:
    __slots__ = ("proc", "t0", "last_io", "url")

    def __init__(self, proc, url):
        self.proc, self.url = proc, url
        self.t0 = self.last_io = time.monotonic()


_ytm_slots = threading.BoundedSemaphore(YTM_MAX_STREAMS)
_ytm_procs = {}
_ytm_procs_lock = threading.Lock()
_ytm_titles = {}
_ytm_cover_cache = {}
_ytm_cover_lock = threading.Lock()
_ytm_cover_gen_lock = threading.Lock()


def music_active(max_idle=None):
    lim = YTM_IDLE_SEC if max_idle is None else max_idle
    now = time.monotonic()
    with _ytm_procs_lock:
        return any(now - mp.last_io < lim and mp.proc.poll() is None for mp in _ytm_procs.values())


def music_busy(max_age=60):
    return bool(_ytm_procs) or _MUSIC_MARK.elsewhere(max_age)


def music_release_idle(why):
    now = time.monotonic()
    with _ytm_procs_lock:
        victims = [mp for mp in _ytm_procs.values() if now - mp.last_io >= YTM_IDLE_SEC]
    for mp in victims:
        _kill_quiet(mp.proc)
    if victims:
        print(f"[ytm] nhuong {len(victims)} luong nhac dang ranh ({why})", flush=True)
    return len(victims)


def music_kill_all(why=""):
    with _ytm_procs_lock:
        ps = list(_ytm_procs.values())
    for mp in ps:
        _kill_quiet(mp.proc)


def music_trim():
    MUSIC_SEARCH.clear()
    with _ytm_cover_lock:
        _ytm_cover_cache.clear()


def _ytm_title(url, vid):
    if vid in _ytm_titles:
        return _ytm_titles[vid]
    t = ""
    try:
        q = urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(f"https://www.youtube.com/oembed?url={q}&format=json", timeout=5) as r:
            t = str(json.loads(r.read().decode("utf-8", "replace")).get("title", "")).strip()
    except Exception:
        t = ""
    t = t or "YouTube Music"
    if len(_ytm_titles) > 300:
        _ytm_titles.clear()
    _ytm_titles[vid] = t
    return t


def _ytm_clean_hdr(h):
    return "".join(l + "\r\n" for l in (h or "").split("\r\n")
                   if l and not l.lower().startswith(("range:", "host:", "connection:", "accept-encoding:")))


def _ytm_cover_get(vid, px):
    key = (vid, px)
    with _ytm_cover_lock:
        data = _ytm_cover_cache.get(key)
    if data:
        return data
    with _ytm_cover_gen_lock:
        with _ytm_cover_lock:
            data = _ytm_cover_cache.get(key)
        if data:
            return data
        src = None
        for name in ("mqdefault", "hqdefault", "default"):
            try:
                rq = urllib.request.Request(f"https://i.ytimg.com/vi/{vid}/{name}.jpg", headers={"User-Agent": UA})
                with urllib.request.urlopen(rq, timeout=8) as r:
                    src = r.read(600_000)
                if src:
                    break
            except Exception as e:
                print(f"[ytm] anh bia {name} loi: {type(e).__name__}", flush=True)
        if not src:
            print(f"[ytm] khong tai duoc anh bia {vid}", flush=True)
            return None
        data = b""
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(src))
            im.draft("RGB", (px * 2, px * 2))
            im = im.convert("RGB")
            s = max(px / im.width, px / im.height)
            nw, nh = max(px, int(round(im.width * s))), max(px, int(round(im.height * s)))
            im = im.resize((nw, nh), Image.BILINEAR)
            l, t = (nw - px) // 2, (nh - px) // 2
            im = im.crop((l, t, l + px, t + px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=70)
            data = buf.getvalue()
        except Exception:
            data = b""
        if not data:
            try:
                vf = f"scale={px}:{px}:force_original_aspect_ratio=increase,crop={px}:{px}"
                pr = subprocess.run(_nice_prefix() + ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-vf", vf,
                                                      "-frames:v", "1", "-q:v", "5", "-f", "mjpeg", "pipe:1"],
                                    input=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
                data = pr.stdout
            except Exception as e:
                print(f"[ytm] loi cat anh bia: {type(e).__name__}", flush=True)
        if not data:
            return None
        with _ytm_cover_lock:
            if len(_ytm_cover_cache) > 150:
                for k in list(_ytm_cover_cache)[:60]:
                    _ytm_cover_cache.pop(k, None)
            _ytm_cover_cache[key] = data
        return data


def _ytm_prefetch_list(ids, px):
    for v in ids[:12]:
        try:
            _ytm_cover_get(v, px)
        except Exception:
            pass


@app.route("/ytm_search")
def ytm_search():
    query = request.args.get("q", "").strip()[:200]
    print(f"[ytm] tim kiem yeu cau q={query!r}", flush=True)
    try:
        n = max(1, min(25, int(request.args.get("n", 8))))
    except ValueError:
        n = 8
    if not query:
        return jsonify([])
    res = [dict(r) for r in MUSIC_SEARCH.search(query, n)]
    base = _base_url()
    for r in res:
        r["thumb"] = f"{base}/ytm_cover?id={r['id']}{_tk_q()}"
    if res:
        threading.Thread(target=_ytm_prefetch_list,
                         args=([r["id"] for r in res if _YTM_ID_ONLY.match(str(r.get("id", "")))], YTM_COVER_PREFETCH_PX),
                         daemon=True).start()
    return jsonify(res)


@app.route("/ytm_cover")
def ytm_cover():
    vid = request.args.get("id", "").strip()
    if not _YTM_ID_ONLY.match(vid):
        return Response("id khong hop le", 400)
    try:
        px = max(32, min(240, int(request.args.get("px", YTM_COVER_PX_DEFAULT))))
    except ValueError:
        px = YTM_COVER_PX_DEFAULT
    data = _ytm_cover_get(vid, px)
    if not data:
        return Response("khong lay duoc anh", 502)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/ytm")
def ytm_info():
    url = _normalize_input_url(request.args.get("url", ""))
    if not url:
        return jsonify(success=False, message="URL khong hop le"), 400
    m = _YT_ID_RE.search(url)
    vid = m.group(1) if m else ""
    try:
        MUSIC.resolve(url)
    except ResolveError as e:
        return jsonify(success=False, message=str(e)[:120]), 502
    except Exception as e:
        return jsonify(success=False, message=str(e)[:120]), 502
    if vid and _YTM_ID_ONLY.match(vid):
        threading.Thread(target=_ytm_cover_get, args=(vid, YTM_COVER_PREFETCH_PX), daemon=True).start()
    base = _base_url()
    audio_url = f"{base}/ytm_audio?url={urllib.parse.quote(url, safe='')}{_tk_q()}"
    return jsonify(success=True, title=_ytm_title(url, vid or url), audio_url=audio_url,
                   cover_url=(f"{base}/ytm_cover?id={vid}{_tk_q()}" if vid else ""))


@app.route("/ytm_audio")
def ytm_audio():
    url = _normalize_input_url(request.args.get("url", ""))
    if not url:
        return Response("URL khong hop le", 400)
    ARB.begin_play("music")
    try:
        res = MUSIC.resolve(url)
    except ResolveError as e:
        return Response(str(e)[:120], 502)
    except Exception as e:
        return Response(str(e)[:120], 502)
    src, hdr, px = res.v, res.vh, res.px
    if not src or not _url_is_public(src):
        return Response("nguon audio khong hop le", 502)
    if not _ytm_slots.acquire(blocking=False):
        with _ytm_procs_lock:
            old = min(_ytm_procs.values(), key=lambda mp: mp.last_io, default=None)
        if old is not None:
            _kill_quiet(old.proc)
        if not _ytm_slots.acquire(timeout=4):
            return Response("server dang phat nhieu bai, thu lai sau", 503)
    cmd = _nice_prefix() + ["ffmpeg", "-nostdin", "-v", "error",
                            "-protocol_whitelist", "http,https,tcp,tls,crypto,data,httpproxy",
                            "-reconnect", "1", "-reconnect_streamed", "1",
                            "-reconnect_delay_max", "5", "-rw_timeout", "20000000"]
    if px and _is_youtube(url) and res.cl.startswith(("direct", "ytdlp")):
        cmd += ["-http_proxy", px]
    hdr = _ytm_clean_hdr(hdr)
    if hdr:
        cmd += ["-headers", hdr]
    _ss = _int_arg("ss", 0, 0, 36000)
    if _ss > 0:
        cmd += ["-ss", str(_ss)]
    cmd += ["-threads", str(FFMPEG_THREADS), "-i", src, "-vn", "-map", "0:a:0",
            "-ac", "1", "-ar", str(YTM_RATE), "-c:a", "libmp3lame", "-b:a", YTM_BITRATE,
            "-id3v2_version", "0", "-write_xing", "0", "-flush_packets", "1", "-f", "mp3", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    except Exception as e:
        _ytm_slots.release()
        print(f"[ytm] loi khoi dong ffmpeg: {type(e).__name__}: {e}", flush=True)
        return Response("khong chay duoc ffmpeg", 500)
    mp = _MusicProc(proc, url)
    with _ytm_procs_lock:
        _ytm_procs[id(mp)] = mp
    _MUSIC_MARK.touch(True)
    t0 = time.monotonic()
    print(f"[ytm] phat {url} ({YTM_BITRATE} {YTM_RATE}Hz) nguon={res.cl}", flush=True)

    def gen():
        sent = 0
        last_touch = time.monotonic()
        try:
            while True:
                b = proc.stdout.read(8192)
                if not b:
                    break
                sent += len(b)
                _t = time.monotonic()
                mp.last_io = _t
                if _t - last_touch > 15:
                    last_touch = _t
                    _MUSIC_MARK.touch(True)
                yield b
                mp.last_io = time.monotonic()
        finally:
            with _ytm_procs_lock:
                _ytm_procs.pop(id(mp), None)
                left = bool(_ytm_procs)
            if not left:
                _MUSIC_MARK.touch(False)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
            _ytm_slots.release()
            print(f"[ytm] ket thuc sau {time.monotonic() - t0:.0f}s, gui {sent // 1024}KB", flush=True)

    return Response(gen(), mimetype="audio/mpeg", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

import io
import bisect
import itertools
import zlib
import http.cookiejar
import concurrent.futures
from html.parser import HTMLParser
_IMG_PIXEL_CAP = int(os.environ.get("BR_MAX_IMG_PIXELS", "4000000"))
_IMG_JPEG_PIXEL_CAP = int(os.environ.get("BR_MAX_JPEG_PIXELS", "40000000"))
try:
    from PIL import Image, ImageDraw, ImageFont
    Image.MAX_IMAGE_PIXELS = max(_IMG_PIXEL_CAP, _IMG_JPEG_PIXEL_CAP) * 2
    _PIL_OK = True
except Exception as _pil_e:
    _PIL_OK = False
    print(f"[br] KHONG NAP DUOC Pillow ({_pil_e}) - /br/* se tra 503", flush=True)

OUT_W = 320
OUT_H = 170
BR_TOKEN = os.environ.get("BR_TOKEN", "")
BR_SS = max(1, min(3, int(os.environ.get("BR_SS", "2"))))
BR_JPEG_Q = int(os.environ.get("BR_JPEG_Q", "65"))
MAX_JPEG = int(os.environ.get("BR_MAX_JPEG", "56000"))
BR_JPEG_Q_HI = int(os.environ.get("BR_JPEG_Q_HI", "82"))
BR_JPEG_Q_SCROLL = int(os.environ.get("BR_JPEG_Q_SCROLL", "74"))
BR_JPEG_444 = _env_bool("BR_JPEG_444", True)


def _enc_jpeg(im, q0):
    """JPEG baseline 4:4:4 (chu nao hon, het vo khoi mau), ha chat luong dan cho toi khi <= MAX_JPEG."""
    kw = {"subsampling": 0} if BR_JPEG_444 else {}
    q = max(30, min(95, q0))
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, **kw)
        if buf.tell() <= MAX_JPEG or q <= 30:
            return buf.getvalue()
        q -= 8
BR_IMAGES = os.environ.get("BR_IMAGES", "1") == "1"
BR_MAX_IMG = int(os.environ.get("BR_MAX_IMG", "8"))
BR_TIMEOUT = int(os.environ.get("BR_FETCH_TIMEOUT", "12"))
BR_FONT_PX = int(os.environ.get("BR_FONT_PX", "13"))
BR_SEARCH_URL = os.environ.get("BR_SEARCH_URL", "https://html.duckduckgo.com/html/?q=")
BR_IMG_WAIT = float(os.environ.get("BR_IMG_WAIT", "1"))
BR_MAX_HTML = int(os.environ.get("BR_MAX_HTML", "700000"))
BR_MAX_BLOCKS = int(os.environ.get("BR_MAX_BLOCKS", "1500"))
BR_BUSY_SEC = float(os.environ.get("BR_BUSY_SEC", "20"))
_IMG_MAX = max(256, OUT_W * BR_SS)
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Mobile Safari/537.36")

_br_last_activity = 0.0
_br_last_url = ""

def _br_busy():
    return time.time() - _br_last_activity < BR_BUSY_SEC


_FONT_REG = [os.environ.get("BR_FONT", ""),
             "/system/fonts/Roboto-Regular.ttf", "/system/fonts/NotoSans-Regular.ttf",
             "/system/fonts/DroidSans.ttf", "/system/fonts/OPPOSans-R.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             os.path.expanduser("~/font.ttf")]
_font_cache = {}
_font_paths = None

def _find_fonts():
    global _font_paths
    if _font_paths is None:
        reg = next((p for p in _FONT_REG if p and os.path.exists(p)), "")
        bold = ""
        if reg:
            for a, b in (("Regular", "Bold"), ("-R.", "-B."), ("Sans.", "Sans-Bold."), ("Sans-Regular", "Sans-Bold")):
                c = reg.replace(a, b)
                if c != reg and os.path.exists(c):
                    bold = c
                    break
        _font_paths = (reg, bold or reg)
        print(f"[br] font: {_font_paths[0] or 'KHONG TIM THAY (dat BR_FONT=duong_dan.ttf)'}", flush=True)
    return _font_paths

def _font(px, bold=False):
    key = (int(px), bool(bold))
    f = _font_cache.get(key)
    if f is None:
        reg, bd = _find_fonts()
        path = bd if bold else reg
        try:
            f = ImageFont.truetype(path, int(px)) if path else ImageFont.load_default(int(px))
        except Exception:
            f = ImageFont.load_default()
        _font_cache[key] = f
    return f

_cj = http.cookiejar.CookieJar()
class _PubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_is_public(newurl):
            raise urllib.error.URLError("blocked host (redirect toi dia chi noi bo)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj), _PubRedirect())

try:
    import requests as _rq
    from requests.adapters import HTTPAdapter
except Exception:
    _rq = None
_sess = None
if _rq is not None:
    _sess = _rq.Session()
    _ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
    _sess.mount("http://", _ad)
    _sess.mount("https://", _ad)
    _sess.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,image/*;q=0.8,*/*;q=0.5",
                          "Accept-Language": "vi,en;q=0.8", "Accept-Encoding": "gzip, deflate"})

def _iter_body(r, deadline):
    raw = r.raw
    if hasattr(raw, "read1"):
        while time.monotonic() < deadline:
            try:
                c = raw.read1(65536, decode_content=True)
            except TypeError:
                c = raw.read1(65536)
            if not c:
                return
            yield c
        return
    for c in r.iter_content(8192):
        yield c
        if time.monotonic() > deadline:
            return

def _http(url, data=None, limit=1500000, timeout=None):
    to = timeout or BR_TIMEOUT
    if not _url_is_public(url):
        raise RuntimeError("blocked host (dia chi noi bo / khong hop le)")
    if _sess is not None:
        deadline = time.monotonic() + max(to * 2, 8)
        method = "POST" if data is not None else "GET"
        for _hop in range(7):
            r = _sess.request(method, url, data=data,
                              headers={"Content-Type": "application/x-www-form-urlencoded"} if data is not None else None,
                              timeout=(min(to, 6), to), stream=True, allow_redirects=False)
            try:
                loc = r.headers.get("Location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    url = urllib.parse.urljoin(url, loc)
                    if not _url_is_public(url):
                        raise RuntimeError("blocked host (redirect toi dia chi noi bo)")
                    if r.status_code in (301, 302, 303):
                        method, data = "GET", None
                    continue
                if r.status_code >= 400:
                    raise RuntimeError("HTTP Error %d" % r.status_code)
                buf, n = [], 0
                for c in _iter_body(r, deadline):
                    buf.append(c)
                    n += len(c)
                    if n >= limit or time.monotonic() > deadline:
                        break
                raw = b"".join(buf)[:limit]
                ctype = r.headers.get("Content-Type", "") or ""
                m = re.search(r"charset=[\"']?([\w-]+)", ctype, re.I)
                return url, (ctype.split(";")[0].strip().lower() or "text/plain"), (m.group(1) if m else None), raw
            finally:
                r.close()
        raise RuntimeError("qua nhieu lan chuyen huong")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,image/*;q=0.8,*/*;q=0.5",
        "Accept-Language": "vi,en;q=0.8", "Accept-Encoding": "gzip, deflate"})
    with _opener.open(req, timeout=to) as r:
        raw = r.read(limit)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        elif enc == "deflate":
            try:
                raw = zlib.decompress(raw)
            except Exception:
                raw = zlib.decompress(raw, -15)
        return r.geturl(), r.headers.get_content_type(), r.headers.get_content_charset(), raw

def _decode(raw, cs):
    if not cs:
        m = re.search(rb'charset=["\']?([A-Za-z0-9_-]+)', raw[:3000])
        cs = m.group(1).decode() if m else "utf-8"
    try:
        return raw.decode(cs, "replace")
    except Exception:
        return raw.decode("utf-8", "replace")

_VIDEO_RE = re.compile(
    r"^https?://([a-z0-9-]+\.)*("
    r"youtube\.com/(watch|shorts/|live/|embed/)|youtu\.be/|"
    r"facebook\.com/(reel/|watch/?\?v=|[^/]+/videos/|video\.php)|fb\.watch/|"
    r"instagram\.com/(reel|reels|tv)/|vimeo\.com/\d+|dailymotion\.com/video/|"
    r"bilibili\.com/video/)", re.I)
_MEDIA_EXT = re.compile(r"\.(mp4|webm|m3u8|mov|mkv|m4v)(\?|#|$)", re.I)

def _is_video_url(u):
    return bool(_VIDEO_RE.search(u or "") or _MEDIA_EXT.search(u or ""))

def _clean_link(u):
    p = urllib.parse.urlparse(u)
    if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l/"):
        t = urllib.parse.parse_qs(p.query).get("uddg")
        if t:
            return t[0]
    return u

def _to_url(t):
    t = (t or "").strip()
    if not t or t.startswith("about:"):
        return "about:home"
    if re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I):
        return t
    if " " not in t and ("." in t or t.lower().startswith("localhost")):
        return "https://" + t
    return BR_SEARCH_URL + urllib.parse.quote_plus(t)

_HEAVY_OPEN = re.compile(r"<(?:!--|(script|style|noscript|svg|template)\b)", re.I)
_HEAVY_CLOSE = {t: re.compile(r"</%s\s*>" % t, re.I) for t in ("script", "style", "noscript", "svg", "template")}

def _strip_heavy(s):
    out, pos, n = [], 0, len(s)
    while True:
        m = _HEAVY_OPEN.search(s, pos)
        if m is None:
            out.append(s[pos:])
            break
        out.append(s[pos:m.start()])
        tag = m.group(1)
        if tag is None:
            end = s.find("-->", m.end())
            if end < 0:
                break
            pos = end + 3
        else:
            c = _HEAVY_CLOSE[tag.lower()].search(s, m.end())
            if c is None:
                break
            pos = c.end()
    return "".join(out)

def _feed_doc(d, body):
    step = 24000
    for i in range(0, len(body), step):
        d.feed(body[i:i + step])
        if len(d.blocks) >= BR_MAX_BLOCKS:
            break
    d.close()
    d.flush()
_SKIP = {"script", "style", "noscript", "svg", "template", "select", "canvas", "object", "textarea", "head"}
_BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer", "main", "ul", "ol", "table",
          "blockquote", "pre", "nav", "aside", "dl", "dt", "dd", "figure", "figcaption", "details", "summary",
          "tbody", "thead", "fieldset", "center", "address", "body", "html"}
_TEXT_INPUT = {"text", "search", "email", "url", "tel", "number", "password", ""}

class _Doc(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.blocks = []
        self.runs = []
        self.title = ""
        self.meta = {}
        self.skip = 0
        self.in_title = False
        self.href = None
        self.bold = 0
        self.head = 0
        self.forms = []
        self.fi = -1
        self.btn = None
        self.tail_space = True
        self.seen_video = set()

    def _abs(self, u):
        try:
            return urllib.parse.urljoin(self.base, html.unescape(u.strip()))
        except Exception:
            return ""

    def flush(self):
        if self.runs:
            self.blocks.append(("text", self.runs, self.head))
            self.runs = []
        self.tail_space = True

    def _form(self, action="", method="get"):
        self.forms.append({"action": self._abs(action) if action else self.base, "method": method, "fields": {}, "texts": []})
        self.fi = len(self.forms) - 1

    def _video(self, u):
        if u and u not in self.seen_video:
            self.seen_video.add(u)
            self.flush()
            self.blocks.append(("video", u, ""))

    def _close_btn(self):
        if self.btn is not None:
            b, self.btn = self.btn, None
            self.blocks.append(("button", b["fi"], (b["label"] or "OK").strip()[:24], b["name"], b["value"]))

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in _SKIP:
            if tag == "head":
                return
            if tag == "textarea" and not self.skip:
                if self.fi < 0:
                    self._form()
                nm = a.get("name", "")
                self.flush()
                self.blocks.append(("input", self.fi, nm, "", a.get("placeholder", "")))
                self.forms[self.fi]["texts"].append(nm)
                self.forms[self.fi]["fields"].setdefault(nm, "")
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k in ("og:title", "og:description", "og:video", "og:image", "og:video:url") and a.get("content"):
                self.meta[k] = a["content"]
        elif tag == "base" and a.get("href"):
            self.base = self._abs(a["href"])
        elif tag == "a":
            h = a.get("href", "")
            if h and not h.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.href = _clean_link(self._abs(h))
        elif tag in ("b", "strong"):
            self.bold += 1
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.flush()
            self.head = int(tag[1])
        elif tag == "hr":
            self.flush()
            self.blocks.append(("hr",))
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-original") or ""
            if not src and a.get("srcset"):
                src = a["srcset"].split()[0]
            try:
                tiny = int(re.sub(r"\D", "", a.get("width", "999")) or 999) < 24 or int(re.sub(r"\D", "", a.get("height", "999")) or 999) < 24
            except Exception:
                tiny = False
            self.flush()
            if src and not src.startswith("data:") and not tiny:
                self.blocks.append(("img", self._abs(src), a.get("alt", "").strip(), self.href))
            elif a.get("alt", "").strip():
                self.blocks.append(("alt", a["alt"].strip()))
        elif tag == "video":
            s = a.get("src", "")
            if s:
                self._video(self._abs(s))
        elif tag == "source":
            s = a.get("src", "")
            if s and ("video" in a.get("type", "video") or _MEDIA_EXT.search(s)):
                self._video(self._abs(s))
        elif tag == "iframe":
            s = self._abs(a.get("src", "")) if a.get("src") else ""
            m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{6,})", s)
            if m:
                self._video("https://www.youtube.com/watch?v=" + m.group(1))
            elif _is_video_url(s):
                self._video(s)
        elif tag == "form":
            self.flush()
            self._form(a.get("action", ""), (a.get("method", "get") or "get").lower())
        elif tag == "input":
            t = (a.get("type", "text") or "text").lower()
            nm = a.get("name", "")
            if self.fi < 0 and t not in ("hidden",):
                self._form()
            if t == "hidden":
                if self.fi >= 0 and nm:
                    self.forms[self.fi]["fields"][nm] = a.get("value", "")
            elif t in _TEXT_INPUT:
                self.flush()
                self.blocks.append(("input", self.fi, nm, a.get("value", ""), a.get("placeholder", "") or a.get("aria-label", "")))
                self.forms[self.fi]["texts"].append(nm)
                if nm:
                    self.forms[self.fi]["fields"].setdefault(nm, a.get("value", ""))
            elif t in ("checkbox", "radio"):
                if nm and "checked" in a and self.fi >= 0:
                    self.forms[self.fi]["fields"][nm] = a.get("value", "on")
            elif t in ("submit", "button", "image"):
                self.flush()
                self.blocks.append(("button", self.fi, a.get("value") or a.get("alt") or "OK", nm, a.get("value", "")))
        elif tag == "button":
            self._close_btn()
            self.flush()
            if self.fi < 0:
                self._form()
            self.btn = {"fi": self.fi, "label": "", "name": a.get("name", ""), "value": a.get("value", "")}
        elif tag == "li":
            self.flush()
            self.runs.append(("\u2022", None, 0, 0, False))
        elif tag in _BLOCK:
            self.flush()

    def handle_endtag(self, tag):
        if tag in _SKIP:
            if tag != "head":
                self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        elif tag == "a":
            self.href = None
        elif tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.flush()
            self.head = 0
        elif tag == "form":
            self._close_btn()
            self.flush()
            self.fi = -1
        elif tag == "button":
            self._close_btn()
        elif tag in _BLOCK:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title += data
            return
        if self.btn is not None:
            self.btn["label"] += " ".join(data.split()) + " "
            return
        words = data.split()
        if not words:
            if data:
                self.tail_space = True
            return
        lead = data[:1].isspace()
        for i, w in enumerate(words):
            glue = (i == 0 and not lead and not self.tail_space and bool(self.runs))
            self.runs.append((w, self.href, 1 if (self.bold or self.head) else 0, self.head, glue))
        self.tail_space = data[-1:].isspace()
        if len(self.runs) > 6000:
            self.flush()

def _mk_doc(url, title=""):
    d = _Doc(url)
    d.title = title
    return d

def _text(d, s, head=0, bold=0):
    d.blocks.append(("text", [(w, None, bold, head, False) for w in s.split()], head))

_HOME_HTML = """<h2>Trình duyệt</h2>
<form action="%s" method="get"><input name="q" placeholder="Tìm kiếm hoặc gõ link"><button>Tìm</button></form>
<p>Dán link video (YouTube, Facebook...) để phát.</p>
<ul>
<li><a href="https://vi.wikipedia.org">Wikipedia</a></li>
<li><a href="https://vnexpress.net">VnExpress</a></li>
<li><a href="https://tuoitre.vn">Tuổi Trẻ</a></li>
<li><a href="https://thanhnien.vn">Thanh Niên</a></li>
<li><a href="https://news.ycombinator.com">Hacker News</a></li>
</ul>"""

def _fetch_image(u):
    try:
        _, ct, _, raw = _http(u, limit=500000, timeout=4)
        return _decode_image(raw)
    except Exception:
        return None

_IMG_DECODE_SEM = threading.BoundedSemaphore(2)

def _decode_image(raw):
    try:
        with _IMG_DECODE_SEM:
            im = Image.open(io.BytesIO(raw))
            px = im.width * im.height
            if px > (_IMG_JPEG_PIXEL_CAP if im.format == "JPEG" else _IMG_PIXEL_CAP):
                return None
            try:
                im.draft("RGB", (_IMG_MAX, _IMG_MAX))
            except Exception:
                pass
            im.load()
            if im.width > _IMG_MAX or im.height > _IMG_MAX:
                im.thumbnail((_IMG_MAX, _IMG_MAX), Image.BILINEAR)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                return bg
            return im.convert("RGB")
    except Exception:
        return None

_IMG_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4)

def _load_images(doc, imgs):
    if not (BR_IMAGES and _PIL_OK):
        return
    urls = []
    for b in doc.blocks:
        if b[0] == "img" and b[1] not in imgs and b[1] not in urls and b[1].startswith("http"):
            urls.append(b[1])
        if len(urls) >= BR_MAX_IMG:
            break
    if not urls:
        return
    futs = {u: _IMG_POOL.submit(_fetch_image, u) for u in urls}
    _done, pending = concurrent.futures.wait(list(futs.values()), timeout=BR_IMG_WAIT)

    def _take():
        got = False
        for u, f in futs.items():
            if u not in imgs and f.done():
                try:
                    r = f.result()
                    if r is not None:
                        imgs[u] = r
                        got = True
                except Exception:
                    pass
        return got

    _take()
    if pending:
        def _late():
            concurrent.futures.wait(list(pending), timeout=20)
            if _take():
                _S.pages.clear()
        threading.Thread(target=_late, daemon=True).start()

def _yt_oembed_title(url):
    try:
        _, _, cs, raw = _http("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe=""),
                              limit=20000, timeout=3)
        return str(json.loads(_decode(raw, cs)).get("title") or "")
    except Exception:
        return ""

def _fetch_doc(url, data=None):
    imgs = {}
    if url == "about:home":
        d = _Doc("about:home")
        d.feed(_HOME_HTML % BR_SEARCH_URL.split("?")[0])
        d.close()
        d.flush()
        d.title = "Trình duyệt"
        return d, imgs, "about:home"
    if _is_video_url(url) and data is None:
        d = _mk_doc(url, "Video")
        host = urllib.parse.urlparse(url).netloc
        title = ""
        if not _MEDIA_EXT.search(url):
            if _is_youtube(url):
                title = _yt_oembed_title(url)
            if not title:
                try:
                    u2, ct, cs, raw = _http(url, limit=150000, timeout=3)
                    t = _Doc(u2)
                    t.feed(_decode(raw, cs))
                    title = t.meta.get("og:title") or t.title
                    url = u2 if _is_video_url(u2) else url
                except Exception:
                    pass
        _text(d, "Video", 2, 1)
        if title:
            _text(d, html.unescape(title.strip())[:160])
        _text(d, host)
        d.blocks.append(("video", url, ""))
        return d, imgs, url
    final, ct, cs, raw = _http(url, data=data)
    if ct.startswith("image/"):
        d = _mk_doc(final, final)
        d.blocks.append(("img", final, "", None))
        im = _decode_image(raw) if _PIL_OK else None
        if im is not None:
            imgs[final] = im
        return d, imgs, final
    if ct.startswith(("video/", "audio/")) or ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl"):
        d = _mk_doc(final, "Video")
        _text(d, "Media", 2, 1)
        _text(d, urllib.parse.urlparse(final).netloc)
        d.blocks.append(("video", final, ""))
        return d, imgs, final
    body = _decode(raw, cs)
    if ct == "text/plain":
        d = _mk_doc(final, final)
        for ln in body.splitlines()[:400]:
            _text(d, ln)
        return d, imgs, final
    body = _strip_heavy(body)[:BR_MAX_HTML]
    d = _Doc(final)
    _feed_doc(d, body)
    if d.title:
        d.title = html.unescape(" ".join(d.title.split()))
    og = d.meta.get("og:video") or d.meta.get("og:video:url")
    if og and not d.seen_video:
        d.blocks.insert(0, ("video", urllib.parse.urljoin(final, og), ""))
    _load_images(d, imgs)
    return d, imgs, final

class _Page:
    pass

_C_TEXT = (25, 25, 25)
_C_LINK = (0, 70, 200)
_C_PLAY = (190, 30, 30)
_C_MUTED = (120, 120, 120)

_serial = itertools.count(1)
_jpeg_cache = {}

def _render(doc, imgs, url, portrait):
    S = BR_SS
    W = (OUT_H if portrait else OUT_W)
    VH = (OUT_W if portrait else OUT_H)
    Ws = W * S
    M = 4 * S
    base = BR_FONT_PX * S
    ops = []
    hits = []
    wl = {}
    y = 3 * S
    cap = 9000 * S
    host = urllib.parse.urlparse(url).netloc or url

    def tw(f, s):
        k = (id(f), s)
        v = wl.get(k)
        if v is None:
            v = wl[k] = f.getlength(s)
        return v

    f0 = _font(base * 0.8)
    ops.append((y, y + int(f0.size * 1.3) + S, "text", M, host[:60], f0, _C_MUTED, False))
    y += int(base * 0.8 * 1.5)

    def add_hit(x0, y0, x1, y1, act):
        hits.append((x0 / S, y0 / S, x1 / S, y1 / S, act))

    for b in doc.blocks:
        if y > cap:
            break
        k = b[0]
        if k == "text":
            runs, head = b[1], b[2]
            sz = int(base * {1: 1.35, 2: 1.2, 3: 1.1}.get(head, 1.0))
            lh = int(sz * 1.3)
            x = M
            line = []
            lines = []
            for (w, href, bold, hl, glue) in runs:
                f = _font(sz, bool(bold or head))
                ww = tw(f, w)
                sp = 0 if (glue or x == M) else tw(f, " ")
                if x + sp + ww > Ws - M and x > M:
                    lines.append(line)
                    line, x, sp = [], M, 0
                if ww > Ws - 2 * M:
                    if x > M:
                        lines.append(line)
                        line, x, sp = [], M, 0
                    while ww > Ws - 2 * M and len(w) > 1:
                        n = max(1, min(len(w) - 1, int(len(w) * (Ws - 2 * M) / ww)))
                        part, w = w[:n], w[n:]
                        line.append((M, part, href, f, tw(f, part)))
                        lines.append(line)
                        line = []
                        ww = tw(f, w)
                    x, sp = M, 0
                line.append((x + sp, w, href, f, ww))
                x = x + sp + ww
            if line:
                lines.append(line)
            for ln in lines:
                for (lx, w, href, f, ww) in ln:
                    play = bool(href and _is_video_url(href))
                    col = _C_PLAY if play else (_C_LINK if href else _C_TEXT)
                    ops.append((y, y + lh + 2 * S, "text", lx, w, f, col, bool(href)))
                    if href:
                        add_hit(lx - 2 * S, y, lx + ww + 2 * S, y + lh, ("play", href) if play else ("link", href))
                y += lh
            y += int(sz * 0.35)
        elif k == "alt":
            f = _font(base * 0.85)
            ops.append((y, y + int(base * 1.2) + S, "text", M, "[" + b[1][:50] + "]", f, _C_MUTED, False))
            y += int(base * 1.2)
        elif k == "hr":
            ops.append((y + S, y + 2 * S, "line", M, Ws - M))
            y += 4 * S
        elif k == "img":
            im = imgs.get(b[1])
            if im is None:
                if b[2]:
                    ops.append((y, y + int(base * 1.2) + S, "text", M, "[" + b[2][:50] + "]", _font(base * 0.85), _C_MUTED, False))
                    y += int(base * 1.2)
                continue
            maxw, maxh = Ws - 2 * M, int(VH * 0.7) * S
            r = min(maxw / im.width, maxh / im.height, float(S))
            nw, nh = max(1, int(im.width * r)), max(1, int(im.height * r))
            ops.append((y, y + nh, "img", M, im.resize((nw, nh), Image.BILINEAR)))
            if b[3]:
                add_hit(M, y, M + nw, y + nh, ("play", b[3]) if _is_video_url(b[3]) else ("link", b[3]))
            y += nh + 3 * S
        elif k == "input":
            f = _font(base)
            h = int(base * 1.9)
            val = b[3] or ""
            ops.append((y, y + h, "input", M, Ws - M, (val or b[4] or "...")[:60], bool(val), f))
            add_hit(M, y, Ws - M, y + h, ("input", b[1], b[2]))
            y += h + 3 * S
        elif k == "button":
            f = _font(base, True)
            label = (b[2] or "OK")[:24]
            h = int(base * 1.9)
            bw = min(Ws - 2 * M, int(tw(f, label)) + 16 * S)
            ops.append((y, y + h, "button", M, M + bw, label, f, (225, 232, 250), _C_TEXT))
            add_hit(M, y, M + bw, y + h, ("submit", b[1], b[3], b[4]))
            y += h + 3 * S
        elif k == "video":
            f = _font(base * 1.1, True)
            label = "\u25B6  Phát video"
            h = int(base * 2.2)
            ops.append((y, y + h, "button", M, Ws - M, label, f, _C_PLAY, (255, 255, 255)))
            add_hit(M, y, Ws - M, y + h, ("play", b[1]))
            y += h + 4 * S
    if len(doc.blocks) == 0 or y < 40 * S:
        ops.append((y, y + int(base * 1.4), "text", M, "(Trang không có chữ - có thể cần JavaScript)", _font(base * 0.85), _C_MUTED, False))
        y += int(base * 1.4)
    total = y + 4 * S
    ops.sort(key=lambda o: o[0])
    p = _Page()
    p.url, p.title, p.hits, p.total = url, doc.title, hits, total / S
    p.ops = ops
    p.ys = [o[0] for o in ops]
    p.maxh = max((o[1] - o[0] for o in ops), default=0)
    p.vw, p.vh, p.portrait = W, VH, portrait
    p.serial = next(_serial)
    return p

_band_cache = {}

def _band(page, idx):
    key = (page.serial, idx)
    im = _band_cache.get(key)
    if im is not None:
        return im
    S = BR_SS
    BH = page.vh * S * 2
    b0 = idx * BH
    b1 = b0 + BH
    im = Image.new("RGB", (page.vw * S, BH), (255, 255, 255))
    dr = ImageDraw.Draw(im)
    for o in page.ops[bisect.bisect_left(page.ys, b0 - page.maxh):]:
        y0, y1, t = o[0], o[1], o[2]
        if y0 >= b1:
            break
        if y1 <= b0:
            continue
        oy = y0 - b0
        if t == "text":
            _, _, _, x, s, f, col, ul = o
            dr.text((x, oy), s, font=f, fill=col)
            if ul:
                ly = oy + int(f.size * 1.12)
                dr.line((x, ly, x + f.getlength(s), ly), fill=col, width=max(1, S // 2))
        elif t == "line":
            dr.line((o[3], oy, o[4], oy), fill=(200, 200, 200), width=S)
        elif t == "img":
            im.paste(o[4], (o[3], oy))
        elif t == "input":
            _, _, _, x0, x1, s, filled, f = o
            dr.rounded_rectangle((x0, oy, x1, oy + (y1 - y0)), radius=4 * S, outline=(130, 130, 130), width=S, fill=(250, 250, 250))
            dr.text((x0 + 5 * S, oy + int((y1 - y0 - f.size) / 2) - S), s, font=f, fill=_C_TEXT if filled else (150, 150, 150))
        elif t == "button":
            _, _, _, x0, x1, s, f, bg, fg = o
            dr.rounded_rectangle((x0, oy, x1, oy + (y1 - y0)), radius=5 * S, fill=bg, outline=(160, 170, 200) if bg[0] > 200 else None)
            tw_ = f.getlength(s)
            dr.text((x0 + (x1 - x0 - tw_) / 2, oy + int((y1 - y0 - f.size) / 2) - S), s, font=f, fill=fg)
    if len(_band_cache) >= 8:
        _band_cache.pop(next(iter(_band_cache)))
    _band_cache[key] = im
    return im

def _jpeg(page, scroll):
    S = BR_SS
    scroll = max(0.0, min(scroll, max(0.0, page.total - page.vh)))
    key = (page.serial, int(scroll * S))
    hit = _jpeg_cache.get(key)
    if hit is not None:
        return hit, scroll
    top = int(scroll * S)
    VHs = page.vh * S
    BH = VHs * 2
    Ws = page.vw * S
    i0, i1 = top // BH, (top + VHs - 1) // BH
    if i0 == i1:
        o0 = top - i0 * BH
        img = _band(page, i0).crop((0, o0, Ws, o0 + VHs))
    else:
        img = Image.new("RGB", (Ws, VHs), (255, 255, 255))
        cut_h = (i1 * BH) - top
        img.paste(_band(page, i0).crop((0, top - i0 * BH, Ws, BH)), (0, 0))
        img.paste(_band(page, i1).crop((0, 0, Ws, VHs - cut_h)), (0, cut_h))
    if S > 1:
        try:
            v = img.reduce(S)
        except Exception:
            v = img.resize((page.vw, page.vh), Image.BOX)
    else:
        v = img
    if page.total > page.vh:
        d = ImageDraw.Draw(v)
        bh = max(10, int(page.vh * page.vh / page.total))
        by = int(scroll / (page.total - page.vh) * (page.vh - bh))
        d.rectangle((page.vw - 2, by, page.vw - 1, by + bh), fill=(110, 110, 110))
    out = _enc_jpeg(v, BR_JPEG_Q_HI)
    if len(_jpeg_cache) >= 16:
        _jpeg_cache.pop(next(iter(_jpeg_cache)))
    _jpeg_cache[key] = out
    return out, scroll

class _State:
    def __init__(self):
        self.docs = {}
        self.pages = {}
        self.url = ""
        self.hist = []
        self.scroll = 0.0
        self.focus = None

    def reset(self):
        self.__init__()

    def doc(self):
        return self.docs[self.url][0]

    def page(self, portrait):
        key = (self.url, portrait)
        p = self.pages.get(key)
        if p is None:
            d, imgs = self.docs[self.url]
            if len(self.pages) > 4:
                self.pages.clear()
            p = self.pages[key] = _render(d, imgs, self.url, portrait)
        return p

    def invalidate(self):
        self.pages.clear()

    def goto(self, url, data=None, push=True):
        _lk.release()
        try:
            try:
                d, imgs, final = _fetch_shared(url, data)
            except Exception as e:
                _log("tai that bai:", url, e)
                d, imgs, final = _mk_doc(url, "Loi"), {}, url
                _text(d, "Không tải được trang", 2, 1)
                _text(d, str(e)[:160])
                _text(d, url[:200])
        finally:
            _lk.acquire()
        if push and self.url and self.url != final:
            self.hist.append((self.url, self.scroll))
            del self.hist[:-30]
        self.docs.pop(final, None)
        self.docs[final] = (d, imgs)
        while len(self.docs) > 3:
            self.docs.pop(next(iter(self.docs)))
        self.url, self.scroll, self.focus = final, 0.0, None
        self.pages.clear()
        return final

_S = _State()
_lk = threading.RLock()
_inflight = {}
_inflight_lock = threading.Lock()

def _fetch_shared(url, data=None):
    key = (url, data)
    with _inflight_lock:
        fut = _inflight.get(key)
        mine = fut is None
        if mine:
            fut = _inflight[key] = concurrent.futures.Future()
    if mine:
        try:
            fut.set_result(_fetch_doc(url, data))
        except BaseException as e:
            fut.set_exception(e)
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)
    return fut.result(timeout=60)

def _submit(fi, bname=None, bval=None):
    d = _S.doc()
    if not (0 <= fi < len(d.forms)):
        return None
    f = d.forms[fi]
    fields = dict(f["fields"])
    if bname:
        fields[bname] = bval or ""
    if f["method"] == "post":
        return _S.goto(f["action"], data=urllib.parse.urlencode(fields).encode())
    p = urllib.parse.urlparse(f["action"])
    q = dict(urllib.parse.parse_qsl(p.query))
    q.update({k: v for k, v in fields.items() if k})
    return _S.goto(urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q), fragment="")))

BR_ENGINE = os.environ.get("BR_ENGINE", "auto").strip().lower()
BR_CR_ZOOM = max(1.0, min(3.0, float(os.environ.get("BR_CR_ZOOM", "1.25"))))
BR_CR_TIMEOUT = float(os.environ.get("BR_CR_TIMEOUT", "15"))
BR_CR_IDLE = float(os.environ.get("BR_CR_IDLE", "300"))
BR_CR_MIN_MB = float(os.environ.get("BR_CR_MIN_MB", "900"))
BR_CR_MEM_MAX = float(os.environ.get("BR_CR_MEM_MAX", "0.90"))
BR_CR_RETRY = float(os.environ.get("BR_CR_RETRY", "120"))
BR_CR_SOFT = float(os.environ.get("BR_CR_SOFT", "0.80"))
BR_CR_RECYCLE = int(os.environ.get("BR_CR_RECYCLE", "30"))
BR_CR_JS_MB = int(os.environ.get("BR_CR_JS_MB", "128"))
BR_CR_SINGLE = os.environ.get("BR_CR_SINGLE", "0") == "1"
BR_CR_KEEP = os.environ.get("BR_CR_KEEP", "1") == "1"
BR_CR_STREAM_FRAC = float(os.environ.get("BR_CR_STREAM_FRAC", "0.60"))
BR_CR_STRICT_DNS = os.environ.get("BR_CR_STRICT_DNS", "0") == "1"
BR_CR_LOAD_MS = int(float(os.environ.get("BR_CR_LOAD_WAIT", "1.2")) * 1000)
BR_CR_NETIDLE_MS = int(float(os.environ.get("BR_CR_IDLE_WAIT", "0.25")) * 1000)
BR_CR_TAP_WAIT_MS = int(os.environ.get("BR_CR_TAP_WAIT_MS", "120"))
BR_CR_SCROLL_WAIT_MS = int(os.environ.get("BR_CR_SCROLL_WAIT_MS", "30"))
_CR_BLOCK_TYPES = ("media", "font", "websocket", "eventsource", "manifest", "texttrack", "ping", "prefetch",
                   "cspviolationreport")
_CR_BLOCK_HOSTS = ("doubleclick.net", "googlesyndication.com", "googleadservices.com", "google-analytics.com",
                   "googletagmanager.com", "adservice.google.", "adnxs.com", "taboola.com", "outbrain.com",
                   "scorecardresearch.com", "hotjar.com", "connect.facebook.net", "amazon-adsystem.com",
                   "criteo.", "moatads.com", "quantserve.com", "clarity.ms", "adsrvr.org", "pubmatic.com",
                   "rubiconproject.com", "casalemedia.com", "googletagservices.com", "pagead2.", "adform.net",
                   "mc.yandex.", "ads-twitter.com", "snap.licdn.com", "analytics.tiktok.com")

_CR_INIT_JS = """(()=>{
window.open=function(u){if(u){try{location.href=u}catch(e){}}return null;};
document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[target]');
if(a&&a.target!=='_self')a.target='_self';},true);
})();"""

_CR_FOCUS_JS = """()=>{const e=document.activeElement;if(!e)return null;const t=e.tagName;
if(t==='INPUT'){const ty=(e.type||'text').toLowerCase();
if(['button','submit','checkbox','radio','image','reset','file','range','color'].includes(ty))return null;return 'line';}
if(t==='TEXTAREA'){const f=e.form,nm=(e.name||'').toLowerCase();
if(f&&(e.rows<=1||['q','query','search','s'].includes(nm)||f.getAttribute('role')==='search'))return 'line';return 'multi';}
if(e.isContentEditable)return 'multi';return null;}"""

_CR_SB_JS = """(z)=>{const de=document.documentElement,sy=window.scrollY,tot=de.scrollHeight,ih=window.innerHeight;
let b=document.getElementById('__rsb');
if(tot>ih+4){if(!b){b=document.createElement('div');b.id='__rsb';const s=b.style;s.position='fixed';s.right='0';
s.width=(2*z)+'px';s.background='#6e6e6e';s.zIndex='2147483647';s.pointerEvents='none';(document.body||de).appendChild(b);}
const bh=Math.max(10*z,ih*ih/tot);b.style.height=bh+'px';b.style.top=(sy/(tot-ih)*(ih-bh))+'px';b.style.display='block';}
else if(b){b.style.display='none';}
return [sy,tot,ih];}"""

def _jpeg_dims(b):
    i, n = 2, len(b)
    while i + 9 < n:
        if b[i] != 0xFF:
            i += 1
            continue
        m = b[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            return ((b[i + 7] << 8) | b[i + 8], (b[i + 5] << 8) | b[i + 6])
        if m == 0xFF:
            i += 1
        elif m == 0x01 or 0xD0 <= m <= 0xD9:
            i += 2
        else:
            i += 2 + ((b[i + 2] << 8) | b[i + 3])
    return None

def _host_quick_ok(host, url):
    if not url.startswith(("http://", "https://")):
        return False
    host = (host or "").strip("[]").rstrip(".")
    if not host:
        return False
    if ALLOW_PRIVATE_HOSTS:
        return True
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa")):
        return False
    try:
        a = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return True
    a = getattr(a, "ipv4_mapped", None) or a
    return a.is_global

_CR_PAGE_CSS = "body{font-family:sans-serif;margin:10px;font-size:15px}input{width:70%;font-size:15px;padding:4px}button{font-size:15px;padding:4px 8px}li{margin:6px 0}"

def _cr_home_html():
    act = html.escape(BR_SEARCH_URL.split("?")[0], quote=True)
    return ("<!doctype html><meta charset=utf-8><title>Trình duyệt</title><style>%s</style><h2>Trình duyệt</h2>"
            "<form action=\"%s\" method=\"get\"><input name=\"q\" placeholder=\"Tìm kiếm hoặc gõ link\"> <button>Tìm</button></form>"
            "<p>Dán link video (YouTube, Facebook...) để phát.</p><ul>"
            "<li><a href=\"https://vi.wikipedia.org\">Wikipedia</a></li><li><a href=\"https://vnexpress.net\">VnExpress</a></li>"
            "<li><a href=\"https://tuoitre.vn\">Tuổi Trẻ</a></li><li><a href=\"https://thanhnien.vn\">Thanh Niên</a></li>"
            "<li><a href=\"https://news.ycombinator.com\">Hacker News</a></li></ul>") % (_CR_PAGE_CSS, act)

def _cr_error_html(msg, url):
    return ("<!doctype html><meta charset=utf-8><title>Loi</title><style>%s</style><h3>Không tải được trang</h3><p>%s</p><p>%s</p>"
            % (_CR_PAGE_CSS, html.escape(str(msg)[:160]), html.escape(str(url)[:200])))

def _cr_video_html(url):
    return ("<!doctype html><meta charset=utf-8><title>Video</title><style>%s</style><h3>Video</h3><p>%s</p><p>Đang chuyển sang trình phát...</p>"
            % (_CR_PAGE_CSS, html.escape(urllib.parse.urlparse(url).netloc + urllib.parse.urlparse(url).path[:60])))

class _CrUnavailable(RuntimeError):
    pass

class _Chromium:

    def __init__(self):
        self.pw = self.browser = self.ctx = self.page = None
        self.vp = None
        self.play = None
        self.focus = None
        self.home = False
        self.err_url = ""
        self.shown_err = None
        self.navs = 0
        self.born = self.last_used = time.monotonic()

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise _CrUnavailable("chua cai playwright: %s" % e)
        args = ["--disable-gpu", "--disable-software-rasterizer", "--disable-dev-shm-usage", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--disable-default-apps", "--disable-translate",
                "--disable-component-update", "--disable-breakpad", "--disable-notifications", "--mute-audio",
                "--no-first-run", "--no-default-browser-check", "--metrics-recording-only", "--force-color-profile=srgb",
                "--disable-features=TranslateUI,MediaRouter,AudioServiceOutOfProcess,OptimizationHints,Prerender2,"
                "site-per-process,IsolateOrigins", "--disable-site-isolation-trials",
                "--renderer-process-limit=1", "--disk-cache-size=1", "--media-cache-size=1",
                "--js-flags=--max-old-space-size=%d" % BR_CR_JS_MB]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args += ["--no-sandbox", "--no-zygote"]
        if BR_CR_SINGLE:
            args.append("--single-process")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(headless=True, args=args)
        except Exception as e:
            raise _CrUnavailable("khong mo duoc Chromium (da chay 'playwright install chromium'?): %s" % str(e)[:200])
        self.ctx = self.browser.new_context(
            user_agent=UA, viewport={"width": 400, "height": 212}, device_scale_factor=1.0 / BR_CR_ZOOM, accept_downloads=False,
            locale=os.environ.get("BR_CR_LOCALE", "vi-VN"), service_workers="block")
        self.ctx.set_default_timeout(BR_CR_TIMEOUT * 1000)
        self.ctx.add_init_script(_CR_INIT_JS)
        self.ctx.route("**/*", self._route)
        self.ctx.on("page", self._on_page)
        self.page = self.ctx.new_page()
        self.page.on("dialog", lambda d: d.dismiss())

    def close(self):
        for o in (self.ctx, self.browser):
            try:
                if o:
                    o.close()
            except Exception:
                pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.browser = self.ctx = self.page = None
        gc.collect()

    def _on_page(self, p):
        if self.page is not None and p is not self.page:
            try:
                p.close()
            except Exception:
                pass

    def _route(self, route):
        try:
            req = route.request
            url = req.url
            if url.startswith(("data:", "blob:", "about:")):
                return route.continue_()
            rt = req.resource_type
            if rt in _CR_BLOCK_TYPES or (rt == "image" and not BR_IMAGES):
                return route.abort()
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if any(h in host for h in _CR_BLOCK_HOSTS):
                return route.abort()
            top = rt == "document" and req.is_navigation_request() and req.frame.parent_frame is None
            if top or BR_CR_STRICT_DNS:
                if not _url_is_public(url):
                    return route.abort("blockedbyclient")
            elif not _host_quick_ok(host, url):
                return route.abort("blockedbyclient")
            if top and _is_video_url(url):
                self.play = url
                return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=_cr_video_html(url))
            route.continue_()
        except Exception:
            try:
                route.abort()
            except Exception:
                pass

    def ensure(self, portrait, home=True):
        w, h = (OUT_H, OUT_W) if portrait else (OUT_W, OUT_H)
        vp = (int(w * BR_CR_ZOOM), int(h * BR_CR_ZOOM))
        if vp != self.vp:
            self.page.set_viewport_size({"width": vp[0], "height": vp[1]})
            self.vp = vp
        if home and self.page.url == "about:blank" and not self.home and not self.shown_err:
            self.go("about:home")

    def cur_url(self):
        u = self.page.url
        if self.shown_err and u == self.shown_err[0]:
            return self.shown_err[1]
        if u.startswith("chrome-error:"):
            return self.err_url or u
        return "about:home" if (self.home and u == "about:blank") else u

    def set_html(self, body):
        try:
            self.page.set_content(body, wait_until="domcontentloaded")
        except Exception:
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            self.page.set_content(body, wait_until="domcontentloaded")

    def settle(self):
        try:
            self.page.wait_for_load_state("load", timeout=BR_CR_LOAD_MS)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=BR_CR_NETIDLE_MS)
        except Exception:
            pass
        self.page.wait_for_timeout(40)

    def go(self, url):
        self.play, self.focus = None, None
        self.shown_err = None
        if url == "about:home":
            self.set_html(_cr_home_html())
            self.home = True
            return
        self.home = False
        self.err_url = url
        self.navs += 1
        before = self.page.url
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=BR_CR_TIMEOUT * 1000)
        except Exception as e:
            msg = str(e)
            if self.play:
                return
            if "Timeout" in msg:

                self._halt()
                if self.page.url == before:
                    self.show_error("Het thoi gian cho (%ds)" % BR_CR_TIMEOUT, url)
                    return
            elif "ERR_ABORTED" not in msg:
                self.show_error(msg.splitlines()[0] if msg else "loi", url)
                return
        self.settle()

    def _halt(self):
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            try:
                cdp.send("Page.stopLoading")
            finally:
                cdp.detach()
        except Exception:
            pass

    def show_error(self, msg, url):
        self.set_html(_cr_error_html(msg, url))
        self.shown_err = (self.page.url, url)

    def get_focus(self):
        try:
            return self.page.evaluate(_CR_FOCUS_JS)
        except Exception:
            return None

    def shot(self, portrait, fast=False):
        W, VH = (OUT_H, OUT_W) if portrait else (OUT_W, OUT_H)
        pg = self.page
        try:
            pg.evaluate(_CR_SB_JS, BR_CR_ZOOM)
        except Exception:
            pass
        raw = pg.screenshot(type="png", caret="hide", animations="disabled")
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.size != (W, VH):
            im = im.resize((W, VH), Image.LANCZOS)
        return _enc_jpeg(im, BR_JPEG_Q_SCROLL if fast else BR_JPEG_Q_HI)

    def act(self, action, a, portrait, resume=""):
        self.last_used = time.monotonic()
        if resume and action != "open" and self.page.url == "about:blank":
            self.go(resume)
        self.ensure(portrait, home=action not in ("open",))
        z = BR_CR_ZOOM
        pg = self.page
        if action == "open":
            self.go(_to_url(a.get("u", "")))
        elif action == "tap":
            sx, sy = _f(a, "x"), _f(a, "y")
            px, py = ((OUT_H - 1 - sy), sx) if portrait else (sx, sy)
            self.play = None
            old = pg.url
            pg.mouse.click(px * z, py * z)
            pg.wait_for_timeout(BR_CR_TAP_WAIT_MS)
            if pg.url != old:
                self.home = False
                self.navs += 1
                self.settle()
            self.focus = self.get_focus()
        elif action == "scroll":
            dy = _f(a, "dy") * z
            if dy:
                moved = pg.evaluate("d=>{const b=window.scrollY;window.scrollBy({top:d,behavior:'instant'});return window.scrollY-b}", dy)
                if not moved:
                    pg.mouse.move(self.vp[0] / 2, self.vp[1] / 2)
                    pg.mouse.wheel(0, dy)
                pg.wait_for_timeout(BR_CR_SCROLL_WAIT_MS)
        elif action == "type":
            t = (a.get("t") or "")[:300]
            if t and self.focus:
                kind = self.focus
                pg.keyboard.press("Control+A")
                pg.keyboard.insert_text(t)
                if kind == "line":
                    old = pg.url
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(300)
                    self.home = False
                    if pg.url != old:
                        self.settle()
                    self.focus = None
                else:
                    self.focus = self.get_focus()
            elif t:
                self.go(_to_url(t))
        elif action == "back":
            self.play, self.focus = None, None
            try:
                pg.go_back(wait_until="domcontentloaded", timeout=BR_CR_TIMEOUT * 1000)
                self.settle()
            except Exception:
                self._halt()
            if pg.url in ("about:blank", ""):
                self.go("about:home")
        elif action == "reload":
            if self.cur_url() == "about:home":
                self.go("about:home")
            elif self.shown_err and pg.url == self.shown_err[0]:
                self.go(self.shown_err[1])
            else:
                self.play = None
                try:
                    pg.reload(wait_until="domcontentloaded", timeout=BR_CR_TIMEOUT * 1000)
                except Exception:
                    self._halt()
                self.settle()
        jpg = self.shot(portrait, fast=(action == "scroll"))
        play, self.play = self.play, None
        return jpg, self.focus is not None and action in ("tap", "type"), play, self.cur_url()

_cr = None
_cr_fail_until = 0.0
_cr_last_url = ""
_cr_reaper_on = False
_cr_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="chromium")

def _cr_enabled():
    if BR_ENGINE == "html" or not _PIL_OK or time.monotonic() < _cr_fail_until:
        return False
    if BR_ENGINE == "auto":
        mi = _mem_info()
        if mi and mi[1] < BR_CR_MIN_MB:
            return False
    return True

_cr_resume = ""

def _cr_close_async(resume=False):
    global _cr, _cr_resume
    _cr_resume = _cr_last_url if (resume and _cr_last_url not in ("", "about:home")) else ""
    c, _cr = _cr, None
    if c is not None:
        try:
            _cr_pool.submit(c.close)
        except Exception:
            pass

def _cr_release_for_stream():
    if not BR_CR_KEEP or _mem_frac() > BR_CR_STREAM_FRAC:
        _cr_close_async(resume=True)

def _cr_prewarm():
    global _cr, _cr_reaper_on
    if _cr is not None or not _cr_enabled() or _mem_frac() > BR_CR_SOFT:
        return
    c = _Chromium()
    _cr_run(c.start)
    _cr = c
    if not _cr_reaper_on:
        _cr_reaper_on = True
        threading.Thread(target=_cr_reaper, daemon=True).start()
    print("[br] chromium khoi dong san", flush=True)

def _cr_reaper():
    while True:
        time.sleep(15)
        c = _cr
        if c is not None and time.monotonic() - c.last_used > BR_CR_IDLE:
            _log("chromium nhan roi -> tat (nho trang dang xem)")
            _cr_close_async(resume=True)

def _cr_act(action, a, portrait):
    global _cr, _cr_last_url, _cr_reaper_on, _cr_resume
    frac = _mem_frac()
    if frac > BR_CR_MEM_MAX:
        raise _CrUnavailable("RAM %.0f%% > %.0f%%" % (frac * 100, BR_CR_MEM_MAX * 100))
    if _cr is None:
        ARB.trim_caches()
        _cr = _Chromium()
    c = _cr
    if c.page is None:
        _cr_run(c.start)
    if not _cr_reaper_on:
        _cr_reaper_on = True
        threading.Thread(target=_cr_reaper, daemon=True).start()
    res, _cr_resume = _cr_resume, ""
    jpg, edit, play, url = _cr_run(c.act, action, a, portrait, res)
    _cr_last_url = url
    frac = _mem_frac()
    if (frac > BR_CR_SOFT or c.navs >= BR_CR_RECYCLE) and time.monotonic() - c.born > 15:
        _log("don RAM: %.0f%%, %d luot -> khoi dong lai Chromium" % (frac * 100, c.navs))
        _cr_close_async(resume=True)
    _S.url = url
    return jpg, edit, play

def _cr_run(fn, *args):
    return _cr_pool.submit(fn, *args).result(timeout=BR_CR_TIMEOUT + 40)

def _act(action, a, portrait):
    global _cr_fail_until
    if _cr_enabled():
        try:
            return _cr_act(action, a, portrait)
        except Exception as e:
            why = (type(e).__name__ + ": " + str(e)).splitlines()[0][:220]
            _log("chromium loi -> dung bo HTML (thu lai sau %ds):" % BR_CR_RETRY, why)
            _cr_fail_until = time.monotonic() + BR_CR_RETRY
            _cr_close_async()
            u = _cr_last_url
            _S.reset()
            if u and u != "about:home" and action != "open":
                _S.goto(u, push=False)
    return _act_html(action, a, portrait)

def _act_html(action, a, portrait):
    edit, play = False, None
    if not _S.url:
        _S.goto("about:home", push=False)
    if action == "open":
        t = a.get("u", "")
        u = _S.goto(_to_url(t))
        if _is_video_url(u):
            play = u
    elif action == "tap":
        pg = _S.page(portrait)
        sx, sy = _f(a, "x"), _f(a, "y")
        px, py = ((OUT_H - 1 - sy), sx) if portrait else (sx, sy)
        py += _S.scroll
        hit = None
        for (x0, y0, x1, y1, act) in pg.hits:
            if x0 - 3 <= px <= x1 + 3 and y0 - 3 <= py <= y1 + 3:
                hit = act
                break
        if hit:
            if hit[0] == "link":
                _S.goto(hit[1])
            elif hit[0] == "play":
                play = hit[1]
            elif hit[0] == "input":
                _S.focus = (hit[1], hit[2])
                edit = True
            elif hit[0] == "submit":
                _submit(hit[1], hit[2], hit[3])
    elif action == "scroll":
        _S.scroll += _f(a, "dy")
    elif action == "type":
        t = (a.get("t") or "")[:300]
        d = _S.doc()
        if _S.focus and 0 <= _S.focus[0] < len(d.forms) and t:
            fi, nm = _S.focus
            f = d.forms[fi]
            f["fields"][nm] = t
            _S.focus = None
            for i, b in enumerate(d.blocks):
                if b[0] == "input" and b[1] == fi and b[2] == nm:
                    d.blocks[i] = ("input", fi, nm, t, b[4])
            _S.invalidate()
            if len(f["texts"]) <= 1 or nm in ("q", "query", "s", "search", "k", "keyword", "text", "term"):
                u = _submit(fi)
                if u and _is_video_url(u):
                    play = u
        elif t:
            u = _S.goto(_to_url(t))
            if _is_video_url(u):
                play = u
    elif action == "back":
        if _S.hist:
            u, sc = _S.hist.pop()
            if u in _S.docs:
                _S.url, _S.scroll, _S.focus = u, sc, None
            else:
                _S.goto(u, push=False)
                _S.scroll = sc
    elif action == "reload":
        u, sc = _S.url, _S.scroll
        _S.docs.pop(u, None)
        _S.goto(u, push=False)
        _S.scroll = sc
    pg = _S.page(portrait)
    jpg, _S.scroll = _jpeg(pg, _S.scroll)
    return jpg, edit, play

def _f(a, k, d=0.0):
    try:
        return float(a.get(k, d))
    except Exception:
        return d

def _br_auth():
    if not BR_TOKEN:
        return Response("BR_TOKEN chua duoc dat tren server", 403)
    if not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return Response("forbidden", 403)
    return None

@app.route("/br/play")
def br_play():
    global _br_last_activity
    bad = _br_auth()
    if bad:
        return bad
    _br_last_activity = time.time()
    url = (request.args.get("u") or _br_last_url or "").strip()
    if not url or url.startswith("about:"):
        return Response("chua co trang nao dang mo - mo link video truoc", 409)
    if "://" not in url:
        url = "https://" + url
    print(f"[br] play {url}", flush=True)
    _cr_release_for_stream()
    return _stream_impl(url)

@app.route("/br/<action>")
def br_action(action):
    global _br_last_activity, _br_last_url
    bad = _br_auth()
    if bad:
        return bad
    _br_last_activity = time.time()
    if action == "close":
        with _lk:
            _S.reset()
        _cr_close_async()
        return Response("closed", 200)
    if action not in ("open", "tap", "scroll", "type", "back", "reload", "frame"):
        return Response("unknown action", 404)
    if not _PIL_OK:
        return Response("thieu Pillow: them Pillow vao requirements.txt roi deploy lai", 503)
    if BR_KILL_ON_LEAVE and action in ("open", "tap", "type", "back", "reload"):
        _kill_stream(_who())
    a = request.args.to_dict()
    portrait = a.get("o") == "1"
    t0 = time.monotonic()
    try:
        with _lk:
            jpg, edit, play = _act(action, a, portrait)
            url = _S.url
    except Exception as e:
        _log(traceback.format_exc())
        return Response("loi: %s" % str(e)[:200], 500)
    _br_last_url = play or url
    _log(f"{action} {int((time.monotonic() - t0) * 1000)}ms {len(jpg)}B {url[:70]}{' PLAY' if play else ''}")
    h = {"X-Url": _clip_pct(urllib.parse.quote(url, safe=":/?&=%#+@,;~-._!*()'$"), 300), "X-Edit": "1" if edit else "0", "Cache-Control": "no-store"}
    if play:
        h["X-Play"] = _clip_pct(urllib.parse.quote(play, safe=":/?&=%#@,+;~-._!*()'$"), 700)
    return Response(jpg, mimetype="image/jpeg", headers=h)


class Arbiter:
    def __init__(self):
        self.fg = ""
        self.fg_t = 0.0
        self.lock = threading.Lock()

    def begin_play(self, app):
        global _br_last_activity
        with self.lock:
            prev, self.fg, self.fg_t = self.fg, app, time.monotonic()
        if app != "video":
            video_release_idle(f"nhuong cho {app}")
        if app != "music":
            music_release_idle(f"nhuong cho {app}")
        if app in ("video", "music"):
            _br_last_activity = 0.0
            _cr_release_for_stream()
        if prev != app:
            print(f"[app] {prev or '-'} -> {app} (chi nhuong tai nguyen ranh) | RAM {_mem_str()}", flush=True)

    def any_active(self):
        return video_active() or music_active() or _br_busy()

    def trim_caches(self):
        try:
            _band_cache.clear()
            _jpeg_cache.clear()
            _S.pages.clear()
        except Exception:
            pass
        music_trim()
        gc.collect()

    def relieve(self, why=""):
        self.trim_caches()
        video_release_idle("RAM cao: " + why)
        music_release_idle("RAM cao: " + why)
        if _cr is not None and not _br_busy():
            _cr_close_async(resume=True)
        _free_ram()

    def switch_hard(self, app):
        with self.lock:
            self.fg, self.fg_t = app, time.monotonic()
        if app != "video":
            video_kill_all("ESP da doi sang app " + app)
        if app != "music":
            music_kill_all()
        if app != "browser" and app != "web":
            _cr_close_async(resume=True)
        self.trim_caches()
        _free_ram()


ARB = Arbiter()


def _idle_for(sec):
    return (_active_streams == 0 and _reqbusy == 0 and not _stream_elsewhere() and not _br_busy() and not music_busy()
            and time.monotonic() - _last_req >= sec)

AUTO_UPDATE = os.environ.get("YTDLP_AUTO_UPDATE", "1") == "1"
UPDATE_HOURS = float(os.environ.get("YTDLP_UPDATE_HOURS", "12"))
YTDLP_CHANNEL = os.environ.get("YTDLP_CHANNEL", "nightly").strip().lower()
_update_lock = threading.Lock()
_last_update_try = 0.0

def _ytdlp_version():
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return "?"

YTDLP_PIP_TIMEOUT = float(os.environ.get("YTDLP_PIP_TIMEOUT", "900"))

def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))

def _ytdlp_latest():
    try:
        req = urllib.request.Request(("https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest" if YTDLP_CHANNEL == "nightly" else "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"),
                                     headers={"User-Agent": "relay-server", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read(1_000_000).decode("utf-8", "replace")).get("tag_name") or None
    except Exception:
        return None

def _update_ytdlp(min_gap=600):
    global _last_update_try
    with _update_lock:
        if time.monotonic() - _last_update_try < min_gap and _last_update_try:
            return False
        _last_update_try = time.monotonic()
        before = _ytdlp_version()
        latest = _ytdlp_latest()
        if latest and _ver_tuple(before) and _ver_tuple(before) >= _ver_tuple(latest):
            print(f"[update] yt-dlp {before} da la ban moi nhat ({latest}) -> bo qua pip", flush=True)
            return False
        mi = _mem_info()
        if mi and mi[0] > mi[1] * 0.55:
            print(f"[update] RAM dang dung {mi[0]:.0f}/{mi[1]:.0f}MB, khong du cho pip -> de lan sau", flush=True)
            return False
        try:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "--quiet"]
            if YTDLP_CHANNEL == "nightly":
                cmd.append("--pre")
            cmd.append("yt-dlp[default]")
            if shutil.which("nice"):
                cmd = ["nice", "-n", "19"] + cmd
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=YTDLP_PIP_TIMEOUT)
            if r.returncode != 0:
                print(f"[update] pip loi: {r.stderr[-300:]}", flush=True)
                return False
        except Exception as e:
            print(f"[update] khong chay duoc pip: {e}", flush=True)
            return False
        after = _ytdlp_version()
        print(f"[update] yt-dlp {before} -> {after}", flush=True)
        return before != after

YTDLP_QUIET_SEC = float(os.environ.get("YTDLP_QUIET_SEC", "600"))

def _restart_when_idle():
    for _ in range(720):
        if _idle_for(YTDLP_QUIET_SEC):
            print("[update] khoi dong lai de nap yt-dlp moi", flush=True)
            if "gunicorn" in os.path.basename(sys.argv[0] or ""):

                import signal
                os.kill(os.getppid(), signal.SIGHUP)
                return
            os.execv(sys.executable, [sys.executable] + sys.argv)
        time.sleep(10)

def _update_and_maybe_restart(min_gap=600):
    for _ in range(720):
        if _idle_for(YTDLP_QUIET_SEC):
            break
        time.sleep(10)
    else:
        print("[update] may ban lien tuc >1h, bo qua dot cap nhat nay", flush=True)
        return
    if _update_ytdlp(min_gap):
        _restart_when_idle()

_update_pending = threading.Event()

def trigger_update_async():
    if AUTO_UPDATE and not _update_pending.is_set():
        _update_pending.set()

        def _run():
            try:
                _update_and_maybe_restart(1800)
            finally:
                _update_pending.clear()
        threading.Thread(target=_run, daemon=True).start()

def _maybe_update_on_use():
    if not AUTO_UPDATE or not HAS_YT:
        return
    if _last_update_try and time.monotonic() - _last_update_try < UPDATE_HOURS * 3600:
        return
    trigger_update_async()


@app.route("/")
def root_hidden():
    return _405()


@app.route("/healthz")
def healthz():
    return "ok", 200


def _status_data():
    now = time.monotonic()
    return {
        "role": ROLE, "mem": _mem_str(), "up_min": int((now - _BOOT_MONO) / 60), "cookies": COOKIES.info(),
        "engine": BR_ENGINE + ("(chromium dang chay)" if _cr is not None else ""),
        "heat": round(HEAT.level(), 2), "proxies": PROXIES.snapshot(), "fg": ARB.fg,
        "pot": ("on" if POT.enabled() else ("off" if not POT.base else "loi-tam-thoi")),
        "api": ("ok" if API_STATE.ok() else (f"nghi {int(API_STATE.block_until - now)}s ({API_STATE.why})" if YT_API_KEY else "chua co key")),
        "video": dict(VIDEO.snapshot(), active=video_active(), sessions=len(_sessions)),
        "music": dict(MUSIC.snapshot(), active=music_active(), procs=len(_ytm_procs)),
        "last_real_req": None if not _real_req_seen else int((now - _last_req) / 60),
        "ytdlp": _ytdlp_version() if HAS_YT else "-",
    }


@app.route("/status")
def health_check():
    if not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return _405()
    d = _status_data()
    if request.args.get("fmt") == "json":
        return jsonify(d), 200
    dn = "on" if (YT_DIRECT and now_ok(VIDEO.direct_off_until)) else "off"
    return (f"OK | role={ROLE} | mem={d['mem']} | up={d['up_min']}m | cookies={d['cookies']} | engine={d['engine']} | "
            f"direct={dn} | heat={d['heat']} | video_block={d['video']['block_left']}s | music_block={d['music']['block_left']}s | "
            f"last_real_req={'none' if d['last_real_req'] is None else str(d['last_real_req']) + 'm ago'}"), 200


def now_ok(until):
    return time.monotonic() >= until


@app.route("/cookies", methods=["GET", "POST"])
def cookies_update():
    if not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return "forbidden - them ?k=BR_TOKEN vao URL", 403
    if request.method == "GET":
        return f"cookies: {COOKIES.info()}", 200
    data = request.get_data(cache=False)[:600000]
    txt = data.decode("utf-8", "replace")
    if "youtube.com" not in txt or "\t" not in txt:
        return "noi dung khong giong cookies.txt (Netscape) cua youtube.com", 400
    COOKIES.update(txt)
    VIDEO.reset("cookies moi")
    MUSIC.reset("cookies moi")
    return f"da cap nhat cookies: {len(txt)} bytes", 200


@app.route("/dbg")
def esp_exit_report():
    def _c(k, n=100):
        v = (request.args.get(k) or "?")[:n]
        return "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in v)
    print(f"[esp-exit] why={_c('why', 120)} res={_c('res', 4)} played={_c('ms', 10)}ms recv={_c('kb', 10)}KB "
          f"rssi={_c('rssi', 6)} heap={_c('heap', 10)} wifi={_c('wst', 4)} "
          f"drops={_c('drops', 5)} dwhy={_c('dwhy', 3)}(1=connected()false 2=wifi 3=stall 4=doc loi) di=[{_c('di', 110)}] | {_who()}",
          flush=True)
    if request.args.get("end") == "1" and request.args.get("sid"):
        _sess_drop(request.args.get("sid"), "ESP bao thoat han")
    return Response("", 204)


@app.route("/app")
def app_switch():
    n = (request.args.get("n") or "").strip().lower()
    if n in ("none", "home"):
        video_kill_all("ESP ve man hinh chinh")
        music_kill_all()
        ARB.fg = ""
        ARB.trim_caches()
        _free_ram()
    elif n in ("video", "music", "web", "browser"):
        ARB.switch_hard(n)
    else:
        return Response("n=video|music|web|none", 400)
    return Response("", 204)

_TR_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=vi&dt=t&q="
_tr_cache = {}
_tr_lock = threading.Lock()
_tr_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)

def _tr_one(text):
    with _tr_lock:
        hit = _tr_cache.get(text)
    if hit is not None:
        return hit
    req = urllib.request.Request(_TR_URL + urllib.parse.quote(text, safe=""), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    segs = data[0] or []
    first = ((segs[0][0] if segs else "") or "").strip()
    rest = "".join((sg[0] or "") for sg in segs[1:]).strip()
    res = (first, rest)
    if first:
        with _tr_lock:
            if len(_tr_cache) >= 120:
                _tr_cache.pop(next(iter(_tr_cache)), None)
            _tr_cache[text] = res
    return res

@app.route("/tr", methods=["POST"])
def translate_batch():
    body = request.get_json(silent=True)
    q = body.get("q") if isinstance(body, dict) else None
    if not isinstance(q, list):
        return jsonify({"r": []})
    qs = [str(x)[:900] for x in q[:16]]
    if not qs:
        return jsonify({"r": []})
    futs = [_tr_pool.submit(_tr_one, q) for q in qs]
    concurrent.futures.wait(futs, timeout=9)
    out = []
    for f in futs:
        try:
            t, sn = f.result(timeout=0) if f.done() else ("", "")
        except Exception:
            t, sn = "", ""
        out.append({"t": t, "s": sn})
    return jsonify({"r": out})


# ===== /gs: relay goi Tavily thay thiet bi (cache + dich san sang tieng Viet) =====
TAVILY_KEY = _env_str("TAVILY_API_KEY")
_GS_TTL = max(0, _env_int("GS_CACHE_TTL", 600))
_GS_MAX = max(8, _env_int("GS_CACHE_MAX", 64))
_gs_cache = collections.OrderedDict()
_gs_lock = threading.Lock()
_gs_fast_ok = True


def _gs_clean(s, mx):
    s = " ".join(str(s or "").split())
    b = s.encode("utf-8")
    if len(b) <= mx:
        return s
    return b[:mx].decode("utf-8", "ignore").rstrip()


def _gs_clip_words(s, mx):
    if len(s) <= mx:
        return s
    cut = s.rfind(" ", 0, mx)
    return s[:cut if cut > mx // 2 else mx]


def _gs_call(query, n, depth):
    body = json.dumps({"query": query, "max_results": n, "search_depth": depth, "include_answer": False,
                       "include_images": True, "include_raw_content": False}).encode("utf-8")
    req = urllib.request.Request("https://api.tavily.com/search", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + TAVILY_KEY})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return 200, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def _gs_fetch(query, n):
    """Thu toi da 1 lan lai (loi mang/5xx). Chi ha cap ultra-fast khi Tavily bao 400/422."""
    global _gs_fast_ok
    retried = False
    for _ in range(3):
        code, data = _gs_call(query, n, "ultra-fast" if _gs_fast_ok else "basic")
        if code == 200:
            return 200, data
        if _gs_fast_ok and code in (400, 422):
            _gs_fast_ok = False
            continue
        if 400 <= code < 500 and code != 408:
            return code, None
        if retried:
            return code, None
        retried = True
        time.sleep(0.15)
    return 0, None


@app.route("/gs")
def google_search():
    if not TAVILY_KEY:
        return jsonify({"e": "no_key"}), 501
    q = " ".join((request.args.get("q") or "").split())[:200]
    if not q:
        return jsonify({"r": [], "i": [], "tv": 0})
    try:
        n = max(1, min(12, int(request.args.get("n", 8))))
    except ValueError:
        n = 8
    vi = request.args.get("vi") == "1"
    key = (q.lower(), n, vi)
    if _GS_TTL:
        with _gs_lock:
            hit = _gs_cache.get(key)
            if hit and time.monotonic() - hit[0] < _GS_TTL:
                _gs_cache.move_to_end(key)
                return Response(hit[1], mimetype="application/json")
    code, data = _gs_fetch(q, n)
    if code != 200 or not isinstance(data, dict):
        return jsonify({"e": code}), 502
    rows = []
    for it in (data.get("results") or []):
        u = str(it.get("url") or "")
        if not u:
            continue
        rows.append({"u": u[:300], "t": _gs_clean(it.get("title"), 160), "s": _gs_clean(it.get("content"), 900)})
        if len(rows) >= n:
            break
    imgs = []
    for v in (data.get("images") or []):
        u = v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else "")
        imgs.append(u if isinstance(u, str) and u.startswith("http") and len(u) < 330 else "")
        if len(imgs) >= len(rows):
            break
    tv = 0
    if vi and rows:
        futs = [_tr_pool.submit(_tr_one, r["t"] + "\n" + _gs_clip_words(r["s"], 400)) for r in rows]
        concurrent.futures.wait(futs, timeout=8)
        for r, f in zip(rows, futs):
            if not f.done():
                f.cancel()
                continue
            try:
                t, sn = f.result(timeout=0)
            except Exception:
                continue
            if t:
                r["t"] = _gs_clean(t, 160)
                if sn:
                    r["s"] = _gs_clean(sn, 900)
                tv = 1
    body = json.dumps({"r": rows, "i": imgs, "tv": tv}, ensure_ascii=False, separators=(",", ":"))
    if rows and _GS_TTL and (tv or not vi):
        with _gs_lock:
            _gs_cache[key] = (time.monotonic(), body)
            while len(_gs_cache) > _GS_MAX:
                _gs_cache.popitem(last=False)
    return Response(body, mimetype="application/json")


def _prewarm_browser():
    time.sleep(2)
    try:
        if _PIL_OK:
            base = BR_FONT_PX * BR_SS
            for m in (0.8, 0.85, 1.0, 1.1, 1.2, 1.35):
                _font(base * m)
                _font(base * m, True)
        if _sess is not None:
            u = urllib.parse.urlsplit(BR_SEARCH_URL)
            _sess.head(f"{u.scheme}://{u.netloc}/", timeout=6)
        if os.environ.get("BR_CR_PREWARM", "1" if ROLES == {"browser"} else "0") == "1":
            _cr_prewarm()
        print("[br] prewarm xong", flush=True)
    except Exception as e:
        _cause = repr(getattr(e, "__cause__", None) or getattr(e, "__context__", None) or "")
        print(f"[br] prewarm loi: {type(e).__name__}: {str(e)[:200]} | goc: {_cause[:300]}", flush=True)


def _pot_selfcheck():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    server = os.path.join(home, "bgutil-ytdlp-pot-provider", "server")
    script = os.path.join(server, "src", "generate_once.ts")
    try:
        from importlib.metadata import version as _v
        print(f"[pot] plugin pip: bgutil-ytdlp-pot-provider {_v('bgutil-ytdlp-pot-provider')}", flush=True)
    except Exception as e:
        print(f"[pot] LOI: plugin pip KHONG co ({e})", flush=True)
        return
    if not os.path.isfile(script):
        print(f"[pot] LOI: khong thay script server: {script}", flush=True)
        return
    nm = os.path.join(server, "node_modules")
    cache = os.path.join(home, ".cache", "bgutil-ytdlp-pot-provider")
    cmd = ["deno", "run", "--allow-env", "--allow-net", f"--allow-ffi={nm}",
           f"--allow-write={cache}", f"--allow-read={cache},{nm}", script, "--version"]
    try:
        env = dict(os.environ, DENO_NO_PROMPT="1", DENO_NO_UPDATE_CHECK="1", FORCE_COLOR="false")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env, stdin=subprocess.DEVNULL)
        out = (r.stdout or "").strip()[:120]
        err = (r.stderr or "").strip()[-300:]
        if r.returncode == 0:
            print(f"[pot] script-deno CHAY DUOC, version={out}", flush=True)
        else:
            print(f"[pot] LOI: script-deno that bai rc={r.returncode} stdout={out} stderr={err}", flush=True)
    except Exception as e:
        print(f"[pot] LOI: khong chay duoc deno: {type(e).__name__}: {str(e)[:200]}", flush=True)


def _warmup_once():
    time.sleep(_env_float("YT_WARMUP_DELAY", 25))
    if ARB.any_active():
        return
    vid = os.environ.get("YT_WARMUP_VIDEO", "dQw4w9WgXcQ")
    t0 = time.monotonic()
    try:
        VIDEO.resolve(f"https://www.youtube.com/watch?v={vid}", _warm_height(), background=True)
        print(f"[warmup] xong sau {time.monotonic() - t0:.0f}s", flush=True)
    except Exception as e:
        print(f"[warmup] loi: {str(e)[:120]}", flush=True)


RAM_WATCH = _env_bool("RAM_WATCH", True)
RAM_SHED_FRAC = _env_float("RAM_SHED_FRAC", 0.78)
RAM_PANIC_FRAC = _env_float("RAM_PANIC_FRAC", 0.88)
RAM_IDLE_SEC = _env_float("RAM_IDLE_SEC", 75)


def _ram_watchdog():
    last_rep = 0.0
    last_shed = 0.0
    idle_done = None
    while True:
        time.sleep(2 if (_active_streams > 0 or music_busy()) else 5)
        try:
            f = _mem_frac()
            now = time.monotonic()
            if f > RAM_SHED_FRAC and now - last_shed > 5:
                last_shed = now
                ARB.trim_caches()
                _free_ram()
                if f > RAM_PANIC_FRAC:
                    ARB.relieve(f"RAM {f * 100:.0f}%")
                _ram_report(f"RAM {f * 100:.0f}% -> don cache")
            elif now - last_rep > 20 and (f > 0.55 or _active_streams > 0 or music_busy()):
                last_rep = now
                _ram_report("dinh ky")
            _idle = now - _last_req
            _live = any(not s_.dead for s_ in list(_sessions.values()))
            if (_idle > RAM_IDLE_SEC and not _live and _active_streams <= 0 and not music_busy()
                    and not _br_busy() and idle_done != _last_req):
                idle_done = _last_req
                ARB.trim_caches()
                if _cr is not None:
                    _cr_close_async(resume=False)
                _free_ram()
                _ram_report(f"nhan roi {_idle:.0f}s -> don sach")
        except Exception:
            traceback.print_exc()


_bg_started = False
_bg_lock = threading.Lock()


def _start_background_tasks():
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    if HAS_BROWSER and os.environ.get("BR_PREWARM", "1" if __name__ == "__main__" else "0") == "1":
        threading.Thread(target=_prewarm_browser, daemon=True).start()
    if os.environ.get("YT_WARMUP", "0") == "1" and HAS_VIDEO:
        threading.Thread(target=_warmup_once, daemon=True).start()
    if os.environ.get("POT_SELFCHECK", "0") == "1":
        threading.Thread(target=_pot_selfcheck, daemon=True).start()
    if RAM_WATCH:
        threading.Thread(target=_ram_watchdog, daemon=True).start()
    print(f"[power] role={ROLE} warmup={os.environ.get('YT_WARMUP', '0')} pot={'on' if POT.base else 'off'} "
          f"proxies={len(PROXIES.items)} relay_url={RELAY_URL or '-'}", flush=True)
    if HAS_YT:
        print(f"[update] yt-dlp hien tai: {_ytdlp_version()} | auto_update={AUTO_UPDATE} kiem tra khi dang dung, toi da moi {UPDATE_HOURS}h", flush=True)


if __name__ == "__main__":
    _start_background_tasks()
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
elif os.environ.get("BG_ON_IMPORT", "1") == "1":
    _start_background_tasks()
