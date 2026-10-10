import subprocess
import sys
import shlex
import time
import random
import os
import traceback
import json
import re
import html
import urllib.request
import urllib.parse
import threading
import fcntl
import hmac
import queue
from flask import Flask, request, Response, jsonify
import yt_dlp
from werkzeug.serving import WSGIRequestHandler

app = Flask(__name__)

RELAY_URL = os.environ.get("RELAY_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://combined-relay-server-33xi.onrender.com"

# ROLE: all (mac dinh) | video (chi /stream /search /random) | browser (chi /br/*)
# Dung de chay 2 server rieng cung 1 file: server video khong bat Chrome, server browser khong chay ffmpeg.
ROLE = os.environ.get("ROLE", "all").strip().lower()


@app.before_request
def _role_gate():
    p = request.path
    if ROLE == "video" and p.startswith("/br"):
        return "disabled on this server (ROLE=video)", 404
    if ROLE == "browser" and p.startswith(("/stream", "/search", "/random")):
        return "disabled on this server (ROLE=browser)", 404
    # /diag va /myip chay yt-dlp / goi mang -> khong de nguoi la bam vao lam ton CPU va "dot" uy tin IP
    if p in ("/diag", "/myip") and BR_TOKEN and not hmac.compare_digest(request.args.get("k", ""), BR_TOKEN):
        return "forbidden - them ?k=BR_TOKEN vao URL", 403
    return None


@app.route("/")
def health_check():

    return (f"OK | {RELAY_URL} | AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS} "
            f"cookies={'yes' if os.path.exists(COOKIES_FILE) else 'NO'} | browser={'token-ok' if os.environ.get('BR_TOKEN') else 'NO-TOKEN'} | build=def3 | mem={_mem_str()}"), 200

PORT = int(os.environ.get("PORT", "8000"))   # Render cap cong qua bien moi truong PORT


@app.route("/healthz")
def healthz():
    return "ok", 200


def _mem_str():
    mi = _mem_info()
    return f"{mi[0]:.0f}/{mi[1]:.0f}MB" if mi else "?"


_SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(_SECRET_COOKIES):


    import shutil
    # Chi copy Secret lan dau. yt-dlp tu ghi lai cookies da duoc YouTube xoay vao file nay khi chay;
    # neu moi lan restart (auto-update yt-dlp moi 12h) lai chep de Secret cu len thi mat ban moi -> bi het han.
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


MJPEG_Q = os.environ.get("MJPEG_Q", "10")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "1")
AUDIO_RATE = os.environ.get("AUDIO_RATE", "16000")
print(f"[config] AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS}", flush=True)


def _api_get(endpoint, params):
    params = dict(params, key=YT_API_KEY)
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso_duration_to_seconds(d):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", d or "")
    if not m:
        return 0
    days, hrs, mins, secs = (int(x) if x else 0 for x in m.groups())
    return days * 86400 + hrs * 3600 + mins * 60 + secs


