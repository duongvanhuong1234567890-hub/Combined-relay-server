#!/usr/bin/env python3
"""
combined_relay_server.py
============================================================================
Relay server DUY NHẤT cho firmware T-Display S3 Video Player - gộp
tiktok_relay_server.py + yt_relay_server.py thành 1 file, chạy CHUNG 1
cổng (mặc định 8000) để .ino chỉ cần trỏ về đúng 1 host cho cả TikTok
lẫn YouTube (không còn RELAY_HOST_HOME/AWAY và YT_RELAY_HOST_HOME/AWAY
tách riêng nữa - xem RELAY_HOST_HOME/AWAY hợp nhất trong file .ino).

Cung cấp cả 3 endpoint mà firmware gọi tới:

  GET /search?q=<từ khóa>&n=<số kết quả>
      -> [ { "id": "<video id>", "title": "...", "duration": "3:32" }, ... ]
      (YouTube search qua yt-dlp - TikTok không có endpoint search, xem
      /random bên dưới)

  GET /stream?url=<youtube url / ytsearch1:... / link TikTok cụ thể>&w=&h=&height_cap=&fps=
      -> resolve link đó bằng yt-dlp rồi transcode sang AVI (MJPEG + PCM)
      cho firmware phát - dùng CHUNG 1 logic cho cả 2 nền tảng, vì yt-dlp
      tự nhận diện domain trong URL và xử lý đúng cách tương ứng.

  GET /random
      -> trả về {"url": "<1 link random>"} lấy từ PLAYLIST_FILE (mỗi dòng
      1 link TikTok, tự soạn - xem hướng dẫn dưới). Rỗng/không tìm thấy
      file -> 404.

Cách dùng playlist TikTok: tự mở TikTok, copy link "Share" của mỗi video
muốn có trong playlist, thêm mỗi link 1 dòng vào file PLAYLIST_FILE (mặc
định "tiktok_links.txt", cùng thư mục với file này).

CHẠY TRÊN TERMUX:
  pkg install python ffmpeg
  pip install flask yt-dlp
  termux-wake-lock   (cần thêm: pkg install termux-api - giữ CPU không bị
                       Android hạ xung khi chạy nền, tắt luôn tối ưu pin
                       cho Termux trong Cài đặt > Ứng dụng > Termux > Pin
                       > Không giới hạn)
  python combined_relay_server.py

  [yt-dlp-SABR fix] YouTube liên tục siết định dạng "SABR-only" theo từng
  client (android/web/ios/...) - khi bị chặn hết định dạng nhỏ (adaptive),
  yt-dlp phải rơi về itag 18 (360p GỘP SẴN, full độ dài gốc - có video ca
  nhạc lên tới vài trăm MB) và YouTube thường siết tốc độ tải itag này với
  bên thứ ba xuống rất thấp (~50-60KB/s trong thực tế đo được) - không đủ
  để ffmpeg giải mã+mã hoá lại kịp, gây hiện tượng phát 1-2 giây rồi phải
  chờ tải. Đây là hạn chế phía YouTube, KHÔNG phải do buffer/code relay
  này - xem resolve_stream_urls() bên dưới đã thử mở rộng danh sách client
  dự phòng để tăng khả năng vẫn còn client nào đó chưa bị SABR chặn cho
  đúng video đang phát, nhưng không phải lúc nào cũng giải quyết được -
  cách chắc chắn hơn là cài PO Token provider (ví dụ bgutil-ytdlp-pot-
  provider, đã hỗ trợ Termux) để mở lại được các định dạng adaptive nhỏ.
  Vì yt-dlp/YouTube đổi cách xử lý liên tục, nhớ cập nhật yt-dlp thường
  xuyên: pip install -U yt-dlp
  (mặc định lắng nghe 0.0.0.0:8000 - dán IP LAN hoặc link cloudflared vào
   RELAY_HOST_HOME/AWAY trong file .ino - CHỈ 1 cặp macro cho cả 2 nền
   tảng, không còn YT_RELAY_HOST_HOME/AWAY riêng nữa)

  Không còn chạy song song 2 file cũ (tiktok_relay_server.py +
  yt_relay_server.py) nữa - file này thay thế CẢ HAI. Có thể xoá 2 file
  cũ hoặc giữ lại làm tham khảo, không ảnh hưởng gì vì .ino giờ chỉ gọi
  1 host duy nhất.
============================================================================
"""

