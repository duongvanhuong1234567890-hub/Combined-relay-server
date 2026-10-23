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
import ipaddress
import gc
from flask import Flask, request, Response, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler

# glibc tao nhieu "arena" bo nho (mot cai cho moi luong) -> RSS phinh len va khong tra ve. Do voi 4 luong giai ma anh:
# dinh RSS ~86MB -> ~52MB, sau khi giai phong ~45MB -> ~29MB khi gioi han con 2 arena. (malloc_trim thi KHONG co tac dung - da do.)
if not os.environ.get("MALLOC_ARENA_MAX"):
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").mallopt(-8, 2)   # M_ARENA_MAX = -8
    except Exception:
        pass

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # chan POST /cookies (va moi body) khong lo


@app.errorhandler(Exception)
def _on_error(e):
    """Loi khong luong truoc -> ghi log day du, tra 500 ngan gon (truoc day Flask tra trang HTML dai cho firmware)."""
    if isinstance(e, HTTPException):
        return e
    print("[error]", traceback.format_exc(), flush=True)
    return "loi may chu", 500


# ---- Chan SSRF: server la may chu trung gian, khong duoc phep bi lay lam cong truy cap mang noi bo ----
# ALLOW_PRIVATE_HOSTS=1 de tat (vd test voi may trong LAN).
ALLOW_PRIVATE_HOSTS = os.environ.get("ALLOW_PRIVATE_HOSTS", "") == "1"
_SAFE_HOST_SUFFIX = (".googlevideo.com", ".youtube.com", ".ytimg.com", ".googleusercontent.com")
_host_cache = {}
_host_lock = threading.Lock()


def _host_public(host):
    """True neu moi dia chi IP cua host deu la dia chi cong cong (khong loopback/private/link-local/CGNAT...)."""
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
        _host_cache[host] = (now + (300 if ok else 10), ok)  # loi DNS chi nho 10s de khong "an" nham lau
    return ok


def _url_is_public(url):
    try:
        p = urllib.parse.urlparse(url or "")
        return p.scheme in ("http", "https") and _host_public(p.hostname)
    except ValueError:
        return False


def _clip_pct(s, n):
    """Cat chuoi da percent-encode toi da n ky tu ma khong cat dut giua mot ma %XX."""
    s = s[:n]
    i = s.rfind("%")
    return s[:i] if i != -1 and i > len(s) - 3 else s

RELAY_URL = os.environ.get("RELAY_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://combined-relay-server-33xi.onrender.com"

ROLE = os.environ.get("ROLE", "all").strip().lower()
ROLE = {"youtube": "video", "yt": "video", "google": "browser", "gg": "browser"}.get(ROLE, ROLE)

# yt-dlp nap ~hang nghin lop extractor (ton RAM + CPU luc khoi dong). ROLE=browser khong bao gio dung -> chi nap khi can.
if ROLE == "browser":
    class _LazyYT:
        def __getattr__(self, name):
            import yt_dlp as _real
            globals()["yt_dlp"] = _real
            return getattr(_real, name)
    yt_dlp = _LazyYT()
else:
    import yt_dlp
# Tuy chon: dat STREAM_TOKEN de /stream /search /random chi nhan request co &tk=STREAM_TOKEN (chong ke la xai ke quota/CPU).
# De trong = mo nhu cu (khong lam hong firmware hien tai).
STREAM_TOKEN = os.environ.get("STREAM_TOKEN", "").strip()


def _tok_ok(given, expected):
    """So sanh token an toan, khong no 500 khi chuoi co ky tu khong phai ASCII."""
    try:
        return bool(expected) and hmac.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


_last_req = time.monotonic()
KILL_ON_LEAVE = os.environ.get("KILL_ON_LEAVE", "1") == "1"


@app.before_request
def _role_gate():
    global _br_last_activity, _last_req
    p = request.path
    if p not in ("/", "/healthz"):
        _last_req = time.monotonic()
    if ROLE == "video" and p.startswith("/br"):
        return "disabled on this server (ROLE=video)", 404
    if ROLE == "browser" and p.startswith(("/stream", "/search", "/random")):
        return "disabled on this server (ROLE=browser)", 404
    # /diag va /myip lo thong tin noi bo + chay yt-dlp: BAT BUOC co token (truoc day de trong BR_TOKEN la mo toang)
    if p in ("/diag", "/myip") and not _tok_ok(request.args.get("k", ""), BR_TOKEN):
        return "forbidden - can dat BR_TOKEN tren server va them ?k=BR_TOKEN vao URL", 403
    if STREAM_TOKEN and p.startswith(("/stream", "/search", "/random", "/tr")) \
            and not _tok_ok(request.args.get("tk", ""), STREAM_TOKEN):
        return "forbidden - them &tk=STREAM_TOKEN vao URL", 403
    if p.startswith(("/stream", "/search", "/random")):
        _br_last_activity = 0.0  # da chuyen sang YouTube -> browser khong con \"ban\" (cho phep lam nong ngay)
        if KILL_ON_LEAVE and p.startswith("/search"):
            _kill_stream()  # tim kiem moi = da roi video cu, nha CPU/RAM cua ffmpeg dang treo
        _cr_close_async()       # sang YouTube: tra RAM cua Chromium cho ffmpeg/yt-dlp
    return None


@app.route("/")
def health_check():

    return (f"OK | role={ROLE} | {RELAY_URL} | AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS} "
            f"cookies={'yes' if os.path.exists(COOKIES_FILE) else 'NO'} | browser={'token-ok' if os.environ.get('BR_TOKEN') else 'NO-TOKEN'} | engine={BR_ENGINE}{'(chromium dang chay)' if _cr is not None else ''} | build=gg1 | mem={_mem_str()}"), 200

PORT = int(os.environ.get("PORT", "8000"))


@app.route("/cookies", methods=["GET", "POST"])
def cookies_update():
    """Thay cookies.txt ngay khong can deploy lai. POST noi dung cookies (Netscape) len /cookies?k=BR_TOKEN.
    Vi du: curl -X POST --data-binary @cookies.txt "https://server/cookies?k=TOKEN". GET xem trang thai."""
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
    # yt-dlp ghi nguoc cookie jar vao file khi dong -> neu thay file giua luc dang chay, ban cu se de len ban moi.
    # Giu _resolve_gate (moi lan chay yt-dlp deu nam trong gate) de viec thay file khong bi chen ngang.
    if not _resolve_gate.acquire(timeout=30):
        return "server dang ban (yt-dlp dang chay), thu lai sau it giay", 503
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(txt if txt.endswith("\n") else txt + "\n")
        os.replace(tmp, target)
        COOKIES_FILE = target
        # cookies moi -> xoa trang thai "dang bi chan"/"tam bo cach khong cookies" cu de thu lai ngay
        _yt_block_until = 0.0
        _yt_preferred = None
        _nocookie_fails = 0
        _nocookie_skip_until = 0.0
        _neg_cache.clear()
    finally:
        _resolve_gate.release()
    return f"da cap nhat cookies: {len(txt)} bytes", 200


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


PLAYLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_links.txt")


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
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "1")   # do: 2 luong khong giam CPU-giay (1.17 vs 1.12s/25s video), chi them RAM va tranh gianh quota 0.1 CPU
STREAM_FAST_DECODE = os.environ.get("STREAM_FAST_DECODE", "1") == "1"   # bo loc deblock khi giai ma: -7% CPU, PSNR 41.9dB (gan nhu khong khac)
STREAM_SRC_AUTO = os.environ.get("STREAM_SRC_AUTO", "1") == "1"        # chi lay nguon vua du cho khung ra (240p thay vi 360p: -34% CPU)
STREAM_NICE = int(os.environ.get("STREAM_NICE", "5"))
# Giu nhip THOI GIAN THUC cho ffmpeg. Log cho thay 30s dau ffmpeg chay ~10x toc do that (600-850KB/s trong khi video chi can ~70-100KB/s)
# roi dot ngot ve ~100-250KB/s (client_wait tang vot): thiet bi bi "dap" du lieu luc dau -> giat, sau khi bo dem day moi muot.
# STREAM_READRATE: toc do doc nguon toi da so voi thoi gian that (1.15 = nhanh hon 15% de luon day buffer). 0 = tat.
# STREAM_READ_BURST: so giay du lieu duoc doc nhanh luc dau de thiet bi co buffer khoi dau (chi dung duoc neu ffmpeg co -readrate_initial_burst).
STREAM_READRATE = float(os.environ.get("STREAM_READRATE", "1.15"))
STREAM_READ_BURST = float(os.environ.get("STREAM_READ_BURST", "4"))
_ff_caps = None


