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
from flask import Flask, request, Response, jsonify
import yt_dlp
from werkzeug.serving import WSGIRequestHandler

app = Flask(__name__)

RELAY_URL = os.environ.get("RELAY_URL", "https://combined-relay-server-395q.onrender.com")


@app.route("/")
def health_check():

    return (f"OK | {RELAY_URL} | AUDIO_RATE={AUDIO_RATE} MJPEG_Q={MJPEG_Q} FFMPEG_THREADS={FFMPEG_THREADS} "
            f"cookies={'yes' if os.path.exists(COOKIES_FILE) else 'NO'}"), 200

PORT = 8000


_SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(_SECRET_COOKIES):


    import shutil
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


@app.route("/search")
def search():
    query = request.args.get("q", "")
    n = int(request.args.get("n", 5))
    if not query:
        return jsonify([])

    if YT_API_KEY:
        try:
            res = search_via_api(query, n)
            print(f"[search] YouTube API OK: {len(res)} ket qua")
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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


def _resolve_stream_urls_uncached(video_url, height_cap):
    """Dùng yt-dlp lấy URL luồng trực tiếp - dùng CHUNG cho cả YouTube lẫn
    TikTok, yt-dlp tự nhận diện domain trong video_url và xử lý đúng cách
    tương ứng. Đòi "bestvideo+bestaudio" (2 luồng tách biệt) rồi để ffmpeg
    tự ghép khi mux, vì phần lớn nguồn không còn phát format gộp sẵn.
    Trả về (video_url, audio_url, video_headers, audio_headers) - audio_url
    có thể None nếu video đó hiếm hoi vẫn có format gộp sẵn."""
    fmt = (
        f"bestvideo[height<={height_cap}][vcodec^=avc1]+bestaudio"
        f"/bestvideo[height<={height_cap}]+bestaudio"
        f"/best[height<={height_cap}]"
        f"/best"
    )
    _dbg = os.environ.get("YT_DEBUG", "") == "1"   # dat YT_DEBUG=1 tren Render de xem ly do tung client that bai
    ydl_opts = {
        "quiet": not _dbg,
        "no_warnings": not _dbg,
        "verbose": _dbg,
        "format": fmt,
        "skip_download": True,


        # De yt-dlp tu chon player client (ban moi tu xu ly PO token / cookies).
        # Muon ep client thi dat bien moi truong YT_PLAYER_CLIENTS="tv,web_safari"
        # Can JS runtime (node/deno) de giai chu ky YouTube - xem Dockerfile.
        "js_runtimes": {"node": {}, "deno": {}},
        "remote_components": ["ejs:github"],
        "geo_bypass": True,
        "socket_timeout": 15,
    }
    _pc = os.environ.get("YT_PLAYER_CLIENTS", "").strip()
    if _pc:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [c.strip() for c in _pc.split(",") if c.strip()]}}
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
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


# ---- Du phong: lay luong qua Piped (khi yt-dlp bi YouTube chan 429) ----
# Piped la ban YouTube ma nguon mo co API JSON; instance cong cong co the chet/bi chan bat cu luc nao,
# nen chi dung lam du phong, thu lan luot nhieu instance.
PIPED_FALLBACK = os.environ.get("PIPED_FALLBACK", "1") == "1"
PIPED_INSTANCES_ENV = os.environ.get("PIPED_INSTANCES", "")
_piped_cache = {"t": 0.0, "list": []}


def _http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; relay/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _piped_instances():
    if PIPED_INSTANCES_ENV.strip():
        return [u.strip().rstrip("/") for u in PIPED_INSTANCES_ENV.split(",") if u.strip()]
    if time.monotonic() - _piped_cache["t"] < 3600 and _piped_cache["list"]:
        return _piped_cache["list"]
    urls = []
    try:
        for it in _http_json("https://piped-instances.kavin.rocks/"):
            u = (it.get("api_url") or "").rstrip("/")
            if u.startswith("https://") and u not in urls:
                urls.append(u)
    except Exception as e:
        print(f"[piped] khong lay duoc danh sach instance: {e}", flush=True)
    if "https://pipedapi.kavin.rocks" not in urls:
        urls.append("https://pipedapi.kavin.rocks")
    _piped_cache.update(t=time.monotonic(), list=urls)
    return urls


