"""Tests for app/handler.py — preflight_check shape and load unwrap behaviors."""

from unittest.mock import MagicMock

import pytest

from app.handler import HandlerClass


def _mock_client(list_connections_raises: Exception | None = None) -> MagicMock:
    client = MagicMock()
    if list_connections_raises is None:
        client.list_connections.return_value = []
    else:
        client.list_connections.side_effect = list_connections_raises
    return client


@pytest.mark.asyncio
async def test_preflight_returns_per_check_shape():
    """PART-1221: the setup UI iterates top-level keys expecting each to carry
    `.success`. A flat {success, message, data} caused every top-level key to
    render as a failed check. Return the SDK's per-check shape instead."""
    handler = HandlerClass(client=_mock_client())
    result = await handler.preflight_check()
    assert list(result.keys()) == ["connection"]
    check = result["connection"]
    assert check["success"] is True
    assert "successMessage" in check
    assert "failureMessage" in check
    # No stray legacy keys.
    assert "message" not in result
    assert "data" not in result


@pytest.mark.asyncio
async def test_preflight_calls_list_connections():
    """The check must actually hit Omni — otherwise a bad token returns 200."""
    client = _mock_client()
    await HandlerClass(client=client).preflight_check()
    client.list_connections.assert_called_once()
