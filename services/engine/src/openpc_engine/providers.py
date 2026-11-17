"""Where the box sends its big-model turns: Amaara Engine.

The appliance has a small local model and a 6 W budget. Anything bigger is routed
to **Amaara Engine**, which is OpenAI-compatible, meters usage and decides which
model serves a turn (`amaara/auto`). The box therefore needs one provider entry —
a base URL, a model id, and the *name* of the environment variable holding the key.
The key itself never lives in this file: it is read from the environment at spawn
and passed to the child, so a leaked config file leaks a URL.

If no provider is configured the box is not broken: the child starts with whatever
it already has (the local llama.cpp model), and the companion shows that instead of
pretending a cloud engine is attached.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("/etc/openpc/providers.json")


class ProviderError(ValueError):
    """The provider file exists but cannot be used."""


@dataclass(slots=True)
class Provider:
    id: str
    kind: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    #: Whether the key is actually present at spawn time. Filled in on load.
    has_key: bool = field(default=False)

    def env(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        """The variables the child needs. Absent key means absent variables."""
        source = os.environ if environ is None else environ
        overlay: dict[str, str] = {}
        if self.base_url:
            overlay["OPENAI_BASE_URL"] = self.base_url
            overlay["AMAARA_ENGINE_URL"] = self.base_url
        if self.model:
            overlay["AMAARA_ENGINE_MODEL"] = self.model
        if self.api_key_env:
            key = source.get(self.api_key_env, "")
            if key:
                overlay["OPENAI_API_KEY"] = key
                overlay["AMAARA_ENGINE_KEY"] = key
        return overlay

    def public(self) -> dict[str, Any]:
        """What the companion may see: never the key, only whether there is one."""
        return {
            "id": self.id,
            "kind": self.kind,
            "baseUrl": self.base_url,
            "model": self.model,
            "hasKey": self.has_key,
        }


@dataclass(slots=True)
class Providers:
    active: str = ""
    entries: dict[str, Provider] = field(default_factory=dict)

    def current(self) -> Provider | None:
        return self.entries.get(self.active)

    def env(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        provider = self.current()
        return provider.env(environ) if provider else {}

    def public(self) -> dict[str, Any]:
        provider = self.current()
        return {
            "active": self.active,
            "provider": provider.public() if provider else None,
            "count": len(self.entries),
        }


def load(path: Path = DEFAULT_PATH, environ: dict[str, str] | None = None) -> Providers:
    """Read the provider file. A missing file is not an error, a broken one is."""
    source = os.environ if environ is None else environ
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Providers()
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProviderError(f"{path} must contain an object")

    entries: dict[str, Provider] = {}
    for provider_id, spec in (raw.get("providers") or {}).items():
        if not isinstance(spec, dict):
            raise ProviderError(f"provider {provider_id!r} must be an object")
        api_key_env = str(spec.get("apiKeyEnv", "") or "")
        entries[provider_id] = Provider(
            id=provider_id,
            kind=str(spec.get("kind", "openai")),
            base_url=str(spec.get("baseUrl", "") or ""),
            model=str(spec.get("model", "") or ""),
            api_key_env=api_key_env,
            has_key=bool(source.get(api_key_env)) if api_key_env else False,
        )

    active = str(raw.get("active", "") or "")
    if active and active not in entries:
        raise ProviderError(f"active provider {active!r} is not in the file")
    return Providers(active=active, entries=entries)
