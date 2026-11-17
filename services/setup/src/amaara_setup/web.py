"""The first-boot UI: one page at a time, served on loopback to a kiosk browser.

Deliberately stdlib-only (`http.server`) and server-rendered. On a 6 W box the
setup screen must cost nothing: no framework, no bundler, no font download — the
only assets are the CSS in ``ui`` and the fonts already installed for the desktop.

Flow, and the reason for each step:

    /            welcome + license key. The OEM reaches this screen, proves the
                 image boots and powers off here. The customer later types their
                 key into the same screen.
    /software    what to install. Units come from the image (offline-safe);
                 packages need the network and say so.
    /installing  every step, live, so a failure names one line instead of
                 leaving a spinner forever.
    /ready       done; the desktop takes over on the next boot.
"""

from __future__ import annotations

import html
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import catalog, ui
from .apply import Result, apply_profiles
from .license import LicenseError, load_manifest, redeem
from .state import SetupState, StateError


class SetupApp:
    """Everything the HTTP layer needs, with no globals so tests can own it."""

    def __init__(
        self,
        state_path: Path,
        manifest_path: Path | None,
        public_key_path: Path | None,
        *,
        dry_run: bool = False,
        allow_poweroff: bool = True,
    ) -> None:
        self.state_path = state_path
        self.manifest_path = manifest_path
        self.public_key_path = public_key_path
        self.dry_run = dry_run
        self.allow_poweroff = allow_poweroff
        self._progress: Result | None = None
        self._running = False
        self._lock = threading.Lock()

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> SetupState:
        return SetupState.load(self.state_path)

    def save(self, state: SetupState) -> None:
        state.save(self.state_path)

    # -- license -----------------------------------------------------------

    def check_license(self, key: str) -> str:
        if not key.strip():
            raise LicenseError("enter the license key from your supplier")
        if self.manifest_path is None or not self.manifest_path.exists():
            raise LicenseError("no license manifest on this device: ask your supplier for a reflash")
        if self.public_key_path is None or not self.public_key_path.exists():
            raise LicenseError("this image has no license public key")
        manifest = load_manifest(self.manifest_path)
        payload = redeem(key, manifest, self.public_key_path.read_bytes())
        state = self.state
        state.license_id = f"{payload.batch}:{payload.tier}"
        state.license_tier = payload.tier
        state.license_batch = payload.batch
        self.save(state)
        return payload.tier

    # -- install -----------------------------------------------------------

    def start_install(self, chosen: list[str]) -> None:
        with self._lock:
            if self._running:
                return
            profiles = catalog.resolve(chosen)
            state = self.state
            state.profiles = [p.id for p in profiles]
            state.move("installing")
            self.save(state)
            self._progress = Result(dry_run=self.dry_run)
            self._running = True
        threading.Thread(target=self._install, args=(profiles,), daemon=True).start()

    def _install(self, profiles) -> None:
        result = apply_profiles(profiles, dry_run=self.dry_run)
        state = self.state
        # Per profile, not all-or-nothing: a package that needed a network and did
        # not have one must not hide the units that did come up.
        failed = {step.profile for step in result.steps if not step.ok}
        state.installed = [p.id for p in profiles if p.id not in failed]
        try:
            state.move("complete")
        except StateError:
            pass
        self.save(state)
        self._progress = result
        self._running = False

    @property
    def progress(self) -> dict:
        return {
            "running": self._running,
            "complete": self.state.complete,
            "result": self._progress.public() if self._progress else None,
        }