import subprocess
import shlex
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


# [uptime-fix] Route gốc "/" chỉ để trả 200 cho các dịch vụ giám sát uptime
# (vd. UptimeRobot) - gói free của UptimeRobot không cho tuỳ chỉnh "Accepted
# status codes" nên mặc định coi 404 là "Down", dù server thực ra vẫn sống
# bình thường (firmware .ino không bao giờ gọi "/", chỉ gọi /search, /stream,
# /random - route này không ảnh hưởng gì tới logic phát video).
@app.route("/")
def health_check():
    return "OK", 200

PORT = 8000  # 1 cổng duy nhất cho cả /search, /stream (YouTube + TikTok), /random

# [bot-check-fix] IP datacenter (Render, Codespaces, ...) bị YouTube chặn với
# lỗi "Sign in to confirm you're not a bot" - cách khắc phục đáng tin cậy duy
# nhất là đưa cho yt-dlp cookies của 1 tài khoản Google đã đăng nhập thật (xem
# hướng dẫn xuất cookies.txt cuối file này). ĐỪNG commit cookies.txt vào git
# repo (ai có cookies là đăng nhập được luôn tài khoản đó) - nhớ thêm dòng
# "cookies.txt" vào file .gitignore của repo.
#
# - Trên Render: có thể dùng "Secret Files" (Environment > Secret Files), nó
#   tự mount vào /etc/secrets/<tên file> mà không lưu trong git - nếu dùng
#   cách đó thì đổi biến COOKIES_FILE bên dưới lại thành
#   "/etc/secrets/cookies.txt".
# - Trên Codespaces (không có Secret Files): đặt file cookies.txt cùng thư
#   mục với file .py này (upload thủ công qua Explorer, KHÔNG qua git) -
#   COOKIES_FILE bên dưới sẽ tự tìm đúng chỗ đó.
#
# Nếu COOKIES_FILE không tồn tại, mọi thứ vẫn chạy như cũ (không cookies) -
# không bắt buộc phải có mới chạy được server.
_SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(_SECRET_COOKIES):
    # Secret Files của Render là read-only, mà yt-dlp cố ghi lại cookie jar
    # khi kết thúc -> copy sang /tmp (ghi được) rồi dùng bản copy.
    import shutil
    shutil.copyfile(_SECRET_COOKIES, "/tmp/cookies.txt")
    COOKIES_FILE = "/tmp/cookies.txt"

# [debug] In ngay lúc server khởi động xem Secret File có thực sự được Render
# mount vào đúng chỗ hay không - nếu log không thấy dòng này khi service
# start lại, tức là chưa deploy code mới; nếu thấy "KHONG TIM THAY" thì lỗi
# nằm ở bước tạo Secret File trên Render (sai tên file, hoặc chưa lưu).
if os.path.exists(COOKIES_FILE):
    _sz = os.path.getsize(COOKIES_FILE)
    print(f"[cookies] TIM THAY {COOKIES_FILE} - kich thuoc {_sz} bytes")
    if _sz < 200:
        print("[cookies] CANH BAO: file qua nho, co the dan thieu noi dung hoac rong")
else:
    print(f"[cookies] KHONG TIM THAY {COOKIES_FILE} - Secret File chua duoc tao dung, hoac sai ten file")

