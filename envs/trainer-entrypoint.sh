#!/usr/bin/env bash
#
# Trainer container entrypoint.
#
# Brings the container into the hecaton tailnet (userspace mode, no host
# kernel deps), exports an HTTPS proxy so trainer code in this container
# transparently reaches `*.<tailnet>.ts.net`, then execs the trainer's
# own command.
#
# Required env:
#   TS_AUTHKEY           Tailscale auth key (recommended: reusable +
#                        ephemeral + pre-approved + tag:trainer).
#
# Optional env:
#   TS_HOSTNAME          Tailscale device hostname. Default: $HOSTNAME.
#   TS_SOCKS_PORT        SOCKS5 port for tailscaled. Default: 1055.
#
# Usage in the image:
#   COPY trainer-entrypoint.sh /usr/local/bin/
#   ENTRYPOINT ["trainer-entrypoint.sh"]
#   CMD ["python", "-m", "your.trainer"]

set -euo pipefail

: "${TS_AUTHKEY:?TS_AUTHKEY is required (Tailscale auth key for the trainer)}"
TS_HOSTNAME="${TS_HOSTNAME:-$(hostname)}"
TS_SOCKS_PORT="${TS_SOCKS_PORT:-1055}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "trainer-entrypoint: installing tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# Userspace networking: no /dev/net/tun, no NET_ADMIN, works inside any
# container. tailscaled binds a SOCKS5 proxy on $TS_SOCKS_PORT.
mkdir -p /var/lib/tailscale /var/run/tailscale
tailscaled \
  --tun=userspace-networking \
  --socks5-server="localhost:${TS_SOCKS_PORT}" \
  --state=/var/lib/tailscale/tailscaled.state \
  --socket=/var/run/tailscale/tailscaled.sock \
  >/var/log/tailscaled.log 2>&1 &
TAILSCALED_PID=$!

# Wait for tailscaled to come up before `tailscale up`.
for _ in $(seq 1 20); do
  if tailscale --socket=/var/run/tailscale/tailscaled.sock status >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

tailscale --socket=/var/run/tailscale/tailscaled.sock up \
  --authkey="$TS_AUTHKEY" \
  --hostname="$TS_HOSTNAME" \
  --accept-dns=true \
  --accept-routes

# Route HTTP(S) traffic from the trainer process through tailscaled.
# Python `httpx` and `requests` both honor these.
export ALL_PROXY="socks5h://localhost:${TS_SOCKS_PORT}"
export HTTPS_PROXY="$ALL_PROXY"
export HTTP_PROXY="$ALL_PROXY"
export NO_PROXY="localhost,127.0.0.1"

# If the user-supplied command exits, take tailscaled down with it.
trap 'kill -TERM "$TAILSCALED_PID" 2>/dev/null || true' EXIT

exec "$@"