def _youtube_id(video_url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", video_url)
    return m.group(1) if m else None


def _pick_piped_streams(data, height_cap):
    cap = int(height_cap)
    vids = [v for v in data.get("videoStreams", []) if v.get("url") and (v.get("height") or 0) and v["height"] <= cap]
    combined = sorted((v for v in vids if not v.get("videoOnly")), key=lambda v: v["height"], reverse=True)
    if combined:
        return combined[0]["url"], None
    only = sorted((v for v in vids if v.get("videoOnly")),
                  key=lambda v: (str(v.get("codec", "")).startswith("avc1"), v["height"]), reverse=True)
    auds = sorted((a for a in data.get("audioStreams", []) if a.get("url")),
                  key=lambda a: a.get("bitrate") or 0, reverse=True)
    if only and auds:
        return only[0]["url"], auds[0]["url"]
    return None


def _resolve_via_piped(video_url, height_cap):
    vid = _youtube_id(video_url)
    if not vid:
        raise RuntimeError("khong phai link YouTube, bo qua Piped")
    last = None
    for base in _piped_instances()[:6]:
        try:
            data = _http_json(f"{base}/streams/{vid}")
            picked = _pick_piped_streams(data, height_cap)
            if picked:
                print(f"[piped] OK qua {base}", flush=True)
                return picked[0], picked[1], "", ""
            last = RuntimeError(f"{base}: khong co luong phu hop")
        except Exception as e:
            last = e
            print(f"[piped] {base} loi: {e}", flush=True)
    raise last or RuntimeError("khong co instance Piped nao")


# ---- Du phong cuoi: RapidAPI (youtube138) - GIOI HAN CUNG 500 luot/thang nen chi dung khi yt-dlp va Piped deu that bai ----
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "youtube138.p.rapidapi.com")


def _fmt_height(f):
    h = f.get("height")
    if isinstance(h, int):
        return h
    m = re.match(r"(\d+)p", str(f.get("qualityLabel") or f.get("quality") or ""))
    return int(m.group(1)) if m else 0


def _pick_rapid_streams(data, height_cap):
    """Do nhieu dinh dang phan hoi pho bien: formats (co san hinh+tieng) va adaptiveFormats (tach roi)."""
    sd = data.get("streamingData") if isinstance(data.get("streamingData"), dict) else data
    cap = int(height_cap)
    usable = lambda f: isinstance(f, dict) and f.get("url")
    formats = [f for f in (sd.get("formats") or []) if usable(f)]
    adaptive = [f for f in (sd.get("adaptiveFormats") or []) if usable(f)]
    mime = lambda f: str(f.get("mimeType") or "")

    combined = [f for f in formats if _fmt_height(f) <= cap]
    if combined:
        return max(combined, key=_fmt_height)["url"], None
    vids = [f for f in adaptive if mime(f).startswith("video/") and 0 < _fmt_height(f) <= cap]
    auds = [f for f in adaptive if mime(f).startswith("audio/")]
    if vids and auds:
        vid = max(vids, key=lambda f: ("avc1" in mime(f), _fmt_height(f)))
        aud = max(auds, key=lambda f: f.get("bitrate") or 0)
        return vid["url"], aud["url"]
    if formats:                                   # khong co luong nao <= cap thi lay luong nho nhat co san
        return min(formats, key=_fmt_height)["url"], None
    return None