def search_via_api(query, n):
    data = _api_get("search", {
        "part": "snippet", "type": "video", "maxResults": n, "q": query,
    })
    items = data.get("items", [])
    ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    durations = {}
    if ids:
        vids = _api_get("videos", {"part": "contentDetails", "id": ",".join(ids)})
        for v in vids.get("items", []):
            durations[v["id"]] = _iso_duration_to_seconds(v["contentDetails"].get("duration"))
    return [
        {
            "id": vid,
            "title": html.unescape(it["snippet"]["title"]),
            "duration": format_duration(durations.get(vid)),
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


_search_cache = {}           # (q, n) -> (thoi_diem, ket_qua)
SEARCH_TTL = int(os.environ.get("SEARCH_TTL", "600"))


@app.route("/search")
def search():
    query = request.args.get("q", "")
    try:
        n = max(1, min(25, int(request.args.get("n", 5))))
    except ValueError:
        n = 5
    if not query:
        return jsonify([])
    _ck = (query.strip().lower(), n)
    _hit = _search_cache.get(_ck)
    if _hit and time.monotonic() - _hit[0] < SEARCH_TTL:
        print(f"[search] CACHE HIT {query!r}", flush=True)
        return jsonify(_hit[1])

    if YT_API_KEY:
        try:
            res = search_via_api(query, n)
            print(f"[search] YouTube API OK: {len(res)} ket qua")
            if res:
                if len(_search_cache) > 100:
                    _search_cache.clear()
                _search_cache[_ck] = (time.monotonic(), res)
            warm_results(res)
            return jsonify(res)
        except Exception as e:
            print(f"[search] YouTube API loi ({e}) - roi ve yt-dlp")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    results = []
    try:
        with _resolve_gate, yt_dlp.YoutubeDL(ydl_opts) as ydl:     # chung cong voi resolve: 1 yt-dlp / luc, khong tranh ghi cookies.txt
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
            for entry in info.get("entries", []):
                if not entry:
                    continue
                results.append({
                    "id": entry.get("id", ""),
                    "title": entry.get("title", ""),
                    "duration": format_duration(entry.get("duration")),
                })
    except Exception as e:
        print(f"[search] error: {e}")
        return jsonify([])

    if results:
        if len(_search_cache) > 100:
            _search_cache.clear()
        _search_cache[_ck] = (time.monotonic(), results)
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


# ---- Chuoi thu lai nhieu "player client" khi YouTube chan IP server ----
# Loi "Failed to extract any player response" thuong la YouTube nghi IP datacenter cua Render la bot,
# hoac cookies bi vo hieu (cookies dung chung nhieu IP se bi YouTube xoay/huy). Moi lan thu doi
# cach hoi YouTube mot kieu khac; cach nao thanh cong thi nho lai de lan sau thu truoc.
# (ten, player_client, dung cookies?, bat js runtime + ejs?)
_YT_ATTEMPTS_FULL = [
    # Cach khong cookies truoc: khong lam "chay" cookies tren IP datacenter
    ("android_vr",   ["android_vr"],     False, False),
    ("ios",          ["ios"],            False, False),
    ("web_embedded", ["web_embedded"],   False, True),
    # Cach can cookies de sau cung
    ("tv",           ["tv"],             True,  True),
    ("mac-dinh",     None,               True,  False),
    ("runtime+ejs",  None,               True,  True),
]
# Ban gon (mac dinh): 3 cach thay vi 6. Bo ios/web_embedded (khong cookies, gan nhu luon hong tren IP datacenter)
# va gop "mac-dinh" vao "runtime+ejs". Moi cach that bai ton ~15s CPU + RAM (deno/node), nen bot di giam OOM tren goi 512MB.
# Dat YT_ATTEMPTS_FULL=1 tren Render de dung lai day du 6 cach.
_YT_ATTEMPTS_LITE = [
    ("android_vr",   ["android_vr"],     False, False),
    ("tv",           ["tv"],             True,  True),
    ("runtime+ejs",  None,               True,  True),
]
_YT_ATTEMPTS = _YT_ATTEMPTS_FULL if os.environ.get("YT_ATTEMPTS_FULL", "") == "1" else _YT_ATTEMPTS_LITE
_yt_preferred = None     # ten cach da thanh cong gan nhat
# Khi YouTube chan IP, thu lai lien tuc chi ton CPU (0.1 CPU) va lam IP bi chan nang hon.
# Sau khi MOI cach deu that bai vi bi chan -> nghi YT_BLOCK_BACKOFF giay, tra loi loi ngay (khong cho 45s).
YT_BLOCK_BACKOFF = float(os.environ.get("YT_BLOCK_BACKOFF", "60"))
_yt_block_until = 0.0
_BLOCK_HINTS = ("Sign in", "player response", "not a bot", "HTTP Error 429", "HTTP Error 403")

# Cac cach KHONG cookies hay that bai lien tuc tren IP datacenter va moi lan ton ~15s tren goi Free,
# an het ngan sach thoi gian truoc khi toi cach co cookies. Neu chung that bai YT_NOCOOKIE_MAX_FAILS lan
# lien tiep thi tam bo qua chung YT_NOCOOKIE_COOLDOWN giay (chi khi co file cookies), thu cach co cookies truoc.
YT_NOCOOKIE_MAX_FAILS = int(os.environ.get("YT_NOCOOKIE_MAX_FAILS", "2"))
YT_NOCOOKIE_COOLDOWN = float(os.environ.get("YT_NOCOOKIE_COOLDOWN", "1800"))
_nocookie_fails = 0
_nocookie_skip_until = 0.0

# Dau ra qua proxy (chi dung cho YouTube): YT_PROXY="http://user:pass@host:port" (yt-dlp con ho tro socks5://...,
# nhung phan ffmpeg phat luong chi dung duoc proxy http://). Link YouTube gan voi IP luc resolve nen CA yt-dlp
# lan ffmpeg deu phai di qua cung proxy.
YT_PROXY = os.environ.get("YT_PROXY", "").strip()


def _is_youtube(url):
    return re.search(r"(youtube\.com|youtu\.be)", url or "") is not None


def _build_ydl_opts(fmt, client=None, use_cookies=True, runtimes=False, proxy=""):
    _dbg = os.environ.get("YT_DEBUG", "") == "1"   # dat YT_DEBUG=1 tren Render de xem log chi tiet
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
    # Tuy chon: PO token tu dich vu bgutil-ytdlp-pot-provider (plugin yt-dlp), vd YT_POT_URL=http://127.0.0.1:4416
    _pot = os.environ.get("YT_POT_URL", "").strip()
    if _pot:
        opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {"base_url": [_pot]}
    if runtimes:
        # Can deno/node de giai chu ky YouTube (xem Dockerfile); runtime nao khong co thi yt-dlp bo qua
        opts["js_runtimes"] = {"deno": {}, "node": {}}
        opts["remote_components"] = ["ejs:github"]
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
            info = info["entries"][0]
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

        return info["url"], None, format_ffmpeg_headers(info), ""


_NO_RETRY_HINTS = ("Video unavailable", "Private video", "has been removed", "not available in your country")


_resolve_gate = threading.Lock()     # chi 1 lan resolve yt-dlp tai 1 thoi diem (tiet kiem RAM goi 512MB)


def _resolve_stream_urls_uncached(video_url, height_cap):
    with _resolve_gate:
        return _resolve_stream_urls_inner(video_url, height_cap)


def _resolve_stream_urls_inner(video_url, height_cap):
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
    is_yt = _is_youtube(video_url)
    if not is_yt:
        return _extract_with(video_url, _build_ydl_opts(fmt, use_cookies=True))
    _left = _yt_block_until - time.monotonic()
    if _left > 0:
        raise RuntimeError(f"YouTube dang chan IP - tam nghi them {_left:.0f}s (YT_BLOCK_BACKOFF) de khong bi chan nang hon")

    attempts = list(_YT_ATTEMPTS)
    # Muon ep client thi dat YT_PLAYER_CLIENTS="tv,web_safari" - se duoc thu dau tien
    _pc = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
    if _pc:
        attempts.insert(0, ("env", _pc, True, True))
    if _yt_preferred:
        attempts.sort(key=lambda a: a[0] != _yt_preferred)   # sort on dinh: cach tot nhat len dau
    if os.path.exists(COOKIES_FILE) and time.monotonic() < _nocookie_skip_until:
        with_cookies = [a for a in attempts if a[2]]
        if with_cookies:
            attempts = with_cookies
            print("[resolve] tam bo cac cach khong cookies (that bai lien tiep), thu cach co cookies", flush=True)

    budget = float(os.environ.get("YT_RESOLVE_BUDGET", "45"))   # tong thoi gian toi da cho moi lan resolve
    t_start = time.monotonic()
    errors = []
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
            if not use_cookies and "player response" in msg:
                _nocookie_fails += 1
                if _nocookie_fails >= YT_NOCOOKIE_MAX_FAILS:
                    _nocookie_skip_until = time.monotonic() + YT_NOCOOKIE_COOLDOWN
            if any(h in msg for h in _NO_RETRY_HINTS):
                break
    _yt_preferred = None
    if errors and any(h in e for e in errors for h in _BLOCK_HINTS):
        _yt_block_until = time.monotonic() + YT_BLOCK_BACKOFF
    raise RuntimeError(" | ".join(errors)[:700])



# ---- Bo nho dem link da resolve + lam nong (prefetch) ----
# Resolve bang yt-dlp la buoc cham nhat (vai giay toi hon chuc giay). Link CDN song vai gio nen
# giu lai 30 phut; sau khi tim kiem xong thi resolve san vai ket qua dau trong nen, luc nguoi dung
# chon video thi da co san.
RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", "1800"))
WARM_TOP_N = int(os.environ.get("WARM_TOP_N", "1"))
_resolve_cache = {}          # (video_url, height_cap) -> (thoi_diem, ket_qua)
_resolve_locks = {}          # (video_url, height_cap) -> Lock, tranh resolve trung nhau
_resolve_guard = threading.Lock()
_active_streams = 0
_cur_proc = None                 # ffmpeg dang phat; yeu cau /stream moi se giet cai cu de khong chay 2 ffmpeg cung luc
_cur_lock = threading.Lock()


# ---- Tu dong cap nhat yt-dlp ----
# YouTube doi co che lien tuc nen yt-dlp ban cu hay hong. Server tu chay pip upgrade luc khoi dong,
# dinh ky, va khi gap loi extract; neu co ban moi thi tu khoi dong lai tien trinh (doi luc khong phat).
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


def _update_ytdlp(min_gap=600):
    """Chay pip upgrade yt-dlp. Tra ve True neu phien ban thay doi. Khong chay lai trong min_gap giay."""
    global _last_update_try
    with _update_lock:
        if time.monotonic() - _last_update_try < min_gap and _last_update_try:
            return False
        _last_update_try = time.monotonic()
        before = _ytdlp_version()
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "--quiet", "yt-dlp[default]"],
                capture_output=True, text=True, timeout=240,
                preexec_fn=lambda: os.nice(19))   # pip chay do uu tien thap, khong tranh CPU voi ffmpeg
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
    for _ in range(360):                      # cho toi da ~1 gio de khong cat luong dang phat
        if _active_streams == 0 and not _br_busy():
            print("[update] khoi dong lai de nap yt-dlp moi", flush=True)
            _br_stop()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        time.sleep(10)