class Handler(BaseHTTPRequestHandler):
    app: SetupApp  # set by make_server
    server_version = "amaara-setup"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        pass

    def _send(self, body: bytes, status: int = HTTPStatus.OK, kind: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib
        path = urlparse(self.path).path
        state = self.app.state
        if path == "/healthz":
            return self._send(b'{"status":"ok"}', kind="application/json")
        if path == "/state":
            return self._send(json.dumps(state.public()).encode(), kind="application/json")
        if path == "/api/progress":
            return self._send(json.dumps(self.app.progress).encode(), kind="application/json")

        if state.complete and path in ("/", "/welcome"):
            return self._send(self.ready(state))
        if path in ("/", "/welcome"):
            return self._send(self.welcome(state))
        if path == "/software":
            if state.phase == "welcome":
                return self._redirect("/")
            return self._send(self.software(state))
        if path == "/installing":
            if state.phase not in ("installing", "complete"):
                return self._redirect("/")
            return self._send(self.installing(state))
        if path == "/ready":
            return self._send(self.ready(state))
        return self._send(b"not found", HTTPStatus.NOT_FOUND, kind="text/plain")

    def do_POST(self) -> None:  # noqa: N802 - stdlib
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        if path == "/license":
            key = (form.get("key") or [""])[0]
            try:
                self.app.check_license(key)
                state = self.app.state
                if state.phase == "welcome":
                    state.move("licensed")
                    self.app.save(state)
            except LicenseError as exc:
                return self._send(self.welcome(self.app.state, error=str(exc)), HTTPStatus.BAD_REQUEST)
            return self._redirect("/software")

        if path == "/software":
            try:
                self.app.start_install(form.get("profiles") or [])
            except ValueError as exc:
                return self._send(self.software(self.app.state, error=str(exc)), HTTPStatus.BAD_REQUEST)
            return self._redirect("/installing")

        if path == "/poweroff":
            # The OEM's exit: prove the box boots, then ship it powered off with
            # nothing consumed. In dev there is no systemd, so it only records.
            state = self.app.state
            state.note_factory_check()
            self.app.save(state)
            if self.app.allow_poweroff:
                try:
                    import subprocess

                    subprocess.run(["systemctl", "poweroff"], check=False, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass
            return self._send(self.checked(state))

        return self._send(b"not found", HTTPStatus.NOT_FOUND, kind="text/plain")

    # -- views -------------------------------------------------------------

    def welcome(self, state: SetupState, error: str = "") -> bytes:
        err = (
            f'<div class="err"><b>That key was not accepted.</b>{html.escape(error)}</div>'
            if error
            else ""
        )
        content = f"""
{err}
<h1>Welcome to <em>AmaaraOS</em></h1>
<p class="sub">Enter the license key from your supplier. Everything after this is
installed on this machine — no account, no cloud, nothing leaves the device.</p>

<form method="post" action="/license">
  <div class="panel">
    <div class="label">License key</div>
    <input type="text" name="key" placeholder="AMAARA-XXXXX-XXXXX-XXXXX-XXXXX"
           autocomplete="off" autocapitalize="characters" spellcheck="false" autofocus>
    <div class="hint">Five groups of five characters. Dashes and case do not matter.</div>
  </div>
  <div class="actions">
    <button class="primary" type="submit">Continue</button>
    <div class="spacer"></div>
  </div>
</form>

<form method="post" action="/poweroff" class="actions">
  <button class="link" type="submit">Supplier? This unit is checked — power off and ship it</button>
</form>
"""
        return ui.shell(content, title="Welcome", device=state.device_id, batch=state.batch,
                        checks=state.factory_checks, phase=state.phase, step="License")

    def software(self, state: SetupState, error: str = "") -> bytes:
        err = f'<div class="err"><b>Could not start.</b>{html.escape(error)}</div>' if error else ""
        chosen = set(catalog.defaults())
        cards = []
        for profile in catalog.selectable():
            selected = " sel" if profile.id in chosen and profile.available else ""
            off = "" if profile.available else " off"
            checked = "checked" if profile.id in chosen and profile.available else ""
            disabled = "" if profile.available else "disabled"
            if not profile.available:
                tag = '<span class="tag now">Later</span>'
            elif profile.packages:
                tag = '<span class="tag inc">Needs internet</span>'
            else:
                tag = '<span class="tag inc">Offline</span>'
            cards.append(f"""
<label class="choice{selected}{off}">
  <input type="checkbox" name="profiles" value="{profile.id}" {checked} {disabled}>
  <span><span class="t">{html.escape(profile.name)}</span>
    <span class="d">{html.escape(profile.summary) if profile.available else html.escape(profile.unavailable)}</span></span>
  {tag}
</label>""")
        always = ", ".join(p.name for p in catalog.CATALOG if p.always)
        content = f"""
{err}
<h1>Choose what this <em>does</em></h1>
<p class="sub">The agent runtime is always included — it is the part that makes this a
computer an agent can drive. Everything below runs locally.</p>

<div class="panel" style="padding:18px 20px">
  <div class="row" style="grid-template-columns:16px 1fr auto;border-bottom:0">
    <span class="pip ok"></span>
    <span><span class="t">Agent runtime</span>
      <span class="d">WSS engine · desktop session · semantic control. Always installed.</span></span>
    <span class="tag inc">Included</span>
  </div>
</div>

<form method="post" action="/software">
  {''.join(cards)}
  <div class="actions">
    <button class="primary" type="submit">Install</button>
    <span class="hint" style="margin:0">You can change this later in Settings.</span>
  </div>
</form>
<p class="hint" style="margin-top:26px">Included with every install: <b>{html.escape(always)}</b></p>
"""
        return ui.shell(content, title="Software", device=state.device_id, batch=state.batch,
                        checks=state.factory_checks, phase=state.phase, step="Software")

    def installing(self, state: SetupState) -> bytes:
        progress = self.app.progress
        result = progress["result"]
        rows, done, total = [], 0, 0
        if result:
            steps = result["steps"]
            total = len(steps)
            done = sum(1 for step in steps if step["ok"])
            for step in steps:
                pip = "ok" if step["ok"] else "bad"
                rows.append(f"""
  <div class="row">
    <span class="pip {pip}"></span>
    <span><span class="t">{html.escape(step["target"] or step["kind"])}</span>
      <span class="meta">{html.escape(step["profile"])} · {html.escape(step["detail"] or "done")}</span></span>
    <span class="at">{step["seconds"]}s</span>
  </div>""")
        elif state.profiles:
            rows.append('<div class="row"><span class="pip wait"></span>'
                        '<span><span class="t">Starting</span>'
                        '<span class="meta">working through your choices</span></span>'
                        '<span class="at"></span></div>')
        else:
            rows.append('<div class="row"><span class="pip wait"></span>'
                        '<span><span class="t">Queued</span></span><span class="at"></span></div>')
        pct = int(100 * done / total) if total else (100 if progress["complete"] else 8)
        after = (
            '<div class="actions"><a href="/ready"><button class="primary" type="button">'
            "Continue</button></a></div>"
            if progress["complete"]
            else ""
        )
        content = f"""
<h1>Setting up<em>One moment.</em></h1>
<p class="sub">This finishes on its own. Leave it alone and it will be ready shortly.</p>
<div class="bar"><i style="width:{pct}%"></i></div>
<div class="panel" style="padding:8px 22px">{''.join(rows)}</div>
{after}
"""
        return ui.shell(content, title="Setting up", device=state.device_id, batch=state.batch,
                        checks=state.factory_checks, phase=state.phase, step="Ready",
                        refresh=0 if progress["complete"] else 2)

    def ready(self, state: SetupState) -> bytes:
        installed = ", ".join(state.installed) or "agent runtime"
        content = f"""
<h1>This computer is <em>yours</em></h1>
<p class="sub">Setup is complete. When the screen comes back it is the AmaaraOS
desktop, and the agent is already running.</p>

<div class="panel">
  <div class="row" style="border-bottom:1px solid #f0e7de">
    <span class="pip ok"></span>
    <span><span class="t">License</span>
      <span class="meta">{html.escape(state.license_tier or "—")} · {html.escape(state.license_batch or "—")}</span></span>
    <span class="tag inc">Active</span>
  </div>
  <div class="row" style="border-bottom:1px solid #f0e7de">
    <span class="pip ok"></span>
    <span><span class="t">Installed</span>
      <span class="meta">{html.escape(installed)}</span></span>
    <span class="tag inc">Ready</span>
  </div>
  <div class="row" style="border-bottom:1px solid #f0e7de">
    <span class="pip ok"></span>
    <span><span class="t">Device</span>
      <span class="meta">{html.escape(state.device_id)}</span></span>
    <span class="tag inc">1.0.0</span>
  </div>
  <div class="row" style="border-bottom:0">
    <span class="pip wait"></span>
    <span><span class="t">Pair a phone or desktop</span>
      <span class="meta">run this on the box to mint a one-time code</span>
      <span class="badge" style="margin-top:8px">openpc-engine pair-code</span></span>
    <span class="tag inc">Next</span>
  </div>
</div>
"""
        return ui.shell(content, title="Ready", device=state.device_id, batch=state.batch,
                        checks=state.factory_checks, phase=state.phase, step="Ready")

    def checked(self, state: SetupState) -> bytes:
        content = f"""
<h1>Checked<em>Ready to ship.</em></h1>
<p class="sub">This unit booted, reached the license screen and recorded the check.
Power it off and pack it — the customer completes setup with their own key.</p>

<div class="panel">
  <div class="row" style="border-bottom:1px solid #f0e7de">
    <span class="pip ok"></span>
    <span><span class="t">Image</span><span class="meta">AmaaraOS 1.0.0 · Open-PC One</span></span>
    <span class="tag inc">Boots</span>
  </div>
  <div class="row" style="border-bottom:1px solid #f0e7de">
    <span class="pip ok"></span>
    <span><span class="t">Device</span><span class="meta">{html.escape(state.device_id)}</span></span>
    <span class="tag inc">Recorded</span>
  </div>
  <div class="row" style="border-bottom:0">
    <span class="pip wait"></span>
    <span><span class="t">License</span><span class="meta">not consumed — the customer enters it</span></span>
    <span class="tag now">Pending</span>
  </div>
</div>
<div class="actions"><button class="ghost" type="button" onclick="location.reload()">Refresh</button></div>
"""
        return ui.shell(content, title="Checked", device=state.device_id, batch=state.batch,
                        checks=state.factory_checks, phase=state.phase, step="License")


def make_server(app: SetupApp, host: str = "127.0.0.1", port: int = 8099) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def serve(app: SetupApp, host: str = "127.0.0.1", port: int = 8099) -> None:
    server = make_server(app, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
