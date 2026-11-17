# amaara-update

The signed update client for AmaaraOS. It refuses more than it accepts:

| Condition | Result |
|---|---|
| no `FEED_URL` configured | refuse, download nothing |
| no `/etc/amaara/update.pub` | refuse — an update client without a key is a remote shell |
| signature does not verify | refuse |
| manifest is for another channel | refuse |
| download does not hash to the signed SHA-256 | delete it, refuse |
| shorter than the signed size | delete it, refuse |
| nothing newer | succeed, do nothing |

Only then does it hand the file to `ota-update`, which writes the **inactive** slot
and arms systemd-boot's three-try counter; a slot that fails to reach
`openpc.target` three times is dropped and the previous one boots unchanged.

## Signing host

```bash
amaara-update keygen --out keys/                     # once
amaara-update sign --private-key keys/update.private.pem --manifest body.json > manifest.json
```

`os/image/amaara/update.pub` is a build input: put the public half there and the
image bakes it in. It is not in the repository.

## Appliance

```bash
amaara-update status     # version + last check
amaara-update check      # is the feed offering something newer?
amaara-update apply --dry-run
amaara-update apply
```

`amaara-update.timer` runs the check daily with jitter. Whether it also *applies*
is `AUTO_APPLY` in `/etc/amaara/update.conf`, default `no`.