def _update_and_maybe_restart(min_gap=600):
    for _ in range(360):                      # pip install ton RAM/CPU: cho luc khong phat + khong dung trinh duyet
        if _active_streams == 0 and not _br_busy():
            break
        time.sleep(10)
    if _update_ytdlp(min_gap):
        _restart_when_idle()


def _update_loop():
    # Tre lan cap nhat dau tien: luc moi khoi dong goi Free CPU rat yeu, pip install chay song song
    # lam cham/hong nhung yeu cau dau tien (YouTube, trinh duyet).
    time.sleep(float(os.environ.get("YTDLP_FIRST_UPDATE_DELAY", "300")))
    _update_and_maybe_restart(min_gap=0)
    while True:
        time.sleep(UPDATE_HOURS * 3600)
        _update_and_maybe_restart(min_gap=0)


def trigger_update_async():
    """Goi khi gap loi extract: cap nhat nen (toi da 1 lan / 30 phut)."""
    if AUTO_UPDATE:
        threading.Thread(target=_update_and_maybe_restart, args=(1800,), daemon=True).start()


def resolve_stream_urls(video_url, height_cap):
    key = (video_url, str(height_cap))
    now = time.monotonic()
    with _resolve_guard:
        hit = _resolve_cache.get(key)
        if hit and now - hit[0] < RESOLVE_TTL:
            print(f"[resolve] CACHE HIT {video_url}", flush=True)
            return hit[1]
        lock = _resolve_locks.setdefault(key, threading.Lock())
    with lock:                      # neu dang warm cung video thi doi no xong roi dung ket qua
        with _resolve_guard:
            hit = _resolve_cache.get(key)
            if hit and time.monotonic() - hit[0] < RESOLVE_TTL:
                print(f"[resolve] CACHE HIT (sau khi cho warm) {video_url}", flush=True)
                return hit[1]
        t0 = time.monotonic()
        try:
            res = _resolve_stream_urls_uncached(video_url, height_cap)
        except Exception as e_ytdlp:
            print(f"[resolve] yt-dlp that bai ({str(e_ytdlp)[:120]})", flush=True)
            raise
        print(f"[resolve] yt-dlp mat {time.monotonic() - t0:.1f}s cho {video_url}", flush=True)
        with _resolve_guard:
            _resolve_cache[key] = (time.monotonic(), res)
            if len(_resolve_cache) > 60:    # don bot ban cu
                for k in sorted(_resolve_cache, key=lambda k: _resolve_cache[k][0])[:20]:
                    _resolve_cache.pop(k, None)
            for k in [k for k, l in _resolve_locks.items() if not l.locked() and k not in _resolve_cache]:
                _resolve_locks.pop(k, None)     # truoc day _resolve_locks chi tang, khong bao gio giam
        return res


def _warm_worker(video_ids, height_cap):
    for vid in video_ids:
        if _active_streams > 0:
            print("[warm] dang co luong phat, bo qua lam nong", flush=True)
            return
        if _br_busy() or _resolve_gate.locked() or time.monotonic() < _yt_block_until:
            print("[warm] may dang ban (trinh duyet/resolve khac), bo qua lam nong", flush=True)
            return
        try:
            resolve_stream_urls(f"https://www.youtube.com/watch?v={vid}", height_cap)
        except Exception as e:
            print(f"[warm] loi {vid}: {e}", flush=True)


def warm_results(results):
    ids = [r["id"] for r in results if r.get("id")][:WARM_TOP_N]
    if not ids:
        return
    try:
        min_height = int(os.environ.get("MIN_HEIGHT", "240"))
    except ValueError:
        min_height = 240
    threading.Thread(target=_warm_worker, args=(ids, str(min_height)), daemon=True).start()


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


