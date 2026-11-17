#!/bin/bash
# Bring up the appliance desktop and put a browser-compatible stream in front of it.
#
# Layout:
#   openpc-session (the appliance's own script) -> Xvfb :0 + XFCE + Chromium
#   x11vnc            -> the :0 framebuffer on :5900
#   websockify + noVNC -> :6080, which is what you open in a browser
#
# openpc-session traps EXIT and runs `kill 0`, so it must not share this process
# group or stopping the container would kill the supervisor with it.
set -uo pipefail

log() { printf '%s desktop: %s\n' "$(date -Is)" "$*" >&2; }

mkdir -p /run/openpc /tmp/.X11-unix /tmp/.ICE-unix
chmod 1777 /tmp/.X11-unix /tmp/.ICE-unix

log "starting openpc-session"
setsid /opt/openpc/bin/openpc-session &
SESSION_PID=$!

# Xvfb is up when xdpyinfo answers; XFCE and Chromium come after that.
for _ in $(seq 1 200); do
  xdpyinfo -display :0 >/dev/null 2>&1 && break
  if ! kill -0 "$SESSION_PID" 2>/dev/null; then
    log "openpc-session died before the display came up"
    exit 1
  fi
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

log "noVNC on :6080 (open http://localhost:6080/vnc.html)"
# Foreground: when websockify exits the container exits and compose restarts it.
exec websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900
