# amaara-setup

First-boot setup for AmaaraOS: license activation, the install catalogue, and the
UI the box shows before it has an owner.

## The two-phase handoff

The same welcome screen serves both parties, because the OEM's job is only to
prove the image boots:

1. **OEM / supplier** boots the unit, reaches *Welcome to AmaaraOS*, and presses
   *"this unit is checked — power off and ship it"*. That records
   `factory_checks += 1` and powers the box off. No key, no state consumed.
2. **Customer** boots the same unit, types their license key, picks what to
   install, and the box finishes on its own.

## License keys

Keys verify **offline**: the key is a random 25-character Crockford-base32 string,
and the device holds a manifest (the SHA-256 of every key in the batch) signed
with A-Tech's Ed25519 key. The appliance images bake in the public half only.

```bash
# our signing host
amaara-setup keygen --out keys/
amaara-setup generate-license --private-key keys/license.private.pem \
    --batch BATCH-001 --tier standard --count 50 --out out/
# the appliance
amaara-setup verify --key AMAARA-… --manifest /etc/amaara/license.json \
    --public-key /etc/amaara/license.pub
```

## Commands

| command | what it does |
|---|---|
| `amaara-setup serve` | the first-boot UI on `127.0.0.1:8099` |
| `amaara-setup status` | the setup state as JSON |
| `amaara-setup catalog` | the installable profiles as JSON |
| `amaara-setup apply` | apply profiles without the UI (dry run with `--dry-run`) |
| `amaara-setup complete` | mark setup done (development) |