def _stream_impl(video_url):
    t_req = time.monotonic()
    w = request.args.get("w", "320")
    h = request.args.get("h", "170")
    height_cap = request.args.get("height_cap", "360")


    min_height = int(os.environ.get("MIN_HEIGHT", "240"))
    try:
        height_cap = str(max(int(height_cap), min_height))
    except ValueError:
        height_cap = str(min_height)
    def _clamp(name, default, lo, hi):
        try:
            return str(max(lo, min(hi, int(request.args.get(name, default)))))
        except ValueError:
            return str(default)
    fps = _clamp("fps", 15, 1, 30)
    q = _clamp("q", MJPEG_Q, 2, 31)   # cang nho cang net (2..31)
    # Uu tien MUOT hon do net: khong cho q nho hon STREAM_Q_MIN (anh nho hon -> ESP giai ma nhanh hon). Dat STREAM_Q_MIN=2 de tat.
    q = str(min(31, max(int(q), int(os.environ.get("STREAM_Q_MIN", "12")))))
    _fmin = int(os.environ.get("STREAM_FPS_MIN", "0"))     # >0: khong cho fps thap hon muc nay
    if _fmin:
        fps = str(min(30, max(int(fps), _fmin)))
    # Toc do lay mau am thanh (PCM 16-bit mono = ar*2 byte/giay). Mang yeu thi thiet bi xin thap de giam bang thong
    ar = _clamp("ar", AUDIO_RATE, 4000, 22050)

    if not video_url:
        return Response(status=400)

    # Chrome (~170MB+) va yt-dlp+deno/node (~100-150MB) khong duoc cung song tren goi 512MB -> dong Chrome TRUOC khi resolve
    if _worker is not None and _worker.ctx is not None:
        print("[stream] dong Chrome truoc khi resolve de nhuong RAM cho yt-dlp", flush=True)
        _br_stop()
    try:
        video_direct_url, audio_direct_url, video_headers, audio_headers = resolve_stream_urls(video_url, height_cap)
    except Exception as e:


        print(f"[stream] resolve error: {e}")
        traceback.print_exc()
        if "player response" in str(e) or "Sign in" in str(e):
            trigger_update_async()
        return jsonify({"error": "resolve_failed", "detail": str(e)[:300]}), 502


    RECONNECT_ARGS = (
        "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
        "-reconnect_delay_max 5 -rw_timeout 20000000 "
    )
    if YT_PROXY and _is_youtube(video_url):
        RECONNECT_ARGS += f"-http_proxy {shlex.quote(YT_PROXY)} "
    video_headers_arg = f"-headers {shlex.quote(video_headers)} " if video_headers else ""
    audio_headers_arg = f"-headers {shlex.quote(audio_headers)} " if audio_headers else ""


    THREADS_ARG = f"-threads {FFMPEG_THREADS} "
    # Mac dinh ffmpeg mo so luong thread loc = so nhan MAY CHU (nhieu) du container chi co 0.1-0.5 CPU -> bi throttle. Ep ve FFMPEG_THREADS.
    FILTER_ARG = f"-filter_threads {FFMPEG_THREADS} "
    # Khoi dong ffmpeg nhanh hon: bot thoi gian do thong tin dau vao (mac dinh ~5s moi luong)
    PROBE_ARG = "-probesize 500000 -analyzeduration 500000 -fflags +nobuffer "

    if audio_direct_url:
        cmd = (
            f"ffmpeg -nostdin -v error "
            f"{RECONNECT_ARGS}{video_headers_arg}{PROBE_ARG}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{RECONNECT_ARGS}{audio_headers_arg}{PROBE_ARG}{THREADS_ARG}-i {shlex.quote(audio_direct_url)} "
            f"-map 0:v:0 -map 1:a:0 "
            f"{THREADS_ARG}{FILTER_ARG}"
            f"-vf fps={fps},scale={w}:{h}:flags=fast_bilinear "


            f"-c:v mjpeg -q:v {q} "


            f"-c:a pcm_s16le -ar {ar} -ac 1 "
            f"-flush_packets 1 -f avi pipe:1"
        )
    else:
        cmd = (
            f"ffmpeg -nostdin -v error {RECONNECT_ARGS}{video_headers_arg}{PROBE_ARG}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{THREADS_ARG}{FILTER_ARG}"
            f"-vf fps={fps},scale={w}:{h}:flags=fast_bilinear "
            f"-c:v mjpeg -q:v {q} "
            f"-c:a pcm_s16le -ar {ar} -ac 1 "
            f"-flush_packets 1 -f avi pipe:1"
        )


    if _worker is not None and _worker.ctx is not None:
        print("[stream] dang phat video -> dong Chrome de nhuong RAM", flush=True)
        threading.Thread(target=_br_stop, daemon=True).start()
    global _cur_proc
    with _cur_lock:
        _old = _cur_proc
        if _old is not None and _old.poll() is None:
            print("[stream] co yeu cau moi -> tat ffmpeg cu de nhuong CPU", flush=True)
            try:
                _old.kill()
            except Exception:
                pass
        proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, bufsize=1 << 20)
        _cur_proc = proc
    # Mo rong ong dan stdout cua ffmpeg (mac dinh 64KB) len 1MB de ffmpeg chay truoc, khong bi nghen khi mang cham
    try:
        fcntl.fcntl(proc.stdout.fileno(), 1031, 1 << 20)   # F_SETPIPE_SZ = 1031 (Linux)
    except Exception:
        pass


    bps_cap = int(os.environ.get("STREAM_MAX_BPS_CAP", "1200000"))
    try:
        target_bps = int(request.args.get("bps", os.environ.get("STREAM_MAX_BPS", "150000")))
    except ValueError:
        target_bps = int(os.environ.get("STREAM_MAX_BPS", "150000"))
    # Burst dau luong: gui khong han che ~768KB dau de thiet bi dem xong truoc nhanh (chi bi gioi han boi duong mang),
    # sau do moi pacing theo target_bps. Giam thoi gian cho luc moi bam phat.
    burst_bytes = int(os.environ.get("STREAM_BURST_BYTES", str(768 * 1024)))
    target_bps = max(25000, min(bps_cap, target_bps))   # san 25KB/s (truoc la 50KB/s) cho muc mang cuc yeu

    def generate():
        global _active_streams
        _active_streams += 1
        t0 = time.monotonic()
        sent = 0
        first = True
        try:
            while True:
                chunk = proc.stdout.read1(32768)     # read1: tra ngay phan co san, khong cho du 32KB (giam tre khung dau)
                if not chunk:
                    break
                if first:
                    first = False
                    print(f"[stream] byte dau tien sau {time.monotonic() - t_req:.1f}s ke tu luc nhan yeu cau", flush=True)
                sent += len(chunk)
                expected_elapsed = max(0, sent - burst_bytes) / target_bps
                actual_elapsed = time.monotonic() - t0
                if expected_elapsed > actual_elapsed:
                    time.sleep(expected_elapsed - actual_elapsed)
                yield chunk
        finally:
            _active_streams -= 1
            try:
                proc.stdout.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass

    def _reap():
        # Neu client ngat truoc khi generator kip chay thi khoi finally cua generate() khong bao gio chay -> ffmpeg mo coi an CPU/RAM
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

    resp = Response(generate(), mimetype="video/avi")
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
    mode = request.args.get("mode", "cur")
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
    # Thu rieng 1 player client: /diag?v=<id>&mode=def&client=android_vr   (them &nocookie=1 de bo cookies)
    _cl = request.args.get("client", "").strip()
    if _cl:
        opts["extractor_args"] = {"youtube": {"player_client": [c.strip() for c in _cl.split(",") if c.strip()]}}
    if os.path.exists(COOKIES_FILE) and request.args.get("nocookie") != "1":
        opts["cookiefile"] = COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            lg.lines.append(f"=== THANH CONG: {info.get('title')} ===")
    except Exception as e:
        lg.lines.append(f"=== THAT BAI: {str(e)[:300]} ===")
    text = "\n".join(head) + "\n\n" + "\n".join(lg.lines)
    return Response(text, mimetype="text/plain; charset=utf-8")