def _resolve_via_rapidapi(video_url, height_cap):
    if not RAPIDAPI_KEY:
        raise RuntimeError("chua dat RAPIDAPI_KEY")
    vid = _youtube_id(video_url)
    if not vid:
        raise RuntimeError("khong phai link YouTube")
    req = urllib.request.Request(
        f"https://{RAPIDAPI_HOST}/video/streaming-data/?id={vid}",
        headers={"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    picked = _pick_rapid_streams(data, height_cap)
    if not picked:
        keys = list(data.keys())[:15] if isinstance(data, dict) else type(data).__name__
        raise RuntimeError(f"khong tim thay luong trong phan hoi RapidAPI, cac khoa: {keys}")
    print("[rapidapi] OK (da tru 1 luot)", flush=True)
    return picked[0], picked[1], "", ""


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
                capture_output=True, text=True, timeout=240)
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
        if _active_streams == 0:
            print("[update] khoi dong lai de nap yt-dlp moi", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        time.sleep(10)


def _update_and_maybe_restart(min_gap=600):
    if _update_ytdlp(min_gap):
        _restart_when_idle()


def _update_loop():
    _update_and_maybe_restart(min_gap=0)
    while True:
        time.sleep(UPDATE_HOURS * 3600)
        _update_and_maybe_restart(min_gap=0)


def trigger_update_async():
    """Goi khi gap loi extract: cap nhat nen (toi da 1 lan / 30 phut)."""
    if AUTO_UPDATE:
        threading.Thread(target=_update_and_maybe_restart, args=(1800,), daemon=True).start()


def resolve_stream_urls(video_url, height_cap, allow_paid=True):
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
            res = None
            print(f"[resolve] yt-dlp that bai ({str(e_ytdlp)[:120]})", flush=True)
            if PIPED_FALLBACK:
                try:
                    res = _resolve_via_piped(video_url, height_cap)
                except Exception as e_piped:
                    print(f"[resolve] Piped that bai: {e_piped}", flush=True)
            if res is None and allow_paid and RAPIDAPI_KEY:
                try:
                    res = _resolve_via_rapidapi(video_url, height_cap)
                except Exception as e_rapid:
                    print(f"[resolve] RapidAPI that bai: {e_rapid}", flush=True)
            if res is None:
                raise e_ytdlp
        print(f"[resolve] yt-dlp mat {time.monotonic() - t0:.1f}s cho {video_url}", flush=True)
        with _resolve_guard:
            _resolve_cache[key] = (time.monotonic(), res)
            if len(_resolve_cache) > 60:    # don bot ban cu
                for k in sorted(_resolve_cache, key=lambda k: _resolve_cache[k][0])[:20]:
                    _resolve_cache.pop(k, None)
        return res


def _warm_worker(video_ids, height_cap):
    for vid in video_ids:
        if _active_streams > 0:
            print("[warm] dang co luong phat, bo qua lam nong", flush=True)
            return
        try:
            resolve_stream_urls(f"https://www.youtube.com/watch?v={vid}", height_cap, allow_paid=False)
        except Exception as e:
            print(f"[warm] loi {vid}: {e}", flush=True)


def warm_results(results):
    ids = [r["id"] for r in results if r.get("id")][:WARM_TOP_N]
    if not ids:
        return
    try:
        min_height = int(os.environ.get("MIN_HEIGHT", "360"))
    except ValueError:
        min_height = 360
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
    t_req = time.monotonic()
    video_url = request.args.get("url", "")
    w = request.args.get("w", "320")
    h = request.args.get("h", "170")
    height_cap = request.args.get("height_cap", "360")


    min_height = int(os.environ.get("MIN_HEIGHT", "360"))
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
    # Toc do lay mau am thanh (PCM 16-bit mono = ar*2 byte/giay). Mang yeu thi thiet bi xin thap de giam bang thong
    ar = _clamp("ar", AUDIO_RATE, 4000, 22050)

    if not video_url:
        return Response(status=400)

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
        "-reconnect_delay_max 5 "
    )
    video_headers_arg = f"-headers {shlex.quote(video_headers)} " if video_headers else ""
    audio_headers_arg = f"-headers {shlex.quote(audio_headers)} " if audio_headers else ""


    THREADS_ARG = f"-threads {FFMPEG_THREADS} "
    # Khoi dong ffmpeg nhanh hon: bot thoi gian do thong tin dau vao (mac dinh ~5s moi luong)
    PROBE_ARG = "-probesize 500000 -analyzeduration 500000 -fflags +nobuffer "

    if audio_direct_url:
        cmd = (
            f"ffmpeg -v error "
            f"{RECONNECT_ARGS}{video_headers_arg}{PROBE_ARG}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{RECONNECT_ARGS}{audio_headers_arg}{PROBE_ARG}{THREADS_ARG}-i {shlex.quote(audio_direct_url)} "
            f"-map 0:v:0 -map 1:a:0 "
            f"{THREADS_ARG}"
            f"-vf fps={fps},scale={w}:{h}:flags=bicubic "


            f"-c:v mjpeg -q:v {q} "


            f"-c:a pcm_s16le -ar {ar} -ac 1 "
            f"-flush_packets 1 -f avi pipe:1"
        )
    else:
        cmd = (
            f"ffmpeg -v error {RECONNECT_ARGS}{video_headers_arg}{PROBE_ARG}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{THREADS_ARG}"
            f"-vf fps={fps},scale={w}:{h}:flags=bicubic "
            f"-c:v mjpeg -q:v {q} "
            f"-c:a pcm_s16le -ar {ar} -ac 1 "
            f"-flush_packets 1 -f avi pipe:1"
        )


    proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, bufsize=1 << 20)
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
                chunk = proc.stdout.read(32768)
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
            proc.stdout.close()
            proc.terminate()

    return Response(generate(), mimetype="video/avi")


if __name__ == "__main__":
    print(f"[update] yt-dlp hien tai: {_ytdlp_version()} | auto_update={AUTO_UPDATE} moi {UPDATE_HOURS}h", flush=True)
    if AUTO_UPDATE:
        threading.Thread(target=_update_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