def _ffmpeg_caps():
    """Do mot lan xem ffmpeg tren may co -readrate / -readrate_initial_burst khong (ban cu hon thi lui ve -re)."""
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
STREAM_SKIP_NOREF = os.environ.get("STREAM_SKIP_NOREF", "1") == "1"   # fps ra thap: bo qua khung B khong tham chieu ngay luc giai ma (-15% CPU, hinh khong doi)
STREAM_FPS_MAX = int(os.environ.get("STREAM_FPS_MAX", "12"))   # tran fps phia server (bo qua fps firmware gui len neu cao hon)
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
        if reason == "rateLimitExceeded":  # nghen tuc thoi, khong phai het quota ngay -> nghi ngan thoi
            _api_block_until = time.monotonic() + min(60, YT_API_COOLDOWN)
        elif reason in ("quotaExceeded", "dailyLimitExceeded", "keyInvalid", "keyExpired",
                        "accessNotConfigured", "forbidden", "ipRefererBlocked"):
            _api_block_until = time.monotonic() + YT_API_COOLDOWN
        raise YTApiError(e.code, reason, msg) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        # mang chap chon / timeout / JSON hong: truoc day KHONG nghi -> moi lan tim kiem deu cho them 10s truoc khi roi ve yt-dlp
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
        # loi o buoc lay thoi luong KHONG duoc vut bo ca danh sach da tim thanh cong (da ton 100 don vi quota)
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
    """Xem IP dau ra cua server (truc tiep va qua YT_PROXY neu co) - mo tren trinh duyet de kiem tra nhanh."""
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

    # IP dang bi YouTube chan: dung goi them (se chi lam chan nang hon) - tra rong ngay cho firmware
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
    # Khong cho vo han: truoc day moi request tim kiem xep hang cho khoa va dong thread khi resolve ket lau
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
                    "channel": entry.get("channel") or entry.get("uploader") or "",  # dong bo voi nhanh API
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
    """Chuyển dict http_headers mà yt-dlp gắn theo từng format thành chuỗi
    header ffmpeg hiểu (mỗi dòng 'Key: Value', kết bằng \\r\\n) - dùng với
    cờ -headers trước mỗi -i. Không có bước này, ffmpeg tải trực tiếp URL
    CDN mà KHÔNG có User-Agent/header khớp với phiên đã resolve - nguồn
    thường vẫn cho phép mở kết nối (200 OK) nhưng cắt giữa chừng sau vài
    giây vì thấy request "không giống" trình phát hợp lệ."""
    headers = fmt.get("http_headers") or {}
    if not headers:
        return ""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


_YT_ATTEMPTS_FULL = [
    ("android_vr",   ["android_vr"],     False, False),
    ("ios",          ["ios"],            False, False),
    ("web_embedded", ["web_embedded"],   False, True),
    ("tv",           ["tv"],             True,  True),
    ("tv_simply",    ["tv_simply"],      False, False),
    ("mweb",         ["mweb"],           True,  True),
    ("mac-dinh",     None,               True,  False),
    ("runtime+ejs",  None,               True,  True),
]
_YT_ATTEMPTS_LITE = [
    ("android_vr",   ["android_vr"],     False, False),
    ("tv",           ["tv"],             True,  True),
    ("tv_simply",    ["tv_simply"],      False, False),
    ("mweb",         ["mweb"],           True,  True),
    ("runtime+ejs",  None,               True,  True),
]
_YT_ATTEMPTS = _YT_ATTEMPTS_FULL if os.environ.get("YT_ATTEMPTS_FULL", "") == "1" else _YT_ATTEMPTS_LITE
_yt_preferred = None
YT_BLOCK_BACKOFF = float(os.environ.get("YT_BLOCK_BACKOFF", "60"))
_yt_block_until = 0.0
# Dau hieu IP bi chan (anh huong MOI video). Bo "Sign in" tran: "Sign in to confirm your age" la loi RIENG cua 1 video
# (gioi han tuoi) - truoc day mot video 18+ lam ca server nghi 60s voi tat ca nguoi dung.
_BLOCK_HINTS = ("not a bot", "player response", "HTTP Error 429", "Too Many Requests", "HTTP Error 403")
_AGE_HINTS = ("confirm your age", "age-restricted", "inappropriate for some users")
_OUTDATED_HINTS = ("player response", "Unable to extract", "nsig", "Signature extraction")

YT_NOCOOKIE_MAX_FAILS = int(os.environ.get("YT_NOCOOKIE_MAX_FAILS", "2"))
YT_NOCOOKIE_COOLDOWN = float(os.environ.get("YT_NOCOOKIE_COOLDOWN", "1800"))
_nocookie_fails = 0
_nocookie_skip_until = 0.0

YT_PROXY = os.environ.get("YT_PROXY", "").strip()
YT_FMT_LEAN = os.environ.get("YT_FMT_LEAN", "1") == "1"


_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_youtube(url):
    """Xet theo hostname that su (truoc day 'evil.com/?r=youtube.com' cung bi coi la YouTube)."""
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _YT_HOSTS)


def _build_ydl_opts(fmt, client=None, use_cookies=True, runtimes=False, proxy=""):
    _dbg = os.environ.get("YT_DEBUG", "") == "1"
    opts = {
        "quiet": not _dbg,
        "no_warnings": not _dbg,
        "verbose": _dbg,
        "format": fmt,
        "skip_download": True,
        "geo_bypass": True,
        "socket_timeout": 10,
        "noplaylist": True,
        "retries": 1,
        "extractor_retries": 1,
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": list(client)}}
    _pot = os.environ.get("YT_POT_URL", "").strip()
    if _pot:
        opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {"base_url": [_pot]}
    if runtimes:
        opts["js_runtimes"] = {"deno": {}, "node": {}}
        # YT_REMOTE_EJS=0: khong tai script giai ma tu GitHub luc chay (dung ban dong goi san trong yt-dlp-ejs)
        opts["remote_components"] = ["ejs:github"] if os.environ.get("YT_REMOTE_EJS", "1") == "1" else []
    if proxy:
        opts["proxy"] = proxy
    if use_cookies and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _extract_with(video_url, ydl_opts):
    """Chay yt-dlp mot lan voi ydl_opts, tra ve (video_url, audio_url, video_headers, audio_headers).
    audio_url co the None neu video do van co format gop san."""
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


# Loi CHAC CHAN rieng cua video -> dung ngay, khong thu cach khac.
_NO_RETRY_STRONG = ("Private video", "has been removed", "not available in your country", "This video is private",
                    "members-only", "Join this channel", "live event will begin", "Premieres in",
                    "account associated with this video has been terminated")
# Loi MO HO: mot so client (android_vr...) bao "unavailable" cho video van xem duoc bang client khac (vd can dang nhap)
# -> phai that bai >= 2 cach moi ket luan la hong han (truoc day 1 cach bao la bo cuoc, nho 90s).
_NO_RETRY_WEAK = ("Video unavailable", "This video is not available")



_resolve_gate = threading.Lock()
YT_GATE_WAIT = float(os.environ.get("YT_GATE_WAIT", "90"))
YT_WARM_BUDGET = float(os.environ.get("YT_WARM_BUDGET", "20"))
YT_NEG_TTL = float(os.environ.get("YT_NEG_TTL", "90"))


class ResolveError(RuntimeError):
    """permanent=True: video hong han (rieng tu/da go/chan vung...) -> khong thu lai, nho tam thoi."""
    def __init__(self, msg, permanent=False):
        super().__init__(msg)
        self.permanent = permanent


_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/|v/))([A-Za-z0-9_-]{11})")


