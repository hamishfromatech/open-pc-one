"""The companion's window: a local server and one page.

The desktop app is a browser view onto a process that holds the connection. That
shape is deliberate: the reusable part is ``client.py`` and the frame protocol, so
wrapping this page in Tauri (or porting the client to Flutter for mobile) does not
change a single message on the wire.

Why a local server instead of the page talking to the box directly: the box is
self-signed and speaks WSS with a pin, and browsers cannot pin. The process holds
the socket; the page talks plain HTTP to localhost.

Everything shown is what the *box* reports — its CPU, its memory, its temperature.
The customer's own computer runs the big model; this window shows the thing that is
awake at 3am.
"""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

CREAM = "#F9F4EE"
INK = "#0B0908"
MUTED = "#6E6058"
HAIR = "#E4D6CA"
RED = "#D72E35"
RED_DEEP = "#A8202A"


class Session:
    """Everything the page needs, updated by the socket's reader thread."""

    def __init__(self, companion: Any) -> None:
        self.companion = companion
        self.lock = threading.Lock()
        self.state = "connecting"
        self.appliance: dict[str, Any] = {}
        self.pin = ""
        self.stats: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []
        self.streaming = False
        self.last_error = ""
        self.stats_at = 0.0

    # -- from the socket ---------------------------------------------------

    def on_state(self, state: str) -> None:
        with self.lock:
            self.state = state

    def on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        with self.lock:
            if kind == "host_hello":
                self.appliance = self.appliance or {}
                self.streaming = False
                for message in frame.get("messages") or []:
                    self._add_message(message)
            elif kind == "host_stats":
                self.stats = frame.get("stats") or {}
                self.stats_at = time.time()
            elif kind == "message_start":
                self.streaming = True
            elif kind == "message_update":
                event = frame.get("assistantMessageEvent") or {}
                if event.get("type") == "text_delta":
                    self._append_delta(str(event.get("delta") or ""))
            elif kind == "message_end":
                self.streaming = False
                self._seal_message()
            elif kind == "agent_end":
                self.streaming = False
                self._seal_message()
            elif kind == "host_error":
                self.last_error = str(frame.get("message") or frame.get("code") or "error")
            elif kind == "host_engine_down":
                self.last_error = f"engine down: {frame.get('reason', '')}"
                self.streaming = False

    def _add_message(self, message: dict[str, Any]) -> None:
        content = message.get("content")
        text = content if isinstance(content, str) else _content_text(content)
        if text:
            self.messages.append({"role": message.get("role", "assistant"), "text": text})

    def _append_delta(self, delta: str) -> None:
        if not delta:
            return
        if not self.messages or self.messages[-1].get("role") != "assistant":
            self.messages.append({"role": "assistant", "text": "", "partial": True})
        self.messages[-1]["text"] += delta

    def _seal_message(self) -> None:
        if self.messages and self.messages[-1].get("partial"):
            self.messages[-1].pop("partial", None)

    # -- from the page -----------------------------------------------------

    def send_prompt(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self.lock:
            self.messages.append({"role": "user", "text": text})
            self.streaming = True
            self.last_error = ""
        self.companion.prompt(text)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            # The TLS pin is the thing a person should be able to compare with the
            # box. The client captures it where the transport allows; the appliance's
            # own certFingerprint is the same value and is always present.
            pin = self.pin or str((self.appliance or {}).get("certFingerprint") or "")
            return {
                "state": self.state,
                "appliance": self.appliance,
                "pin": pin,
                "stats": self.stats,
                "statsAgeSeconds": round(time.time() - self.stats_at, 1) if self.stats_at else None,
                "messages": list(self.messages),
                "streaming": self.streaming,
                "error": self.last_error,
            }


def _content_text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>AmaaraOS Companion</title>
<style>
:root{--cream:__CREAM__;--ink:__INK__;--muted:__MUTED__;--hair:__HAIR__;--red:__RED__;--red2:__RED2__}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--cream);color:var(--ink);font:14px/1.5 "Inter","Segoe UI",system-ui,sans-serif}
.shell{display:grid;grid-template-columns:300px 1fr;height:100vh}
aside{border-right:1px solid var(--hair);padding:22px 18px;overflow:auto}
main{display:flex;flex-direction:column;min-width:0}
header{border-bottom:1px solid var(--hair);padding:14px 22px;display:flex;align-items:center;gap:12px}
.wordmark{font-family:Georgia,"DejaVu Serif",serif;font-weight:700;font-size:15px}
.rule{width:22px;height:4px;background:var(--red)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:#2F7D4F}.dot.warn{background:var(--red)}
.status{color:var(--muted);font-size:12px}
h2{font:600 10.5px/1 inherit;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);margin:20px 0 10px}
.card{background:#FFFDF9;border:1px solid var(--hair);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.card .k{color:var(--muted);font-size:11.5px}
.card .v{font:600 18px/1.2 "DejaVu Sans Mono",monospace}
.bar{height:5px;border-radius:999px;background:#EFE3D8;overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;background:var(--red)}
.meta{font:11.5px/1.7 "DejaVu Sans Mono",monospace;color:var(--muted)}
#log{flex:1;overflow:auto;padding:22px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:70ch;border-radius:12px;padding:11px 14px;white-space:pre-wrap;word-wrap:break-word}
.me{align-self:flex-end;background:var(--red);color:#fff}
.bot{align-self:flex-start;background:#FFFDF9;border:1px solid var(--hair)}
form{display:flex;gap:10px;padding:16px 22px;border-top:1px solid var(--hair)}
input[type=text]{flex:1;padding:13px 15px;border:1px solid var(--hair);border-radius:10px;background:#FFFDF9;
  font-size:14px;color:var(--ink)}
input:focus{outline:none;border-color:var(--red);box-shadow:0 0 0 3px rgba(215,46,53,.12)}
button{border:0;border-radius:10px;padding:13px 20px;background:var(--red);color:#fff;font-weight:600;cursor:pointer}
button:hover{background:var(--red2)}button.ghost{background:transparent;color:var(--red2);border:1px solid var(--hair)}
</style></head><body>
<div class="shell">
  <aside>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <div class="rule"></div><div class="wordmark">AmaaraOS</div>
    </div>
    <div class="status" id="state">connecting…</div>

    <h2>This box</h2>
    <div class="meta" id="device">—</div>

    <h2>Resources</h2>
    <div class="card"><div class="k">CPU</div><div class="v" id="cpu">—</div><div class="bar"><i id="cpubar" style="width:0%"></i></div></div>
    <div class="card"><div class="k">Memory</div><div class="v" id="mem">—</div><div class="bar"><i id="membar" style="width:0%"></i></div></div>
    <div class="card"><div class="k">Disk</div><div class="v" id="disk">—</div><div class="bar"><i id="diskbar" style="width:0%"></i></div></div>
    <div class="card"><div class="k">Temperature</div><div class="v" id="temp">—</div></div>
    <div class="meta" id="units" style="margin-top:6px"></div>

    <h2>Link</h2>
    <div class="meta" id="pin">pin —</div>
  </aside>

  <main>
    <header>
      <div class="dot" id="dot"></div>
      <div class="wordmark" id="title">Companion</div>
      <div style="flex:1"></div>
      <button class="ghost" id="abort" type="button">Stop</button>
    </header>
    <div id="log"></div>
    <form id="say">
      <input type="text" id="text" placeholder="Ask the box…" autocomplete="off" autofocus>
      <button type="submit">Send</button>
    </form>
  </main>
</div>
<script>
const esc = s => (s||"").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const gb = b => b ? (b/1024/1024/1024).toFixed(b>10*1024*1024*1024?0:1)+" GB" : "—";

async function refresh(){
  try {
    const r = await fetch('/api/state'); const s = await r.json();
    document.getElementById('state').textContent =
      s.state + (s.error ? " — " + s.error : "") + (s.streaming ? " · thinking…" : "");
    document.getElementById('dot').className = 'dot ' + (s.state === 'ready' ? 'ok' : 'warn');
    const a = s.appliance || {};
    document.getElementById('title').textContent = a.name || "Companion";
    document.getElementById('device').textContent =
      (a.name||"?") + "\\n" + (a.model||"") + "\\n" + (a.id||"") + "\\n" + (s.pin ? "pin " + s.pin.slice(0,23) + "…" : "");
    const st = s.stats || {}, cpu = (st.cpu||{}).percent, mem = st.memory, d = st.disk;
    document.getElementById('cpu').textContent = cpu == null ? "—" : cpu.toFixed(1) + "%";
    document.getElementById('cpubar').style.width = (cpu||0) + "%";
    document.getElementById('mem').textContent = mem ? (mem.percent + "%") : "—";
    document.getElementById('membar').style.width = (mem?mem.percent:0) + "%";
    document.getElementById('disk').textContent = d ? (d.percent + "%") : "—";
    document.getElementById('diskbar').style.width = (d?d.percent:0) + "%";
    document.getElementById('temp').textContent = st.temperatureC == null ? "no sensor" : st.temperatureC.toFixed(1) + " °C";
    const units = st.units || {};
    document.getElementById('units').textContent = Object.entries(units).map(([k,v]) => k+": "+v).join("\\n")
      + (st.uptimeSeconds ? "\\nup " + Math.round(st.uptimeSeconds/3600) + " h" : "");
    document.getElementById('pin').textContent = s.pin ? "pin " + s.pin : "pin —";
    const log = document.getElementById('log');
    log.innerHTML = (s.messages||[]).map(m =>
      '<div class="msg ' + (m.role === 'user' ? 'me' : 'bot') + '">' + esc(m.text) + '</div>').join("");
    log.scrollTop = log.scrollHeight;
  } catch(e) { document.getElementById('state').textContent = 'companion unreachable'; }
}
document.getElementById('say').addEventListener('submit', async ev => {
  ev.preventDefault();
  const input = document.getElementById('text');
  const text = input.value.trim(); if (!text) return;
  input.value = '';
  await fetch('/api/prompt', {method:'POST', headers:{'Content-Type':'application/json'},
                              body: JSON.stringify({text})});
  refresh();
});
document.getElementById('abort').addEventListener('click', () => fetch('/api/abort', {method:'POST'}));
refresh(); setInterval(refresh, 900);
</script></body></html>"""


def render_page() -> bytes:
    """Substitute the brand tokens.

    Plain replace, not `%` formatting: the stylesheet is full of percentages and
    every one of them would need escaping, which is exactly the kind of bug that
    only shows up when someone opens the window.
    """
    html = PAGE
    for name, value in (
        ("CREAM", CREAM), ("INK", INK), ("MUTED", MUTED),
        ("HAIR", HAIR), ("RED2", RED_DEEP), ("RED", RED),
    ):
        html = html.replace(f"__{name}__", value)
    return html.encode()


class Handler(BaseHTTPRequestHandler):
    session: Session  # injected by make_server
    server_version = "amaara-companion"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        pass

    def _json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib
        path = urlparse(self.path).path
        if path == "/":
            body = render_page()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            return self._json(self.session.snapshot())
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/api/prompt":
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, HTTPStatus.BAD_REQUEST)
            self.session.send_prompt(str(payload.get("text", "")))
            return self._json({"ok": True})
        if path == "/api/abort":
            self.session.companion.abort()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def make_server(session: Session, host: str = "127.0.0.1", port: int = 8123) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"session": session})
    return ThreadingHTTPServer((host, port), handler)


def serve(session: Session, host: str = "127.0.0.1", port: int = 8123) -> None:
    server = make_server(session, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