# =====================================================================
# TRINH DUYET TU XA cho T-Display S3  (/br/<action>)
# Chrome that (Playwright, giao dien dien thoai) chay NGAY TRONG server nay:
# chup man hinh -> JPEG 320x170 gui ve ESP32; ESP32 gui lai cham / vuot / go chu.
# - Chi khoi dong Chrome khi co nguoi dung /br/..., tu tat sau BR_IDLE_SEC giay roi.
# - BR_TOKEN (bat buoc): mat khau, firmware phai khai bao cung gia tri, neu khong -> 403.
# - BR_PROFILE: thu muc luu cookie/phien dang nhap (mac dinh /tmp/br_profile).
# - BR_W, BR_H, BR_JPEG_Q, BR_IDLE_SEC: tinh chinh (mac dinh 320, 170, 55, 300).
# =====================================================================
BR_W = int(os.environ.get("BR_W", "320"))
BR_H = int(os.environ.get("BR_H", "170"))
OUT_W = 320
SCALE = BR_W / float(OUT_W)          # toa do man hinh ESP32 -> toa do CSS
BR_TOKEN = os.environ.get("BR_TOKEN", "")
BR_JPEG_Q = int(os.environ.get("BR_JPEG_Q", "40"))
BR_ALLOW_MEDIA = os.environ.get("BR_ALLOW_MEDIA", "0") == "1"
BR_GOTO_TIMEOUT = int(os.environ.get("BR_GOTO_TIMEOUT", "10")) * 1000   # cho tai trang toi da (ms)
BR_SHOT_TIMEOUT = int(os.environ.get("BR_SHOT_TIMEOUT", "6")) * 1000    # moi lan chup anh toi da (ms)
BR_REQ_TIMEOUT = int(os.environ.get("BR_REQ_TIMEOUT", "10"))            # toi da bao lau thi tra anh tam (s) - Chrome van chay tiep o nen, tranh ESP bi -11/504
BR_JS_HEAP_MB = int(os.environ.get("BR_JS_HEAP_MB", "96"))     # trang nang (TikTok/FB) cham tran heap 96MB -> GC chay lien tuc ton CPU; thu 160 neu con RAM
BR_NO_ROUTE = os.environ.get("BR_NO_ROUTE", "0") == "1"        # =1: khong loc request qua Python (moi request 1 vong IPC, ton CPU) ma dung co Chrome
BR_MAX_QUEUE = int(os.environ.get("BR_MAX_QUEUE", "2"))        # toi da so thao tac cho trong hang doi khi Chrome ket
BR_BLOCK_IMAGES = os.environ.get("BR_BLOCK_IMAGES", "0") == "1"   # =1: duyet chi chu, nhanh gap nhieu lan tren 0.1 CPU
IDLE_SEC = int(os.environ.get("BR_IDLE_SEC", "300"))   # khoi dong lai Chrome mat 20-40s tren goi free -> khong tat qua som
PROFILE = os.environ.get("BR_PROFILE", "/tmp/br_profile")
MAX_JPEG = 60000                      # khop BR_BUF_MAX trong firmware (61440)
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")

_BLOCK_URL = ("google-analytics", "googletagmanager", "doubleclick", "googlesyndication", "adservice",
              "scorecardresearch", "hotjar", "sentry.io", "/monitor_browser/", "mon-va.", "mcs-va.", "log-va.",
              "connect.facebook.net")

# Che do BR_NO_ROUTE=1: chan quang cao/theo doi bang host-resolver-rules, font bang --disable-remote-fonts,
# video tu phat bang autoplay-policy (thay cho Playwright route). Chi dung duoc cho muc theo ten mien (bo muc bat dau bang "/").
_BR_EXTRA_ARGS = []
if BR_NO_ROUTE:
    _hr = ", ".join(f"MAP *{_b}* ~NOTFOUND" for _b in _BLOCK_URL if not _b.startswith("/"))
    _BR_EXTRA_ARGS = ["--disable-remote-fonts",
                      "--autoplay-policy=document-user-activation-required",
                      "--host-resolver-rules=" + _hr]
    if BR_BLOCK_IMAGES:
        _BR_EXTRA_ARGS.append("--blink-settings=imagesEnabled=false")

