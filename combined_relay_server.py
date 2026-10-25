import subprocess
import sys
import time
import random
import os
import traceback
import json
import re
import html
import urllib.request
import urllib.parse
import urllib.error
import threading
import fcntl
import shutil
import hmac
import socket
import struct
import ipaddress
import gc
from flask import Flask, request, Response, jsonify, g
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler

import logging
import builtins

LOG_VERBOSE = os.environ.get("LOG_VERBOSE", "0") == "1"
_LOG_KEEP = ("loi", "error", "canh bao", "khong tim thay", "blocked", "traceback", "exception", "ket thuc")
_real_print = builtins.print


def print(*args, **kwargs):
    if not LOG_VERBOSE:
        txt = " ".join(str(a) for a in args).lower()
        if not any(k in txt for k in _LOG_KEEP):
            return
    _real_print(*args, **kwargs)


if not LOG_VERBOSE:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

if not os.environ.get("MALLOC_ARENA_MAX"):
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").mallopt(-8, 2)
    except Exception:
        pass

# ==================== WARP: doi IP thoat YouTube (khong can sua Dockerfile) ====================
# Tu tai wireproxy + wgcf luc khoi dong, dang ky WARP mien phi, mo HTTP proxy 127.0.0.1:WARP_PORT
# roi dat YT_PROXY (phai chay TRUOC dong YT_PROXY = os.environ.get(...) ben duoi).
# Tat bang bien moi truong WARP=0. Neu da dat YT_PROXY thu cong thi bo qua. Loi thi chay nhu cu (khong proxy).
WARP_ON = os.environ.get("WARP", "1") == "1"
WARP_PORT = int(os.environ.get("WARP_PORT", "8118"))
_WARP_DIR = "/tmp/warp"


def _warp_log(msg):
    _real_print("[warp] " + msg, flush=True)


def _warp_fetch(url, dest, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _warp_check(port):
    """True neu proxy HTTP tren port that su di qua WARP."""
    try:
        px = f"http://127.0.0.1:{port}"
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": px, "https": px}))
        with op.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=12) as r:
            txt = r.read(4000).decode("utf-8", "replace")
        return "warp=on" in txt or "warp=plus" in txt
    except Exception:
        return False


def _warp_start():
    if not WARP_ON or os.environ.get("YT_PROXY", "").strip():
        return
    if os.environ.get("ROLE", "all").strip().lower() in ("browser", "google", "gg"):
        return
    try:
        import platform
        import tarfile
        px = f"http://127.0.0.1:{WARP_PORT}"
        if _warp_check(WARP_PORT):  # worker khoi dong lai (HUP): proxy cu van dang chay
            os.environ["YT_PROXY"] = px
            _warp_log(f"OK (dung lai proxy dang chay) -> YT_PROXY={px}")
            return
        os.makedirs(_WARP_DIR, exist_ok=True)
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if not arch:
            _warp_log(f"loi: kien truc khong ho tro: {platform.machine()}")
            return
        # Uu tien ban da cai san trong image (Dockerfile); chi tai ve /tmp neu khong co
        wp = shutil.which("wireproxy") or os.path.join(_WARP_DIR, "wireproxy")
        wg = shutil.which("wgcf") or os.path.join(_WARP_DIR, "wgcf")
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
        if not os.path.exists(wg):
            # Khong dung api.github.com (hay bi "rate limit exceeded" tren IP Render).
            # /releases/latest se redirect sang .../tag/vX.Y.Z -> lay so phien ban tu URL cuoi.
            tags = []
            try:
                req = urllib.request.Request("https://github.com/ViRb3/wgcf/releases/latest", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    m = re.search(r"/tag/(v[0-9][0-9.]*)", r.geturl())
                    if m:
                        tags.append(m.group(1))
            except Exception as e:
                _warp_log(f"canh bao: khong lay duoc phien ban wgcf moi nhat ({type(e).__name__}), dung ban ghim")
            tags.append("v2.2.22")  # ban ghim du phong
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
        conf += f"\n[http]\nBindAddress = 127.0.0.1:{WARP_PORT}\n"
        cpath = os.path.join(_WARP_DIR, "wireproxy.conf")
        with open(cpath, "w", encoding="utf-8") as fh:
            fh.write(conf)
        logf = open(os.path.join(_WARP_DIR, "wireproxy.log"), "ab")
        proc = subprocess.Popen([wp, "-c", cpath], stdout=logf, stderr=logf, stdin=subprocess.DEVNULL)
        ok = False
        for _ in range(8):
            time.sleep(1)
            if proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", WARP_PORT), timeout=1).close()
                ok = _warp_check(WARP_PORT)
                break
            except OSError:
                continue
        if ok:
            os.environ["YT_PROXY"] = px
            _warp_log(f"OK -> YT_PROXY={px}")
        else:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                with open(os.path.join(_WARP_DIR, "wireproxy.log"), "rb") as fh:
                    tail = fh.read()[-300:].decode("utf-8", "replace")
            except Exception:
                tail = ""
            _warp_log(f"loi: proxy khong len, chay khong proxy | {tail}")
    except Exception as e:
        _warp_log(f"loi: {type(e).__name__}: {str(e)[:200]}")


_warp_start()
# ==================== het khoi WARP ====================

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000


@app.errorhandler(Exception)
def _on_error(e):
    if isinstance(e, HTTPException):
        return e
    print("[error]", traceback.format_exc(), flush=True)
    return "loi may chu", 500


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
        if len(_host_cache) > 500:
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

RELAY_URL = os.environ.get("RELAY_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://tiktokcasio.onrender.com"

ROLE = os.environ.get("ROLE", "all").strip().lower()
ROLE = {"youtube": "video", "yt": "video", "google": "browser", "gg": "browser"}.get(ROLE, ROLE)

if ROLE == "browser":
    class _LazyYT:
        def __getattr__(self, name):
            import yt_dlp as _real
            globals()["yt_dlp"] = _real
            return getattr(_real, name)
    yt_dlp = _LazyYT()
else:
    import yt_dlp
STREAM_TOKEN = os.environ.get("STREAM_TOKEN", "").strip()
# Mac dinh DONG: neu khong dat STREAM_TOKEN thi /stream /search /tr tra 503 (tranh ai cung dung/giet luong).
# Chi dat ALLOW_OPEN_STREAM=1 khi chu y muon server mo cong khai.
ALLOW_OPEN_STREAM = os.environ.get("ALLOW_OPEN_STREAM", "0") == "1"
if not STREAM_TOKEN and not ALLOW_OPEN_STREAM:
    print("[config] canh bao: chua dat STREAM_TOKEN -> /stream /search /tr bi khoa (503)", flush=True)


def _tok_ok(given, expected):
    try:
        return bool(expected) and hmac.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


RELAY_PURPOSE = "chi tai ve va giai ma (ffmpeg -> MJPEG/AVI) cho thiet bi LilyGo T-Display S3 cua chu server"
_stat = {"since": time.time(), "streams": 0, "last": 0.0}
_stat_lock = threading.Lock()


@app.after_request
def _purpose_headers(resp):
    resp.headers["X-Relay-Purpose"] = "transcode-only-for-LilyGo-T-Display-S3"
    resp.headers["X-Relay-Media-Storage"] = "none"
    return resp


_last_req = time.monotonic()
KILL_ON_LEAVE = os.environ.get("KILL_ON_LEAVE", "1") == "1"  # video MOI se tat video cu
BR_KILL_ON_LEAVE = os.environ.get("BR_KILL_ON_LEAVE", "0") == "1"  # thoat/back/tap trong trinh duyet: KHONG tat stream
PARK_SEC = float(os.environ.get("PARK_SEC", "120"))  # thoat video: giu ffmpeg chay nen toi da n giay (0 = tat ngay nhu cu)
_reqbusy = 0
_reqbusy_lock = threading.Lock()


def _reqbusy_add(d):
    global _reqbusy
    with _reqbusy_lock:
        _reqbusy = max(0, _reqbusy + d)


@app.teardown_request
def _reqbusy_done(exc):
    if getattr(g, "_reqbusy_on", False):
        g._reqbusy_on = False
        _reqbusy_add(-1)


@app.before_request
def _role_gate():
    global _br_last_activity, _last_req
    p = request.path
    if p not in ("/", "/healthz", "/about", "/dbg") and not p.startswith("/_src/"):
        _last_req = time.monotonic()
        g._reqbusy_on = True
        _reqbusy_add(1)
    if ROLE == "video" and p.startswith("/br"):
        return "disabled on this server (ROLE=video)", 404
    if ROLE == "browser" and p.startswith(("/stream", "/search")):
        return "disabled on this server (ROLE=browser)", 404
    if p in ("/diag", "/myip") and not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return "forbidden - can dat BR_TOKEN tren server va them ?k=BR_TOKEN vao URL", 403
    if p.startswith(("/stream", "/search", "/tr")):
        if not STREAM_TOKEN and not ALLOW_OPEN_STREAM:
            return "chua dat STREAM_TOKEN tren server - dat bien STREAM_TOKEN hoac ALLOW_OPEN_STREAM=1", 503
        if STREAM_TOKEN and not _tok_ok(request.args.get("tk", ""), STREAM_TOKEN):
            return "forbidden - them &tk=STREAM_TOKEN vao URL", 403
    if p.startswith(("/stream", "/search")):
        # Khong giet luong dang phat khi co /search: thiet bi van duyet menu trong luc video chay.
        # Client ngat ket noi se duoc phat hien qua GeneratorExit / call_on_close (_reap).
        _br_last_activity = 0.0
        _cr_release_for_stream()
    return None


@app.route("/")
def health_check():

    return (f"OK | role={ROLE} | {RELAY_URL} | AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS} "
            f"cookies={'yes' if os.path.exists(COOKIES_FILE) else 'NO'} | browser={'token-ok' if os.environ.get('BR_TOKEN') else 'NO-TOKEN'} | engine={BR_ENGINE}{'(chromium dang chay)' if _cr is not None else ''} | build=gg3-yt3 | direct={'on' if YT_DIRECT and time.monotonic() >= _direct_off_until else 'off'} | mem={_mem_str()}"), 200

PORT = int(os.environ.get("PORT", "8000"))


@app.route("/cookies", methods=["GET", "POST"])
def cookies_update():
    global COOKIES_FILE, _yt_block_until, _yt_preferred, _nocookie_fails, _nocookie_skip_until
    if not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return "forbidden - them ?k=BR_TOKEN vao URL", 403
    if request.method == "GET":
        if os.path.exists(COOKIES_FILE):
            age = int(time.time() - os.path.getmtime(COOKIES_FILE))
            return f"cookies: {os.path.getsize(COOKIES_FILE)} bytes, cap nhat {age}s truoc", 200
        return "cookies: chua co", 200
    data = request.get_data(cache=False)[:600000]
    txt = data.decode("utf-8", "replace")
    if "youtube.com" not in txt or "\t" not in txt:
        return "noi dung khong giong cookies.txt (Netscape) cua youtube.com", 400
    target = COOKIES_FILE if COOKIES_FILE.startswith("/tmp/") or not os.path.exists(_SECRET_COOKIES) else "/tmp/cookies.txt"
    tmp = target + ".new"
    if not _resolve_gate.acquire(timeout=30):
        return "server dang ban (yt-dlp dang chay), thu lai sau it giay", 503
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(txt if txt.endswith("\n") else txt + "\n")
        os.replace(tmp, target)
        COOKIES_FILE = target
        _yt_block_until = 0.0
        _yt_preferred = None
        _nocookie_fails = 0
        _nocookie_skip_until = 0.0
        _yt_fails.clear()
        _nocookie_vids.clear()
        _fail_cache.clear()
        _neg_cache.clear()
    finally:
        _resolve_gate.release()
    return f"da cap nhat cookies: {len(txt)} bytes", 200


@app.route("/about")
def about():
    up = int(time.time() - _stat["since"])
    last = int(time.time() - _stat["last"]) if _stat["last"] else None
    body = {
        "purpose": RELAY_PURPOSE,
        "what_it_does": [
            "nhan link/ID video do thiet bi LilyGo gui len",
            "tai luong video tam thoi bang yt-dlp/ffmpeg",
            "giai ma va ha kich thuoc/chat luong (MJPEG + audio) de ESP32-S3 phat duoc",
            "truyen thang ve thiet bi, khong luu lai",
        ],
        "what_it_does_not": [
            "khong luu video/audio ra o dia (ffmpeg pipe truc tiep, bo dem trong RAM, Cache-Control: no-store); chi cookies.txt cua chu server nam tren dia",
            "khong dang lai, khong chia se, khong cong khai kho video nao",
            "khong co tai khoan nguoi dung / khong thu thap du lieu ca nhan",
        ],
        "access_control": "can token" if STREAM_TOKEN else "khong dat STREAM_TOKEN",
        "uptime_s": up,
        "streams_since_start": _stat["streams"],
        "last_stream_s_ago": last,
    }
    return jsonify(body), 200


@app.route("/healthz")
def healthz():
    return "ok", 200


def _mem_str():
    mi = _mem_info()
    return f"{mi[0]:.0f}/{mi[1]:.0f}MB" if mi else "?"


_SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(_SECRET_COOKIES):
    if not os.path.exists("/tmp/cookies.txt"):
        shutil.copyfile(_SECRET_COOKIES, "/tmp/cookies.txt")
    COOKIES_FILE = "/tmp/cookies.txt"


if os.path.exists(COOKIES_FILE):
    _sz = os.path.getsize(COOKIES_FILE)
    print(f"[cookies] TIM THAY {COOKIES_FILE} - kich thuoc {_sz} bytes")
    if _sz < 200:
        print("[cookies] CANH BAO: file qua nho, co the dan thieu noi dung hoac rong")
else:
    print(f"[cookies] KHONG TIM THAY {COOKIES_FILE} - Secret File chua duoc tao dung, hoac sai ten file")


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


YT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")


MJPEG_Q = os.environ.get("MJPEG_Q", "6")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "1")
STREAM_FAST_DECODE = os.environ.get("STREAM_FAST_DECODE", "1") == "1"
STREAM_SRC_AUTO = os.environ.get("STREAM_SRC_AUTO", "1") == "1"
STREAM_NICE = int(os.environ.get("STREAM_NICE", "5"))
STREAM_READRATE = float(os.environ.get("STREAM_READRATE", "1.4"))
STREAM_READ_BURST = float(os.environ.get("STREAM_READ_BURST", "4"))
# Gioi han "lead" (giay video da gui vuot qua thoi gian thuc). 0 = tat. Chong day du lieu don len phia truoc.
STREAM_MAX_LEAD = float(os.environ.get("STREAM_MAX_LEAD", "12"))
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
STREAM_SKIP_NOREF = os.environ.get("STREAM_SKIP_NOREF", "1") == "1"
STREAM_FPS_MAX = int(os.environ.get("STREAM_FPS_MAX", "12"))
AUDIO_RATE = os.environ.get("AUDIO_RATE", "16000")
print(f"[config] AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS}", flush=True)


YT_API_LANG = os.environ.get("YT_API_LANG", "vi")
YT_API_REGION = os.environ.get("YT_API_REGION", "VN")
YT_API_COOLDOWN = int(os.environ.get("YT_API_COOLDOWN", "3600"))
_api_block_until = 0.0


class YTApiError(Exception):
    def __init__(self, status, reason, message=""):
        super().__init__(f"HTTP {status} {reason} {message}".strip())
        self.status = status
        self.reason = reason


def _api_get(endpoint, params):
    global _api_block_until
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
        if reason == "rateLimitExceeded":
            _api_block_until = time.monotonic() + min(60, YT_API_COOLDOWN)
        elif reason in ("quotaExceeded", "dailyLimitExceeded", "keyInvalid", "keyExpired",
                        "accessNotConfigured", "forbidden", "ipRefererBlocked"):
            _api_block_until = time.monotonic() + YT_API_COOLDOWN
        raise YTApiError(e.code, reason, msg) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        _api_block_until = time.monotonic() + min(30, YT_API_COOLDOWN)
        raise YTApiError(0, "network", str(e)[:100]) from None


def _iso_duration_to_seconds(d):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", d or "")
    if not m:
        return 0
    days, hrs, mins, secs = (int(x) if x else 0 for x in m.groups())
    return days * 86400 + hrs * 3600 + mins * 60 + secs


def search_via_api(query, n):
    sp = {"part": "snippet", "type": "video", "maxResults": n, "q": query,
          "videoEmbeddable": "any", "safeSearch": "moderate",
          "fields": "items(id/videoId,snippet(title,channelTitle,liveBroadcastContent))"}
    if YT_API_LANG:
        sp["relevanceLanguage"] = YT_API_LANG
    if YT_API_REGION:
        sp["regionCode"] = YT_API_REGION
    data = _api_get("search", sp)
    items = data.get("items", [])
    ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    durations = {}
    if ids:
        try:
            vids = _api_get("videos", {"part": "contentDetails", "id": ",".join(ids),
                                       "fields": "items(id,contentDetails/duration)"})
            for v in vids.get("items", []):
                durations[v["id"]] = _iso_duration_to_seconds(v["contentDetails"].get("duration"))
        except Exception as e:
            print(f"[search] bo qua thoi luong ({str(e)[:80]})", flush=True)
    return [
        {
            "id": vid,
            "title": html.unescape(it["snippet"]["title"]),
            "channel": html.unescape(it["snippet"].get("channelTitle", "")),
            "duration": ("LIVE" if it["snippet"].get("liveBroadcastContent") == "live"
                         else format_duration(durations.get(vid))),
        }
        for it in items
        for vid in [it["id"]["videoId"]]
        if it.get("id", {}).get("videoId")
    ]


@app.route("/myip")
def myip():
    def _get(proxy=""):
        try:
            handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
            with urllib.request.build_opener(*handlers).open("https://api.ipify.org", timeout=10) as r:
                return r.read().decode().strip()
        except Exception as e:
            return f"loi: {str(e)[:120]}"
    lines = [f"IP truc tiep (Render): {_get()}"]
    if YT_PROXY:
        lines.append(f"IP qua YT_PROXY:       {_get(YT_PROXY)}")
    else:
        lines.append("YT_PROXY: chua dat")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


_search_cache = {}
_search_lock = threading.Lock()
SEARCH_TTL = int(os.environ.get("SEARCH_TTL", "600"))


def _search_cache_get(ck):
    with _search_lock:
        hit = _search_cache.get(ck)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return hit[1]
    return None


def _search_cache_put(ck, res):
    now = time.monotonic()
    with _search_lock:
        for k in [k for k, v in _search_cache.items() if now - v[0] >= SEARCH_TTL]:
            _search_cache.pop(k, None)
        if len(_search_cache) >= 100:
            for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:30]:
                _search_cache.pop(k, None)
        _search_cache[ck] = (now, res)


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()[:200]
    try:
        n = max(1, min(25, int(request.args.get("n", 5))))
    except ValueError:
        n = 5
    if not query:
        return jsonify([])
    _ck = (query.lower(), n)
    _hit = _search_cache_get(_ck)
    if _hit is not None:
        print(f"[search] CACHE HIT {query!r}", flush=True)
        return jsonify(_hit)

    if YT_API_KEY and time.monotonic() >= _api_block_until:
        try:
            res = search_via_api(query, n)
            print(f"[search] YouTube API OK: {len(res)} ket qua", flush=True)
            if res:
                _search_cache_put(_ck, res)
            warm_results(res)
            return jsonify(res)
        except Exception as e:
            _rest = _api_block_until - time.monotonic()
            extra = f" - nghi API {_rest:.0f}s" if _rest > 0 else ""
            print(f"[search] YouTube API loi ({e}){extra} - roi ve yt-dlp", flush=True)

    if time.monotonic() < _yt_block_until:
        print("[search] YouTube dang chan IP, bo qua tim kiem yt-dlp", flush=True)
        return jsonify([])

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "socket_timeout": 10,
    }
    if YT_PROXY:
        ydl_opts["proxy"] = YT_PROXY
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    results = []
    if not _resolve_gate.acquire(timeout=20):
        print("[search] yt-dlp dang ban (resolve khac), bo qua tim kiem du phong", flush=True)
        return jsonify([])
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
            for entry in (info or {}).get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                results.append({
                    "id": entry["id"],
                    "title": entry.get("title") or "",
                    "channel": entry.get("channel") or entry.get("uploader") or "",
                    "duration": ("LIVE" if entry.get("live_status") == "is_live"
                                 else format_duration(entry.get("duration"))),
                })
    except Exception as e:
        print(f"[search] error: {e}", flush=True)
        return jsonify([])
    finally:
        _resolve_gate.release()

    if results:
        _search_cache_put(_ck, results)
    warm_results(results)
    return jsonify(results)


