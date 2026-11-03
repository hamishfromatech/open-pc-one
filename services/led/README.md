# openpc-led

The appliance's only local UI: one systemd service, four unambiguous states.

```bash
openpc-led --led-name openpc:status --run-dir /run/openpc
openpc-led --mock                 # no sysfs; useful for a dry run
```

| State | Look |
|-------|------|
| `NO_NETWORK` | amber solid |
| `IDLE` | blue slow breathe (2.4 s) |
| `WORKING` | cyan fast pulse (4 Hz) |
| `ATTENTION` | red double-blink |

Priority `ATTENTION > NO_NETWORK > WORKING > IDLE`. Patterns are pure functions
of a monotonic clock, so they are deterministic and testable.

Inputs are files, not sockets: `/run/openpc/attention` (any service),
`/run/openpc/engine-busy` (the engine), and the default route.

## Tests

```bash
../../.venv/bin/python -m pytest tests      # 15 tests
```