EDIT_JS = """() => {
  const e = document.activeElement; if (!e) return false;
  const t = (e.tagName || '').toLowerCase();
  if (t === 'textarea') return true;
  if (t === 'input') {
    const ty = (e.type || 'text').toLowerCase();
    return !['button','submit','checkbox','radio','image','reset','file','range','color','hidden'].includes(ty);
  }
  return !!e.isContentEditable;
}"""

# Nhan Enter sau khi go neu o do la o tim kiem / form chi co 1 o nhap.
ENTER_JS = """() => {
  const e = document.activeElement; if (!e) return false;
  const ty = (e.type || '').toLowerCase();
  if (ty === 'search' || e.getAttribute('role') === 'searchbox' ||
      e.getAttribute('enterkeyhint') === 'search') return true;
  if (e.form) {
    const n = e.form.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio])').length;
    return n === 1;
  }
  return false;
}"""


def _log(*a):
    print("[br]", *a, flush=True)


import base64 as _b64
_PLACEHOLDER_JPG = _b64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABQODxIPDRQSEBIXFRQYHjIhHhwcHj0sLiQySUBMS0dARkVQWnNiUFVtVkVGZIhlbXd7gYKBTmCNl4x9lnN+gXz/2wBDARUXFx4aHjshITt8U0ZTfHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHz/wAARCACqAUADASIAAhEBAxEB/8QAGgABAAMBAQEAAAAAAAAAAAAAAAIDBAEFBv/EADEQAQACAgECBQMDAAsAAAAAAAABAgMRBBIhBRMiMVEyQWEUI3EGNTZSc3SRkrHC8f/EABUBAQEAAAAAAAAAAAAAAAAAAAAB/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8A+VAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAF/Cx1y83j48kbpfJWto+YmVDT4b/WXF/xqf8wDVysvBwcvNhngVmtLzXcZbRPaf5VczgxTLgnizbJi5Mbxb99+2p/LvP42fL4nyYx4clptmtrVZ7926cVb8rw3w6cs0vhi3mXpPetp76ifntoGDJ4Xlpjy2rlw5LYo3kpS+7V+UeP4bm5HFnk1tjrjrfotNra6e29z+Hq8Ovp8QmOFOCscfJHXabTaZ+J3OmKtpj+jVoiffl6n/aDLyuDk42OmWb48mK/aL47bjfwnTw3JOOl8uXDg8yN0rlvqbR8/+rp/s3H+b/6LPGsGXkc2mbBjtkxZaV8uaxuPb2Bh5vCy8G9KZ+nqvSL6id639p/PZp4PAxcnw7lZr5aUvjmsVm0zEV3Pffb7/ZPx+tqZOHS87tXi0iZ3vv3c8PrbJ4P4lSlZtb9udRG5+oGXBwb57ZenJjjHinVstrar+P8AVP8AQZMXL41L+XlpmtHRatvTfvr394X8PjxXw3PmviyZ5jLFPJi0xEdvqmI7/htzVmseCROGMM+dPojfp9cfPcHicvH5XLzY+mK9N7RqJ3Ed/lS387j5c3P5+THSbVxZLTeY+0blTHA5M2w1jDbeeN44/vQDMJWralpraNWrOpiftKIAAAAAAAAAAAAAAAAAAAAAAAACVbWpaLUma2rO4mJ1MSiA0W53LtExblZ5iftOSVETMTuJ1MfdwBfbl8m07tyMszqa97z7fCvzL+V5XXby99XTvtv518oAJ+ZfyvK67eXvq6d9t/Ovl6NedxbY6xaeVx56Yi9MFoil/wA6+zywGrn8v9Zni1adGOlIpSu96rHspxZsuC3VhyXx2+aWmJVgLacjPjva9M2St7fVaLTEz/JPIzT07zZJ6J6q+qfTPzCoBZ52Xd58y/7n1+qfV/PyRnzRNJjLeJxxqk9U+n+PhWA7MzMzMzuZ95cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH//Z")


def _pending_response():
    """Chrome con ban (0.1 CPU) -> tra NGAY anh gan nhat (hoac anh 'Dang tai...') de ESP khong cho qua lau.
    Viec dang chay van tiep tuc o nen; yeu cau /br/frame ke tiep se lay anh moi."""
    w = _worker
    jpg = w.last_jpg if (w is not None and w.last_jpg) else _PLACEHOLDER_JPG
    return Response(jpg, mimetype="image/jpeg", headers={
        "X-Url": urllib.parse.quote(_br_last_url or "", safe=":/?&=%#")[:300],
        "X-Edit": "0",
        "X-Pending": "1",
        "Cache-Control": "no-store",
    })


def _mem_info():
    """(dang dung MB, gioi han MB) cua container (cgroup v2/v1); None neu khong doc duoc.
    Chi tinh RAM that (anon), khong tinh page cache."""
    try:
        for cur, lim, stat, keys in (
                ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.stat", ("anon",)),
                ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes",
                 "/sys/fs/cgroup/memory/memory.stat", ("total_rss", "rss"))):
            if not (os.path.exists(cur) and os.path.exists(lim)):
                continue
            limit_s = open(lim).read().strip()
            if not limit_s.isdigit() or int(limit_s) >= (1 << 40):
                continue
            used = int(open(cur).read().strip())
            try:
                for line in open(stat):
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


