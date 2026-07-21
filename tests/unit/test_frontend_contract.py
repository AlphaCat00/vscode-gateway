"""Regression checks for the server-rendered frontend assets."""

from __future__ import annotations

from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "vscode_gateway"


def _asset(*parts: str) -> str:
    return (SOURCE_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_key_page_and_script_keep_upload_contract() -> None:
    template = _asset("templates", "keys.html")
    script = _asset("static", "keys.js")

    assert 'name="name"' in template
    assert 'name="private_key"' in template
    assert all(f'id="key-slot-{key_type}"' in template for key_type in ("ed25519", "rsa", "ecdsa"))
    assert "generate-key" not in template
    assert "Generate" not in template
    assert 'body.append("private_key", keyFile)' in script
    assert 'apiRequest("/api/ssh/keys", { method: "POST", body })' in script


def test_config_page_and_script_keep_guidance_and_revision_contract() -> None:
    template = _asset("templates", "ssh_config.html")
    script = _asset("static", "ssh_config.js")

    assert "automatically tried" in template
    assert "IdentityFile" in template
    assert "IdentityAgent" in template
    assert "RemoteCommand" in template
    assert '"/api/ssh/config"' in script
    assert "expectedRevision" in script
    assert "remotecommand" not in script


def test_dashboard_script_keeps_existing_actions_and_page_links() -> None:
    script = _asset("static", "dashboard.js")

    assert '"/api/ssh/hosts/trust"' in script
    assert "`/api/sessions/${encodeURIComponent(alias)}/retry`" in script
    assert 'configLink.href = "/settings/ssh"' in script
    assert 'keysLink.href = "/settings/keys"' in script


def test_page_request_helpers_redirect_expired_sessions() -> None:
    contracts = (
        ("dashboard.js", "fetchJSON", "response"),
        ("keys.js", "apiRequest", "response"),
        ("ssh_config.js", "fetchJSON", "resp"),
    )
    for filename, helper, response_name in contracts:
        script = _asset("static", filename)
        assert f"async function {helper}" in script
        assert f"{response_name}.status === 401" in script
        assert 'window.location.replace("/login")' in script
