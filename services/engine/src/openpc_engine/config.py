"""Engine configuration.

Every value has an environment-variable default so systemd units stay thin, and
a CLI flag override for development. Nothing here reads secrets at import time.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STATE_DIR = Path("/var/lib/openpc")
DEV_STATE_DIR = Path(os.path.expanduser("~/.openpc"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_state_dir() -> Path:
    env = os.environ.get("OPENPC_STATE_DIR")
    if env:
        return Path(env)
    if os.geteuid() == 0:
        return DEFAULT_STATE_DIR
    return DEV_STATE_DIR


@dataclass(slots=True)
class EngineConfig:
    """Runtime configuration for ``openpc-engine``."""

    # --- network -----------------------------------------------------------
    bind_host: str = "0.0.0.0"
    bind_port: int = 8095
    #: Path to the TLS certificate chain. Generated on first boot if missing.
    tls_cert: Path = field(default_factory=lambda: _default_state_dir() / "tls" / "engine.crt")
    tls_key: Path = field(default_factory=lambda: _default_state_dir() / "tls" / "engine.key")

    # --- state -------------------------------------------------------------
    state_dir: Path = field(default_factory=_default_state_dir)
    #: Root for per-session a-coder-cli session directories (JSONL source of truth).
    session_root: Path = field(default_factory=lambda: _default_state_dir() / "sessions")
    #: Registered client public keys.
    devices_file: Path = field(default_factory=lambda: _default_state_dir() / "devices.json")
    #: One-time desktop pairing codes (root-only; deleted when redeemed).
    pairing_file: Path = field(default_factory=lambda: _default_state_dir() / "pairing.json")
    #: Runtime flag directory shared with the LED service (/run is a tmpfs, so
    #: flags vanish on reboot by design).
    run_dir: Path = field(default_factory=lambda: Path("/run/openpc") if os.geteuid() == 0 else _default_state_dir() / "run")

    # --- engine child process ---------------------------------------------
    #: Command (argv prefix) that starts the RPC child. Defaults to the
    #: appliance's installed ``a-coder-cli``; a list so development can point at
    #: ``("node", "/path/to/dist/cli.js")`` or a test double.
    engine_command: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            shlex.split(os.environ.get("OPENPC_ENGINE_COMMAND", os.environ.get("OPENPC_ENGINE_BIN", "a-coder-cli")))
        )
    )
    engine_args: tuple[str, ...] = ()
    #: Working directory the agent sees. The agent drives this desktop; on the
    #: appliance this is the XFCE/Chrome session user's home directory.
    agent_cwd: Path = field(default_factory=lambda: Path("/home/openpc") if Path("/home/openpc").exists() else Path.home())
    engine_start_timeout: float = 60.0
    #: Restart budget for a crashed child. After this many consecutive failures
    #: inside ``restart_window`` the session is left down and reported.
    restart_max: int = 3
    restart_window: float = 120.0

    # --- session policy ----------------------------------------------------
    #: ENGINE.md §5: compaction is the OS's job. A 24/7 session must not stall
    #: at 3 a.m. because the context filled up; the bridge turns auto-compaction
    #: on for every child it spawns. Re-applied after every respawn, because
    #: the setting lives in the child process, not on disk.
    auto_compaction: bool = field(default_factory=lambda: _env_bool("OPENPC_AUTO_COMPACTION", True))
    #: ENGINE.md §7: retry transient provider errors with backoff. The hard stop
    #: after repeated failures is the supervisor's restart budget, not this.
    auto_retry: bool = field(default_factory=lambda: _env_bool("OPENPC_AUTO_RETRY", True))
    idle_timeout: float = float(os.environ.get("OPENPC_IDLE_TIMEOUT", "1800"))
    idle_sweep_interval: float = 30.0
    #: Messages sent to a late joiner. Older history is paged on demand.
    replay_limit: int = 200
    #: How long a bootstrap policy command may take before it is abandoned.
    policy_timeout: float = 15.0

    # --- local model -------------------------------------------------------
    #: The model manifest is the only place a model is named. ``None`` means
    #: "look in the standard places" (env, /etc/openpc/models.json, the repo).
    model_manifest: Path | None = None

    # --- backpressure ------------------------------------------------------
    client_queue_frames: int = 512
    #: A client that has not consumed this many frames is dropped for resync.
    max_pending_commands: int = 64

    # --- auth --------------------------------------------------------------
    auth_challenge_ttl: float = 30.0
    pairing_code_ttl: float = 600.0

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.session_root, self.tls_cert.parent):
            Path(path).mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)

    def session_dir(self, session_id: str) -> Path:
        """Per-session CLI session directory.

        Each supervised engine process gets its own ``--session-dir`` so the
        appliance can list and resume sessions without scanning a shared tree.
        """
        return self.session_root / session_id
