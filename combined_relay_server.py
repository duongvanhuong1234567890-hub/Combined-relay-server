import subprocess
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
AUDIO_RATE = os.environ.get("AUDIO_RATE", "8000")
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


def resolve_stream_urls(video_url, height_cap):
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
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "skip_download": True,


        "extractor_args": {
            "youtube": {
                "player_client": [
                    "android", "ios", "tv", "mweb", "android_music",
                    "web_embedded", "web",
                ]
            }
        },
        "geo_bypass": True,
        "socket_timeout": 15,
    }
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
    video_url = request.args.get("url", "")
    w = request.args.get("w", "320")
    h = request.args.get("h", "170")
    height_cap = request.args.get("height_cap", "360")


    min_height = int(os.environ.get("MIN_HEIGHT", "360"))
    try:
        height_cap = str(max(int(height_cap), min_height))
    except ValueError:
        height_cap = str(min_height)
    fps = request.args.get("fps", "15")

    if not video_url:
        return Response(status=400)

    try:
        video_direct_url, audio_direct_url, video_headers, audio_headers = resolve_stream_urls(video_url, height_cap)
    except Exception as e:


        print(f"[stream] resolve error: {e}")
        traceback.print_exc()
        return Response(status=502)


    RECONNECT_ARGS = (
        "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
        "-reconnect_delay_max 5 "
    )
    video_headers_arg = f"-headers {shlex.quote(video_headers)} " if video_headers else ""
    audio_headers_arg = f"-headers {shlex.quote(audio_headers)} " if audio_headers else ""


    THREADS_ARG = f"-threads {FFMPEG_THREADS} "

    if audio_direct_url:
        cmd = (
            f"ffmpeg -v error "
            f"{RECONNECT_ARGS}{video_headers_arg}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{RECONNECT_ARGS}{audio_headers_arg}{THREADS_ARG}-i {shlex.quote(audio_direct_url)} "
            f"-map 0:v:0 -map 1:a:0 "
            f"{THREADS_ARG}"
            f"-vf fps={fps},scale={w}:{h}:flags=bicubic "


            f"-c:v mjpeg -q:v {MJPEG_Q} "


            f"-c:a pcm_s16le -ar {AUDIO_RATE} -ac 1 "
            f"-f avi pipe:1"
        )
    else:
        cmd = (
            f"ffmpeg -v error {RECONNECT_ARGS}{video_headers_arg}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{THREADS_ARG}"
            f"-vf fps={fps},scale={w}:{h}:flags=bicubic "
            f"-c:v mjpeg -q:v {MJPEG_Q} "
            f"-c:a pcm_s16le -ar {AUDIO_RATE} -ac 1 "
            f"-f avi pipe:1"
        )


    proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, bufsize=1 << 20)


    target_bps = int(os.environ.get("STREAM_MAX_BPS", "150000"))

    def generate():
        t0 = time.monotonic()
        sent = 0
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                sent += len(chunk)
                expected_elapsed = sent / target_bps
                actual_elapsed = time.monotonic() - t0
                if expected_elapsed > actual_elapsed:
                    time.sleep(expected_elapsed - actual_elapsed)
                yield chunk
        finally:
            proc.stdout.close()
            proc.terminate()

    return Response(generate(), mimetype="video/avi")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
