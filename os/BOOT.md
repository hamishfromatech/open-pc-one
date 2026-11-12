# Booting Open-PC One

This is the pilot appliance image: Debian trixie, systemd-boot, a read-only root
on `openpc-root-a`, an empty `openpc-root-b` for OTA, and a separate
`openpc-var` partition. `openpc.target` is `default.target`. The box has no
monitor, no getty and no console login.

Status: **pilot**. The image below was verified by a cold boot in QEMU. There is
no reference Open-PC unit on this bench, so Bluetooth, WiFi, the physical LED
ring, TPM enrolment and A/B rollback are **unexercised** (see
[Stubs](#6-stubs-and-unexercised-paths)).

## 1. Get a built image

The image is built by the `image` workflow on GitHub Actions (x86_64 Linux):

<https://github.com/hamishfromatech/open-pc-one/actions/workflows/image.yml>

1. Open the latest successful run.
2. Download the `openpc-one-pilot-image` artifact. It contains
   `openpc-one-image.zst` (about 1.7 GiB; the raw disk is 21 GiB).
3. Read the hashes from the same run: the "Upload image" step log prints
   `raw sha256:` and `zst sha256:`, and the run summary lists both.
4. Decompress and verify:

```bash
zstd -d openpc-one-image.zst -o openpc-one.raw
shasum -a 256 openpc-one-image.zst   # must equal zst sha256
shasum -a 256 openpc-one.raw        # must equal raw sha256
```

The uploaded image is **probe-free**. CI injects the boot probe into the root
slot, boots that copy, checks the appliance, then removes the probe and verifies
its absence by mounting the image again before upload. A shipped image contains
no `openpc-smoke` unit, no `OPENPC_LED_MOCK` and no automatic poweroff.

## 2. Build it yourself from a clean clone

`os/image/artifacts` is generated, not committed. Nothing that needs it will run
until the pinned binaries and their `SHA256SUMS` are in place, so the order
matters:

1. **Native artefacts.** Either download the `openpc-artifacts` artifact from
   the latest `image` run into `os/image/artifacts/`, or reproduce the
   `artifacts` job (a-coder-cli release, cua-driver release, llama.cpp built
   from the pinned commit). Then:

```bash
ls os/image/artifacts
./os/scripts/pin-artifacts          # writes SHA256SUMS (never lists itself)
./os/scripts/pin-artifacts --check  # fail closed on a mismatch
```

2. **Weights.** `./os/scripts/fetch-model` downloads the GGUF named by
   `config/models.json` into `os/image/models/` (gitignored). A pilot or retail
   build aborts without it.
3. **Wheels.** `make wheels` builds the three service wheels and downloads the
   pinned runtime wheels for Python 3.13 / x86_64 into `os/image/wheels/`.
4. **Build** on Linux only:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
make wheels
sudo ./os/scripts/build-image --target pilot
```

`build-image` re-runs the model licence gate, builds the wheels, verifies
`SHA256SUMS`, then runs mkosi. A missing or mismatched artefact aborts the
build; that is deliberate. The build needs mkosi 25 (CI installs the commit
behind v25.3); qemu and OVMF are only needed to boot the result. The output is
`openpc-one_0.1.0.raw` in mkosi's output directory.

Reproducibility caveat: the native binaries, the llama.cpp commit and the mkosi
commit are pinned, but packages still come from the live Debian trixie archive.
Two builds on different days can differ in package versions. Pinning trixie to a
`snapshot.debian.org` timestamp is not done yet.

## 3. Boot it in QEMU

The kernel command line already contains `console=ttyS0`, so serial is how you
see anything. There is no graphical console and no login prompt.

Linux (KVM when available, TCG otherwise):

```bash
cp /usr/share/OVMF/OVMF_VARS_4M.fd /tmp/OVMF_VARS.fd
qemu-system-x86_64 \
  -machine q35,accel=kvm:tcg -cpu max -m 6144 -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=/tmp/OVMF_VARS.fd \
  -drive file=openpc-one.raw,format=raw,if=virtio,cache=unsafe \
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
  -display none -serial stdio -monitor none
```

Add `-snapshot` if you want to boot repeatedly without writing to the raw file.
Quit QEMU with `Ctrl-A X`.

macOS works with Homebrew QEMU, but the firmware paths differ: use
`$(brew --prefix qemu)/share/qemu/edk2-x86_64-code.fd` and a writable copy of
`edk2-i386-vars.fd`. Expect TCG speed. That path is not what CI verifies.

## 4. Expected boot output

Under TCG the appliance target is reached in about 60 seconds; with KVM it is
much faster. The line that marks a successful boot is:

```
[  OK  ] Reached target openpc.target - Open-PC One appliance target.
```

CI then has the injected probe print this block. The shipped image does not, but
the block is the exact pass condition for the image artifact:

```
SMOKE_BEGIN
SMOKE_U_cua-driver:state=active
SMOKE_U_cua-driver:restarts=0
SMOKE_U_cua-driver:failed=no
SMOKE_U_llama:state=active
SMOKE_U_llama:restarts=0
SMOKE_U_llama:failed=no
SMOKE_U_openpc-bless:state=active
SMOKE_U_openpc-bless:restarts=0
SMOKE_U_openpc-bless:failed=no
SMOKE_U_openpc-engine:state=active
SMOKE_U_openpc-engine:restarts=0
SMOKE_U_openpc-engine:failed=no
SMOKE_U_openpc-led:state=active
SMOKE_U_openpc-led:restarts=0
SMOKE_U_openpc-led:failed=no
SMOKE_U_openpc-provision:state=active
SMOKE_U_openpc-provision:restarts=0
SMOKE_U_openpc-provision:failed=no
SMOKE_U_openpc-session:state=active
SMOKE_U_openpc-session:restarts=0
SMOKE_U_openpc-session:failed=no
SMOKE_PORT_8095=1
SMOKE_PORT_8080=1
SMOKE_SOCK_CUA=1
SMOKE_VAR_MOUNTED=1
SMOKE_MODELS=1
SMOKE_FU_BSMOKE_FU_E
SMOKE_END
```

What CI asserts:

- six units are active with zero restarts and are not failed: `openpc-led`,
  `openpc-session`, `llama`, `cua-driver`, `openpc-bless`, `openpc-engine`;
- `openpc-provision` is sampled too, but it legitimately exits 0 on a wired
  boot, so it is only required to be not-failed with zero restarts;
- `:8095` (engine WSS) and `:8080` (llama.cpp, loopback) have listeners;
- `/run/openpc/cua-driver.sock` exists;
- `/var` is mounted from `openpc-var` and contains at least one `.gguf`;
- `systemctl --failed` is empty.

## 5. Talking to it

The engine listens on **WSS** on `:8095` with a self-signed appliance
certificate. It is not an HTTP endpoint: an HTTP probe gets a TLS error, not a
health check. Use a WebSocket client that can skip certificate verification:

```bash
openssl s_client -connect <vm-ip>:8095 -servername openpc.local </dev/null
websocat --insecure wss://<vm-ip>:8095/v1/rpc
```

Pairing and the frame sequence are in `contract/engine.asyncapi.yaml`. llama.cpp
binds `127.0.0.1:8080` inside the guest, so it is not reachable from the host.
cua-driver is a unix socket (`/run/openpc/cua-driver.sock`), not a TCP port.

There is no console login and no default password. SSH is key-only and no key is
baked in. To get a shell for debugging, loop-mount `openpc-root-a`, add a public
key to `/home/openpc/.ssh/authorized_keys`, and boot that copy; use the serial
console only to read the log.

## 6. Stubs and unexercised paths

- **Bluetooth / BLE WiFi provisioning.** QEMU has no radio. The unit starts,
  BlueZ is absent, and it logs `Bluetooth provisioning unavailable ... relying
  on the wired network`. The GATT and NetworkManager paths are not verified
  against hardware.
- **LED ring.** There is no real `/sys/class/leds` ring here. CI runs the LED
  service with `OPENPC_LED_MOCK=1` in its own probe copy only; the shipped image
  has no mock.
- **Speech.** `openpc-speech.service` is not enabled; its container image is not
  built.
- **A/B OTA.** `root-a`, `root-b` and `var` exist, but no boot-counted loader
  entry is installed, so `systemd-bless-boot` reports
  `Not booted with boot counting in effect` and `ota-update` / rollback is
  unexercised.
- **TPM2 and secure boot.** The QEMU line above has no vTPM and the image is
  unsigned, so `enroll-tpm2` is unexercised.
- **Retail.** `config/models.json` still marks the model licence `unverified`, so
  only `--target dev` and `--target pilot` builds are expected to work.

Harmless messages on the pilot boot: Chromium's GPU process exits under TCG
(software rendering), and Xvfb logs the usual X11 socket noise. Neither affects
the appliance units.
