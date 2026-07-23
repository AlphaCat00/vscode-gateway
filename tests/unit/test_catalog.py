"""Tests for SSH catalog and alias discovery."""

import pytest

from vscode_gateway.ssh_config import compute_config_revision, discover_aliases


def test_discover_aliases_positive_literals() -> None:
    config = """
Host myserver
    HostName 10.0.0.1

Host another-host
    HostName example.com
"""
    aliases = discover_aliases(config)
    assert "myserver" in aliases
    assert "another-host" in aliases


@pytest.mark.parametrize("pattern", ["*", "!excluded", "foo*", "bar?", "baz[1-3]"])
def test_discover_aliases_excludes_nonliteral_patterns(pattern: str) -> None:
    config = f"""
Host {pattern}
Host valid
"""
    aliases = discover_aliases(config)
    assert pattern not in aliases
    assert "valid" in aliases


def test_discover_aliases_multiple_per_host() -> None:
    config = """
Host srv1 srv2 srv3
    HostName example.com
"""
    aliases = discover_aliases(config)
    assert "srv1" in aliases
    assert "srv2" in aliases
    assert "srv3" in aliases


def test_discover_aliases_deduplication() -> None:
    config = """
Host dup dup
    HostName example.com
"""
    aliases = discover_aliases(config)
    assert len([a for a in aliases if a == "dup"]) == 1


def test_discover_aliases_ignores_comments() -> None:
    config = """
# Host commented-out
Host actual
# Another comment
"""
    aliases = discover_aliases(config)
    assert "commented-out" not in aliases
    assert "actual" in aliases


def test_compute_config_revision() -> None:
    rev1 = compute_config_revision("hello")
    rev2 = compute_config_revision("hello")
    rev3 = compute_config_revision("world")
    assert rev1 == rev2
    assert rev1 != rev3
    assert rev1.startswith("sha256:")
