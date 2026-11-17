# AmaaraOS Companion

The window onto an Open-PC One: what the box is doing, and a chat with the agent
running on it.

The customer's own computer is where the big model runs. This app is the part that
stays in touch with the machine that is awake at 3am.

```bash
pip install -e .
amaara-companion --host openpc.local:8095 --pair AMAARA-XXXXX-XXXXX-XXXXX-XXXXX
# then open http://127.0.0.1:8123
```

First run needs a pairing code, minted on the box:

```bash
openpc-engine pair-code          # on the appliance
```

After that the companion's Ed25519 key is registered and `--pair` is not needed.

## Why a local page instead of a native window (yet)

The box is self-signed and pins its certificate, and browsers cannot pin — so the
process holds the socket and the page talks plain HTTP to localhost. The reusable
part is `client.py` plus the frame protocol: wrapping this in Tauri, or porting the
client to Flutter for mobile, does not change one message on the wire.

## Frames used

| Frame | Direction | Purpose |
|---|---|---|
| `pair` / `auth` | → | register the device, then sign a nonce |
| `subscribe` | → | attach to a session, receive `host_hello` + live frames |
| `prompt` (`message`) | → | start a turn |
| `abort` | → | stop the current turn |
| `host_stats` | → | ask the box for CPU/memory/disk/temperature/units |
| `host_stats`, `message_update`, `message_end`, `host_error` | ← | what the window draws |

`host_stats` is answered on demand rather than pushed: a box reporting to nobody is
exactly the idle work an always-on appliance should not do.
