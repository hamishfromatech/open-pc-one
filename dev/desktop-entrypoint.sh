#!/bin/bash
# Bring up AmaaraOS in a container and put a browser-compatible stream in front.
#
# Layout:
#   amaara-setup (demo)  -> license + install profiles on :8099
#   openpc-session       -> Xvfb :0 + XFCE + Chromium (kiosk on the setup screen
#                           until setup is complete, then the agent's desktop)
#   x11vnc               -> the :0 framebuffer on :5900
#   websockify + noVNC   -> :6080, which is what you open in a browser
#
# openpc-session traps EXIT and runs `kill 0`, so it must not share this process
# group or stopping the container would kill the supervisor with it.
set -uo pipefail

log() { printf '%s desktop: %s\n' "$(date -Is)" "$*" >&2; }

STATE_DIR="${AMAARA_SETUP_STATE_DIR:-/var/lib/amaara/setup}"
mkdir -p /run/openpc /tmp/.X11-unix /tmp/.ICE-unix "$STATE_DIR" /etc/amaara
chmod 1777 /tmp/.X11-unix /tmp/.ICE-unix

# A throwaway signing key and a batch of demo keys, generated at start-up. The
# real image ships only a public key and a manifest signed on our signing host;
# this exists so the flow can be clicked through without one.
if [ ! -s /etc/amaara/license.json ]; then
  log "generating a demo license batch (throwaway key, not for shipping)"
  python3 -m amaara_setup.cli keygen --out /etc/amaara/keys >/dev/null 2>&1 \
    || log "keygen failed"
  python3 -m amaara_setup.cli generate-license \
      --private-key /etc/amaara/keys/license.private.pem \
      --batch DEMO-CONTAINER --tier demo --count 5 \
      --out /etc/amaara >/dev/null 2>&1 || log "generate-license failed"
  cp /etc/amaara/keys/license.pub /etc/amaara/license.pub 2>/dev/null || true
  mv /etc/amaara/manifest-DEMO-CONTAINER.json /etc/amaara/license.json 2>/dev/null || true
  DEMO_KEY="$(head -n 1 /etc/amaara/keys-DEMO-CONTAINER.txt 2>/dev/null || true)"
  log "demo license key: ${DEMO_KEY:-<none>}"
fi

start_setup() {
  local phase
  phase="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1]))["phase"])
except Exception:
    print("welcome")' "$STATE_DIR/state.json" 2>/dev/null || echo welcome)"
  # 0.0.0.0 inside the container so compose can publish it on 127.0.0.1 of the
  # host. The appliance binds loopback only; this is the preview.
  log "starting amaara-setup on :8099 (phase: $phase)"
  python3 -m amaara_setup.cli --state-dir "$STATE_DIR" serve \
      --bind 0.0.0.0:8099 \
      --license-manifest /etc/amaara/license.json \
      --license-pubkey /etc/amaara/license.pub \
      --dry-run --no-poweroff &
}

if [ "${AMAARA_SETUP_DISABLE:-0}" != "1" ]; then
  start_setup
fi

# systemd restarts the session unit on the appliance; the container has no
# systemd, so the same supervision lives here. It matters for the setup handover:
# when setup finishes, the session deliberately exits and comes back without the
# kiosk.
session_loop() {
  while :; do
    setsid /opt/openpc/bin/openpc-session &
    local pid=$!
    wait "$pid"
    log "openpc-session exited; restarting in 2s"
    sleep 2
  done
}
log "starting openpc-session"
session_loop &

# Xvfb is up when xdpyinfo answers; XFCE and Chromium come after that.
for _ in $(seq 1 200); do
  xdpyinfo -display :0 >/dev/null 2>&1 && break
  sleep 0.2
done
if ! xdpyinfo -display :0 >/dev/null 2>&1; then
  log "display :0 never became ready"
  exit 1
fi
log "display :0 ready"

# -nopw is deliberate and dev-only: this VNC endpoint has no authentication, the
# appliance has no VNC at all, and the container is bound to localhost by the
# compose file. Never publish this port on a routable interface.
x11vnc -display :0 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -xkb -noxdamage -quiet &
VNC_PID=$!

sleep 0.5
if ! kill -0 "$VNC_PID" 2>/dev/null; then
  log "x11vnc failed to start"
  exit 1
fi

log "noVNC on :6080 (open http://localhost:6081/vnc.html)"
# Foreground: when websockify exits the container exits and compose restarts it.
exec websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900
