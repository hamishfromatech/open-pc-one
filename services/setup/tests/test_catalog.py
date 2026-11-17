"""The catalogue is the answer to "what does this box install?"."""

from __future__ import annotations

import pytest
import setup_testkit as helpers  # noqa: F401  (puts src/ on sys.path)
from amaara_setup import catalog


def test_the_agent_runtime_cannot_be_deselected():
    resolved = catalog.resolve([])
    assert "agent" in [p.id for p in resolved]


def test_defaults_resolve_in_catalog_order():
    resolved = [p.id for p in catalog.resolve(catalog.defaults())]
    assert resolved == ["agent", "local-ai"]


def test_a_profile_that_cannot_work_says_why_and_is_refused():
    voice = catalog.by_id("voice")
    assert not voice.available
    assert voice.unavailable
    with pytest.raises(ValueError, match="speech image"):
        catalog.resolve(["voice"])


def test_unknown_profile_is_an_error_not_silently_dropped():
    with pytest.raises(ValueError, match="unknown profile"):
        catalog.resolve(["kubernetes"])


def test_package_profiles_warn_about_the_network():
    public = {p["id"]: p for p in (c.public() for c in catalog.CATALOG)}
    assert public["dev-tools"]["requiresNetwork"] is True
    assert public["local-ai"]["requiresNetwork"] is False