# File danh sách link cho /random - tự soạn, mỗi dòng 1 link TikTok, dòng
# trống hoặc bắt đầu bằng "#" bị bỏ qua (dùng để ghi chú). Cùng thư mục với
# file này trừ khi bạn đổi thành đường dẫn tuyệt đối.
PLAYLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_links.txt")


# Ép HTTP/1.0 để tránh "Transfer-Encoding: chunked" - board đọc socket thô,
# không tự giải mã chunked-encoding của HTTP/1.1, nên header hex chunk-size
# bị lẫn vào đầu dữ liệu AVI làm sai lệch parseAviNet(). HTTP/1.0 gửi dữ
# liệu thô, đóng kết nối khi hết - không còn framing lạ chen vào.
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


# [api-key] Key YouTube Data API v3 đọc từ biến môi trường YOUTUBE_API_KEY
# (Render > Environment) - KHÔNG hardcode key vào file này/git. Chỉ dùng cho
# /search (API key không bị "bot check" như yt-dlp trên IP datacenter). Phần
# /stream vẫn dùng yt-dlp vì Data API không trả link luồng video.
# Quota: mỗi lần search.list tốn 100 đơn vị, mặc định 10.000/ngày (~100 lượt
# tìm/ngày). Hết quota hoặc key lỗi -> tự rơi về yt-dlp như cũ.
YT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

