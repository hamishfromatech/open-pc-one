# Speech service (interface only)

The appliance needs ears and a mouth:

- **ASR** - streaming Zipformer (Apache-2.0), containerised.
- **TTS** - `gradium/phonon-31m-en` (Apache-2.0, 64.5 MB q8 GGUF, 80 ms frames
  at 24 kHz).

**Phonon is TTS, not ASR.** The `40m` Phonon models are CC BY-NC 4.0; only the
English 31m model is Apache-2.0, so commercial TTS is English-only.

## Contract the engine expects

`openpc-speech.service` starts the container on `127.0.0.1:8096` (host network,
read-only rootfs, `--cap-drop=ALL`). The image is `openpc/speech:0.1.0` with
models mounted read-only from `/var/lib/openpc/models/speech`.

```
POST /v1/asr        audio/pcm;rate=16000 chunked in -> text stream out
POST /v1/tts        {"text": "...", "voice": "en"}     -> audio/pcm 24 kHz
GET  /v1/health     -> {"asr": true, "tts": true, "tts_languages": ["en"]}
```

This is the interface only. Nothing in the engine calls this service yet; the
service speaks text `prompt` over WSS. The unit is ordered before `cua-driver`
so wiring speech in later does not change boot order.

## Licensing gate

Before the first retail build, confirm the exact ASR checkpoint's terms - several
community Zipformer conversions are not Apache-2.0 even though the architecture
is. See `NOTICE`.