def format_ffmpeg_headers(fmt):
    headers = fmt.get("http_headers") or {}
    if not headers:
        return ""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


_YT_ATTEMPTS_ALL = [
    # (ten, player_client, dung_cookies, dung_js_runtime)
    ("visionos",     ["visionos"],       False, False),
    ("android_vr",   ["android_vr"],     False, False),
    ("web_embedded", ["web_embedded"],   False, False),
    ("tv",           ["tv"],             True,  True),
    ("mweb",         ["mweb"],           True,  True),
    ("android",      ["android"],        False, False),
    ("runtime+ejs",  None,               True,  True),
]


def _ytdlp_client_table():
    for mod in ("yt_dlp.extractor.youtube._base", "yt_dlp.extractor.youtube"):
        try:
            m = __import__(mod, fromlist=["INNERTUBE_CLIENTS"])
            t = getattr(m, "INNERTUBE_CLIENTS", None)
            if t:
                return t
        except Exception:
            continue
    return None


def _build_attempts():
    """Chi giu client ma ban yt-dlp dang cai biet (ban cu khong biet 'visionos' thi bo qua, khong ton luot)."""
    if ROLE == "browser":
        return _YT_ATTEMPTS_ALL
    t = _ytdlp_client_table()
    out = []
    for a in _YT_ATTEMPTS_ALL:
        if t is not None and a[1] and any(c not in t for c in a[1]):
            continue
        out.append(a)
    return out or _YT_ATTEMPTS_ALL


_YT_ATTEMPTS_LITE = _YT_ATTEMPTS_ALL
_YT_ATTEMPTS = _build_attempts()
_yt_preferred = None
YT_BLOCK_BACKOFF = float(os.environ.get("YT_BLOCK_BACKOFF", "30"))
YT_BLOCK_MAX = float(os.environ.get("YT_BLOCK_MAX", "900"))
YT_MIN_GAP = float(os.environ.get("YT_MIN_GAP", "2.5"))
_block_level = 0
_yt_last_call = 0.0
_yt_pace_lock = threading.Lock()


def _yt_pace():
    global _yt_last_call
    with _yt_pace_lock:
        wait = _yt_last_call + YT_MIN_GAP * random.uniform(1.0, 1.6) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _yt_last_call = time.monotonic()
_yt_block_until = 0.0
_BLOCK_HINTS = ("not a bot", "player response", "HTTP Error 429", "Too Many Requests", "HTTP Error 403")
_AGE_HINTS = ("confirm your age", "age-restricted", "inappropriate for some users")
_OUTDATED_HINTS = ("player response", "Unable to extract", "nsig", "Signature extraction")
_HARD_BLOCK_HINTS = ("not a bot", "HTTP Error 429", "Too Many Requests")
YT_BLOCK_VIDEOS_HARD = int(os.environ.get("YT_BLOCK_VIDEOS_HARD", "2"))
YT_BLOCK_VIDEOS_SOFT = int(os.environ.get("YT_BLOCK_VIDEOS_SOFT", "3"))
YT_BLOCK_WINDOW = float(os.environ.get("YT_BLOCK_WINDOW", "180"))
YT_FAIL_TTL = float(os.environ.get("YT_FAIL_TTL", "20"))


