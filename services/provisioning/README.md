# openpc-provisioning

First-boot WiFi provisioning over BLE, then mDNS. No keyboard, no monitor.

```bash
openpc-provision --interface wlan0          # on the appliance
openpc-provision --simulate \
  --simulate-credentials "HomeNet:password123"   # on a laptop, no radio
```

| Module | Responsibility |
|--------|----------------|
| `state.py` | the six states, legal transitions, credential validation |
| `ble.py` | GATT UUIDs, `OpenPC-XXXX` naming, payload parsing, simulated transport |
| `wifi.py` | NetworkManager keyfile + `nmcli` join (no passphrase on argv) |
| `mdns.py` | `_openpc._tcp` Avahi service file |
| `service.py` | the flow; the only place that knows the order of operations |
| `bluez_dbus.py` | real BlueZ GATT peripheral (Linux only, needs `dbus-next`) |
| `cli.py` | entry point |

## Invariants

- The passphrase never appears in a command line, a log line, or a `repr`.
- A failed join returns to advertising: there is no keyboard, so a dead end would
  mean a factory reset.
- The phone's Ed25519 public key is registered in the same write that carries the
  passphrase - one trust ceremony.
- The TLS pin is delivered here, out of band from the WLAN; that is what makes a
  self-signed certificate safe.

## Tests

```bash
../../.venv/bin/python -m pytest tests      # 45 tests, no Bluetooth stack
```
