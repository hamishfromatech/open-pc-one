# os/ - the appliance image

Everything here is config that ends up inside the image, plus the scripts the
image installs to `/opt/openpc/bin`.

```
os/
├── image/
│   ├── mkosi.conf              distribution, packages, output
│   ├── mkosi.repart/           esp + root-a + root-b (empty) + var
│   ├── mkosi.postinst          user, wheels, pinned artefacts, units, console off
│   └── artifacts/              pinned native binaries + SHA256SUMS (not in git)
├── overlay/                    copied verbatim over /
│   └── etc/
│       ├── nftables.conf
│       ├── sysctl.d/90-openpc-hardening.conf
│       ├── systemd/journald.conf.d/openpc.conf
│       ├── systemd/system/*.service, openpc.target, run-openpc-chrome.mount
│       ├── systemd/system.conf.d/openpc.conf       (watchdog + timeouts)
│       ├── systemd/zram-generator.conf
│       └── tmpfiles.d/openpc.conf
├── scripts/                    installed to /opt/openpc/bin
│   ├── openpc-session          Xvfb + XFCE + Chromium, fails loudly
│   ├── ota-update              write inactive slot, arm 3-try boot
│   ├── enroll-tpm2             PCR 0+7 + recovery key
│   ├── read-only-root          overlayfs prep + machine-id preservation
│   ├── model-config            licence gate + renders /etc/openpc from config/models.json
│   ├── openpc-llama            execs llama-server with the rendered argv
│   └── build-image             Linux-only mkosi wrapper (--target dev|pilot|retail)
└── tests/                      image config + model gate, tested like code
```

## Build (Linux host or CI only)

```bash
# 1. Put the pinned binaries in place, then pin their hashes.
#    Required: cua-driver, a-coder-cli, and the llama.cpp tarball named by
#    config/models.json -> llamaCpp.artifact (currently llama.cpp-lfm3.tar.gz).
ls os/image/artifacts/
./os/scripts/pin-artifacts          # writes SHA256SUMS (never lists itself)
./os/scripts/pin-artifacts --check  # verify without writing

# 2. Wheels, so the image build needs no network.
make wheels

# 3. Build. The target runs the model licence gate first:
#    retail fails closed while config/models.json says licence.status=unverified.
./os/scripts/build-image --target pilot     # dev | pilot | retail
```

`mkosi.postinst` aborts if `SHA256SUMS` is missing or does not match. That is
deliberate: an unattended appliance must never boot a binary nobody verified.

It also re-runs the licence gate against `os/image/build-target` (written by
`build-image`) and renders `/etc/openpc/{models.json,llama.args,model.json}` from
`config/models.json`. `llama.service` names no model: it runs
`/opt/openpc/bin/openpc-llama`, which reads `/etc/openpc/llama.args`. Swap models
by editing the manifest, not a unit.

## Boot target

`openpc.target` is the appliance. It pulls in the LED, provisioning, session,
llama, speech, cua-driver, engine and the Chrome tmpfs mount.

```bash
systemctl isolate openpc.target     # hard reset of the whole agent stack
systemctl list-dependencies openpc.target
```

## Verify the config without building

```bash
make test-os
# or, on the box:
systemd-analyze verify /etc/systemd/system/openpc-*.service /etc/systemd/system/llama.service
systemd-analyze critical-chain openpc-engine.service
nft -c -f /etc/nftables.conf
sysctl --system --dry-run
```

## What to check first on hardware

1. `systemd-analyze critical-chain openpc-engine.service` - the documented order,
   not the one you assumed.
2. `systemctl status openpc-session` - Xvfb ready before XFCE; Chromium window at
   1920x1080, not 1x1 (a missing WM does that).
3. `findmnt /run/openpc/chrome` - tmpfs, 768M, owned by `openpc`.
4. `touch /etc/probe` must fail (read-only root).
5. `bootctl list` - the running entry has no `+N` counter after a successful boot.
