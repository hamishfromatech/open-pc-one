# Open-PC One

A headless, always-on appliance that runs an AI agent 24/7 with **no monitor
attached**. This repository is the runtime it ships: the RPC-to-WebSocket
bridge, BLE provisioning, the status LED, and the Linux image configuration.

## Prerequisites

- **Docker** (recommended, nothing else needed), or
- **Python 3.11+** and **make** for the native path.
- The tests need **no Bluetooth, no WiFi, no root and no appliance hardware**.
  Provisioning uses a simulated transport and the LED uses a mock driver.

## Run it

```bash
git clone https://github.com/hamishfromatech/open-pc-one.git
cd open-pc-one

make docker-test   # full suite inside the dev container
make docker-run    # engine :8095 (fake child) + MJPEG stand-in :8090
```

Or natively:

```bash
make venv
make test
make run-fake      # WSS bridge on wss://127.0.0.1:8095
```

`make run-fake` starts one supervised fake engine child. It prints the TLS
certificate pin at start-up; the certificate is self-signed, so pin the SHA-256
rather than trusting a public CA. `make docker-run` additionally serves a
generated MJPEG stream on `http://localhost:8090` (`/healthz`, `/v1/mjpeg`) as a
stand-in for the real desktop.

### What to expect on `wss://localhost:8095`

The socket speaks the frame protocol in
[`contract/engine.asyncapi.yaml`](contract/engine.asyncapi.yaml):

1. Server sends `host_challenge` with a single-use nonce.
2. Client replies with `auth` (an Ed25519 signature over the nonce) and then
   `subscribe {sessionId}`.
3. Server replays `host_hello {state, messages, boundary}`, then live frames:
   `agent_start`, `message_update`, `message_end`, `agent_end`.
4. `prompt {id, message}` starts a turn; `success: true` on the response means
   **accepted**, not finished. Completion arrives as `agent_end`.

To mint a pairing code for a client:

```bash
PYTHONPATH=services/engine/src ./.venv/bin/python -m openpc_engine \
  --state-dir ./.devstate pair-code
```

## Ports

| Port | Service |
|---|---|
| 8095 | `openpc-engine` — WSS bridge to the agent |
| 8090 | Open-PC agent server — MJPEG stream and REST |
| 8098 | `cua-driver` — semantic desktop control |
| 8096 | speech service — ASR/TTS, loopback only |

## Layout

```
services/engine/         openpc-engine  - RPC <-> WSS bridge, fan-out, replay, auth
services/provisioning/   openpc-provision - BLE GATT -> NetworkManager -> mDNS
services/led/            openpc-led     - deterministic status patterns, one service
services/speech/         ASR + TTS service interface
os/                      image config, systemd units, hardening, A/B OTA
config/models.json       model manifest: repo, quant, context, licence, llama.cpp args
contract/                AsyncAPI (WSS :8095) + vendored OpenAPI (REST :8090)
dev/                     development-only helpers (MJPEG stand-in)
scripts/                 repository checks
```

## Licence

MIT. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE), which also records the
third-party components and the model-weights licensing (the model weights are
**not** covered by this repository's MIT licence).