class _RecentFails:
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


_yt_fails = _RecentFails(YT_BLOCK_WINDOW)

YT_NOCOOKIE_MAX_FAILS = int(os.environ.get("YT_NOCOOKIE_MAX_FAILS", "2"))
YT_NOCOOKIE_COOLDOWN = float(os.environ.get("YT_NOCOOKIE_COOLDOWN", "1800"))
_nocookie_fails = 0
_nocookie_skip_until = 0.0
_nocookie_vids = _RecentFails(YT_BLOCK_WINDOW)

YT_PROXY = os.environ.get("YT_PROXY", "").strip()
YT_FMT_LEAN = os.environ.get("YT_FMT_LEAN", "1") == "1"
YT_LIGHT = os.environ.get("YT_LIGHT", "1") == "1"


_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_youtube(url):
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _YT_HOSTS)


def _build_ydl_opts(fmt, client=None, use_cookies=True, runtimes=False, proxy=""):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "skip_download": True,
        "geo_bypass": True,
        "socket_timeout": 10,
        "noplaylist": True,
        "retries": 1,
        "extractor_retries": 1,
    }
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
        opts["remote_components"] = ["ejs:github"] if os.environ.get("YT_REMOTE_EJS", "1") == "1" else []
    if proxy:
        opts["proxy"] = proxy
    if use_cookies and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _extract_with(video_url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
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
            video_url_out = video_fmt["url"]
            video_headers = format_ffmpeg_headers(video_fmt)
            audio_url_out = audio_fmt["url"] if audio_fmt else None
            audio_headers = format_ffmpeg_headers(audio_fmt) if audio_fmt else ""
            return video_url_out, audio_url_out, video_headers, audio_headers

        if not info.get("url"):
            raise RuntimeError("khong lay duoc URL luong tu yt-dlp")
        return info["url"], None, format_ffmpeg_headers(info), ""


_NO_RETRY_STRONG = ("Private video", "has been removed", "not available in your country", "This video is private",
                    "members-only", "Join this channel", "live event will begin", "Premieres in",
                    "account associated with this video has been terminated")
_NO_RETRY_WEAK = ("Video unavailable", "This video is not available")
# Video bi chan theo vung: doi client yt-dlp khong giup ich, dung vong thu yt-dlp (van cho phep RapidAPI/Piped thu tiep)
_GEO_HINTS = ("not made this video available in your country", "not available in your country")
_GEO_RE = re.compile(r"available in your (?:country|region|location)|blocked it in your country|"
                     r"geo[- ]?restrict|not (?:available|accessible) in your (?:country|region)|from your location", re.I)
_geo_blocked = {}
_geo_lock = threading.Lock()
GEO_TTL = float(os.environ.get("GEO_TTL", "3600"))


def _is_geo_error(e, msg):
    if type(e).__name__ == "GeoRestrictedError" or any(h in msg for h in _GEO_HINTS) or _GEO_RE.search(msg or ""):
        return True
    ei = getattr(e, "exc_info", None)
    return bool(ei and ei[0] is not None and getattr(ei[0], "__name__", "") == "GeoRestrictedError")


def _geo_mark(vid):
    now = time.monotonic()
    with _geo_lock:
        if len(_geo_blocked) > 300:
            for k in [k for k, t in _geo_blocked.items() if now >= t]:
                _geo_blocked.pop(k, None)
        _geo_blocked[vid] = now + GEO_TTL


def _geo_known(vid):
    with _geo_lock:
        return time.monotonic() < _geo_blocked.get(vid, 0.0)


_resolve_gate = threading.Lock()
YT_GATE_WAIT = float(os.environ.get("YT_GATE_WAIT", "90"))
YT_WARM_BUDGET = float(os.environ.get("YT_WARM_BUDGET", "20"))
YT_NEG_TTL = float(os.environ.get("YT_NEG_TTL", "90"))


class ResolveError(RuntimeError):
    def __init__(self, msg, permanent=False):
        super().__init__(msg)
        self.permanent = permanent


_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/|v/))([A-Za-z0-9_-]{11})")


def _canon_url(url):
    m = _YT_ID_RE.search(url or "")
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url


RAPID_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPID_URL = os.environ.get("RAPID_URL", "https://youtube-media-downloader.p.rapidapi.com/v2/video/details").strip()
RAPID_HOST = urllib.parse.urlparse(RAPID_URL).netloc
RAPID_ONLY = os.environ.get("RAPID_ONLY", "0") == "1"