class _Worker(threading.Thread):
    """Playwright (sync) chi dung duoc tren 1 thread -> moi viec xep hang vao day."""

    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.pw = None
        self.ctx = None
        self.page = None
        self.last_jpg = None
        self.busy = False
        self.last = time.time()

    # ---- vong lap thread ----
    def run(self):
        while True:
            try:
                fn, box, ev = self.q.get(timeout=30)
            except queue.Empty:
                if self.ctx and time.time() - self.last > IDLE_SEC:
                    _log("idle -> dong Chrome")
                    self._reset()
                continue
            if box.get("cancel"):      # nguoi goi da het gio -> khong ton CPU chay viec thua
                _log("bo qua viec da qua han")
                ev.set()
                continue
            self.busy = True
            try:
                box["r"] = fn(self)
            except BaseException as e:  # noqa
                box["e"] = e
                box["tb"] = traceback.format_exc()
                if "closed" in str(e).lower():
                    self._reset()
            self.busy = False
            self.last = time.time()
            ev.set()

    def submit(self, fn, timeout=100, keep=False):
        box, ev = {}, threading.Event()
        self.q.put((fn, box, ev))
        if not ev.wait(timeout):
            if not keep:
                box["cancel"] = True
            raise TimeoutError("browser busy/timeout")
        if "e" in box:
            _log(box.get("tb", ""))
            raise box["e"]
        return box["r"]

    # ---- quan ly Chrome ----
    def _reset(self):
        for obj, meth in ((self.ctx, "close"), (self.pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self.ctx = self.pw = self.page = None

    def _on_page(self, p):
        self.page = p            # tab moi (target=_blank) -> chuyen sang tab do

    def ensure(self):
        if self.page is not None and not self.page.is_closed():
            return
        if self.ctx is not None:
            alive = [p for p in self.ctx.pages if not p.is_closed()]
            if alive:
                self.page = alive[-1]
                return
        from playwright.sync_api import sync_playwright   # import tre
        if self.pw is None:
            self.pw = sync_playwright().start()
        if self.ctx is None:
            os.makedirs(PROFILE, exist_ok=True)
            self.ctx = self.pw.chromium.launch_persistent_context(
                PROFILE,
                headless=True,
                viewport={"width": BR_W, "height": BR_H},
                device_scale_factor=OUT_W / float(BR_W),
                is_mobile=True,
                has_touch=True,
                user_agent=UA,
                locale="vi-VN",
                reduced_motion="reduce",
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-extensions", "--mute-audio",
                      "--disable-background-networking",
                      "--renderer-process-limit=1",
                      "--enable-low-end-device-mode",
                      "--disable-smooth-scrolling", "--disable-lcd-text", "--disable-font-subpixel-positioning",
                      "--disable-partial-raster", "--disable-histogram-customizer",
                      f"--js-flags=--max-old-space-size={BR_JS_HEAP_MB}",
                      "--disk-cache-size=1", "--media-cache-size=1",
                      "--disable-breakpad", "--disable-crash-reporter",
                      "--disable-features=site-per-process,TranslateUI,BackForwardCache,"
                      "OptimizationHints,MediaRouter,AudioServiceOutOfProcess,IsolateOrigins",
                      "--disable-component-update", "--disable-sync"] + _BR_EXTRA_ARGS,
            )
            self.ctx.on("page", self._on_page)
            def _lite(route):            # chan video/font/theo doi/quang cao de nhe CPU (goi free)
                try:
                    _rq = route.request
                    if BR_BLOCK_IMAGES and _rq.resource_type == "image":
                        route.abort()
                    elif _rq.resource_type in (("font", "websocket", "eventsource", "ping") if BR_ALLOW_MEDIA
                                             else ("media", "font", "websocket", "eventsource", "ping")) \
                            or any(b in _rq.url for b in _BLOCK_URL):
                        route.abort()
                    else:
                        route.continue_()
                except Exception:
                    pass
            try:
                if not BR_NO_ROUTE:
                    self.ctx.route("**/*", _lite)
            except Exception:
                pass
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    # ---- thao tac ----
    def settle(self, ms=150):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        self.page.wait_for_timeout(ms)

    def _snap(self, q, timeout):
        return self.page.screenshot(type="jpeg", quality=q, animations="disabled", timeout=timeout)

    def shot(self):
        q = BR_JPEG_Q
        try:
            data = self._snap(q, BR_SHOT_TIMEOUT)
        except Exception as e:
            if "imeout" not in str(e):
                raise
            # Trang qua nang (video/JS) lam Chrome khong chup kip: dung tai them va tam dung video roi thu lai
            _log("chup anh cham -> dung tai trang + tam dung video, thu lai")
            try:
                self.page.evaluate("() => { try { window.stop(); } catch (e) {} "
                                   "document.querySelectorAll('video,audio').forEach(v => { try { v.pause(); } catch (e) {} }); }")
            except Exception:
                pass
            try:
                data = self._snap(q, BR_SHOT_TIMEOUT)
            except Exception as e2:
                if "imeout" in str(e2) and self.last_jpg:
                    _log("van cham -> tra anh cu de ESP khong bi timeout")
                    return self.last_jpg
                raise
        while len(data) > MAX_JPEG and q > 20:
            q -= 15
            data = self._snap(q, BR_SHOT_TIMEOUT * 2)
        self.last_jpg = data
        return data


def _norm_url(u):
    u = (u or "").strip()
    if not u:
        return "about:blank"
    if "://" not in u:
        u = "https://" + u
    return u


def _f(a, k, d=0.0):
    try:
        return float(a.get(k, d))
    except Exception:
        return d


def act_open(w, a):
    try:
        w.page.goto(_norm_url(a.get("u")), wait_until="domcontentloaded", timeout=BR_GOTO_TIMEOUT)
    except Exception as e:
        _log("goto:", e)
    try:
        w.page.wait_for_load_state("load", timeout=2500)
    except Exception:
        pass
    w.page.wait_for_timeout(250)


def act_tap(w, a):
    w.page.touchscreen.tap(_f(a, "x") * SCALE, _f(a, "y") * SCALE)
    w.settle(300)


def act_scroll(w, a):
    w.page.mouse.move(BR_W / 2.0, BR_H / 2.0)
    w.page.mouse.wheel(0, _f(a, "dy") * SCALE)
    w.page.wait_for_timeout(120)


def act_type(w, a):
    text = (a.get("t") or "")[:200]
    if text:
        w.page.keyboard.type(text, delay=20)
        try:
            if w.page.evaluate(ENTER_JS):
                w.page.keyboard.press("Enter")
        except Exception:
            pass
    w.settle(500)


def act_back(w, a):
    try:
        w.page.go_back(timeout=10000, wait_until="domcontentloaded")
    except Exception:
        pass
    w.settle(300)


def act_reload(w, a):
    try:
        w.page.reload(timeout=15000, wait_until="domcontentloaded")
    except Exception:
        pass
    w.settle(400)


def act_frame(w, a):
    pass


ACTIONS = {
    "open": act_open, "tap": act_tap, "scroll": act_scroll, "type": act_type,
    "back": act_back, "reload": act_reload, "frame": act_frame,
}

_worker = None
_worker_lock = threading.Lock()


def _get_worker():
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _Worker()
            _worker.start()
        return _worker


def _close_extra_tabs(w):
    """Chi giu 1 tab (tab dang xem); cac tab cu mo tu target=_blank van song se an RAM va lam OOM."""
    try:
        for pg in list(w.ctx.pages):
            if pg is not w.page and not pg.is_closed():
                try:
                    pg.close()
                except Exception:
                    pass
    except Exception:
        pass


def _do(w, fn, a):
    w.ensure()
    fn(w, a)
    _close_extra_tabs(w)
    jpg = w.shot()
    url = w.page.url
    try:
        edit = bool(w.page.evaluate(EDIT_JS))
    except Exception:
        edit = False
    return jpg, url, edit


def _handler(action):
    global _br_last_url
    if not BR_TOKEN:
        return Response("BR_TOKEN chua duoc dat tren server", 403)
    if not hmac.compare_digest(request.args.get("k", ""), BR_TOKEN):
        return Response("forbidden", 403)
    if action == "close":
        w = _get_worker()
        w.submit(lambda ww: ww._reset(), timeout=15)
        return Response("closed", 200)
    fn = ACTIONS.get(action)
    if fn is None:
        return Response("unknown action", 404)
    if _worker is None or _worker.ctx is None:       # Chrome chua chay -> kiem tra con du RAM de mo khong
        mi = _mem_info()
        need = int(os.environ.get("BR_MIN_FREE_MB", "170"))
        if mi and mi[1] - mi[0] < need:
            _log(f"thieu RAM de mo Chrome: dung {mi[0]:.0f}/{mi[1]:.0f}MB, can trong >= {need}MB")
            return Response("thieu RAM - thu lai sau it giay", 503, headers={"Retry-After": "10"})
    a = request.args.to_dict()
    if action == "frame" and _worker is not None and _worker.busy:
        return _pending_response()          # dang ban -> khong xep hang them, tra anh gan nhat
    if _worker is not None:
        # Viec qua han van nam trong hang doi (keep=True) va chay SAU khi ban da sang trang khac -> thao tac tre vai chuc giay.
        # Chrome dang ket thi bo cuon (scroll) moi va khong don qua BR_MAX_QUEUE viec.
        _qn = _worker.q.qsize()
        if (action == "scroll" and (_worker.busy or _qn)) or _qn >= BR_MAX_QUEUE:
            return _pending_response()
    try:
        jpg, url, edit = _get_worker().submit(lambda w: _do(w, fn, a), timeout=BR_REQ_TIMEOUT, keep=True)
    except ImportError:
        return Response("playwright chua duoc cai tren server", 503)
    except TimeoutError:
        return _pending_response()
    except Exception as e:  # noqa
        return Response("loi: %s" % str(e)[:200], 500)
    _br_last_url = url
    return Response(jpg, mimetype="image/jpeg", headers={
        "X-Url": urllib.parse.quote(url, safe=":/?&=%#")[:300],
        "X-Edit": "1" if edit else "0",
        "Cache-Control": "no-store",
    })


_br_last_activity = 0.0
_br_last_url = ""          # URL trang Chrome dang xem (de /br/play phat video that cua trang do)


def _br_busy():
    """True neu co nguoi dang dung trinh duyet (de khong restart server giua chung)."""
    return _worker is not None and (time.time() - _br_last_activity < 120)


def _br_stop():
    try:
        if _worker is not None:
            _worker.submit(lambda ww: ww._reset(), timeout=10)
    except Exception:
        pass


@app.route("/br/play")
def br_play():
    """Phat video THAT (hinh + tieng, giong /stream) cua trang dang xem trong trinh duyet.
    /br/play?k=TOKEN            -> video cua trang hien tai (vd tiktok.com/@ten/video/123)
    /br/play?k=TOKEN&u=<link>   -> link chi dinh. Them w,h,fps,q,bps,ar nhu /stream.
    Chrome duoc tat de nhuong RAM cho ffmpeg (xem _stream_impl)."""
    global _br_last_activity
    _br_last_activity = time.time()
    if not BR_TOKEN:
        return Response("BR_TOKEN chua duoc dat tren server", 403)
    if not hmac.compare_digest(request.args.get("k", ""), BR_TOKEN):
        return Response("forbidden", 403)
    url = (request.args.get("u") or _br_last_url or "").strip()
    if not url or url.startswith("about:"):
        return Response("chua co trang nao dang mo - mo link video truoc", 409)
    if "://" not in url:
        url = "https://" + url
    print(f"[br] play {url}", flush=True)
    return _stream_impl(url)


@app.route("/br/<action>")
def br_action(action):
    global _br_last_activity
    _br_last_activity = time.time()
    return _handler(action)


def _warmup_once():
    """Sau khi khoi dong, tu lay link 1 video luc chua ai dung de nap cache giai ma chu ky YouTube (deno + player JS).
    Neu khong, lan bam video DAU TIEN sau moi lan deploy mat ~20s o buoc nay. Tat bang YT_WARMUP=0."""
    time.sleep(float(os.environ.get("YT_WARMUP_DELAY", "25")))
    if _active_streams > 0 or _br_busy():
        return
    vid = os.environ.get("YT_WARMUP_VIDEO", "dQw4w9WgXcQ")
    t0 = time.monotonic()
    try:
        resolve_stream_urls(f"https://www.youtube.com/watch?v={vid}", os.environ.get("MIN_HEIGHT", "240"))
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
    if os.environ.get("YT_WARMUP", "1") == "1" and ROLE != "browser":
        threading.Thread(target=_warmup_once, daemon=True).start()
    print(f"[update] yt-dlp hien tai: {_ytdlp_version()} | auto_update={AUTO_UPDATE} moi {UPDATE_HOURS}h", flush=True)
    if AUTO_UPDATE:
        threading.Thread(target=_update_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
