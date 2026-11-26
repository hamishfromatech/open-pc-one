# Installing Open-PC One (pilot)

End-to-end path for a pilot user, from a downloaded image to a paired appliance.
It assumes no monitor and no keyboard on the box. Every command below is meant to
run on a workstation, not on the appliance; the appliance is provisioned over
Bluetooth and reached over WebSockets on the LAN.

Status: **pilot**. Flashing, first boot, BLE provisioning, and client pairing are
documented here; the hardware parts that could not be exercised on this bench
(radio, physical LED, NVMe write speed, TPM) are listed in
[BOOT.md section 6](BOOT.md#6-stubs-and-unexercised-paths).

## 1. Obtain the image

From a clean clone of this repository:

```bash
zstd -d openpc-one-image.zst -o openpc-one.raw
shasum -a 256 openpc-one-image.zst   # must equal the zst sha256 from CI
shasum -a 256 openpc-one.raw          # must equal the raw sha256 from CI
```

The image is the artifact `openpc-one-pilot-image` from the `image` workflow at
https://github.com/hamishfromatech/open-pc-one/actions/workflows/image.yml.
If you build instead, follow [BOOT.md section 2](BOOT.md#2-build-it-yourself-from-a-clean-clone)
and produce `openpc-one_0.1.0.raw`.

Keep the verified raw file. All recovery in this document starts from it.

## 2. Flash to a boot medium

Preferred media: a SATA/NVMe SSD in a USB enclosure for the always-on unit, or a
quality USB stick for a bench dry run. Avoid SD cards and unbranded sticks for
pilot hardware.

### 2.1 Identify the target disk very carefully

```bash
# Linux
lsblk -dpno NAME,SIZE,TYPE,MODEL
# macOS
diskutil list
```

The target must be an entire disk device, never a partition. Triple-check the
size and model. Writing to the wrong disk is not recoverable.

### 2.2 Flash with dd

```bash
# Linux (replace sdX with the whole disk device)
sudo dd if=openpc-one.raw of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

```bash
# macOS (replace diskN with the whole disk device)
diskutil unmountDisk /dev/diskN
sudo dd if=openpc-one.raw of=/dev/rdiskN bs=4m
sync
```

### 2.3 Alternative: balenaEtcher

Select `openpc-one.raw`, select the target disk, flash. Etcher verifies the write
by re-reading the image; wait for that step before removing the medium.

### 2.4 Verify the written medium

Re-read and compare hashes where the tools allow:

```bash
sudo cmp openpc-one.raw /dev/sdX 2>/dev/null || echo "size differs, see note"
```

`cmp` may stop at the shorter of the two; a larger target medium is expected to
differ only by trailing zeros. For a definitive check, hash the number of bytes
written and compare that to the raw sha256 over the same byte count.

Flash a second, independent medium at the same time using the same verified raw
file. That second medium is your recovery unit (see section 8).

## 3. First boot

Insert the medium, connect power, and do not attach a keyboard or monitor. The
image is built without a getty, without a console login, and without a default
password, so there is nothing a monitor would show you beyond the serial log.

- Connect an Ethernet cable for the first boot only if you want to skip
  Bluetooth provisioning. Ethernet is tried first and wins silently.
- On a medium with no network the provisioning service starts advertising.
- Expected wait before the appliance target is reached: about 60 seconds on TCG,
  much less with KVM or on real hardware.

There is no power button gate: once power is stable the boot proceeds on its own.

## 4. BLE provisioning (the no-keyboard, no-monitor path)

This is the only supported pairing for a machine without a cable. The mobile
client scans and writes credentials; the passphrase never appears on a command
line.

Sequence observed on the box:

1. NO_NETWORK. The default route is absent.
2. ADVERTISING. The radio advertises a name like `OpenPC-XXXX`. The LED is solid
   amber. The SSID, passphrase, and the phone public key are written by the
   phone.
3. RECEIVING_CREDENTIALS. Credentials are validated (SSID 32 bytes or fewer,
   passphrase 8-63 characters).
4. CONNECTING. NetworkManager joins the network. The passphrase is held in
   memory only.
5. CONNECTED. Advertising stops, `_openpc._tcp` is published on mDNS, and the
   engine becomes reachable at the assigned address.

A failed attempt returns to ADVERTISING, so a mistyped passphrase is retryable
without a factory reset. There is no other recovery on a keyboardless box.

Constraints this path enforces:

- Do not attach a keyboard to satisfy a prompt. The image has no console login by
  design.
- Do not attach a monitor to read a status screen. Discovery and status are over
  mDNS and WSS.
- The appliance advertises only while unprovisioned. Once CONNECTED it stops, so
  a phone that appears after provisioning must use the already-trusted
  credentials or be added later over the network.

## 5. Discover and verify the appliance

After provisioning the address is published as `openpc.local`:

```bash
# Find the published appliance
avahi-browse -rt _openpc._tcp
# or
ping openpc.local
```

The engine listens on WSS port 8095 with a self-signed certificate. Verify the
TLS fingerprint returned by the device-info characteristic during provisioning
against the engine fingerprint before any client trusts the connection:

```bash
openpc-engine fingerprint
```

On the appliance this is installed at `/opt/openpc/venv/bin/openpc-engine`. While
pilot access to a shell is still documented as a recovery action (section 7),
re-run it from that path.

Confirm the units came up cleanly. With shell access:

```bash
systemctl is-active openpc-led openpc-session llama cua-driver openpc-engine
systemctl --failed
```

All of `openpc-led`, `openpc-session`, `llama`, `cua-driver`, and
`openpc-engine` must be active. `systemctl --failed` must be empty.

Confirm the local model and the Chrome run directory:

```bash
find /var/lib/openpc/models -name '*.gguf'
findmnt /run/openpc/chrome
```

## 6. Pair clients

### 6.1 Mobile client

The mobile client paired during BLE provisioning: the phone public key was
registered before the WiFi join, so a join that succeeds never strands an
untrusted phone. It stores the certificate fingerprint and uses it to validate
`openpc.local` (and the assigned address) on every connection.

### 6.2 Desktop client

The desktop client pairs with a one-time code minted on the appliance:

```bash
# Minted on the appliance; code expires after ten minutes and burns on use
/opt/openpc/venv/bin/openpc-engine --state-dir /var/lib/openpc pair-code
```

The command prints JSON with `code`, `expiresAt`, and `ttl`. The code differs
every mint. Enter it in the desktop client before expiry. Single use: once
redeemed the code is deleted from disk.

Because pilot hardware has no console login, see section 7 to obtain a shell
long enough to run this command. Treat that shell as temporary and remove the
debug key afterwards.

## 7. Recovery shell and reinstall (no-keyboard recovery)

There is no baked SSH key and no default password. To get a shell for debugging
or for minting a desktop pairing code:

1. Power off and remove the boot medium.
2. Loop-mount its root partition (`openpc-root-a`) on a workstation.
3. Add a public key to `/home/openpc/.ssh/authorized_keys` in that mount.
4. Boot that copy and connect with the matching key over SSH.
5. Do the work.
6. Remove the debug key and power down.

Keep this as a recovery technique only. It is the only way to reach a shell on a
keyboardless pilot unit, and it must not be taught to end users as the normal
path.

Reinstall or factory recovery:

1. Flash the verified raw image to a fresh or the original medium using section 2.
2. On the original medium, reflash overwrites both root slots and the ESP; the
   data on `openpc-var` is destroyed as part of a clean flash. Back up anything on
   that partition first if you can reach it.
3. If the primary unit will not boot at all, swap in the second independent
   medium from section 8 and boot that. The paired devices and WiFi credentials
   live in the appliance state, so any pairing already stored on the failed medium
   must be repeated on the swapped-in copy.
4. Use the second medium (or a workstation flash) to restore the primary medium,
   then return to normal operation.

## 8. Second independent boot medium (reinstall and recovery)

Provision the media as a pair, not as copies of convenience:

1. Flash the same verified raw image to both media in one session.
2. Record which medium is primary and which is recovery. Label them physically.
3. Boot the recovery medium in QEMU or on spare hardware and confirm the
   smoke checks in section 5. It must pass on its own, not only as a clone of a
   working box.
4. Store it somewhere it cannot be confused with the primary.
5. When the primary medium fails, swap and test end to end rather than assuming
   the recovery medium still boots. Re-flash the recovery medium from the
   verified raw file after any use.

Failure modes and documented recovery:

- Bad flash. The image hash check in section 1 catches this before flashing.
  After flashing, a non-booting medium is replaced, not repaired.
- Interrupted boot. The watchdog is armed at 30 seconds and a clean failure
  reboots. A half-written root slot is not repaired in place; reflash it.
- Bad or changed WiFi credentials. The box returns to advertising. No rebuild
  needed; re-run provisioning from the phone.
- A unit that restarts or fails. Run `systemctl --failed` and the unit logs,
  then reflash if the fault is in the image.
- A lost pairing code or expired desktop code. Mint a new one with section 6.2.
- A medium that no longer boots after updates later. Revert to the recovery
  medium and reflash from the verified raw image.

## 9. Installation checklist

- [ ] Clean clone and downloaded pilot image acquired.
- [ ] Raw image sha256 matches the CI run. Zstd image sha256 matches.
- [ ] Boot medium flashed with verified raw image.
- [ ] Second independent boot medium flashed and labelled.
- [ ] Appliance powered with no keyboard and no monitor attached.
- [ ] Boot reached `openpc.target` within expected time.
- [ ] WiFi provisioned over BLE, or Ethernet connected on first boot.
- [ ] Appliance discovered as `openpc.local` and WSS port 8095 reachable.
- [ ] TLS fingerprint from device-info matches the engine fingerprint.
- [ ] `openpc-led`, `openpc-session`, `llama`, `cua-driver`, and `openpc-engine`
      all active; `systemctl --failed` empty.
- [ ] At least one `.gguf` present under `/var/lib/openpc/models`.
- [ ] `/run/openpc/chrome` is a tmpfs mount.
- [ ] Mobile client registered and connected over WSS.
- [ ] Desktop pairing code minted, redeemed, and burned.
- [ ] Recovery shell procedure verified at least once on spare media.
- [ ] Debug SSH key removed and recovery medium stored separately.

Evidence to record: the exact image artifact run, both raw and zstd hashes,
the device model of the boot medium, the provisioned WiFi join result, and the
client connection timestamps. Hardware that could not be exercised is called out
in [BOOT.md section 6](BOOT.md#6-stubs-and-unexercised-paths) and must not be
claimed as tested.