# [low-bw] Băng thông luồng phát chỉnh được bằng Environment trên Render (không cần sửa code):
#   MJPEG_Q    : độ nén JPEG của ffmpeg, số càng lớn ảnh càng mờ và càng nhẹ (mặc định 20)
#   AUDIO_RATE : tần số lấy mẫu audio, 16000 -> ~32KB/s, 11025 -> ~22KB/s, 8000 -> ~16KB/s
# Firmware đọc audioRate từ AVI header nên không cần nạp lại .ino.
MJPEG_Q = os.environ.get("MJPEG_Q", "20")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "1")
AUDIO_RATE = os.environ.get("AUDIO_RATE", "16000")


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
        f"bestvideo[height<={height_cap}]+bestaudio"
        f"/best[height<={height_cap}]"
        f"/best"
    )
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "skip_download": True,
        # [yt-dlp-SABR fix] YouTube đang siết "SABR-only" theo TỪNG client
        # riêng lẻ (không phải toàn bộ YouTube) - một video có thể bị chặn
        # định dạng nhỏ ở client này nhưng vẫn còn ở client khác, và việc
        # đó thay đổi theo thời gian/video. Trước chỉ thử "android" rồi
        # "web" - giờ thêm "tv" và "ios" vào danh sách dự phòng để tăng cơ
        # hội gặp đúng client chưa bị chặn cho video đang phát (thứ tự này
        # không đảm bảo luôn tránh được SABR - khi CẢ danh sách đều bị chặn
        # định dạng nhỏ, fmt vẫn phải rơi xuống "best" không giới hạn kích
        # thước, tức itag 18 360p gộp sẵn, xem ghi chú SABR ở đầu file).
        # Muốn mở lại được định dạng nhỏ một cách ổn định, cần PO Token
        # provider (bgutil-ytdlp-pot-provider) chứ list client dự phòng chỉ
        # là biện pháp "hên xui" không tốn thêm hạ tầng.
        # [render-ip-fix] Chạy trên IP datacenter (Render) dễ bị YouTube
        # chặn/soft-block theo TỪNG client hơn hẳn IP nhà mạng thường - mở
        # rộng danh sách client dự phòng (thêm mweb, android_music,
        # web_embedded) để tăng khả năng còn ít nhất 1 client chưa bị chặn.
        # Không đảm bảo hết bị chặn hoàn toàn (đó là vấn đề phía IP, xem
        # ghi chú SABR ở đầu file) - đây chỉ tăng tỉ lệ thành công, không
        # phải fix tuyệt đối.
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
        if "entries" in info:  # ytsearch1:... trả về playlist 1 phần tử
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
        # Trường hợp hiếm: yt-dlp tự tìm được 1 format gộp sẵn duy nhất
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
    # [lag-fix] YouTube không có định dạng nào dưới 144p, nên height_cap=100
    # không khớp gì và yt-dlp rơi xuống "/best" (thường 360p trở lên) -> ffmpeg
    # trên CPU yếu của Render phải giải mã quá nặng. Ép tối thiểu 144p.
    try:
        height_cap = str(max(int(height_cap), 144))
    except ValueError:
        height_cap = "144"
    fps = request.args.get("fps", "15")

    if not video_url:
        return Response(status=400)

    try:
        video_direct_url, audio_direct_url, video_headers, audio_headers = resolve_stream_urls(video_url, height_cap)
    except Exception as e:
        # [debug] In cả traceback đầy đủ (không chỉ str(e)) - lỗi yt-dlp
        # thường là 1 exception lồng nhau (DownloadError bọc ExtractorError
        # bọc lý do thật, vd "Sign in to confirm you're not a bot") mà
        # str(e) đôi khi cắt cụt. Xem log Render ngay sau dòng "resolve
        # error" để biết lý do thật.
        print(f"[stream] resolve error: {e}")
        traceback.print_exc()
        return Response(status=502)

    # "-re" ĐÃ BỎ (thử nghiệm): bug gốc là tràn số uint32_t trong
    # parseAviNet() (đã sửa ở file .ino), không phải do thiếu "-re". Ép
    # pace real-time trên 2 input riêng biệt (video/audio) khiến độ trễ
    # mạng dao động khi tải nguồn truyền thẳng thành giật đồng thời cả
    # hình lẫn tiếng. Nếu lỗi "Stream error / disconnected" tái xuất hiện,
    # khôi phục "-re" trước mỗi -i.
    #
    # [yt-dlp-SABR fix] "-reconnect..." thêm trước MỖI -i: khi nguồn bị
    # YouTube siết tốc độ (itag 18 fallback do SABR, xem ghi chú ở đầu
    # file) hoặc rớt mạng giữa chừng, log thực tế cho thấy kết nối HTTP bị
    # "Connection reset by peer" - không có các cờ này, ffmpeg coi đó là
    # lỗi input và toàn bộ luồng chết luôn (muxer "Broken pipe" phía sau,
    # đúng như log). Các cờ dưới cho ffmpeg TỰ kết nối lại (kể cả khi lỗi
    # xảy ra giữa stream, không chỉ lúc mở kết nối) thay vì bỏ cuộc ngay -
    # không giải quyết được tốc độ nguồn đang bị siết, nhưng đỡ việc cả
    # phiên phát bị chết cứng chỉ vì 1 lần rớt mạng thoáng qua.
    RECONNECT_ARGS = (
        "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
        "-reconnect_delay_max 5 "
    )
    video_headers_arg = f"-headers {shlex.quote(video_headers)} " if video_headers else ""
    audio_headers_arg = f"-headers {shlex.quote(audio_headers)} " if audio_headers else ""

    # [perf] -threads 0: để ffmpeg tự chọn số luồng bằng số nhân CPU của máy
    # (mặc định ffmpeg chỉ dùng 1 luồng cho phần lớn filter/encode nếu không
    # set cờ này). mjpeg là codec intra-only (mỗi frame độc lập) nên encode
    # scale-song-song-nhiều-frame rất hiệu quả trên CPU đa nhân của điện
    # thoại - đây là chỗ có khả năng cao nhất đang là nút thắt CPU thực sự
    # (không phải mạng - đã đo mạng nhà đủ nhanh), vì trước đó ffmpeg chỉ
    # chạy 1 nhân trong khi máy có 6-8 nhân rảnh.
    # [lag-fix] Mặc định 1 luồng. "-threads 0" (= số nhân của MÁY CHỦ) hợp với điện thoại 6-8 nhân nhưng trên
    # container Render chỉ được cấp 0.1-0.5 CPU thì nhiều luồng cùng đốt hết hạn mức CPU rồi bị hệ điều hành
    # "phanh" cả loạt -> dữ liệu ra thành từng cục, ngắt quãng, board thấy giật. Đổi bằng biến FFMPEG_THREADS.
    THREADS_ARG = f"-threads {FFMPEG_THREADS} "

    if audio_direct_url:
        cmd = (
            f"ffmpeg -v error "
            f"{RECONNECT_ARGS}{video_headers_arg}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{RECONNECT_ARGS}{audio_headers_arg}{THREADS_ARG}-i {shlex.quote(audio_direct_url)} "
            f"-map 0:v:0 -map 1:a:0 "
            f"{THREADS_ARG}"
            f"-vf scale={w}:{h}:flags=fast_bilinear,fps={fps} "
            # [perf] q:v 20 (tăng từ 16, tăng từ 12 gốc): log Serial cho
            # thấy đỉnh (max) của videoPayloadRead/drawJpg cao gấp 3-4 lần
            # trung bình - đúng lúc cảnh có nhiều chuyển động/chi tiết, khung
            # JPEG lúc đó nặng hơn hẳn khiến cả encode lẫn decode khựng lại
            # đột ngột (giật nặng đúng khi hành động nhanh). Nén thêm ở mọi
            # khung (kể cả khung tĩnh) để hạ luôn kích thước khung "khó",
            # giảm biên độ đỉnh - đổi lại hình mờ hơn 1 chút liên tục.
            f"-c:v mjpeg -q:v {MJPEG_Q} "
            # 16000Hz mono 16-bit (~32000 Bps) thay vì 22050/44100Hz: máy
            # Termux không đủ CPU/băng thông để encode/tải kịp mức cao hơn,
            # gây underrun audio định kỳ. Firmware tự đọc audioRate/audioBits
            # từ AVI header ffmpeg tạo ra, không hardcode ở .ino.
            f"-c:a pcm_s16le -ar {AUDIO_RATE} -ac 1 "
            f"-f avi pipe:1"
        )
    else:
        cmd = (
            f"ffmpeg -v error {RECONNECT_ARGS}{video_headers_arg}{THREADS_ARG}-i {shlex.quote(video_direct_url)} "
            f"{THREADS_ARG}"
            f"-vf scale={w}:{h}:flags=fast_bilinear,fps={fps} "
            f"-c:v mjpeg -q:v {MJPEG_Q} "  # [perf] tăng từ 16, xem giải thích ở nhánh có audio phía trên
            f"-c:a pcm_s16le -ar {AUDIO_RATE} -ac 1 "
            f"-f avi pipe:1"
        )

    # [perf] bufsize=1<<20 (1MB) thay vì mặc định: Python mặc định dùng
    # buffer I/O khá nhỏ cho pipe, khiến vòng đọc bên dưới phải chờ ffmpeg
    # từng đợt ngắn thay vì có sẵn dữ liệu để đọc liên tục - đặt lớn hơn để
    # generate() ít bị đói dữ liệu khi ffmpeg encode dồn cụm (do CPU đa
    # nhân giờ xử lý nhanh hơn nhưng không đều).
    proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, bufsize=1 << 20)

    # [perf] Đọc 64KB/lần thay vì 4KB: giảm số lần gọi syscall read() (mỗi
    # lần đọc nhỏ tốn thêm chi phí chuyển ngữ cảnh Python<->OS khi encode
    # đang chạy nhanh, dồn dữ liệu). Vẫn forward NGAY từng chunk đọc được
    # (không gộp thêm ở tầng ứng dụng) nên độ trễ audio/video giữa các
    # chunk không đổi - chỉ đổi kích thước lần đọc, không đổi cách stream.
    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.terminate()

    return Response(generate(), mimetype="video/avi")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, request_handler=HTTP10RequestHandler)
