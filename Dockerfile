# Stage 1: cai dependency cho script bgutil (can Node/yarn chi luc build)
FROM node:22-slim AS potbuild
ARG BGUTIL_TAG=2.0.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${BGUTIL_TAG} https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /src \
    && cd /src/server && yarn install --frozen-lockfile

# Stage 2: image chay that
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-noto-core fonts-noto-color-emoji fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
# Plugin mac dinh tim server o ~/bgutil-ytdlp-pot-provider/server (HOME=/root)
COPY --from=potbuild /src/server /root/bgutil-ytdlp-pot-provider/server
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium
COPY combined_relay_server.py ./
ENV PORT=8000 MALLOC_ARENA_MAX=2
CMD ["gunicorn","-b","0.0.0.0:8000","-w","1","--threads","16","--timeout","0","combined_relay_server:app"]