def _canon_url(url):
    """youtu.be/ID, m.youtube.com/watch?v=ID&list=..., /shorts/ID -> https://www.youtube.com/watch?v=ID.
    Cache/warm/khoa dung chung 1 khoa cho cung 1 video (truoc day moi dang URL la 1 khoa rieng -> lam nong khong bao gio trung)."""
    m = _YT_ID_RE.search(url or "")
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url


RAPID_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
# URL day du cua endpoint "Get Video Details" (YouTube Media Downloader). Lay chinh xac tu tab Code snippet tren RapidAPI.
RAPID_URL = os.environ.get("RAPID_URL", "https://youtube-media-downloader.p.rapidapi.com/v2/video/details").strip()
RAPID_HOST = urllib.parse.urlparse(RAPID_URL).netloc
RAPID_ONLY = os.environ.get("RAPID_ONLY", "0") == "1"   # =1: RapidAPI loi thi bao loi luon, KHONG roi ve yt-dlp


def _rapid_streams(video_id, height_cap):
    """Lay URL luong tu RapidAPI (YouTube Media Downloader) -> (video_url, audio_url, video_hdr, audio_hdr)."""
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

    # 1) Luong gop co ca hinh lan tieng (vd itag 18, 360p): don gian nhat cho ffmpeg
    combined = [v for v in vitems if v.get("hasAudio") and (v.get("height") or 0) <= cap]
    if combined:
        best = max(combined, key=lambda v: (v.get("height") or 0, "avc1" in str(v.get("mimeType", ""))))
        return best["url"], None, hdr, ""

    # 2) Tach rieng: video khong tieng (uu tien avc1/mp4 de ffmpeg giai ma nhe) + audio nho nhat
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
RAPID_FAST_QUALITY = os.environ.get("RAPID_FAST_QUALITY", "18").strip()   # 18 = MP4 360p co san tieng
RAPID_FILE_WAIT = float(os.environ.get("RAPID_FILE_WAIT", "300"))


