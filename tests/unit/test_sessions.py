"""Tests for session service logic."""

from dataclasses import asdict, fields

from vscode_gateway.models import (
    WorkspaceView,
)


def test_workspace_view_closed_defaults_and_serialization() -> None:
    assert tuple(field.name for field in fields(WorkspaceView)) == (
        "alias",
        "state",
        "session_id",
        "editor_url",
        "connected_clients",
        "disconnect_deadline",
        "stage",
        "error_code",
        "error_message",
        "can_open",
        "can_close",
        "can_retry",
        "can_force_close",
        "has_remote_identity",
        "catalog_missing",
        "ssh_host_key",
    )

    ws = WorkspaceView(
        alias="test-host",
        state="closed",
        can_open=True,
    )
    assert asdict(ws) == {
        "alias": "test-host",
        "state": "closed",
        "session_id": None,
        "editor_url": None,
        "connected_clients": 0,
        "disconnect_deadline": None,
        "stage": None,
        "error_code": None,
        "error_message": None,
        "can_open": True,
        "can_close": False,
        "can_retry": False,
        "can_force_close": False,
        "has_remote_identity": False,
        "catalog_missing": False,
        "ssh_host_key": None,
    }
