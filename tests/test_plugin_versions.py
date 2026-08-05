# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

from __future__ import annotations

import pytest

from pdm_memory.plugins.versions import (
    check_requirement,
    parse_requirement,
    version_satisfies,
)


def test_parse_requirement_name_only() -> None:
    req = parse_requirement("GeoTagger")
    assert req.name == "GeoTagger"
    assert req.operator is None
    assert req.version is None


def test_parse_requirement_with_operator() -> None:
    req = parse_requirement("GeoTagger>=1.2.0")
    assert req.name == "GeoTagger"
    assert req.operator == ">="
    assert req.version == "1.2.0"
    assert req.raw == "GeoTagger>=1.2.0"


def test_version_satisfies() -> None:
    assert version_satisfies("1.2.0", ">=", "1.2.0")
    assert version_satisfies("1.2.1", ">=", "1.2.0")
    assert not version_satisfies("1.1.9", ">=", "1.2.0")
    assert version_satisfies("2.0", ">", "1.9.9")


def test_check_requirement_messages() -> None:
    req = parse_requirement("GeoTagger>=1.2")
    assert check_requirement(req, installed_version=None) == "'GeoTagger>=1.2'"
    assert check_requirement(req, installed_version="1.2.0") is None
    assert "installed 1.0.0" in (
        check_requirement(req, installed_version="1.0.0") or ""
    )


def test_invalid_requirement_fails() -> None:
    with pytest.raises(ValueError):
        parse_requirement(">=1.0")
    with pytest.raises(ValueError):
        parse_requirement("Foo>=")
