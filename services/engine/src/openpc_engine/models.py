"""The local model manifest - configuration, never code.

The appliance must not name a model anywhere in source. Which GGUF loads, its
quantisation, context length and llama.cpp flags all come from
``config/models.json`` (installed to ``/etc/openpc/models.json``), so swapping
models is a manifest edit plus a benchmark run rather than a refactor.

The licence block is load-bearing: ``os/scripts/model-config check --target
retail`` refuses to build a retail image whose active model is anything other
than ``verified``. This module only parses and exposes the data; the gate lives
in the build script so it cannot be bypassed by importing the engine.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "openpc.models/1"
INSTALLED_MANIFEST = Path("/etc/openpc/models.json")
LICENCE_STATUSES = frozenset({"verified", "unverified"})
#: Where the image keeps GGUF weights. Only the basename from the manifest is
#: used, so a manifest cannot point the loader outside this directory.
MODEL_DIR = Path("/var/lib/openpc/models")


class ManifestError(ValueError):
    """The manifest is missing a required field or contradicts itself."""


@dataclass(frozen=True, slots=True)
class ModelLicence:
    id: str
    status: str
    url: str = ""

    def as_public_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status, "url": self.url}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    display_name: str
    repo_id: str
    quant: str
    file: str
    context_length: int
    licence: ModelLicence
    llama_architecture: str
    llama_artifact: str
    llama_args: tuple[str, ...]

    @property
    def path(self) -> Path:
        return MODEL_DIR / self.file

    def as_public_dict(self) -> dict[str, Any]:
        """Client-facing shape. Rides on ``host_hello`` as ``localModel``."""
        return {
            "id": self.key,
            "displayName": self.display_name,
            "repoId": self.repo_id,
            "quant": self.quant,
            "contextLength": self.context_length,
            "licence": self.licence.as_public_dict(),
        }

    def llama_argv(self) -> list[str]:
        """Full ``llama-server`` argv, model first, then the manifest flags."""
        return ["--model", str(self.path), *self.llama_args]


@dataclass(frozen=True, slots=True)
class ModelManifest:
    path: Path
    active: ModelSpec
    models: dict[str, ModelSpec]


def _require(mapping: dict[str, Any], key: str, *, context: str) -> Any:
    value = mapping.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ManifestError(f"{context}: missing required field {key!r}")
    return value


def _string(mapping: dict[str, Any], key: str, *, context: str) -> str:
    value = _require(mapping, key, context=context)
    if not isinstance(value, str):
        raise ManifestError(f"{context}: {key!r} must be a string")
    return value.strip()


def _parse_model(key: str, raw: dict[str, Any]) -> ModelSpec:
    context = f"model {key!r}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{context}: must be an object")

    file = _string(raw, "file", context=context)
    if Path(file).name != file or file in {".", ".."}:
        raise ManifestError(f"{context}: 'file' must be a bare filename, not a path ({file!r})")

    context_length = _require(raw, "contextLength", context=context)
    if not isinstance(context_length, int) or isinstance(context_length, bool) or context_length <= 0:
        raise ManifestError(f"{context}: 'contextLength' must be a positive integer")

    licence_raw = _require(raw, "licence", context=context)
    if not isinstance(licence_raw, dict):
        raise ManifestError(f"{context}: 'licence' must be an object")
    status = _string(licence_raw, "status", context=f"{context} licence")
    if status not in LICENCE_STATUSES:
        allowed = ", ".join(sorted(LICENCE_STATUSES))
        raise ManifestError(f"{context}: licence.status must be one of {allowed}, not {status!r}")

    llama_raw = _require(raw, "llamaCpp", context=context)
    if not isinstance(llama_raw, dict):
        raise ManifestError(f"{context}: 'llamaCpp' must be an object")
    args = llama_raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ManifestError(f"{context}: llamaCpp.args must be a list of strings")
    if any(not arg for arg in args):
        raise ManifestError(f"{context}: llamaCpp.args cannot contain an empty argument")

    return ModelSpec(
        key=key,
        display_name=_string(raw, "displayName", context=context),
        repo_id=_string(raw, "repoId", context=context),
        quant=_string(raw, "quant", context=context),
        file=file,
        context_length=context_length,
        licence=ModelLicence(
            id=_string(licence_raw, "id", context=f"{context} licence"),
            status=status,
            url=str(licence_raw.get("url", "")).strip(),
        ),
        llama_architecture=_string(llama_raw, "architecture", context=f"{context} llamaCpp"),
        llama_artifact=_string(llama_raw, "artifact", context=f"{context} llamaCpp"),
        llama_args=tuple(args),
    )


def parse_manifest(data: Any, *, path: Path) -> ModelManifest:
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: top level must be an object")
    schema = data.get("schema")
    if schema != SCHEMA:
        raise ManifestError(f"{path}: unsupported schema {schema!r} (expected {SCHEMA!r})")

    models_raw = _require(data, "models", context=str(path))
    if not isinstance(models_raw, dict) or not models_raw:
        raise ManifestError(f"{path}: 'models' must be a non-empty object")

    models = {key: _parse_model(key, raw) for key, raw in models_raw.items()}
    active_key = _string(data, "active", context=str(path))
    if active_key not in models:
        raise ManifestError(f"{path}: active model {active_key!r} is not declared in 'models'")
    return ModelManifest(path=path, active=models[active_key], models=models)


def load_manifest(path: str | os.PathLike[str]) -> ModelManifest:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"model manifest not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{source}: invalid JSON ({exc})") from exc
    return parse_manifest(raw, path=source)


def default_manifest_path() -> Path | None:
    """First existing manifest: env override, the installed one, or the repo.

    The repo fallback keeps ``make doctor`` and the tests honest on a dev
    machine; an appliance image always has the installed copy.
    """
    env = os.environ.get("OPENPC_MODEL_MANIFEST")
    if env:
        return Path(env)
    if INSTALLED_MANIFEST.exists():
        return INSTALLED_MANIFEST
    repo = Path(__file__).resolve().parents[4] / "config" / "models.json"
    if repo.exists():
        return repo
    return None


def load_default() -> ModelManifest | None:
    path = default_manifest_path()
    return load_manifest(path) if path is not None else None


__all__ = [
    "INSTALLED_MANIFEST",
    "LICENCE_STATUSES",
    "MODEL_DIR",
    "ManifestError",
    "ModelLicence",
    "ModelManifest",
    "ModelSpec",
    "default_manifest_path",
    "load_default",
    "load_manifest",
    "parse_manifest",
]
