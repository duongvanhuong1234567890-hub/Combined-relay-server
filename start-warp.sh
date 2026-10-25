#!/bin/sh
# Bat WARP (wireproxy) -> HTTP proxy 127.0.0.1:8118, dat YT_PROXY, roi chay lenh goc (CMD) cua Dockerfile.
# Neu WARP loi thi van chay binh thuong nhu cu (khong proxy).
ORIG="$(pwd)"
WP_PORT="${WARP_PORT:-8118}"
DIR=/tmp/warp
mkdir -p "$DIR"

if [ "${WARP:-1}" = "1" ] && [ -z "$YT_PROXY" ] && command -v wgcf >/dev/null 2>&1 && command -v wireproxy >/dev/null 2>&1; then
  cd "$DIR"
  if [ ! -f wgcf-profile.conf ]; then
    wgcf register --accept-tos >/dev/null 2>&1
    wgcf generate >/dev/null 2>&1
  fi
  if [ -f wgcf-profile.conf ]; then
    cp wgcf-profile.conf wireproxy.conf
    printf '\n[http]\nBindAddress = 127.0.0.1:%s\n' "$WP_PORT" >> wireproxy.conf
    wireproxy -c wireproxy.conf > "$DIR/wireproxy.log" 2>&1 &
    sleep 4
    if curl -s -m 12 -x "http://127.0.0.1:$WP_PORT" https://www.cloudflare.com/cdn-cgi/trace | grep -Eq "warp=(on|plus)"; then
      export YT_PROXY="http://127.0.0.1:$WP_PORT"
      echo "[warp] OK -> YT_PROXY=$YT_PROXY"
    else
      echo "[warp] loi: proxy khong len, chay khong proxy"
      tail -n 5 "$DIR/wireproxy.log" 2>/dev/null
    fi
  else
    echo "[warp] loi: khong tao duoc profile WARP (wgcf register that bai)"
  fi
  cd "$ORIG"
fi

exec "$@"
