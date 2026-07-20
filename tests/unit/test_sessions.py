"""Tests for session service logic."""

from vscode_gateway.models import (
    WorkspaceView,
)


def test_workspace_view_closed() -> None:
    ws = WorkspaceView(
        alias="test-host",
        state="closed",
        can_open=True,
    )
    assert ws.alias == "test-host"
    assert ws.state == "closed"
    assert ws.can_open is True
    assert ws.can_close is False
    assert ws.can_retry is False
    assert ws.editor_url is None


def test_workspace_view_ready() -> None:
    import uuid

    sid = uuid.uuid4()
    ws = WorkspaceView(
        alias="test-host",
        state="ready",
        session_id=sid,
        editor_url=f"/editor/{sid}/",
        can_close=True,
    )
    assert ws.state == "ready"
    assert ws.editor_url == f"/editor/{sid}/"
    assert ws.can_close is True


def test_workspace_view_error() -> None:
    ws = WorkspaceView(
        alias="bad-host",
        state="error",
        error_code="ssh_unreachable",
        error_message="Connection refused",
        can_close=True,
        can_retry=True,
    )
    assert ws.state == "error"
    assert ws.can_retry is True