def _rapid_streams(video_id, height_cap):
    if not RAPID_KEY:
        raise ResolveError("chua dat RAPIDAPI_KEY")
    url = RAPID_URL + "?" + urllib.parse.urlencode({"videoId": video_id, "urlAccess": "normal"})
    req = urllib.request.Request(url, headers={
        "x-rapidapi-host": RAPID_HOST,
        "x-rapidapi-key": RAPID_KEY,
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read(5_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", "replace")
        raise ResolveError(f"RapidAPI HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ResolveError(f"RapidAPI loi mang: {str(e)[:100]}") from None

    vitems = [v for v in ((data.get("videos") or {}).get("items") or [])
              if isinstance(v, dict) and v.get("url")]
    aitems = [a for a in ((data.get("audios") or {}).get("items") or [])
              if isinstance(a, dict) and a.get("url")]
    cap = int(height_cap)
    hdr = f"User-Agent: {UA}\r\n"

    combined = [v for v in vitems if v.get("hasAudio") and (v.get("height") or 0) <= cap]
    if combined:
        best = max(combined, key=lambda v: (v.get("height") or 0, "avc1" in str(v.get("mimeType", ""))))
        return best["url"], None, hdr, ""

    vids = [v for v in vitems if str(v.get("mimeType", "")).startswith("video/")
            and (v.get("height") or 0) <= cap]
    if not vids:
        raise ResolveError("RapidAPI khong co luong video phu hop")
    v = max(vids, key=lambda v: ("avc1" in str(v.get("mimeType", "")), v.get("height") or 0))
    if not aitems:
        return v["url"], None, hdr, ""
    a = min(aitems, key=lambda x: x.get("size") or x.get("bitrate") or 0)
    return v["url"], a["url"], hdr, hdr


RAPID_FAST_HOST = os.environ.get("RAPID_FAST_HOST", "youtube-video-fast-downloader-24-7.p.rapidapi.com").strip()
RAPID_FAST_QUALITY = os.environ.get("RAPID_FAST_QUALITY", "18").strip()
RAPID_FAST_QUALITY_LOW = os.environ.get("RAPID_FAST_QUALITY_LOW", "17").strip()
RAPID_FILE_WAIT = float(os.environ.get("RAPID_FILE_WAIT", "45"))
RAPID_POLL = max(0.5, float(os.environ.get("RAPID_POLL", "1.5")))
RAPID_API_TIMEOUT = float(os.environ.get("RAPID_API_TIMEOUT", "40"))
RAPID_SKIP_413 = float(os.environ.get("RAPID_SKIP_413", "1800"))
RAPID_SKIP_OTHER = float(os.environ.get("RAPID_SKIP_OTHER", "90"))
RAPID_SKIP_QUOTA = float(os.environ.get("RAPID_SKIP_QUOTA", "600"))
_rapid_skip = {}
_rapid_big = {}
_rapid_skip_all = 0.0
_rapid_skip_lock = threading.Lock()


def _rapid_should_skip(video_id):
    now = time.monotonic()
    with _rapid_skip_lock:
        return now < _rapid_skip_all or now < _rapid_skip.get(video_id, 0.0)


def _rapid_note_failure(video_id, msg):
    global _rapid_skip_all
    now = time.monotonic()
    with _rapid_skip_lock:
        if "HTTP 413" in msg:
            _rapid_skip[video_id] = now + RAPID_SKIP_413
        elif any(f"HTTP {c}" in msg for c in (401, 402, 403, 429)):
            _rapid_skip_all = now + RAPID_SKIP_QUOTA
        else:
            _rapid_skip[video_id] = now + RAPID_SKIP_OTHER
        if len(_rapid_skip) > 300:
            for k in [k for k, t in _rapid_skip.items() if now >= t]:
                _rapid_skip.pop(k, None)


def _rapid_fast_file(video_id):
    if not RAPID_KEY:
        raise ResolveError("chua dat RAPIDAPI_KEY")
    qualities = [RAPID_FAST_QUALITY]
    if RAPID_FAST_QUALITY_LOW and RAPID_FAST_QUALITY_LOW not in qualities:
        qualities.append(RAPID_FAST_QUALITY_LOW)
    if len(qualities) > 1 and time.monotonic() < _rapid_big.get(video_id, 0.0):
        qualities = qualities[1:]
    data = None
    for qi, qual in enumerate(qualities):
        api = (f"https://{RAPID_FAST_HOST}/download_video/{video_id}?"
               + urllib.parse.urlencode({"quality": qual}))
        req = urllib.request.Request(api, headers={
            "x-rapidapi-host": RAPID_FAST_HOST,
            "x-rapidapi-key": RAPID_KEY,
            "User-Agent": UA,
        })
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
                if len(_rapid_big) > 300:
                    _rapid_big.clear()
                _rapid_big[video_id] = time.monotonic() + RAPID_SKIP_413
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


YT_DIRECT = os.environ.get("YT_DIRECT", "1") == "1"
YT_DIRECT_VERSION = os.environ.get("YT_DIRECT_VERSION", "1.65.10")
YT_DIRECT_TIMEOUT = float(os.environ.get("YT_DIRECT_TIMEOUT", "8"))
YT_DIRECT_MAX_FAILS = int(os.environ.get("YT_DIRECT_MAX_FAILS", "3"))
YT_DIRECT_COOLDOWN = float(os.environ.get("YT_DIRECT_COOLDOWN", "600"))
YT_AUDIO_MAX_BPS = int(os.environ.get("YT_AUDIO_MAX_BPS", "72000"))
_direct_vids = _RecentFails(float(os.environ.get("YT_DIRECT_FAIL_WINDOW", "900")))
_direct_off_until = 0.0


class _DirectFail(Exception):
    pass


YT_DIRECT_CLIENTS = [c.strip().lower() for c in os.environ.get(
    "YT_DIRECT_CLIENTS", "visionos,android_vr,ios,android").split(",") if c.strip()]
YT_DIRECT_PROBE = os.environ.get("YT_DIRECT_PROBE", "1") == "1"
YT_DIRECT_CLIENT_DEAD = float(os.environ.get("YT_DIRECT_CLIENT_DEAD", "180"))
_GEN_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/130.0.0.0 Safari/537.36")
# Gia tri du phong; neu yt-dlp dang cai co client cung ten thi lay cau hinh MOI NHAT tu yt-dlp (xem _dc_cfg)
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
_dc_dead = {}
_dc_pref = None
_yt_visitor = ""
_yt_visitor_t = 0.0


def _df(msg, hard=False):
    e = _DirectFail(msg)
    e.hard = hard
    return e


def _dc_cfg(key):
    base = _DC_STATIC.get(key)
    if not base:
        return None
    cfg = {"cid": base["cid"], "client": dict(base["client"])}
    t = _ytdlp_client_table()
    c = (t or {}).get(key)
    if c:
        try:
            for k, v in ((c.get("INNERTUBE_CONTEXT") or {}).get("client") or {}).items():
                if v not in (None, ""):
                    cfg["client"][k] = v
            cfg["cid"] = c.get("INNERTUBE_CONTEXT_CLIENT_NAME") or cfg["cid"]
        except Exception:
            pass
    return cfg


def _direct_opener():
    if YT_PROXY:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": YT_PROXY, "https": YT_PROXY}))
    return urllib.request.build_opener()


def _dc_probe(url, ua, opener, key):
    """googlevideo hay tra 403 o client nay du player tra URL OK -> thu 2 byte truoc khi giao cho ffmpeg."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Range": "bytes=0-1"})
    try:
        with opener.open(req, timeout=6) as r:
            r.read(2)
    except urllib.error.HTTPError as e:
        raise _df(f"{key}: googlevideo HTTP {e.code}", hard=True) from None
    except (urllib.error.URLError, OSError) as e:
        raise _df(f"{key}: probe loi {str(e)[:50]}", hard=True) from None


def _hls_pick(master, ua, opener, cap):
    """Chon 1 bien the HLS co tieng (muxed) <= cap de ffmpeg khong tai ban nang nhat."""
    req = urllib.request.Request(master, headers={"User-Agent": ua})
    try:
        with opener.open(req, timeout=YT_DIRECT_TIMEOUT) as r:
            txt = r.read(600000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise _df(f"hls HTTP {e.code}", hard=True) from None
    except (urllib.error.URLError, OSError) as e:
        raise _df(f"hls loi mang: {str(e)[:50]}", hard=True) from None
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


def _pick_direct_formats(data, height_cap):
    sd = (data or {}).get("streamingData") or {}
    cap = int(height_cap)

    def mime(f):
        return str(f.get("mimeType", ""))

    def usable(lst):
        return [f for f in (lst or []) if isinstance(f, dict) and isinstance(f.get("url"), str) and f["url"].startswith("http")]

    ada = usable(sd.get("adaptiveFormats"))
    vids = [f for f in ada if mime(f).startswith("video/")]
    auds = [f for f in ada if mime(f).startswith("audio/")]

    def fps_ok(f):
        return (f.get("fps") or 30) <= 30

    pool = ([f for f in vids if "avc1" in mime(f) and fps_ok(f) and (f.get("height") or 0) <= cap]
            or [f for f in vids if fps_ok(f) and (f.get("height") or 0) <= cap]
            or [f for f in vids if (f.get("height") or 0) <= cap])
    if pool:
        v = max(pool, key=lambda f: (f.get("height") or 0, "avc1" in mime(f), -(f.get("bitrate") or 0)))
    elif vids:
        v = min(vids, key=lambda f: (f.get("height") or 99999, f.get("bitrate") or 0))
    else:
        v = None

    if v is not None:
        a = None
        if auds:
            def abr(f):
                return f.get("averageBitrate") or f.get("bitrate") or 0
            low = [f for f in auds if abr(f) <= YT_AUDIO_MAX_BPS]
            a = (max(low, key=lambda f: ("mp4a" in mime(f), abr(f))) if low
                 else min(auds, key=abr))
        return v["url"], (a["url"] if a else None)

    comb = [f for f in usable(sd.get("formats")) if mime(f).startswith("video/")]
    comb = [f for f in comb if (f.get("height") or 0) <= cap] or comb
    if comb:
        return max(comb, key=lambda f: -abs((f.get("height") or 0) - cap))["url"], None
    return None


def _dc_try(key, video_id, height_cap, opener):
    global _yt_visitor, _yt_visitor_t
    cfg = _dc_cfg(key)
    cl = dict(cfg["client"])
    cl.setdefault("hl", "en")
    cl.setdefault("gl", "US")
    ua = cl.get("userAgent") or _GEN_UA
    if _yt_visitor and time.monotonic() - _yt_visitor_t < 21600:
        cl["visitorData"] = _yt_visitor
    body = {
        "context": {"client": cl},
        "videoId": video_id,
        "contentCheckOk": True, "racyCheckOk": True,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
    }
    hd = {"Content-Type": "application/json", "User-Agent": ua, "Origin": "https://www.youtube.com",
          "X-YouTube-Client-Name": str(cfg["cid"]), "X-YouTube-Client-Version": str(cl.get("clientVersion", ""))}
    if cl.get("visitorData"):
        hd["X-Goog-Visitor-Id"] = cl["visitorData"]
    req = urllib.request.Request("https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                                 data=json.dumps(body).encode("utf-8"), method="POST", headers=hd)
    try:
        with opener.open(req, timeout=YT_DIRECT_TIMEOUT) as r:
            data = json.loads(r.read(3_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise _df(f"{key}: HTTP {e.code}", hard=True) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _df(f"{key}: loi mang/JSON: {str(e)[:60]}", hard=True) from None
    if not isinstance(data, dict):
        raise _df(f"{key}: phan hoi khong phai JSON object", hard=True)
    vd = (data.get("responseContext") or {}).get("visitorData")
    if isinstance(vd, str) and vd and (not _yt_visitor or time.monotonic() - _yt_visitor_t >= 21600):
        _yt_visitor, _yt_visitor_t = vd, time.monotonic()
    ps = data.get("playabilityStatus") or {}
    if ps.get("status") != "OK":
        why = f"{ps.get('status', '?')}: {str(ps.get('reason') or '')[:100]}"
        if any(h in why for h in _NO_RETRY_STRONG):
            raise ResolveError(why, permanent=True)
        raise _df(f"{key}: {why}", hard=(ps.get("status") == "LOGIN_REQUIRED"))
    hdr = f"User-Agent: {ua}\r\n"
    pick = _pick_direct_formats(data, height_cap)
    if pick:
        if YT_DIRECT_PROBE:
            _dc_probe(pick[0], ua, opener, key)
        return pick[0], pick[1], hdr, (hdr if pick[1] else "")
    hls = (data.get("streamingData") or {}).get("hlsManifestUrl")
    if isinstance(hls, str) and hls.startswith("http"):
        return _hls_pick(hls, ua, opener, int(height_cap)), None, hdr, ""
    raise _df(f"{key}: khong co luong co URL (can PO token / SABR?)")


def _direct_streams(video_id, height_cap):
    """Thu lan luot cac client InnerTube (client chay duoc lan truoc dung dau). Tra (v_url, a_url, v_hdr, a_hdr)."""
    global _dc_pref
    opener = _direct_opener()
    keys = [k for k in YT_DIRECT_CLIENTS if k in _DC_STATIC]
    now = time.monotonic()
    live = [k for k in keys if now >= _dc_dead.get(k, 0.0)] or keys
    if _dc_pref in live:
        live.sort(key=lambda k: k != _dc_pref)
    errs = []
    for k in live:
        try:
            res = _dc_try(k, video_id, height_cap, opener)
            _dc_pref = k
            _dc_dead.pop(k, None)
            return res
        except _DirectFail as e:
            errs.append(str(e)[:90])
            if getattr(e, "hard", False):
                _dc_dead[k] = time.monotonic() + YT_DIRECT_CLIENT_DEAD
    raise _DirectFail(" | ".join(errs)[:300] or "khong co client truc tiep nao")


def _try_direct(video_id, height_cap):
    global _direct_off_until, _yt_block_until, _block_level
    if not YT_DIRECT or time.monotonic() < _direct_off_until:
        return None
    t0 = time.monotonic()
    try:
        res = _direct_streams(video_id, height_cap)
    except _DirectFail as e:
        n = _direct_vids.add(video_id)
        print(f"[direct] {video_id} that bai: {e} ({n} video khac nhau lien tiep)", flush=True)
        if n >= YT_DIRECT_MAX_FAILS:
            _direct_off_until = time.monotonic() + YT_DIRECT_COOLDOWN
            print(f"[direct] that bai tren {n} video -> tat duong truc tiep {YT_DIRECT_COOLDOWN:.0f}s (dung yt-dlp)", flush=True)
        return None
    except ResolveError:
        raise
    except Exception as e:
        print(f"[direct] loi khong du kien: {type(e).__name__}: {str(e)[:100]}", flush=True)
        return None
    _direct_vids.clear()
    _yt_fails.clear()
    _yt_block_until = 0.0
    _block_level = 0
    print(f"[direct] {video_id} OK sau {time.monotonic() - t0:.1f}s", flush=True)
    return res


WARM_RAPID = os.environ.get("WARM_RAPID", "0") == "1"


RAPID_STREAMS_ON = os.environ.get("RAPID_STREAMS", "1") == "1"
_rs_off_until = 0.0
ALT_ON = os.environ.get("ALT_FRONTENDS", "1") == "1"
PIPED_API = [x.strip().rstrip("/") for x in os.environ.get(
    "PIPED_API", "https://pipedapi.kavin.rocks,https://pipedapi.adminforge.de,https://api.piped.private.coffee"
).split(",") if x.strip()]
INVIDIOUS_API = [x.strip().rstrip("/") for x in os.environ.get(
    "INVIDIOUS_API", "https://inv.nadeko.net,https://yewtu.be"
).split(",") if x.strip()]
ALT_TIMEOUT = float(os.environ.get("ALT_TIMEOUT", "8"))
ALT_BUDGET = float(os.environ.get("ALT_BUDGET", "25"))
ALT_COOLDOWN = float(os.environ.get("ALT_COOLDOWN", "300"))
_alt_dead = {}
_alt_lock = threading.Lock()


def _rapid_streams_safe(video_id, height_cap):
    """Goi _rapid_streams (host tra URL luong, khong tao file -> khong dinh 413)."""
    global _rs_off_until
    try:
        return _rapid_streams(video_id, height_cap)
    except ResolveError as e:
        if any(f"HTTP {c}" in str(e) for c in (401, 402, 403, 429)):
            _rs_off_until = time.monotonic() + RAPID_SKIP_QUOTA
        raise


def _alt_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=ALT_TIMEOUT) as r:
        return json.loads(r.read(3_000_000).decode("utf-8", "replace"))


def _height_of(item):
    h = item.get("height")
    if isinstance(h, int):
        return h
    for k in ("resolution", "qualityLabel", "quality"):
        mm = re.match(r"\s*(\d{3,4})p", str(item.get(k) or ""))
        if mm:
            return int(mm.group(1))
    return 0


def _pick_streams(video_items, audio_items, cap):
    """video_items: dict(url, height, video_only, mime). audio_items: dict(url, bitrate)."""
    hdr = f"User-Agent: {UA}\r\n"
    comb = [v for v in video_items if not v["video_only"] and 0 < v["height"] <= cap]
    if comb:
        best = max(comb, key=lambda v: (v["height"], "avc1" in v["mime"] or "mp4" in v["mime"]))
        return best["url"], None, hdr, ""
    vids = [v for v in video_items if v["video_only"] and 0 < v["height"] <= cap]
    if not vids or not audio_items:
        raise ResolveError("khong co luong phu hop")
    v = max(vids, key=lambda v: ("avc1" in v["mime"], v["height"]))
    a = min(audio_items, key=lambda x: x.get("bitrate") or 0)
    return v["url"], a["url"], hdr, hdr


def _piped_try(base, video_id, cap):
    d = _alt_get_json(f"{base}/streams/{video_id}")
    if not isinstance(d, dict) or d.get("error"):
        raise ResolveError(str((d or {}).get("message") if isinstance(d, dict) else d)[:80])
    vi = [{"url": s["url"], "height": _height_of(s), "video_only": bool(s.get("videoOnly")),
           "mime": str(s.get("mimeType", "")) + str(s.get("codec", ""))}
          for s in (d.get("videoStreams") or []) if isinstance(s, dict) and s.get("url")]
    ai = [{"url": s["url"], "bitrate": s.get("bitrate") or 0}
          for s in (d.get("audioStreams") or []) if isinstance(s, dict) and s.get("url")]
    return _pick_streams(vi, ai, cap)


def _invidious_try(base, video_id, cap):
    d = _alt_get_json(f"{base}/api/v1/videos/{video_id}?local=true")
    if not isinstance(d, dict) or d.get("error"):
        raise ResolveError(str((d or {}).get("error") if isinstance(d, dict) else d)[:80])
    vi = []
    for s in (d.get("formatStreams") or []):
        if isinstance(s, dict) and s.get("url"):
            vi.append({"url": s["url"], "height": _height_of(s), "video_only": False, "mime": str(s.get("type", ""))})
    ai = []
    for s in (d.get("adaptiveFormats") or []):
        if not (isinstance(s, dict) and s.get("url")):
            continue
        t = str(s.get("type", ""))
        if t.startswith("video/"):
            vi.append({"url": s["url"], "height": _height_of(s), "video_only": True, "mime": t})
        elif t.startswith("audio/"):
            ai.append({"url": s["url"], "bitrate": int(s.get("bitrate") or 0)})
    return _pick_streams(vi, ai, cap)


def _alt_streams(video_id, height_cap):
    """Thu lan luot cac instance Piped/Invidious (free, khong can key). Tra cung dinh dang nhu _rapid_streams."""
    cap = int(height_cap)
    t0 = time.monotonic()
    errs = []
    cands = [("piped", b, _piped_try) for b in PIPED_API] + [("invidious", b, _invidious_try) for b in INVIDIOUS_API]
    for kind, base, fn in cands:
        if time.monotonic() - t0 > ALT_BUDGET:
            break
        with _alt_lock:
            if time.monotonic() < _alt_dead.get(base, 0.0):
                continue
        try:
            res = fn(base, video_id, cap)
            if not (_url_is_public(res[0]) and (not res[1] or _url_is_public(res[1]))):
                raise ResolveError("URL khong hop le")
            print(f"[alt] {kind} {base} OK cho {video_id}", flush=True)
            return res
        except Exception as e:
            with _alt_lock:
                _alt_dead[base] = time.monotonic() + ALT_COOLDOWN
            errs.append(f"{kind}:{str(e)[:50]}")
    raise ResolveError("khong instance Piped/Invidious nao dung duoc: " + " | ".join(errs)[:300])


def _rapid_urls_usable(res):
    """Thu 2 byte tu moi URL RapidAPI tra ve. 401/403/410 = URL khoa theo IP/het han (khong dung duoc tu Render);
    loi mang tam thoi thi van coi la dung duoc de khong bo nham."""
    for u in (res[0], res[1] if len(res) > 1 else None):
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


YT_FIRST = os.environ.get("YT_FIRST", "1") == "1"
YT_FIRST_BUDGET = float(os.environ.get("YT_FIRST_BUDGET", "25"))
YT_FIRST_GATE = float(os.environ.get("YT_FIRST_GATE", "5"))


# Chay dua giua 2 NGUON khac nhau (yt-dlp va RapidAPI file) thay vi doi yt-dlp that bai roi moi thu RapidAPI.
# Log that: yt-dlp 33-70s, RapidAPI file 10-50s. Tong tuan tu = ~50-100s, chay dua = min(). RACE_RAPID=0 de tat.
# Luu y: moi lan chay dua se ton 1 luot RapidAPI ke ca khi yt-dlp len truoc.
RACE_RAPID = os.environ.get("RACE_RAPID", "1") == "1"


def _race_ytdlp_rapid(video_url, height_cap, vid):
    box = {"res": None, "src": None, "yt_err": None, "rapid_err": None, "yt_done": False, "rapid_done": False}
    cv = threading.Condition()
    t0 = time.monotonic()

    def _finish(key_err, key_done, err):
        with cv:
            box[key_err] = err
            box[key_done] = True
            cv.notify_all()

    def _yt():
        err = None
        try:
            if _resolve_gate.acquire(timeout=YT_FIRST_GATE):
                try:
                    r = _resolve_stream_urls_inner(video_url, height_cap, YT_FIRST_BUDGET, False)
                    with cv:
                        if box["res"] is None:
                            box["res"], box["src"] = r, "yt-dlp"
                            cv.notify_all()
                finally:
                    _resolve_gate.release()
            else:
                err = ResolveError("yt-dlp bo qua: dang co resolve khac")
        except Exception as e:
            err = e
        _finish("yt_err", "yt_done", err)

    def _rp():
        err = None
        try:
            if _rapid_should_skip(vid):
                err = ResolveError("bo qua RapidAPI (vua that bai)")
            else:
                r = _rapid_fast_file(vid)
                with cv:
                    if box["res"] is None:
                        box["res"], box["src"] = r, "rapidapi"
                        cv.notify_all()
        except ResolveError as e:
            _rapid_note_failure(vid, str(e))
            err = e
        except Exception as e:
            err = e
        _finish("rapid_err", "rapid_done", err)

    threading.Thread(target=_yt, daemon=True).start()
    threading.Thread(target=_rp, daemon=True).start()
    deadline = t0 + max(YT_FIRST_BUDGET + 60.0, RAPID_FILE_WAIT + RAPID_API_TIMEOUT + 10.0)
    with cv:
        while box["res"] is None and not (box["yt_done"] and box["rapid_done"]):
            ye = box["yt_err"]
            if isinstance(ye, ResolveError) and ye.permanent:
                break  # video rieng tu/bi go: khong can cho RapidAPI
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            cv.wait(min(rem, 1.0))
    if box["res"] is not None:
        print(f"[race] {box['src']} thang sau {time.monotonic() - t0:.1f}s cho {vid}", flush=True)
    else:
        print(f"[race] ca 2 nguon that bai sau {time.monotonic() - t0:.1f}s cho {vid}", flush=True)
    return box


def _resolve_stream_urls_uncached(video_url, height_cap, background=False):
    global _rs_off_until
    m = _YT_ID_RE.search(video_url or "")
    is_yt = bool(m and _is_youtube(video_url))
    yt_first_tried = False
    yt_first_err = None
    race_ran = False
    race_rapid_err = None
    if is_yt and _geo_known(m.group(1)):
        # Da biet video bi chan theo vung: khong ton ~30s thu yt-dlp nua, di thang RapidAPI/Piped
        print(f"[resolve] {m.group(1)} bi chan theo vung (da biet) -> bo qua yt-dlp", flush=True)
        yt_first_tried = True
        yt_first_err = ResolveError("video bi chan theo vung (khong phat duoc o vung may chu)")
    elif is_yt and RACE_RAPID and RAPID_KEY and YT_FIRST and not background:
        _bx = _race_ytdlp_rapid(video_url, height_cap, m.group(1))
        if _bx["res"] is not None:
            return _bx["res"]
        race_ran = True
        yt_first_tried = True
        yt_first_err = _bx["yt_err"]
        race_rapid_err = _bx["rapid_err"]
        if isinstance(yt_first_err, ResolveError) and yt_first_err.permanent:
            raise yt_first_err
    elif is_yt and YT_FIRST and not background:
        # Thu yt-dlp truoc (chat luong cao theo height_cap); that bai thi roi xuong direct/RapidAPI
        if _resolve_gate.acquire(timeout=YT_FIRST_GATE):
            yt_first_tried = True
            t_yf = time.monotonic()
            try:
                res = _resolve_stream_urls_inner(video_url, height_cap, YT_FIRST_BUDGET, False)
                print(f"[resolve] yt-dlp (thu truoc) OK sau {time.monotonic() - t_yf:.1f}s cho {m.group(1)}", flush=True)
                return res
            except Exception as e:
                yt_first_err = e
                if isinstance(e, ResolveError) and e.permanent:
                    print(f"[resolve] yt-dlp (thu truoc) loi vinh vien, khong thu cach khac: {str(e)[:200]}", flush=True)
                    raise
                print(f"[resolve] yt-dlp (thu truoc) that bai sau {time.monotonic() - t_yf:.1f}s: {str(e)[:200]} -> thu RapidAPI", flush=True)
            finally:
                _resolve_gate.release()
        else:
            print("[resolve] yt-dlp (thu truoc) bo qua: dang co resolve khac", flush=True)
    if is_yt:
        _d = _try_direct(m.group(1), height_cap)
        if _d:
            return _d
    use_rapid = bool(RAPID_KEY and is_yt) and (WARM_RAPID or not background) and not race_ran
    rapid_err = race_rapid_err

    if use_rapid:
        vid = m.group(1)
        if RAPID_STREAMS_ON and time.monotonic() >= _rs_off_until:
            try:
                res = _rapid_streams_safe(vid, height_cap)
                if not _rapid_urls_usable(res):
                    # URL googlevideo bi khoa theo IP cua RapidAPI -> Render nhan 403, ffmpeg khong ra du lieu
                    _rs_off_until = time.monotonic() + RAPID_SKIP_QUOTA
                    raise ResolveError("RapidAPI streams: URL bi khoa theo IP (googlevideo 403), thu cach khac")
                print(f"[resolve] RapidAPI (streams) OK cho {vid}", flush=True)
                return res
            except ResolveError as e:
                print(f"[resolve] RapidAPI streams that bai: {str(e)[:200]}", flush=True)
                rapid_err = e
        if _rapid_should_skip(vid):
            print(f"[resolve] bo qua RapidAPI file cho {vid} (vua that bai)", flush=True)
        else:
            try:
                return _rapid_fast_file(vid)
            except ResolveError as e:
                print(f"[resolve] RapidAPI that bai: {str(e)[:200]}", flush=True)
                _rapid_note_failure(vid, str(e))
                rapid_err = e

    if is_yt and ALT_ON and not background:
        try:
            return _alt_streams(m.group(1), height_cap)
        except ResolveError as e:
            print(f"[resolve] alt (Piped/Invidious) that bai: {str(e)[:200]}", flush=True)

    if yt_first_tried:
        # yt-dlp da thu o dau ham, khong lap lai (tranh ton them thoi gian)
        raise rapid_err or yt_first_err or ResolveError("khong cach nao chay")

    if use_rapid and (background or RAPID_ONLY):
        raise rapid_err or ResolveError("RapidAPI khong dung duoc (RAPID_ONLY=1)")

    if background:
        if not _resolve_gate.acquire(blocking=False):
            raise ResolveError("busy (dang co resolve khac)")
    elif not _resolve_gate.acquire(timeout=YT_GATE_WAIT):
        raise ResolveError(f"server dang ban, cho qua {YT_GATE_WAIT:.0f}s")
    try:
        return _resolve_stream_urls_inner(video_url, height_cap, YT_WARM_BUDGET if background else None, background)
    finally:
        _resolve_gate.release()


# Chay dua (race): thu nhieu client yt-dlp CUNG LUC, cai nao xong truoc thi dung. YT_RACE_PAR=1 de quay ve tuan tu.
# Moi lan chua co "cach tot" nao duoc nho thi dua YT_RACE_FIRST (mac dinh mweb) len dau.
YT_RACE_PAR = max(1, int(os.environ.get("YT_RACE_PAR", "1")))
YT_RACE_FIRST = os.environ.get("YT_RACE_FIRST", "mweb").strip()


def _resolve_stream_urls_inner(video_url, height_cap, budget=None, background=False):
    global _yt_preferred, _nocookie_fails, _nocookie_skip_until, _yt_block_until, _block_level
    fmt = (
        f"bestvideo[height<={height_cap}][vcodec^=avc1]+bestaudio"
        f"/bestvideo[height<={height_cap}]+bestaudio"
        f"/best[height<={height_cap}]"
        f"/best"
    )
    if YT_FMT_LEAN:
        fmt = (f"bestvideo[height<={height_cap}][vcodec^=avc1][fps<=30]+bestaudio[abr<=72]"
               f"/bestvideo[height<={height_cap}][vcodec^=avc1][fps<=30]+bestaudio/") + fmt
    is_yt = _is_youtube(video_url)
    if not is_yt:
        return _extract_with(video_url, _build_ydl_opts(fmt, use_cookies=True))
    _left = _yt_block_until - time.monotonic()
    if _left > 0:
        raise ResolveError(f"YouTube dang chan IP - tam nghi them {_left:.0f}s (YT_BLOCK_BACKOFF) de khong bi chan nang hon")

    attempts = list(_YT_ATTEMPTS)
    _pc = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
    if _pc:
        attempts.insert(0, ("env", _pc, True, True))
    if _yt_preferred:
        attempts.sort(key=lambda a: a[0] != _yt_preferred)
    _vm = _YT_ID_RE.search(video_url or "")
    vkey = _vm.group(1) if _vm else video_url
    if _nocookie_skip_until and time.monotonic() >= _nocookie_skip_until:
        _nocookie_skip_until = 0.0
        _nocookie_fails = 0
        _nocookie_vids.clear()
    if os.path.exists(COOKIES_FILE) and (time.monotonic() < _nocookie_skip_until or background):
        with_cookies = [a for a in attempts if a[2]]
        if with_cookies:
            attempts = with_cookies
            print("[resolve] tam bo cac cach khong cookies (that bai lien tiep), thu cach co cookies", flush=True)

    if budget is None:
        budget = float(os.environ.get("YT_RESOLVE_BUDGET", "45"))
    t_start = time.monotonic()
    errors = []
    permanent = False
    weak_hits = 0
    pending = list(attempts)
    if not _yt_preferred and YT_RACE_FIRST:
        pending.sort(key=lambda a: a[0] != YT_RACE_FIRST)  # sort on dinh: giu nguyen thu tu con lai
    race = {"res": None, "name": None, "cookies": False, "running": 0, "stop": False}
    race_lock = threading.Lock()
    race_wake = threading.Event()

    def _race_worker(name, client, use_cookies, runtimes):
        nonlocal permanent, weak_hits
        global _nocookie_fails, _nocookie_skip_until
        ck = None
        try:
            _yt_pace()  # van giu khoang cach toi thieu giua cac request len YouTube
            with race_lock:
                if race["stop"] or race["res"] is not None:
                    return
            opts = _build_ydl_opts(fmt, client, use_cookies, runtimes, YT_PROXY)
            if YT_RACE_PAR > 1 and opts.get("cookiefile"):
                # yt-dlp ghi lai cookiefile khi dong -> moi luong 1 ban sao rieng, tranh ghi de nhau
                try:
                    ck = "/tmp/ytck_%d_%d.txt" % (os.getpid(), threading.get_ident())
                    shutil.copyfile(opts["cookiefile"], ck)
                    opts["cookiefile"] = ck
                except OSError:
                    ck = None
            res = _extract_with(video_url, opts)
            with race_lock:
                if race["res"] is None:
                    race["res"], race["name"], race["cookies"] = res, name, use_cookies
        except Exception as e:
            msg = str(e)
            print(f"[resolve] cach '{name}' that bai: {msg[:140]}", flush=True)
            with race_lock:
                errors.append(f"{name}: {msg[:110]}")
                if not background and not use_cookies and ("player response" in msg or "not a bot" in msg):
                    _nocookie_fails = _nocookie_vids.add(vkey)
                    if _nocookie_fails >= YT_NOCOOKIE_MAX_FAILS:
                        _nocookie_skip_until = time.monotonic() + YT_NOCOOKIE_COOLDOWN
                if _is_geo_error(e, msg):
                    # Chan theo vung KHONG phai loi vinh vien: RapidAPI/Piped o vung khac van co the phat duoc
                    print("[resolve] video bi chan theo vung -> bo cac client yt-dlp con lai", flush=True)
                    if _vm:
                        _geo_mark(_vm.group(1))
                    race["stop"] = True
                elif any(h in msg for h in _NO_RETRY_STRONG):
                    permanent = True
                    race["stop"] = True
                elif any(h in msg for h in _NO_RETRY_WEAK):
                    weak_hits += 1
                    if weak_hits >= 2:
                        permanent = True
                        race["stop"] = True
        finally:
            if ck:
                try:
                    os.remove(ck)
                except OSError:
                    pass
            with race_lock:
                race["running"] -= 1
            race_wake.set()

    hard_end = t_start + budget + 60
    while True:
        job = None
        with race_lock:
            if race["res"] is not None or race["stop"]:
                break
            if pending and race["running"] < YT_RACE_PAR and not (errors and time.monotonic() - t_start > budget):
                job = pending.pop(0)
                race["running"] += 1
            elif race["running"] == 0:
                break
        if time.monotonic() > hard_end:
            break
        if job is not None:
            threading.Thread(target=_race_worker, args=job, daemon=True).start()
            continue
        race_wake.wait(0.5)
        race_wake.clear()

    if race["res"] is not None:
        name, use_cookies = race["name"], race["cookies"]
        if _yt_preferred != name:
            print(f"[resolve] cach '{name}' thanh cong", flush=True)
        print(f"[resolve] race xong sau {time.monotonic() - t_start:.1f}s bang '{name}' (song song toi da {YT_RACE_PAR})", flush=True)
        _yt_preferred = name
        _yt_fails.clear()
        _block_level = 0
        if not use_cookies:
            _nocookie_fails = 0
            _nocookie_skip_until = 0.0
            _nocookie_vids.clear()
        return race["res"]
    if not permanent and not background:
        _yt_preferred = None
        _joined = " | ".join(errors)
        if any(h in _joined for h in _BLOCK_HINTS) and not any(h in _joined for h in _AGE_HINTS):
            n_vids = _yt_fails.add(vkey)
            need = YT_BLOCK_VIDEOS_HARD if any(h in _joined for h in _HARD_BLOCK_HINTS) else YT_BLOCK_VIDEOS_SOFT
            if n_vids >= need:
                _wait = min(YT_BLOCK_MAX, YT_BLOCK_BACKOFF * 2 ** min(_block_level, 5)) * random.uniform(0.9, 1.3)
                _block_level += 1
                _yt_block_until = time.monotonic() + _wait
                print(f"[resolve] {n_vids} video khac nhau that bai lien tiep -> nghi {_wait:.0f}s (IP co the bi chan, lan {_block_level})", flush=True)
            else:
                print(f"[resolve] video {vkey} that bai ({n_vids}/{need} video khac nhau) - coi la loi RIENG cua video, khong khoa server", flush=True)
    raise ResolveError(" | ".join(errors)[:700] or "khong cach nao chay", permanent=permanent)


RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", "5400"))
WARM_TOP_N = int(os.environ.get("WARM_TOP_N", "3" if YT_DIRECT else "1"))
_resolve_cache = {}
_neg_cache = {}
_fail_cache = {}
_resolve_locks = {}
_resolve_guard = threading.Lock()
_active_streams = 0
_active_lock = threading.Lock()
_cur_proc = None


def _streams_add(d):
    global _active_streams
    with _active_lock:
        _active_streams = max(0, _active_streams + d)

_cur_lock = threading.Lock()


def _who():
    try:
        ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
        return f"{request.path} tu {ip}"
    except Exception:
        return "?"


FIRST_BYTE_WAIT = float(os.environ.get("FIRST_BYTE_WAIT", "25"))


def _kill_quiet(p):
    try:
        p.kill()
    except Exception:
        pass


def _kill_stream(why=""):
    global _cur_proc
    with _cur_lock:
        p, _cur_proc = _cur_proc, None
    if p is not None and p.poll() is None:
        print(f"[stream] roi video -> tat ffmpeg cu (boi {why or '?'})", flush=True)
        try:
            p.kill()
        except Exception:
            pass


def _park(proc, cleanup):
    """Thiet bi thoat video: giu ffmpeg song, van nhan du lieu nguon (hut bo).
    Tat khi: co video moi (_cur_proc bi kill) hoac het PARK_SEC."""
    tm = threading.Timer(PARK_SEC, _kill_quiet, (proc,))
    tm.daemon = True
    tm.start()

    def run():
        try:
            while proc.poll() is None:
                if not proc.stdout.read1(65536):
                    break
        except Exception:
            pass
        finally:
            tm.cancel()
            _terminate(proc)
            try:
                cleanup()
            except Exception:
                pass
            print("[stream] ffmpeg chay nen da dung (het gio hoac co video moi)", flush=True)

    threading.Thread(target=run, daemon=True).start()


_STREAM_MARK = "/tmp/relay_stream_%d" % os.getpid()


def _mark_stream(on=True):
    """Danh dau 'dang co luong phat' bang file (moi worker gunicorn 1 file) de worker khac biet."""
    try:
        if on:
            with open(_STREAM_MARK, "a"):
                os.utime(_STREAM_MARK, None)
        elif os.path.exists(_STREAM_MARK):
            os.remove(_STREAM_MARK)
    except OSError:
        pass


def _stream_elsewhere(max_age=60):
    try:
        import glob
        now = time.time()
        for f in glob.glob("/tmp/relay_stream_*"):
            if f != _STREAM_MARK and now - os.path.getmtime(f) < max_age:
                return True
    except OSError:
        pass
    return False


def _idle_for(sec):
    return (_active_streams == 0 and _reqbusy == 0 and not _stream_elsewhere() and not _br_busy()
            and time.monotonic() - _last_req >= sec)


AUTO_UPDATE = os.environ.get("YTDLP_AUTO_UPDATE", "1") == "1"
UPDATE_HOURS = float(os.environ.get("YTDLP_UPDATE_HOURS", "6"))
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
                # Dang chay duoi gunicorn: execv trong worker se mo them 1 gunicorn thu hai, tranh cong 8000
                # voi master ("Address already in use") va lam server chet vai giay. Gui HUP cho master de no
                # tu nap worker moi mot cach dung quy trinh.
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


def _update_loop():
    time.sleep(float(os.environ.get("YTDLP_FIRST_UPDATE_DELAY", "300")))
    _update_and_maybe_restart(min_gap=0)
    while True:
        time.sleep(UPDATE_HOURS * 3600)
        _update_and_maybe_restart(min_gap=0)


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


def _res_expiry(res):
    best = None
    for u in (res[0], res[1] if len(res) > 1 else None):
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


def _cache_has(video_url, height_cap):
    with _resolve_guard:
        hit = _resolve_cache.get((_canon_url(video_url), str(height_cap)))
        return bool(hit and time.monotonic() < hit[2])


def _cache_drop(video_url, height_cap):
    with _resolve_guard:
        _resolve_cache.pop((_canon_url(video_url), str(height_cap)), None)


def resolve_stream_urls(video_url, height_cap, background=False, force=False):
    video_url = _canon_url(video_url)
    key = (video_url, str(height_cap))
    with _resolve_guard:
        if force:
            _resolve_cache.pop(key, None)
        now = time.monotonic()
        hit = _resolve_cache.get(key)
        if hit and now < hit[2]:
            print(f"[resolve] CACHE HIT {video_url}", flush=True)
            return hit[1]
        neg = _neg_cache.get(video_url)
        if neg and now < neg[0]:
            raise ResolveError(neg[1], permanent=True)
        fc = _fail_cache.get(video_url)
        if fc and now < fc[0] and not force and not background:
            raise ResolveError(fc[1] + f" (vua loi {now - fc[2]:.0f}s truoc, thu lai sau it giay)", permanent=False)
        if len(_resolve_locks) > 100:
            for k in [k for k, l in _resolve_locks.items() if not l.locked()]:
                _resolve_locks.pop(k, None)
        lock = _resolve_locks.setdefault(key, threading.Lock())
    with lock:
        with _resolve_guard:
            hit = _resolve_cache.get(key)
            if hit and time.monotonic() < hit[2]:
                print(f"[resolve] CACHE HIT (sau khi cho warm) {video_url}", flush=True)
                return hit[1]
        t0 = time.monotonic()
        try:
            res = _resolve_stream_urls_uncached(video_url, height_cap, background)
        except ResolveError as e:
            if e.permanent:
                with _resolve_guard:
                    _neg_cache[video_url] = (time.monotonic() + YT_NEG_TTL, str(e)[:300])
                    if len(_neg_cache) > 200:
                        _now = time.monotonic()
                        for k in [k for k, v in _neg_cache.items() if _now >= v[0]]:
                            _neg_cache.pop(k, None)
            elif not background and YT_FAIL_TTL > 0 and "dang chan IP" not in str(e) and "server dang ban" not in str(e):
                with _resolve_guard:
                    _t = time.monotonic()
                    _fail_cache[video_url] = (_t + YT_FAIL_TTL, str(e)[:300], _t)
                    if len(_fail_cache) > 100:
                        for k in [k for k, v in _fail_cache.items() if _t >= v[0]]:
                            _fail_cache.pop(k, None)
            print(f"[resolve] that bai ({str(e)[:120]})", flush=True)
            raise
        except Exception as e_ytdlp:
            print(f"[resolve] yt-dlp that bai ({str(e_ytdlp)[:120]})", flush=True)
            raise
        _dt = time.monotonic() - t0
        global _resolve_ema
        _resolve_ema = _dt if not _resolve_ema else 0.6 * _resolve_ema + 0.4 * _dt
        print(f"[resolve] yt-dlp mat {_dt:.1f}s cho {video_url} (trung binh {_resolve_ema:.0f}s)", flush=True)
        with _resolve_guard:
            ts = time.monotonic()
            until = ts + RESOLVE_TTL
            exp = _res_expiry(res)
            if exp is not None:
                until = min(until, exp)
            elif res[0] and "googlevideo.com" not in res[0]:
                until = min(until, ts + 480)
            if until > ts + 30:
                _resolve_cache[key] = (ts, res, until)
            for k in [k for k, v in _resolve_cache.items() if ts >= v[2]]:
                _resolve_cache.pop(k, None)
            if len(_resolve_cache) > 60:
                for k in sorted(_resolve_cache, key=lambda k: _resolve_cache[k][0])[:20]:
                    _resolve_cache.pop(k, None)
        return res


_last_height_cap = ""


def _warm_height():
    try:
        floor = int(os.environ.get("MIN_HEIGHT", "240"))
        return str(max(int(_last_height_cap or os.environ.get("WARM_HEIGHT_CAP", "360")), floor))
    except ValueError:
        return "360"


YT_WARM_MAX_COST = float(os.environ.get("YT_WARM_MAX_COST", "25"))
_resolve_ema = 0.0


def _mem_frac():
    mi = _mem_info()
    return (mi[0] / mi[1]) if mi else 0.0


def _trim_caches():
    _band_cache.clear()
    _jpeg_cache.clear()
    _S.pages.clear()
    gc.collect()


def _warm_worker(video_ids, height_cap):
    if _resolve_ema > YT_WARM_MAX_COST:
        print(f"[warm] resolve trung binh {_resolve_ema:.0f}s > {YT_WARM_MAX_COST:.0f}s, bo qua lam nong", flush=True)
        return
    if _mem_frac() > 0.75:
        print("[warm] RAM cao, bo qua lam nong", flush=True)
        return
    for vid in video_ids:
        if _active_streams > 0:
            print("[warm] dang co luong phat, bo qua lam nong", flush=True)
            return
        if _br_busy() or _resolve_gate.locked() or time.monotonic() < _yt_block_until:
            print("[warm] may dang ban (trinh duyet/resolve khac), bo qua lam nong", flush=True)
            return
        try:
            resolve_stream_urls(f"https://www.youtube.com/watch?v={vid}", height_cap, background=True)
        except Exception as e:
            print(f"[warm] loi {vid}: {str(e)[:120]}", flush=True)


def warm_results(results):
    ids = [r["id"] for r in results if r.get("id")][:WARM_TOP_N]
    if not ids:
        return
    threading.Thread(target=_warm_worker, args=(ids, _warm_height()), daemon=True).start()


@app.route("/stream")
def stream():
    return _stream_impl(request.args.get("url", ""))


STREAM_FIT = os.environ.get("STREAM_FIT", "1") == "1"
_YT_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return max(lo, min(hi, int(default)))


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


import http.client
import secrets
import base64
import bisect

TURBO = os.environ.get("TURBO", "1") == "1"
TURBO_WORKERS = max(1, _env_int("TURBO_WORKERS", 5))
TURBO_CHUNK = max(64, _env_int("TURBO_CHUNK_KB", 512)) * 1024
TURBO_HEAD = 128 * 1024
TURBO_MAX_FAIL = max(2, _env_int("TURBO_MAX_FAIL", 12))
TURBO_HEAD_N = 8
TURBO_AHEAD = max(4, _env_int("TURBO_AHEAD_MB", 8) * 1024 * 1024 // TURBO_CHUNK + TURBO_HEAD_N)
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
    def __init__(self, url, hdr, proxy=""):
        self.hdr = hdr
        self.proxy = proxy
        self.size, self.url = self._probe(url)
        self.starts = [0]
        while self.starts[-1] < self.size:
            self.starts.append(self.starts[-1] + _turbo_size(len(self.starts) - 1))
        self.starts[-1] = self.size
        self.n = len(self.starts) - 1
        self.buf, self.inflight = {}, set()
        self.cond = threading.Condition()
        self.cursor, self.closed, self.err = 0, False, None
        self.fails = {}  # idx -> (so lan loi lien tiep, loi cuoi)
        self.t0, self.dl_bytes, self.wait_s = time.monotonic(), 0, 0.0
        for _ in range(TURBO_WORKERS):
            threading.Thread(target=self._work, daemon=True).start()

    def _probe(self, url):
        h = dict(self.hdr)
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

    def _fetch(self, c, idx):
        a = self.starts[idx]
        b = self.starts[idx + 1] - 1
        u = urllib.parse.urlsplit(self.url)
        h = dict(self.hdr)
        h["Range"] = f"bytes={a}-{b}"
        c.request("GET", u.path + ("?" + u.query if u.query else ""), headers=h)
        r = c.getresponse()
        if r.status != 206:
            # Khong doc ca than phan hoi (neu nguon bo qua Range se tra ca file 200 -> tran RAM)
            try:
                r.read(512)
            except Exception:
                pass
            raise _ChunkError(r.status, f"HTTP {r.status}")
        data = r.read()
        if len(data) != b - a + 1:
            raise _ChunkError(r.status, f"thieu du lieu {len(data)}/{b - a + 1}B")
        return data

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
                    if c is None:
                        c = self._conn()
                    data = self._fetch(c, idx)
                    break
                except Exception as e:
                    last = e
                    try:
                        c and c.close()
                    except Exception:
                        pass
                    c = None
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
            if data is None:
                # chunk loi -> _next() se tu xep hang lai; lui dan de khong don dap googlevideo
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
                if nf >= TURBO_MAX_FAIL:
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
    """Moi worker tu mo 1 may chu nho tren cong ngau nhien 127.0.0.1 chi phuc vu /_src/<sid>.
    Tranh 2 loi: (1) nhieu worker gunicorn -> ffmpeg goi nham worker khong co fetcher (404) lam dut luong;
    (2) het thread cua may chu chinh khi ffmpeg goi nguoc lai chinh no."""
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


def _turbo(url, hdr, proxy):
    if not (TURBO and url and url.startswith(("http://", "https://"))) or re.search(r"\.m3u8|\.mpd|/manifest|/playlist", url, re.I):
        return None
    try:
        port = _src_server_port()
        f = _Fetcher(url, _hdr_dict(hdr), proxy)
    except Exception as e:
        print(f"[turbo] bo qua ({str(e)[:100]}) -> doc truc tiep", flush=True)
        return None
    sid = secrets.token_hex(16)
    _fetchers[sid] = f
    return f"http://127.0.0.1:{port}/_src/{sid}", sid


def _inputs(v_url, a_url, v_hdr, a_hdr, is_yt):
    proxy = YT_PROXY if (YT_PROXY and is_yt) else ""
    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        fv = ex.submit(_turbo, v_url, v_hdr, proxy)
        fa = ex.submit(_turbo, a_url, a_hdr, proxy) if a_url else None
        tv, ta = fv.result(), (fa.result() if fa else None)
    sids = [t[1] for t in (tv, ta) if t]
    vi = (tv[0], "", True) if tv else (v_url, v_hdr, False)
    ai = ((ta[0], "", True) if ta else (a_url, a_hdr, False)) if a_url else None
    return vi, ai, sids


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
    """Turbo that bai giua chung: tai not phan con lai bang 1 ket noi (khong song song) de ffmpeg khong bi cat luong."""
    c, tries = None, 0
    try:
        while pos <= end and not f.closed and tries < 4:
            start = pos
            try:
                if c is None:
                    c = f._conn()
                u = urllib.parse.urlsplit(f.url)
                h = dict(f.hdr)
                h["Range"] = f"bytes={pos}-{end}"
                c.request("GET", u.path + ("?" + u.query if u.query else ""), headers=h)
                r = c.getresponse()
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


def _ffmpeg_cmd(v_url, a_url, v_hdr, a_hdr, is_yt, w, h, fps, q, ar, v_local=False, a_local=False):
    def inp(url, hdr, vid=False, local=False):
        a = ["-protocol_whitelist", "http,https,tcp,tls,crypto,data,httpproxy",
             "-reconnect", "1", "-reconnect_at_eof", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "5", "-rw_timeout", "20000000"]
        if YT_PROXY and is_yt and not local:
            a += ["-http_proxy", YT_PROXY]
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


def _nice_prefix():
    if STREAM_NICE > 0 and shutil.which("nice"):
        return ["nice", "-n", str(STREAM_NICE)]
    return []


def _sock_diag(sock):
    """Chan doan ket noi luc luong bi dut: ben kia dong (FIN), bi reset (RST), hay ket noi con song."""
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
                elif not (fcc[:2] == b"ix" or fcc == b"JUNK"):
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

    if _mem_frac() > 0.70:
        print(f"[stream] RAM {_mem_str()} cao -> don cache trinh duyet truoc khi resolve", flush=True)
        _trim_caches()

    if KILL_ON_LEAVE:
        _kill_stream(_who())

    can_retry = _cache_has(video_url, height_cap)
    force = False
    proc = None
    first = b""
    for attempt in (1, 2):
        try:
            v_url, a_url, v_hdr, a_hdr = resolve_stream_urls(video_url, height_cap, force=force)
        except Exception as e:
            print(f"[stream] resolve error: {e}", flush=True)
            if not isinstance(e, ResolveError):
                traceback.print_exc()
            msg = str(e)
            if any(k in msg for k in _OUTDATED_HINTS):
                trigger_update_async()
            return jsonify({"error": "resolve_failed", "detail": msg[:300],
                            "permanent": bool(getattr(e, "permanent", False))}), 502

        if not all(_url_is_public(u) for u in (v_url, a_url) if u):
            print("[stream] URL luong tro vao host noi bo -> chan", flush=True)
            _cache_drop(video_url, height_cap)
            return jsonify({"error": "blocked_host", "detail": "nguon tro toi dia chi noi bo"}), 403

        vi, ai, sids = _inputs(v_url, a_url, v_hdr, a_hdr, is_yt)
        cmd = _ffmpeg_cmd(vi[0], ai[0] if ai else None, vi[1], ai[1] if ai else "", is_yt, w, h, fps, q, ar, vi[2], ai[2] if ai else False)
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
        _cache_drop(video_url, height_cap)
        print("[stream] ffmpeg khong ra du lieu (URL het han / nguon cat ket noi?)", flush=True)
        if attempt == 1 and can_retry:
            force = True
            continue
        break
    if proc is None:
        return jsonify({"error": "ffmpeg_no_output", "detail": "ffmpeg khong nhan duoc luong tu nguon"}), 502

    released = [False]
    parked = [False]

    def _release(drop=True):
        with _active_lock:
            if released[0]:
                return
            released[0] = True
        _streams_add(-1)
        if _active_streams == 0:
            _mark_stream(False)
        if drop:
            _drop_fetchers(sids)

    _csock = request.environ.get("gunicorn.socket")

    def generate():
        t0 = time.monotonic()
        max_gap = 0.0
        last_t2 = t0
        sent = 0
        chunk = first
        w_ff = w_cl = 0.0
        last_log = t0
        last_mark = 0.0
        _mark_stream()
        last_sent = 0
        avi = _AviCounter()

        def _lead():
            return f"{avi.frames / fps - (time.monotonic() - t0):+.1f}s frames={avi.frames}" if avi.ok and avi.frames else "n/a"
        reason = "ffmpeg het du lieu (EOF)"
        try:
            while chunk:
                sent += len(chunk)
                avi.feed(chunk)
                if STREAM_MAX_LEAD > 0 and avi.ok and avi.frames:
                    over = avi.frames / fps - (time.monotonic() - t0) - STREAM_MAX_LEAD
                    if over > 0:
                        time.sleep(min(over, 2.0))
                expected_elapsed = max(0, sent - burst_bytes) / target_bps
                actual_elapsed = time.monotonic() - t0
                if expected_elapsed > actual_elapsed:
                    time.sleep(expected_elapsed - actual_elapsed)
                t1 = time.monotonic()
                if t1 - last_t2 > max_gap:
                    max_gap = t1 - last_t2
                yield chunk
                t2 = time.monotonic()
                last_t2 = t2
                chunk = proc.stdout.read1(65536)
                t3 = time.monotonic()
                w_cl += t2 - t1
                w_ff += t3 - t2
                if t3 - last_mark >= 5:
                    last_mark = t3
                    _mark_stream()
                if t3 - last_log >= 10:
                    span = t3 - last_log
                    print(f"[stream-diag] {span:.0f}s: ffmpeg_wait={w_ff:.1f}s client_wait={w_cl:.1f}s "
                          f"rate={(sent - last_sent) / span / 1000:.0f}KB/s fps={fps} q={q} lead={_lead()}", flush=True)
                    last_log, last_sent, w_ff, w_cl = t3, sent, 0.0, 0.0
        except GeneratorExit:
            reason = "THIET BI DONG KET NOI (client thoat/ngat WiFi/timeout phia ESP)"
            raise
        except BaseException as e:
            reason = f"LOI {type(e).__name__}: {str(e)[:120]}"
            raise
        finally:
            try:
                rc = proc.wait(timeout=1.5) if reason.startswith("ffmpeg") else proc.poll()
            except Exception:
                rc = proc.poll()
            print(f"[stream] KET THUC sau {time.monotonic() - t0:.0f}s, gui {sent // 1024}KB | ly do: {reason} | "
                  f"ffmpeg rc={rc} | lead={_lead()} | RAM {_mem_str()} | "
                  f"max_gap={max_gap:.1f}s im_lang_cuoi={time.monotonic() - last_t2:.1f}s | {_sock_diag(_csock)}", flush=True)
            if (reason.startswith("THIET BI") and PARK_SEC > 0 and proc.poll() is None
                    and _cur_proc is proc):
                parked[0] = True
                _release(drop=False)
                _park(proc, lambda: _drop_fetchers(sids))
                print(f"[stream] thoat video -> giu ffmpeg chay nen toi da {PARK_SEC:.0f}s (video moi se thay the)", flush=True)
            else:
                _release()
                _terminate(proc)

    def _reap():
        if parked[0]:
            return
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        _release()

    with _stat_lock:
        _stat["streams"] += 1
        _stat["last"] = time.time()
        _n = _stat["streams"]
    print(f"[proof] giai ma cho LilyGo #{_n} | {_who()} | {w}x{h}@{fps}fps | khong luu o dia", flush=True)
    resp = Response(generate(), mimetype="video/avi", headers={"Cache-Control": "no-store", "X-Relay-Purpose": "transcode-only-for-LilyGo-T-Display-S3"})
    resp.call_on_close(_reap)
    return resp


class _DiagLogger:
    def __init__(self):
        self.lines = []
    def debug(self, m):
        self.lines.append(str(m))
    def info(self, m):
        self.lines.append(str(m))
    def warning(self, m):
        self.lines.append("WARNING: " + str(m))
    def error(self, m):
        self.lines.append("ERROR: " + str(m))


@app.route("/diag")
def diag():
    vid = request.args.get("v", "dQw4w9WgXcQ")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        return Response("v khong hop le (chi nhan ID video YouTube)", 400, mimetype="text/plain; charset=utf-8")
    mode = request.args.get("mode", "cur")
    if mode not in ("cur", "def", "noremote", "deno"):
        mode = "cur"
    lg = _DiagLogger()
    head = [f"mode={mode} video={vid}", f"python={sys.version.split()[0]} yt-dlp={_ytdlp_version()}"]
    try:
        from importlib.metadata import version as _v
        head.append(f"yt-dlp-ejs={_v('yt-dlp-ejs')}")
    except Exception as e:
        head.append(f"yt-dlp-ejs: khong doc duoc ({e})")
    for exe in ("deno", "node"):
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            head.append(f"{exe}: {(r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else 'khong co dau ra'}")
        except Exception as e:
            head.append(f"{exe}: KHONG CHAY DUOC ({e})")
    head.append(f"cookies={'yes' if os.path.exists(COOKIES_FILE) else 'NO'}")

    opts = {"quiet": False, "verbose": True, "logger": lg, "skip_download": True,
            "format": "bestvideo+bestaudio/best", "socket_timeout": 15}
    if mode == "cur":
        opts["js_runtimes"] = {"node": {}, "deno": {}}
        opts["remote_components"] = ["ejs:github"]
    elif mode == "noremote":
        opts["js_runtimes"] = {"node": {}, "deno": {}}
    elif mode == "deno":
        opts["js_runtimes"] = {"deno": {}}
    _cl = request.args.get("client", "").strip()
    if _cl:
        opts["extractor_args"] = {"youtube": {"player_client": [c.strip() for c in _cl.split(",") if c.strip()]}}
    if os.path.exists(COOKIES_FILE) and request.args.get("nocookie") != "1":
        opts["cookiefile"] = COOKIES_FILE
    if not _resolve_gate.acquire(timeout=30):
        lg.lines.append("=== BO QUA: server dang ban (yt-dlp khac dang chay), thu lai sau ===")
    else:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
                lg.lines.append(f"=== THANH CONG: {info.get('title')} ===")
        except Exception as e:
            lg.lines.append(f"=== THAT BAI: {str(e)[:300]} ===")
        finally:
            _resolve_gate.release()
    text = "\n".join(head) + "\n\n" + "\n".join(lg.lines)
    return Response(text, mimetype="text/plain; charset=utf-8")


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
MAX_JPEG = int(os.environ.get("BR_MAX_JPEG", "45000"))
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
_TAG_END = re.compile(r">")


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
    q = BR_JPEG_Q
    while True:
        buf = io.BytesIO()
        v.save(buf, "JPEG", quality=q)
        if buf.tell() <= MAX_JPEG or q <= 25:
            break
        q -= 10
    out = buf.getvalue()
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
    """(rong, cao) cua JPEG doc tu header, khong can giai ma."""
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
    """Kiem tra nhe (khong DNS) cho tai nguyen phu; DNS day du chi ap dung khi dieu huong trang chinh."""
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
                # Trang treo: huy navigation dang cho, neu khong screenshot se bi ket them ~25s
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

    def shot(self, portrait):
        W, VH = (OUT_H, OUT_W) if portrait else (OUT_W, OUT_H)
        pg = self.page
        try:
            pg.evaluate(_CR_SB_JS, BR_CR_ZOOM)
        except Exception:
            pass
        raw = pg.screenshot(type="jpeg", quality=BR_JPEG_Q, caret="hide", animations="disabled")
        if len(raw) <= MAX_JPEG and _jpeg_dims(raw) == (W, VH):
            return raw
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.size != (W, VH):
            im = im.resize((W, VH), Image.LANCZOS)
        q = BR_JPEG_Q
        while True:
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q)
            if buf.tell() <= MAX_JPEG or q <= 25:
                return buf.getvalue()
            q -= 10

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
        jpg = self.shot(portrait)
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
    """Truoc day moi lan xem video/tim kiem deu tat Chromium -> lan quay lai phai khoi dong lai (lag vai giay)."""
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
        _trim_caches()
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


@app.route("/dbg")
def esp_exit_report():
    """ESP bao ly do no tu thoat video (de biet ben nao ngat). Chi ghi log, khong lam gi khac."""
    def _c(k, n=100):
        v = (request.args.get(k) or "?")[:n]
        return "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in v)
    _real_print(f"[esp-exit] why={_c('why', 120)} res={_c('res', 4)} played={_c('ms', 10)}ms recv={_c('kb', 10)}KB "
                f"rssi={_c('rssi', 6)} heap={_c('heap', 10)} wifi={_c('wst', 4)} | {_who()}", flush=True)
    return Response("", 204)


def _log(*a):
    print("[br]", *a, flush=True)


_TR_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=vi&dt=t&q="
_tr_cache = {}
_tr_lock = threading.Lock()
_tr_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)


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
            if len(_tr_cache) >= 300:
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
        if os.environ.get("BR_CR_PREWARM", "1" if ROLE == "browser" else "0") == "1":
            _cr_prewarm()
        print("[br] prewarm xong", flush=True)
    except Exception as e:
        _cause = repr(getattr(e, "__cause__", None) or getattr(e, "__context__", None) or "")
        print(f"[br] prewarm loi: {type(e).__name__}: {str(e)[:200]} | goc: {_cause[:300]}", flush=True)


def _warmup_once():
    time.sleep(float(os.environ.get("YT_WARMUP_DELAY", "25")))
    if _active_streams > 0 or _br_busy():
        return
    vid = os.environ.get("YT_WARMUP_VIDEO", "dQw4w9WgXcQ")
    t0 = time.monotonic()
    try:
        resolve_stream_urls(f"https://www.youtube.com/watch?v={vid}", _warm_height(), background=True)
        print(f"[warmup] xong sau {time.monotonic() - t0:.0f}s", flush=True)
    except Exception as e:
        print(f"[warmup] loi: {str(e)[:120]}", flush=True)


def _keepalive_loop():
    url = RELAY_URL.rstrip("/") + "/healthz"
    gap = max(60.0, float(os.environ.get("KEEPALIVE_SEC", "540")))
    time.sleep(60)
    while True:
        try:
            urllib.request.urlopen(url, timeout=15).read(16)
        except Exception as e:
            print(f"[keepalive] loi: {str(e)[:80]}", flush=True)
        time.sleep(gap)


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



_bg_started = False
_bg_lock = threading.Lock()


def _start_background_tasks():
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    if os.environ.get("KEEPALIVE", "") == "1":
        threading.Thread(target=_keepalive_loop, daemon=True).start()
    if ROLE != "video" and os.environ.get("BR_PREWARM", "1" if __name__ == "__main__" else "0") == "1":
        threading.Thread(target=_prewarm_browser, daemon=True).start()
    if os.environ.get("YT_WARMUP", "1") == "1" and ROLE != "browser":
        threading.Thread(target=_warmup_once, daemon=True).start()
    threading.Thread(target=_pot_selfcheck, daemon=True).start()
    print(f"[update] yt-dlp hien tai: {_ytdlp_version()} | auto_update={AUTO_UPDATE} moi {UPDATE_HOURS}h", flush=True)
    if AUTO_UPDATE and ROLE != "browser":
        threading.Thread(target=_update_loop, daemon=True).start()


if __name__ == "__main__":
    _start_background_tasks()
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
elif os.environ.get("BG_ON_IMPORT", "1") == "1":
    _start_background_tasks()
