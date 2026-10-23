FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ffmpeg: ma hoa MJPEG/AVI cho /stream
# fonts-dejavu-core: font co dau tieng Viet cho trinh duyet /br (Pillow ve chu)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# deno: yt-dlp can JS runtime de giai ma chu ky YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Chromium that (chi ban headless-shell cho nhe) + thu vien he thong cua no, cho trinh duyet /br (BR_ENGINE=chromium)
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY combined_relay_server.py .

# Render tu dat bien PORT; server doc PORT (mac dinh 8000)
CMD ["python", "combined_relay_server.py"]
