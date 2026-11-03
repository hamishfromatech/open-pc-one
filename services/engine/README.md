# openpc-engine

Bridges `a-coder-cli --mode rpc` (strict JSONL on stdio) to a WSS edge on **:8095**
for the desktop and mobile clients.

```bash
pip install -e .            # or use the repo's .venv
openpc-engine doctor        # handshake with the real a-coder-cli
openpc-engine serve --bind 0.0.0.0:8095 --agent-cwd /home/openpc
openpc-engine pair-code     # one-time code for a desktop client
openpc-engine devices list
openpc-engine fingerprint   # the TLS pin clients must trust
```

## Modules

| Module | Responsibility |
|--------|----------------|
| `jsonl.py` | LF-only byte splitter (16 MiB cap), ASCII-escaped encoder |
| `rpc_process.py` | one supervised child: spawn, read, restart, command/response |
| `hub.py` | `SessionBridge` fan-out + cursors + backpressure; `SessionManager` lifetime + idle reap |
| `ws_server.py` | TLS, auth handshake, subscribe/replay, pass-through routing |
| `auth.py` | appliance cert + pin, device registry, pairing codes, Ed25519 nonces |
| `flags.py` | `/run/openpc/engine-busy` for the LED |
| `cli.py` | `serve`, `doctor`, `fingerprint`, `pair-code`, `devices` |

## Guarantees worth knowing

- **One reader per child.** RPC is single-consumer; nothing else may read the
  child's stdout.
- **`success: true` means accepted.** Completion arrives as events.
- **`hostSeq` is the client cursor**, not the engine's `seq` (which restarts on
  respawn).
- **Backpressure drops and resyncs**, never reorders.
- **The JSONL on disk is the source of truth**; reaping a session loses nothing.

The failure modes that matter are exercised by `tests/test_hub.py`,
`tests/test_rpc_process.py` and `tests/test_ws_server.py`.

## Tests

```bash
../../.venv/bin/python -m pytest tests      # 57 tests, no LLM, no network
```

`tests/fake_engine.py` is a protocol-faithful child with knobs for the failure
modes that matter: `--crash-after N`, `--crash-immediately`, `--burst N`,
`--no-handshake`, `--stall`.
