# Contract

| File | Describes | Generated clients |
|------|-----------|-------------------|
| `engine.asyncapi.yaml` | the WSS :8095 frame protocol (AsyncAPI 3.0) | chat/session transport |
| `openapi.json` | the Open-PC agent server REST surface on :8090 (OpenAPI 3.1.0) | REST + MJPEG control |
| `openapi.sha256` | provenance hash of the vendored `openapi.json` | - |

## The REST contract is vendored, not re-invented

`openapi.json` is copied **verbatim** from the existing Open-PC dashboard
(`dashboard/openapi.json`: Open-PC Agent API, 34 paths, 18 schemas, OpenAPI
3.1.0). `openapi.sha256` pins the exact bytes this repository carries, and
`make test-os` fails if the two ever disagree. That file is already the
generated source of truth for the Tauri and Flutter clients, so it is extended
here rather than replaced with a second REST contract.

- Do not hand-edit `contract/openapi.json`. The upstream spec lives in the
  Open-PC dashboard repository (`dashboard/openapi.json`). That is a **separate
  checkout and is not part of this repository**, so point the copy at your own
  copy of it:

```bash
DASHBOARD=/path/to/open-pc          # the dashboard checkout, not this repo
cp "$DASHBOARD/dashboard/openapi.json" contract/openapi.json
shasum -a 256 contract/openapi.json | awk '{print $1"  openapi.json"}' > contract/openapi.sha256
make test-os        # fails if the committed file and its hash disagree
```

- Generate the clients from this file, never from memory:

```bash
# TypeScript (same generator the dashboard already uses)
npx openapi-ts -i contract/openapi.json -o ../desktop-client/src/api

# Dart
openapi-generator-cli generate -i contract/openapi.json -g dart \
  -o ../mobile-client/lib/api
```

The split stays: **WSS :8095 is for chat and sessions** (AsyncAPI), **REST :8090
is for MJPEG and desktop control** (OpenAPI). Two endpoints, two protocols, one
job each.

## Why AsyncAPI, not OpenAPI

The brief calls for "one OpenAPI contract, generated TS + Dart clients". That is
the right instinct - one contract, generated clients - but OpenAPI describes HTTP
request/response endpoints, not a WebSocket frame protocol. A socket handshake,
server-pushed events, and a monotonic cursor have no honest OpenAPI expression;
forcing them in produces a spec nobody can generate from.

So this repository ships the **socket** contract as AsyncAPI, and leaves OpenAPI
for the REST surface it actually fits:

- The existing Open-PC dashboard's `dashboard/openapi.json` is vendored here as
  `contract/openapi.json` (hash in `openapi.sha256`). That is the REST contract
  to extend, and where `openapi-generator` for Dart belongs.
- Generating Dart from AsyncAPI is supported by the community generators; if the
  tooling proves thin, the fallback is a hand-written 150-line client - smaller
  than the generator plumbing.

If a single OpenAPI document is a hard requirement, the clean way is to add a
thin HTTP facade in front of the engine (`POST /sessions/{id}/prompt`,
`GET /sessions/{id}/messages`, SSE for events) and keep the socket as the
low-latency path.

## Validating

```bash
npx @asyncapi/cli validate contract/engine.asyncapi.yaml
```

## Generating clients

```bash
# TypeScript
npx @asyncapi/cli generate fromTemplate contract/engine.asyncapi.yaml \
  @asyncapi/typescript-client-template -o ../desktop-client/src/contract

# Dart (community template; check output before trusting it)
npx @asyncapi/cli generate fromTemplate contract/engine.asyncapi.yaml \
  @asyncapi/dart-client-template -o ../mobile-client/lib/contract
```

Whatever is generated, keep these three rules in the transport layer, because no
generator encodes them:

1. Pin the TLS certificate fingerprint learned over BLE.
2. Drop frames at or below the `host_hello.boundary`.
3. Treat `success: true` as "accepted", never as "finished".