def _rapid_fast_file(video_id):
    """RapidAPI (youtube-video-fast-downloader) tra URL file mp4 da co tieng.
    File co the chua san sang (404) trong 20-300s, nen cho den khi tai duoc. -> (video_url, None, "", "")"""
    if not RAPID_KEY:
        raise ResolveError("chua dat RAPIDAPI_KEY")
    api = (f"https://{RAPID_FAST_HOST}/download_video/{video_id}?"
           + urllib.parse.urlencode({"quality": RAPID_FAST_QUALITY}))
    req = urllib.request.Request(api, headers={
        "x-rapidapi-host": RAPID_FAST_HOST,
        "x-rapidapi-key": RAPID_KEY,
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            data = json.loads(r.read(200_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", "replace")
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
            pass   # mang chap chon: thu lai
        if time.monotonic() > deadline:
            raise ResolveError(f"file chua san sang sau {RAPID_FILE_WAIT:.0f}s")
        time.sleep(5)
    print("[rapid] file san sang, bat dau phat", flush=True)
    return file_url, None, "", ""


def _resolve_stream_urls_uncached(video_url, height_cap, background=False):
    m = _YT_ID_RE.search(video_url or "")
    use_rapid = bool(RAPID_KEY and m and _is_youtube(video_url))

    if use_rapid:
        # Nguon CHINH: RapidAPI (khong can giu gate yt-dlp)
        try:
            return _rapid_fast_file(m.group(1))
        except ResolveError as e:
            print(f"[resolve] RapidAPI that bai: {str(e)[:200]}", flush=True)
            if background or RAPID_ONLY:
                raise   # warmup / che do chi RapidAPI: khong dung yt-dlp

    # Du phong: yt-dlp
    if background:
        if not _resolve_gate.acquire(blocking=False):
            raise ResolveError("busy (dang co resolve khac)")
    elif not _resolve_gate.acquire(timeout=YT_GATE_WAIT):
        raise ResolveError(f"server dang ban, cho qua {YT_GATE_WAIT:.0f}s")
    try:
        return _resolve_stream_urls_inner(video_url, height_cap, YT_WARM_BUDGET if background else None, background)
    finally:
        _resolve_gate.release()


def _resolve_stream_urls_inner(video_url, height_cap, budget=None, background=False):
    """Dung yt-dlp lay URL luong truc tiep (YouTube, TikTok, Facebook... yt-dlp tu nhan dien domain).
    Doi "bestvideo+bestaudio" (2 luong tach biet) roi de ffmpeg ghep khi mux.
    Voi YouTube: thu lan luot nhieu cach (_YT_ATTEMPTS), cach nao ra truoc thi dung."""
    global _yt_preferred, _nocookie_fails, _nocookie_skip_until, _yt_block_until
    fmt = (
        f"bestvideo[height<={height_cap}][vcodec^=avc1]+bestaudio"
        f"/bestvideo[height<={height_cap}]+bestaudio"
        f"/best[height<={height_cap}]"
        f"/best"
    )
    if YT_FMT_LEAN:
        # Uu tien avc1 <=30fps (giai ma re hon nhieu so voi 60fps/VP9/AV1) + audio <=72kbps (ta ha xuong 16kHz mono nen
        # khong can audio nang). Khong khop thi tu roi ve chuoi goc phia sau -> khong the lam hong viec phat.
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
    if _nocookie_skip_until and time.monotonic() >= _nocookie_skip_until:
        # het thoi gian nghi -> dem lai tu dau (truoc day 1 loi le la nghi tiep ngay)
        _nocookie_skip_until = 0.0
        _nocookie_fails = 0
    # Lam nong nen + co cookies: bo qua cach khong cookies (android_vr an het ngan sach 20s roi moi toi cach co cookies)
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
    for name, client, use_cookies, runtimes in attempts:
        if errors and time.monotonic() - t_start > budget:
            break
        try:
            res = _extract_with(video_url, _build_ydl_opts(fmt, client, use_cookies, runtimes, YT_PROXY))
            if _yt_preferred != name:
                print(f"[resolve] cach '{name}' thanh cong", flush=True)
            _yt_preferred = name
            if not use_cookies:
                _nocookie_fails = 0
                _nocookie_skip_until = 0.0
            return res
        except Exception as e:
            msg = str(e)
            print(f"[resolve] cach '{name}' that bai: {msg[:140]}", flush=True)
            errors.append(f"{name}: {msg[:110]}")
            # Lam nong nen that bai KHONG duoc ghi vao trang thai chung (chan IP / bo cach khong cookies)
            if not background and not use_cookies and "player response" in msg:
                _nocookie_fails += 1
                if _nocookie_fails >= YT_NOCOOKIE_MAX_FAILS:
                    _nocookie_skip_until = time.monotonic() + YT_NOCOOKIE_COOLDOWN
            if any(h in msg for h in _NO_RETRY_STRONG):
                permanent = True
                break
            if any(h in msg for h in _NO_RETRY_WEAK):
                weak_hits += 1
                if weak_hits >= 2:
                    permanent = True
                    break
    if not permanent and not background:
        # video hong rieng le KHONG duoc xoa cach dang chay tot / khong duoc coi la IP bi chan
        # background (warmup): that bai luc khoi dong khong duoc khoa request that 60s
        _yt_preferred = None
        _joined = " | ".join(errors)
        if any(h in _joined for h in _BLOCK_HINTS) and not any(h in _joined for h in _AGE_HINTS):
            _yt_block_until = time.monotonic() + YT_BLOCK_BACKOFF
    raise ResolveError(" | ".join(errors)[:700] or "khong cach nao chay", permanent=permanent)


RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", "1800"))
WARM_TOP_N = int(os.environ.get("WARM_TOP_N", "1"))
_resolve_cache = {}   # key -> (ts, res, hieu_luc_den)
_neg_cache = {}       # video_url -> (het_han, thong_bao)  (video hong han, tranh dap lai 5 cach x 45s)
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


def _kill_stream():
    """Tat ffmpeg dang phat va BO tham chieu: yeu cau /stream dang cho byte dau tien nhin thay _cur_proc doi
    -> hieu la minh bi thay the va dung han (truoc day no tuong ffmpeg loi, resolve lai roi sinh ffmpeg moi de len nguoi dung vua chuyen)."""
    global _cur_proc
    with _cur_lock:
        p, _cur_proc = _cur_proc, None
    if p is not None and p.poll() is None:
        print("[stream] roi video -> tat ffmpeg cu", flush=True)
        try:
            p.kill()
        except Exception:
            pass


def _idle_for(sec):
    return _active_streams == 0 and not _br_busy() and time.monotonic() - _last_req >= sec


AUTO_UPDATE = os.environ.get("YTDLP_AUTO_UPDATE", "1") == "1"
UPDATE_HOURS = float(os.environ.get("YTDLP_UPDATE_HOURS", "12"))
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
    """Ban yt-dlp moi nhat tren GitHub (1 request JSON nho) hoac None neu khong hoi duoc."""
    try:
        req = urllib.request.Request("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                                     headers={"User-Agent": "relay-server", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read(1_000_000).decode("utf-8", "replace")).get("tag_name") or None
    except Exception:
        return None


def _update_ytdlp(min_gap=600):
    """Chay pip upgrade yt-dlp. Tra ve True neu phien ban thay doi. Khong chay lai trong min_gap giay."""
    global _last_update_try
    with _update_lock:
        if time.monotonic() - _last_update_try < min_gap and _last_update_try:
            return False
        _last_update_try = time.monotonic()
        before = _ytdlp_version()
        # Khong co ban moi -> khong can pip (pip giai quyet phu thuoc + tai + cai tren 0.1 CPU ton hang phut CPU va >100MB RAM, moi 12 gio)
        latest = _ytdlp_latest()
        if latest and _ver_tuple(before) and _ver_tuple(before) >= _ver_tuple(latest):
            print(f"[update] yt-dlp {before} da la ban moi nhat ({latest}) -> bo qua pip", flush=True)
            return False
        mi = _mem_info()
        if mi and mi[0] > mi[1] * 0.55:
            print(f"[update] RAM dang dung {mi[0]:.0f}/{mi[1]:.0f}MB, khong du cho pip -> de lan sau", flush=True)
            return False
        try:
            cmd = [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "--quiet", "yt-dlp[default]"]
            if shutil.which("nice"):  # preexec_fn khong an toan khi dang co nhieu thread
                cmd = ["nice", "-n", "19"] + cmd
            # timeout 240s cu: tren 0.1 CPU pip hay chay lau hon -> bi kill GIUA luc go ban cu/cai ban moi -> yt-dlp hong
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


def _restart_when_idle():
    for _ in range(360):
        if _idle_for(20):
            print("[update] khoi dong lai de nap yt-dlp moi", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        time.sleep(10)


def _update_and_maybe_restart(min_gap=600):
    for _ in range(360):
        if _idle_for(90):  # pip upgrade ngon CPU: chi chay khi may that su ranh, khong chen vao luc dang dung
            break
        time.sleep(10)
    else:
        # truoc day het 1 gio van chay pip luc may dang ban -> giat/OOM tren goi Free 512MB
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
    """Goi khi gap loi extract: cap nhat nen (toi da 1 lan / 30 phut, va CHI 1 thread cho cung luc -
    truoc day moi request loi sinh them 1 thread cho toi 1 gio)."""
    if AUTO_UPDATE and not _update_pending.is_set():
        _update_pending.set()

        def _run():
            try:
                _update_and_maybe_restart(1800)
            finally:
                _update_pending.clear()
        threading.Thread(target=_run, daemon=True).start()


def _res_expiry(res):
    """URL googlevideo co tham so expire=<epoch>: tra ve moc monotonic het han tru 120s du phong (None neu khong co).
    Xet CA URL video lan audio va lay moc som nhat (truoc day chi xet URL video -> audio het han truoc van bi giu trong cache)."""
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
                until = min(until, exp)  # khong giu URL da het han (TTL cung 30 phut truoc day co the dai hon han that)
            elif res[0] and "googlevideo.com" not in res[0]:
                until = min(until, ts + 480)  # file RapidAPI chi ton tai ~10 phut
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
    """Do cao client thuc su dang xin (lan /stream gan nhat) de lan lam nong TRUNG khoa cache.
    Truoc day warm dung MIN_HEIGHT=240 con stream mac dinh 360 -> khong bao gio trung, lam nong phi cong ma van cham YouTube."""
    try:
        floor = int(os.environ.get("MIN_HEIGHT", "240"))
        return str(max(int(_last_height_cap or os.environ.get("WARM_HEIGHT_CAP", "360")), floor))
    except ValueError:
        return "360"


YT_WARM_MAX_COST = float(os.environ.get("YT_WARM_MAX_COST", "25"))
_resolve_ema = 0.0   # thoi gian resolve yt-dlp trung binh (giay); may yeu -> cao


def _mem_frac():
    mi = _mem_info()
    return (mi[0] / mi[1]) if mi else 0.0


def _trim_caches():
    """RAM sat tran: bo cac cache dung lai duoc (khung hinh da ve, dai anh, JPEG, trang da dung) truoc khi chay viec nang (yt-dlp + JS runtime)."""
    _band_cache.clear()
    _jpeg_cache.clear()
    _S.pages.clear()
    gc.collect()


def _warm_worker(video_ids, height_cap):
    if _resolve_ema > YT_WARM_MAX_COST:
        # may yeu: 1 lan lam nong ~ _resolve_ema giay CPU day dac trong luc nguoi dung dang chon video khac -> do cho luot that cua ho
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


def load_playlist():
    """Đọc PLAYLIST_FILE, trả về list các link (đã bỏ dòng trống/comment)."""
    if not os.path.exists(PLAYLIST_FILE):
        return []
    with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    return [ln for ln in lines if ln and not ln.startswith("#")]


@app.route("/random")
def random_link():
    links = load_playlist()
    if not links:
        return jsonify({
            "error": f"Playlist rỗng hoặc chưa có file {os.path.basename(PLAYLIST_FILE)} - "
                     f"thêm mỗi link TikTok 1 dòng vào file đó rồi thử lại."
        }), 404
    return jsonify({"url": random.choice(links)})


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
    """Chi nhan http(s) hoac ID YouTube tran (chan file://, ftp://... va chuoi la)."""
    u = (u or "").strip()
    if not u:
        return ""
    if _YT_BARE_ID.match(u):
        return f"https://www.youtube.com/watch?v={u}"
    if "://" not in u:
        u = "https://" + u
    if not re.match(r"^https?://", u, re.I):
        return ""
    return u if _url_is_public(u) else ""   # chan localhost / mang noi bo / metadata cloud (SSRF)


def _ffmpeg_cmd(v_url, a_url, v_hdr, a_hdr, is_yt, w, h, fps, q, ar):
    """Dung LIST thay vi chuoi + shlex.split: w/h/... tu query khong the chen them tham so ffmpeg
    (truoc day w="320 -y /tmp/x" co the ghi file tuy y)."""
    def inp(url, hdr, vid=False):
        # whitelist giao thuc: playlist HLS/DASH do nguon doc hai khong duoc tro sang file:// hay giao thuc la
        a = ["-protocol_whitelist", "http,https,tcp,tls,crypto,data,httpproxy",
             "-reconnect", "1", "-reconnect_at_eof", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "5", "-rw_timeout", "20000000"]
        if YT_PROXY and is_yt:
            a += ["-http_proxy", YT_PROXY]
        if hdr:
            a += ["-headers", hdr]
        a += ["-probesize", "500000", "-analyzeduration", "500000", "-fflags", "+nobuffer",
              "-threads", str(FFMPEG_THREADS)]
        if vid and STREAM_FAST_DECODE:
            a += ["-skip_loop_filter", "all"]   # CHI gan cho input co video (input chi co audio se bi ffmpeg bao "option khong dung")
        if vid and STREAM_SKIP_NOREF and fps <= 12:
            a += ["-skip_frame", "noref"]
        if STREAM_READRATE > 0:
            # Giu nhip THOI GIAN THUC: khong de ffmpeg doc/giai ma nhanh gap 10 lan o 30s dau roi dot ngot tut toc do
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
        # giu ti le khung hinh (Shorts doc khong bi be ngang), them vien den thay vi keo gian
        vf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    else:
        vf = f"fps={fps},scale={w}:{h}:flags=fast_bilinear"
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + inp(v_url, v_hdr, vid=True)
    if a_url:
        cmd += inp(a_url, a_hdr) + ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-threads", str(FFMPEG_THREADS), "-filter_threads", str(FFMPEG_THREADS),
            "-vf", vf, "-pix_fmt", "yuvj420p", "-c:v", "mjpeg", "-q:v", str(q),
            "-c:a", "pcm_s16le", "-ar", str(ar), "-ac", "1",
            "-flush_packets", "1", "-f", "avi", "pipe:1"]
    return cmd


def _nice_prefix():
    """Ha do uu tien ffmpeg: tren 0.1 CPU, Python (pacing, nhan request) can duoc chay truoc de khung hinh khong bi giat
    khi quota CPU sap het. 'nice' exec thang vao ffmpeg nen pid giu nguyen (kill/terminate van dung)."""
    if STREAM_NICE > 0 and shutil.which("nice"):
        return ["nice", "-n", str(STREAM_NICE)]
    return []


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
        # khung ra w x h voi che do "fit": nguon 16:9 can cao ~ w*9/16, nguon doc can cao ~ h -> lay cao hon trong hai, khong hon.
        _cap = min(_cap, max(h, int(w * 9 / 16)))
    height_cap = str(max(_cap, _env_int("MIN_HEIGHT", 240)))
    _last_height_cap = height_cap
    fps = _int_arg("fps", 15, 1, 30)
    _fmin = _env_int("STREAM_FPS_MIN", 0)
    if _fmin:
        fps = min(30, max(fps, _fmin))
    fps = max(1, min(fps, STREAM_FPS_MAX))
    q = min(31, max(_int_arg("q", _env_int("MJPEG_Q", 6), 2, 31), _env_int("STREAM_Q_MIN", 2)))
    ar = _int_arg("ar", _env_int("AUDIO_RATE", 16000), 4000, 22050)
    bps_cap = _env_int("STREAM_MAX_BPS_CAP", 3000000)
    target_bps = _int_arg("bps", _env_int("STREAM_MAX_BPS", 1000000), 25000, max(25000, bps_cap))
    burst_bytes = _env_int("STREAM_BURST_BYTES", 1536 * 1024)
    is_yt = _is_youtube(video_url)

    if _mem_frac() > 0.70:
        print(f"[stream] RAM {_mem_str()} cao -> don cache trinh duyet truoc khi resolve", flush=True)
        _trim_caches()

    if KILL_ON_LEAVE:
        # yeu cau moi = client da bo video cu: nha CPU/RAM NGAY (truoc day ffmpeg cu van chay suot luc resolve ~vai chuc giay)
        _kill_stream()

    # URL lay tu cache co the da chet (het han / IP doi) -> ffmpeg thoat ngay khong ra byte nao,
    # truoc day client nhan 200 rong va cache hong duoc giu den het TTL. Gio: neu byte dau tien khong den,
    # bo cache, resolve lai DUNG 1 LAN roi thu tiep.
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

        # URL luong cuoi cung (sau redirect cua yt-dlp) cung phai la dia chi cong cong
        if not all(_url_is_public(u) for u in (v_url, a_url) if u):
            print("[stream] URL luong tro vao host noi bo -> chan", flush=True)
            _cache_drop(video_url, height_cap)
            return jsonify({"error": "blocked_host", "detail": "nguon tro toi dia chi noi bo"}), 403

        cmd = _ffmpeg_cmd(v_url, a_url, v_hdr, a_hdr, is_yt, w, h, fps, q, ar)
        with _cur_lock:
            _old = _cur_proc
            if _old is not None and _old.poll() is None:
                print("[stream] co yeu cau moi -> tat ffmpeg cu de nhuong CPU", flush=True)
                try:
                    _old.kill()
                except Exception:
                    pass
            try:
                proc = subprocess.Popen(_nice_prefix() + cmd, stdout=subprocess.PIPE, bufsize=1 << 20)
            except OSError as e:
                # truoc day: khong co ffmpeg -> 500 HTML khong ro nguyen nhan
                print(f"[stream] khong chay duoc ffmpeg: {e}", flush=True)
                return jsonify({"error": "ffmpeg_missing", "detail": str(e)[:120]}), 500
            _cur_proc = proc
        _streams_add(1)
        try:
            fcntl.fcntl(proc.stdout.fileno(), 1031, 1 << 20)
        except Exception:
            pass
        try:
            first = proc.stdout.read1(65536)
        except Exception:
            first = b""
        if first:
            print(f"[stream] byte dau tien sau {time.monotonic() - t_req:.1f}s ke tu luc nhan yeu cau", flush=True)
            break
        with _cur_lock:
            superseded = _cur_proc is not proc   # co yeu cau khac (hoac /search, /br) da tat va thay the ffmpeg nay
        _terminate(proc)
        _streams_add(-1)
        proc = None
        if superseded:
            # KHONG resolve lai / KHONG sinh ffmpeg moi: se de len luong cua nguoi dung vua chuyen sang video khac
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

    def _release():
        with _active_lock:
            if released[0]:
                return
            released[0] = True
        _streams_add(-1)

    def generate():
        t0 = time.monotonic()
        sent = 0
        chunk = first
        # chan doan: ffmpeg_wait = thoi gian cho ffmpeg ra du lieu (CPU/nguon cham), client_wait = thoi gian cho thiet bi nhan (WiFi/ESP cham)
        w_ff = w_cl = 0.0
        last_log = t0
        last_sent = 0
        try:
            while chunk:
                sent += len(chunk)
                expected_elapsed = max(0, sent - burst_bytes) / target_bps
                actual_elapsed = time.monotonic() - t0
                if expected_elapsed > actual_elapsed:
                    time.sleep(expected_elapsed - actual_elapsed)
                t1 = time.monotonic()
                yield chunk
                t2 = time.monotonic()
                chunk = proc.stdout.read1(65536)
                t3 = time.monotonic()
                w_cl += t2 - t1
                w_ff += t3 - t2
                if t3 - last_log >= 10:
                    span = t3 - last_log
                    print(f"[stream-diag] {span:.0f}s: ffmpeg_wait={w_ff:.1f}s client_wait={w_cl:.1f}s "
                          f"rate={(sent - last_sent) / span / 1000:.0f}KB/s fps={fps} q={q}", flush=True)
                    last_log, last_sent, w_ff, w_cl = t3, sent, 0.0, 0.0
        finally:
            _release()
            _terminate(proc)

    def _reap():
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        _release()

    resp = Response(generate(), mimetype="video/avi", headers={"Cache-Control": "no-store"})
    resp.call_on_close(_reap)
    return resp


class _DiagLogger:
    """Gom toan bo log cua yt-dlp (ke ca WARNING) de hien tren trinh duyet."""
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
    """Mo tren trinh duyet: /diag?v=<id video>&mode=cur|def|noremote|deno
    cur      = giu nguyen cau hinh dang dung cua server (js_runtimes + remote_components)
    def      = de yt-dlp tu chon JS runtime (khong ep js_runtimes / remote_components)
    noremote = giong cur nhung bo remote_components
    deno     = chi bat deno"""
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
    # chay trong _resolve_gate nhu moi lan yt-dlp khac: tranh 2 yt-dlp cung ghi de file cookies / cung an RAM
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
_IMG_PIXEL_CAP = int(os.environ.get("BR_MAX_IMG_PIXELS", "4000000"))             # PNG/GIF/WebP...: ~16MB RAM moi anh
_IMG_JPEG_PIXEL_CAP = int(os.environ.get("BR_MAX_JPEG_PIXELS", "40000000"))      # JPEG co draft -> RAM nho
try:
    from PIL import Image, ImageDraw, ImageFont
    Image.MAX_IMAGE_PIXELS = max(_IMG_PIXEL_CAP, _IMG_JPEG_PIXEL_CAP) * 2   # PIL chi canh bao/tu choi sau muc nay; kiem tra chinh nam trong _decode_image
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
BR_IMG_WAIT = float(os.environ.get("BR_IMG_WAIT", "2"))
BR_MAX_HTML = int(os.environ.get("BR_MAX_HTML", "700000"))
BR_MAX_BLOCKS = int(os.environ.get("BR_MAX_BLOCKS", "1500"))   # ~3x so khoi can de day 9000px -> dung parse som
BR_BUSY_SEC = float(os.environ.get("BR_BUSY_SEC", "20"))
_IMG_MAX = max(256, OUT_W * BR_SS)   # lon hon chieu rong khung ve (OUT_W*BR_SS - le) la phi RAM: 720 -> 640 khi BR_SS=2
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
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


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
    """Doc than phan hoi theo tung manh NGAY KHI co du lieu (read1) de han chot thoi gian duoc kiem tra thuong xuyen.
    r.iter_content(65536) cho den khi du 64KB hoac het du lieu -> nguon nho giot 1 byte/0.5s lam han chot khong bao gio chay (do duoc: 100s)."""
    raw = r.raw
    if hasattr(raw, "read1"):
        while time.monotonic() < deadline:
            try:
                c = raw.read1(65536, decode_content=True)
            except TypeError:   # urllib3 ban cu: read1 khong nhan decode_content
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
        # giu ket noi TLS song giua cac lan bam (truoc day moi lan mo 1 ket noi + bat tay TLS moi)
        # Tu theo redirect tung buoc de kiem tra host dich o MOI buoc (requests mac dinh theo thang -> SSRF qua 302),
        # va co han chot tong thoi gian (timeout cua requests chi tinh cho tung lan recv: nguon nho giot van treo duoc thread).
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
    r"tiktok\.com/(@[^/]+/video/|t/|v/)|vm\.tiktok\.com/|vt\.tiktok\.com/|"
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
    """Bo comment / script / style / noscript / svg / template trong THOI GIAN TUYEN TINH.
    Regex cu `<script...>.*?</script>` quet toi het chuoi cho MOI the mo khong dong: do thuc te 600KB co 3000 the <script>
    khong dong mat 11.9 giay CPU (= hon 2 phut tren 0.1 CPU) va regex giu GIL nen dong bang ca server, ke ca luong video dang phat.
    The khong dong -> bo het phan con lai (trinh duyet that cung an het phan sau, va HTMLParser cung bo qua no)."""
    out, pos, n = [], 0, len(s)
    while True:
        m = _HEAVY_OPEN.search(s, pos)
        if m is None:
            out.append(s[pos:])
            break
        out.append(s[pos:m.start()])
        tag = m.group(1)
        if tag is None:                      # <!-- ... -->
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
    """Nap HTML cho _Doc theo tung manh va DUNG SOM khi da du khoi de lap day trang (_render cat o ~9000px).
    Trang dai (Wikipedia, bao) truoc day van phai parse het ~700KB bang HTMLParser thuan Python."""
    step = 24000
    for i in range(0, len(body), step):
        d.feed(body[i:i + step])
        if len(d.blocks) >= BR_MAX_BLOCKS:
            break
    d.close()
    d.flush()
# LUU Y: "embed" la the RONG (khong co the dong) -> khong duoc dua vao day, neu khong skip tang len mai mai
# va TOAN BO phan con lai cua trang bien mat (loi cu).
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
        """Dong <button> dang mo (ke ca khi trang quen </button>: truoc day moi chu phia sau bi nuot vao nhan nut)."""
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
<p>Dán link video (YouTube, TikTok, Facebook...) để phát.</p>
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


_IMG_DECODE_SEM = threading.BoundedSemaphore(2)   # giai ma anh la viec CPU: chay song song tren 0.1 CPU khong nhanh hon, chi ton RAM


def _decode_image(raw):
    """bytes -> anh RGB da thu nho (toi da _IMG_MAX), hoac None.
    Tu choi anh qua nhieu pixel (decompression bomb). Do thuc te: PNG 25MP chi nang 84KB nhung can +97MB RAM khi giai ma,
    6 anh song song = +575MB -> OOM tren goi 512MB. JPEG thi giai ma thang o kich thuoc nho (draft) nen duoc phep lon hon."""
    try:
        with _IMG_DECODE_SEM:
            im = Image.open(io.BytesIO(raw))
            px = im.width * im.height
            if px > (_IMG_JPEG_PIXEL_CAP if im.format == "JPEG" else _IMG_PIXEL_CAP):
                return None
            try:
                im.draft("RGB", (_IMG_MAX, _IMG_MAX))  # JPEG: giai ma thang o kich thuoc nho (nhanh, it RAM)
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
        # anh cham khong con chan trang: hien chu ngay, anh den sau se xuat hien o lan cuon/ve khung ke tiep
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
    """-> (doc, imgs, final_url)"""
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
                title = _yt_oembed_title(url)   # JSON ~300B thay vi tai 150KB trang watch chi de lay tieu de
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
        im = _decode_image(raw) if _PIL_OK else None   # truoc day khong thu nho/khong gioi han pixel
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
    body = _strip_heavy(body)[:BR_MAX_HTML]  # HTMLParser thuan Python rat cham voi script/css khong lo
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
    """Chi BO CUC (do chu, ngat dong, toa do) - khong ve ca trang dai nua (truoc day: anh RGB cao 18000px = ~35MB
    moi trang, ve het roi moi cat). _jpeg ve rieng phan dang nhin thay."""
    S = BR_SS
    W = (OUT_H if portrait else OUT_W)
    VH = (OUT_W if portrait else OUT_H)
    Ws = W * S
    M = 4 * S
    base = BR_FONT_PX * S
    ops = []   # (y0, y1, loai, ...)
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
            ops.append((y, y + nh, "img", M, im.resize((nw, nh), Image.BILINEAR)))   # do: LANCZOS cham gap ~2.1 lan, khac biet khong thay duoc o 320px/JPEG q65
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
    """Ve san 1 'dai' cao 2 man hinh 1 lan roi cache: cuon tiep trong dai nay chi con cat + nen JPEG (~vai ms),
    khong ve lai chu moi khung."""
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
            v = img.reduce(S)  # box filter C-toc do, thay LANCZOS
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
        _lk.release()  # nha khoa luc cho mang: cuon / ve khung / nut khac khong con bi do cung 1 trang dang tai
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
        # giu 3 trang gan nhat (dict giu thu tu chen -> bo cai cu nhat), truoc day >5 trang thi xoa SACH ca nut "back"
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
    """Cung 1 URL dang tai (vd firmware timeout roi gui lai) -> dung chung 1 lan tai, khong tai chong nhau."""
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


# ======================= ENGINE CHROMIUM THAT (Playwright) =======================
# BR_ENGINE=auto (mac dinh): dung Chromium neu co Playwright VA RAM container >= BR_CR_MIN_MB, nguoc lai dung bo HTML tho o tren.
# BR_ENGINE=chromium: ep dung (Free 512MB co the bi OOM). BR_ENGINE=html: tat han.
# Moi loi cua Chromium (thieu binary, het RAM, treo...) -> tu lui ve bo HTML, thu lai sau BR_CR_RETRY giay.
# Playwright sync API gan voi luong tao ra no, ma Flask chay moi request 1 luong -> moi thao tac Chromium di qua 1 luong rieng.
BR_ENGINE = os.environ.get("BR_ENGINE", "auto").strip().lower()
BR_CR_ZOOM = max(1.0, min(3.0, float(os.environ.get("BR_CR_ZOOM", "1.25"))))     # viewport CSS = khung hinh * zoom, roi thu nho ve khung hinh
BR_CR_TIMEOUT = float(os.environ.get("BR_CR_TIMEOUT", "15"))                      # giay cho tai trang
BR_CR_IDLE = float(os.environ.get("BR_CR_IDLE", "120"))                           # nhan roi bay nhieu giay -> tat Chromium tra RAM
BR_CR_MIN_MB = float(os.environ.get("BR_CR_MIN_MB", "900"))                       # auto: RAM container toi thieu de bat Chromium
BR_CR_MEM_MAX = float(os.environ.get("BR_CR_MEM_MAX", "0.90"))                    # RAM dung/gioi han vuot muc nay -> khong mo / tat Chromium
BR_CR_RETRY = float(os.environ.get("BR_CR_RETRY", "120"))
BR_CR_SOFT = float(os.environ.get("BR_CR_SOFT", "0.80"))                          # RAM vuot muc nay -> khoi dong lai Chromium (mo lai dung trang dang xem)
BR_CR_RECYCLE = int(os.environ.get("BR_CR_RECYCLE", "30"))                        # sau chung nay luot chuyen trang -> khoi dong lai (Chromium chay lau bi phinh)
BR_CR_JS_MB = int(os.environ.get("BR_CR_JS_MB", "128"))                           # tran heap V8
BR_CR_SINGLE = os.environ.get("BR_CR_SINGLE", "0") == "1"                         # --single-process: tiet kiem RAM nhat nhung de crash
_CR_BLOCK_HOSTS = ("doubleclick.net", "googlesyndication.com", "googleadservices.com", "google-analytics.com",
                   "googletagmanager.com", "adservice.google.", "adnxs.com", "taboola.com", "outbrain.com",
                   "scorecardresearch.com", "hotjar.com", "connect.facebook.net")

_CR_INIT_JS = """(()=>{
window.open=function(u){if(u){try{location.href=u}catch(e){}}return null;};
document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[target]');
if(a&&a.target!=='_self')a.target='_self';},true);
})();"""

# o dang focus la gi: "line" (go xong Enter de gui), "multi" (nhieu dong, khong Enter), null (khong phai o nhap)
_CR_FOCUS_JS = """()=>{const e=document.activeElement;if(!e)return null;const t=e.tagName;
if(t==='INPUT'){const ty=(e.type||'text').toLowerCase();
if(['button','submit','checkbox','radio','image','reset','file','range','color'].includes(ty))return null;return 'line';}
if(t==='TEXTAREA'){const f=e.form,nm=(e.name||'').toLowerCase();
if(f&&(e.rows<=1||['q','query','search','s'].includes(nm)||f.getAttribute('role')==='search'))return 'line';return 'multi';}
if(e.isContentEditable)return 'multi';return null;}"""

_CR_PAGE_CSS = "body{font-family:sans-serif;margin:10px;font-size:15px}input{width:70%;font-size:15px;padding:4px}button{font-size:15px;padding:4px 8px}li{margin:6px 0}"


def _cr_home_html():
    act = html.escape(BR_SEARCH_URL.split("?")[0], quote=True)
    return ("<!doctype html><meta charset=utf-8><title>Trình duyệt</title><style>%s</style><h2>Trình duyệt</h2>"
            "<form action=\"%s\" method=\"get\"><input name=\"q\" placeholder=\"Tìm kiếm hoặc gõ link\"> <button>Tìm</button></form>"
            "<p>Dán link video (YouTube, TikTok, Facebook...) để phát.</p><ul>"
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
    """Moi phuong thuc (tru __init__) CHI duoc goi tren luong _cr_pool."""

    def __init__(self):
        self.pw = self.browser = self.ctx = self.page = None
        self.vp = None
        self.play = None          # link video bi chan o lop mang -> firmware se phat bang ffmpeg
        self.focus = None
        self.home = False
        self.err_url = ""
        self.navs = 0
        self.born = self.last_used = time.monotonic()

    # ---- khoi dong / tat ----
    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise _CrUnavailable("chua cai playwright: %s" % e)
        args = ["--disable-gpu", "--disable-software-rasterizer", "--disable-dev-shm-usage", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--disable-default-apps", "--disable-translate",
                "--disable-component-update", "--disable-breakpad", "--disable-notifications", "--mute-audio",
                "--no-first-run", "--no-default-browser-check", "--metrics-recording-only", "--force-color-profile=srgb",
                "--disable-features=TranslateUI,MediaRouter,AudioServiceOutOfProcess,OptimizationHints,Prerender2",
                "--renderer-process-limit=1", "--disk-cache-size=1", "--media-cache-size=1",
                "--js-flags=--max-old-space-size=%d" % BR_CR_JS_MB]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args += ["--no-sandbox", "--no-zygote"]   # container chay root: Chromium khong khoi dong duoc neu con sandbox
        if BR_CR_SINGLE:
            args.append("--single-process")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(headless=True, args=args)
        except Exception as e:
            raise _CrUnavailable("khong mo duoc Chromium (da chay 'playwright install chromium'?): %s" % str(e)[:200])
        self.ctx = self.browser.new_context(
            user_agent=UA, viewport={"width": 400, "height": 212}, device_scale_factor=1, accept_downloads=False,
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

    # ---- an toan / tiet kiem ----
    def _on_page(self, p):
        if self.page is not None and p is not self.page:   # popup / tab moi: dong ngay (window.open da bi doi thanh dieu huong cung tab)
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
            if not _url_is_public(url):   # chan SSRF: khong cho trinh duyet cham mang noi bo (ke ca qua redirect / subresource)
                return route.abort("blockedbyclient")
            rt = req.resource_type
            if rt in ("media", "font") or (rt == "image" and not BR_IMAGES):
                return route.abort()
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if any(h in host for h in _CR_BLOCK_HOSTS):
                return route.abort()
            if rt == "document" and req.is_navigation_request() and req.frame.parent_frame is None and _is_video_url(url):
                self.play = url   # link video: khong tai trang xem nang, giao cho /br/play (tra trang "Video" nho thay vi loi mang)
                return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=_cr_video_html(url))
            route.continue_()
        except Exception:
            try:
                route.abort()
            except Exception:
                pass

    # ---- dieu huong ----
    def ensure(self, portrait, home=True):
        w, h = (OUT_H, OUT_W) if portrait else (OUT_W, OUT_H)
        vp = (int(w * BR_CR_ZOOM), int(h * BR_CR_ZOOM))
        if vp != self.vp:
            self.page.set_viewport_size({"width": vp[0], "height": vp[1]})
            self.vp = vp
        if home and self.page.url == "about:blank" and not self.home:
            self.go("about:home")

    def cur_url(self):
        u = self.page.url
        if u.startswith("chrome-error:"):   # trang loi mang cua Chromium -> bao dung link nguoi dung da mo
            return self.err_url or u
        return "about:home" if (self.home and u == "about:blank") else u

    def set_html(self, body):
        try:
            self.page.set_content(body, wait_until="domcontentloaded")
        except Exception:   # "Execution context was destroyed": dang co 1 lan dieu huong dang do -> cho xong roi thu lai
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            self.page.set_content(body, wait_until="domcontentloaded")

    def settle(self):
        try:
            self.page.wait_for_load_state("load", timeout=3000)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=800)
        except Exception:
            pass
        self.page.wait_for_timeout(120)

    def go(self, url):
        self.play, self.focus = None, None
        if url == "about:home":
            self.set_html(_cr_home_html())
            self.home = True
            return
        self.home = False
        self.err_url = url
        self.navs += 1
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=BR_CR_TIMEOUT * 1000)
        except Exception as e:
            msg = str(e)
            if self.play:
                return
            if not ("ERR_ABORTED" in msg or "Timeout" in msg):   # het gio / bi huy: cu hien phan da tai duoc
                self.set_html(_cr_error_html(msg.splitlines()[0] if msg else "loi", url))
                return
        self.settle()

    def get_focus(self):
        try:
            return self.page.evaluate(_CR_FOCUS_JS)
        except Exception:
            return None

    # ---- khung hinh ----
    def shot(self, portrait):
        W, VH = (OUT_H, OUT_W) if portrait else (OUT_W, OUT_H)
        raw = self.page.screenshot(type="jpeg", quality=80, caret="hide", animations="disabled")
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.size != (W, VH):
            im = im.resize((W, VH), Image.LANCZOS)   # thu nho (zoom) co loc -> chu ro hon la ve thang o co nho
        try:
            sy, total, ih = self.page.evaluate(
                "()=>[window.scrollY,document.documentElement.scrollHeight,window.innerHeight]")
        except Exception:
            sy, total, ih = 0, 0, 1
        if total > ih + 4:
            d = ImageDraw.Draw(im)
            bh = max(10, int(VH * ih / total))
            by = int(min(1.0, sy / max(1.0, total - ih)) * (VH - bh))
            d.rectangle((W - 2, by, W - 1, by + bh), fill=(110, 110, 110))
        q = BR_JPEG_Q
        while True:
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q)
            if buf.tell() <= MAX_JPEG or q <= 25:
                return buf.getvalue()
            q -= 10

    # ---- 1 thao tac -> (jpg, edit, play, url) ----
    def act(self, action, a, portrait, resume=""):
        self.last_used = time.monotonic()
        if resume and action != "open" and self.page.url == "about:blank":
            self.go(resume)   # vua khoi dong lai de nha RAM / het nhan roi -> mo lai dung trang dang xem
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
            pg.wait_for_timeout(250)
            if pg.url != old:
                self.home = False
                self.navs += 1
                self.settle()
            self.focus = self.get_focus()
        elif action == "scroll":
            dy = _f(a, "dy") * z
            if dy:
                before = pg.evaluate("window.scrollY")
                pg.evaluate("d=>window.scrollBy({top:d,behavior:'instant'})", dy)
                if pg.evaluate("window.scrollY") == before:   # trang cuon trong 1 khung con (SPA) -> thu lan chuot
                    pg.mouse.move(self.vp[0] / 2, self.vp[1] / 2)
                    pg.mouse.wheel(0, dy)
                pg.wait_for_timeout(80)
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
                pass
            if pg.url in ("about:blank", ""):
                self.go("about:home")
        elif action == "reload":
            if self.cur_url() == "about:home":
                self.go("about:home")
            else:
                self.play = None
                try:
                    pg.reload(wait_until="domcontentloaded", timeout=BR_CR_TIMEOUT * 1000)
                except Exception:
                    pass
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
    """Tat Chromium de nhuong RAM (chay xong luot dang do roi moi dong). resume=True: lan sau mo lai dung trang dang xem
    (het nhan roi / don RAM); False: da roi trang web (sang YouTube, dong, loi) -> khong nho."""
    global _cr, _cr_resume
    _cr_resume = _cr_last_url if (resume and _cr_last_url not in ("", "about:home")) else ""
    c, _cr = _cr, None
    if c is not None:
        try:
            _cr_pool.submit(c.close)
        except Exception:
            pass


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
        _trim_caches()   # nha cache cua bo HTML truoc khi nap Chromium
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
        _cr_close_async(resume=True)   # khung hinh nay van tra binh thuong; lan sau mo lai dung trang nay
    _S.url = url   # de X-Url tra dung (khi lui ve HTML se reset _S)
    return jpg, edit, play


def _cr_run(fn, *args):
    return _cr_pool.submit(fn, *args).result(timeout=BR_CR_TIMEOUT + 40)


def _act(action, a, portrait):
    """-> (jpg, edit, play_url). Chromium that neu bat duoc, loi gi cung tu lui ve bo HTML."""
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
                _S.goto(u, push=False)   # tiep tuc dung trang dang xem bang bo HTML
    return _act_html(action, a, portrait)


def _act_html(action, a, portrait):
    """-> (jpg, edit, play_url)"""
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
    _br_last_activity = time.time()   # chi tinh "ban" sau khi qua xac thuc (truoc day ai goi bua cung khoa duoc cap nhat/lam nong)
    url = (request.args.get("u") or _br_last_url or "").strip()
    if not url or url.startswith("about:"):
        return Response("chua co trang nao dang mo - mo link video truoc", 409)
    if "://" not in url:
        url = "https://" + url
    print(f"[br] play {url}", flush=True)
    _cr_close_async()
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
    if KILL_ON_LEAVE and action in ("open", "tap", "type", "back", "reload"):
        _kill_stream()  # vua chuyen tu YouTube sang Google: tat ffmpeg cu de nhuong CPU
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
    h = {"X-Url": _clip_pct(urllib.parse.quote(url, safe=":/?&=%#"), 300), "X-Edit": "1" if edit else "0", "Cache-Control": "no-store"}
    if play:
        h["X-Play"] = _clip_pct(urllib.parse.quote(play, safe=":/?&=%#@,+;~-._!*()'$"), 700)
    return Response(jpg, mimetype="image/jpeg", headers=h)


def _log(*a):
    print("[br]", *a, flush=True)


_TR_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=vi&dt=t&q="
_tr_cache = {}
_tr_lock = threading.Lock()
_tr_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)


def _tr_one(text):
    """Dich 1 doan (tieu de + \\n + mo ta) sang tieng Viet -> (tieu_de, mo_ta). Cung cach cat nhu firmware cu."""
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
        # 8 luong cung ghi cache: khong khoa thi pop(next(iter(...))) co the no "dictionary changed size during iteration"
        with _tr_lock:
            if len(_tr_cache) >= 300:
                _tr_cache.pop(next(iter(_tr_cache)), None)
            _tr_cache[text] = res
    return res


@app.route("/tr", methods=["POST"])
def translate_batch():
    """Firmware gui ca danh sach ket qua Google 1 lan; server dich SONG SONG (truoc day ESP dich noi tiep tung ket qua, 10-14s).
    Vao: {"q": ["tieu de\\nmo ta", ...]}  Ra: {"r": [{"t": "...", "s": "..."}, ...]} (muc loi: t rong -> firmware giu ban goc)."""
    body = request.get_json(silent=True)
    q = body.get("q") if isinstance(body, dict) else None
    if not isinstance(q, list):   # truoc day body la list/chuoi -> AttributeError 500, hoac q la chuoi -> bi cat thanh tung ky tu
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
        print("[br] prewarm xong", flush=True)
    except Exception as e:
        _cause = repr(getattr(e, "__cause__", None) or getattr(e, "__context__", None) or "")
        print(f"[br] prewarm loi: {type(e).__name__}: {str(e)[:200]} | goc: {_cause[:300]}", flush=True)


def _warmup_once():
    """Sau khi khoi dong, tu lay link 1 video luc chua ai dung de nap cache giai ma chu ky YouTube (deno + player JS).
    Neu khong, lan bam video DAU TIEN sau moi lan deploy mat ~20s o buoc nay. Tat bang YT_WARMUP=0."""
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
    """Render Free ngu sau 15 phut khong co request; lan sau danh thuc mat 30-60s (phan lon ~40s truoc khung hinh dau).
    Tu goi /healthz qua URL cong khai moi KEEPALIVE_SEC giay de giu server thuc.
    Luu y: Free chi co 750 gio/thang CHUNG cho ca workspace -> chi bat o 1 service."""
    url = RELAY_URL.rstrip("/") + "/healthz"
    gap = max(60.0, float(os.environ.get("KEEPALIVE_SEC", "540")))
    time.sleep(60)
    while True:
        try:
            urllib.request.urlopen(url, timeout=15).read(16)
        except Exception as e:
            print(f"[keepalive] loi: {str(e)[:80]}", flush=True)
        time.sleep(gap)


if __name__ == "__main__":
    if os.environ.get("KEEPALIVE", "") == "1":
        threading.Thread(target=_keepalive_loop, daemon=True).start()
    if ROLE != "video":
        threading.Thread(target=_prewarm_browser, daemon=True).start()
    if os.environ.get("YT_WARMUP", "1") == "1" and ROLE != "browser":
        threading.Thread(target=_warmup_once, daemon=True).start()
    print(f"[update] yt-dlp hien tai: {_ytdlp_version()} | auto_update={AUTO_UPDATE} moi {UPDATE_HOURS}h", flush=True)
    if AUTO_UPDATE and ROLE != "browser":   # ROLE=browser khong dung yt-dlp: khong ton pip/khoi dong lai de cap nhat no
        threading.Thread(target=_update_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
