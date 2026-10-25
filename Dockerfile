# Stage 1: dependency cho script bgutil (Node/yarn chi can luc build)
FROM node:22-slim AS potbuild
ARG BGUTIL_TAG=2.0.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${BGUTIL_TAG} https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /src \
    && cd /src/server && yarn install --frozen-lockfile

# Stage 2: image chay that
FROM python:3.12-slim
ARG WGCF_VERSION=2.2.22
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl fonts-noto-core fonts-noto-color-emoji fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# WARP preinstall (TUY CHON): loi o day KHONG lam hong build. Neu that bai, server tu tai wgcf/wireproxy luc chay.
#  - wgcf: tai truc tiep tu release (khong dung api.github.com, hay bi rate limit)
#  - wireproxy: WireGuard -> HTTP proxy 127.0.0.1:8118 (khong can NET_ADMIN)
RUN ( set -e; \
      curl -fsSL -o /usr/local/bin/wgcf \
          "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_amd64"; \
      mkdir -p /tmp/wp; \
      curl -fsSL https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz \
          | tar -xz -C /tmp/wp; \
      find /tmp/wp -type f -name wireproxy -exec mv {} /usr/local/bin/wireproxy \; ; \
      chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy; \
      test -s /usr/local/bin/wgcf; \
      test -s /usr/local/bin/wireproxy \
    ) || { echo "[build] WARP preinstall that bai - bo qua, server se tu tai luc chay"; \
           rm -f /usr/local/bin/wgcf /usr/local/bin/wireproxy; }; \
    rm -rf /tmp/wp

COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
# Plugin mac dinh tim server o ~/bgutil-ytdlp-pot-provider/server (HOME=/root)
COPY --from=potbuild /src/server /root/bgutil-ytdlp-pot-provider/server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium

COPY combined_relay_server.py ./

# Render cap bien PORT khi chay; ENV la gia tri du phong cho chay local.
# Dung shell de expand ${PORT}: CMD dang mang cu co cung cong 8000 -> Render khong thay cong.
ENV PORT=8000 MALLOC_ARENA_MAX=2 WARP=1
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT} -w 1 --threads 16 --timeout 0 combined_relay_server:app"]
