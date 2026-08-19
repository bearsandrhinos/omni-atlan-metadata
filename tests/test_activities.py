"""Tests for app/activities.py — focuses on get_workflow_args config normalization."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_EPOCH = "1747156800000"


def _make_base_args(overrides: dict | None = None) -> dict:
    """Simulate what the SDK's get_workflow_args returns from the state store.

    `connection_epoch_ms` is required by the activity; injected by default so
    individual tests don't need to set it unless they're exercising validation.
    """
    base = {
        "workflow_id": "wf-123",
        "workflow_run_id": "run-456",
        "output_path": "./local/tmp/wf-123/run-456",
        "output_prefix": "./local/tmp/",
        "payload": {"connection_epoch_ms": DEFAULT_EPOCH},
        "metadata": {},
        "credentials": {},
    }
    if overrides:
        # Merge payload overrides instead of clobbering so callers don't lose
        # the default connection_epoch_ms.
        payload_override = overrides.pop("payload", None)
        base.update(overrides)
        if payload_override is not None:
            base["payload"] = {**base["payload"], **payload_override}
    return base


async def _call_get_workflow_args(workflow_config: dict, base_args: dict) -> dict:
    """Invoke ActivitiesClass.get_workflow_args with a mocked super() call."""
    from app.activities import ActivitiesClass

    activities = ActivitiesClass()

    with patch.object(
        ActivitiesClass.__bases__[0],
        "get_workflow_args",
        new=AsyncMock(return_value=base_args),
    ):
        return await activities.get_workflow_args(workflow_config)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_defaults_when_state_empty():
    result = await _call_get_workflow_args({}, _make_base_args())
    assert result["metadata"]["connection_epoch_ms"] == DEFAULT_EPOCH
    assert result["metadata"]["page_size"] == 100
    assert result["metadata"]["max_pages"] is None
    assert result["metadata"]["save_output_local"] is False
    assert result["credentials"]["verify_ssl"] is True
    assert result["credentials"]["timeout_seconds"] == 30


@pytest.mark.asyncio
async def test_missing_connection_epoch_ms_raises():
    from temporalio.exceptions import ApplicationError

    base = _make_base_args()
    base["payload"].pop("connection_epoch_ms")
    with pytest.raises(ApplicationError, match="connection_epoch_ms"):
        await _call_get_workflow_args({}, base)


@pytest.mark.asyncio
async def test_non_numeric_connection_epoch_ms_raises():
    from temporalio.exceptions import ApplicationError

    base = _make_base_args({"payload": {"connection_epoch_ms": "not-a-number"}})
    with pytest.raises(ApplicationError, match="connection_epoch_ms"):
        await _call_get_workflow_args({}, base)


# ---------------------------------------------------------------------------
# Connection-blob derivation (production marketplace path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_epoch_derived_from_connection_qn():
    # Marketplace UI doesn't collect connection_epoch_ms — the SDK hands the
    # resolved Connection via base_args["connection"]. Activity must derive
    # the epoch from its qualifiedName.
    base = _make_base_args()
    base["payload"].pop("connection_epoch_ms")
    base["connection"] = {
        "attributes": {"qualifiedName": "default/omni/1733000000000"}
    }
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1733000000000"


@pytest.mark.asyncio
async def test_connection_qn_wins_over_form_field():
    # If both sources are present (operator left a stale form value), the
    # Connection's qualifiedName is authoritative.
    base = _make_base_args({"payload": {"connection_epoch_ms": "9999999999999"}})
    base["connection"] = {
        "attributes": {"qualifiedName": "default/omni/1733000000000"}
    }
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1733000000000"


@pytest.mark.asyncio
async def test_malformed_connection_qn_falls_back_to_form_field():
    # If the Connection blob has a junk QN (missing segments), fall back to
    # the form-field path so local playground keeps working.
    base = _make_base_args({"payload": {"connection_epoch_ms": "1700000000000"}})
    base["connection"] = {"attributes": {"qualifiedName": "junk"}}
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1700000000000"


@pytest.mark.asyncio
async def test_connection_qn_with_extra_segments_still_works():
    # Asset QNs like default/omni/<epoch>/model/<id> share the prefix —
    # split("/")[2] still resolves the epoch.
    base = _make_base_args()
    base["payload"].pop("connection_epoch_ms")
    base["connection"] = {
        "attributes": {
            "qualifiedName": "default/omni/1733000000000/model/abc123/topic/x"
        }
    }
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1733000000000"


# ---------------------------------------------------------------------------
# Values from payload (state store)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_credentials_from_payload():
    base = _make_base_args(
        {
            "payload": {
                "omni_base_url": "https://org.omniapp.co/api",
                "omni_api_token": "tok-abc",
                "connection_epoch_ms": "1700000000000",
            }
        }
    )
    result = await _call_get_workflow_args({}, base)
    assert result["credentials"]["omni_base_url"] == "https://org.omniapp.co/api"
    assert result["credentials"]["omni_api_token"] == "tok-abc"
    assert result["metadata"]["connection_epoch_ms"] == "1700000000000"


@pytest.mark.asyncio
async def test_reads_pagination_from_payload():
    base = _make_base_args({"payload": {"page_size": "100", "max_pages": "5"}})
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["page_size"] == 100
    assert result["metadata"]["max_pages"] == 5


@pytest.mark.asyncio
async def test_null_string_max_pages_becomes_none():
    base = _make_base_args({"payload": {"max_pages": "null"}})
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["max_pages"] is None


@pytest.mark.asyncio
async def test_empty_string_max_pages_becomes_none():
    base = _make_base_args({"payload": {"max_pages": ""}})
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["max_pages"] is None


# ---------------------------------------------------------------------------
# Credentials fallback chain: payload > credentials > metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credentials_fallback_to_credentials_key():
    base = _make_base_args(
        {"credentials": {"omni_base_url": "https://fallback.omniapp.co/api", "omni_api_token": "tok-fb"}}
    )
    result = await _call_get_workflow_args({}, base)
    assert result["credentials"]["omni_base_url"] == "https://fallback.omniapp.co/api"


# ---------------------------------------------------------------------------
# output_path propagated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_output_path_from_base_args():
    base = _make_base_args({"output_path": "./local/tmp/wf-xyz/run-abc"})
    result = await _call_get_workflow_args({}, base)
    assert result["output_path"] == "./local/tmp/wf-xyz/run-abc"


@pytest.mark.asyncio
async def test_output_path_fallback_when_missing():
    base = _make_base_args()
    base.pop("output_path")
    result = await _call_get_workflow_args({}, base)
    assert "wf-123" in result["output_path"]
    assert "run-456" in result["output_path"]


# ---------------------------------------------------------------------------
# workflow_id / workflow_run_id propagated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workflow_ids_propagated():
    base = _make_base_args({"workflow_id": "my-wf", "workflow_run_id": "my-run"})
    result = await _call_get_workflow_args({}, base)
    assert result["workflow_id"] == "my-wf"
    assert result["workflow_run_id"] == "my-run"


# ---------------------------------------------------------------------------
# atlan_source_connection_map normalization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_connection_map_default_empty():
    result = await _call_get_workflow_args({}, _make_base_args())
    assert result["metadata"]["atlan_source_connection_map"] == {}


@pytest.mark.asyncio
async def test_source_connection_map_parses_json_string():
    base = _make_base_args(
        {"payload": {"atlan_source_connection_map": '{"c1": "default/snowflake/1700"}'}}
    )
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["atlan_source_connection_map"] == {
        "c1": "default/snowflake/1700"
    }


@pytest.mark.asyncio
async def test_source_connection_map_accepts_dict():
    base = _make_base_args(
        {"payload": {"atlan_source_connection_map": {"c1": "default/redshift/abc"}}}
    )
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["atlan_source_connection_map"] == {
        "c1": "default/redshift/abc"
    }


@pytest.mark.asyncio
async def test_source_connection_map_invalid_json_yields_empty():
    base = _make_base_args(
        {"payload": {"atlan_source_connection_map": "{not valid json"}}
    )
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["atlan_source_connection_map"] == {}


# ---------------------------------------------------------------------------
# Connection-shape tolerance (the platform delivers three shapes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_epoch_derived_from_atlan_normalized_connection_qn():
    # Atlan create-configs normalizes the Connection asset into this
    # snake_case shape before saving workflow args into the state store.
    base = _make_base_args()
    base["payload"].pop("connection_epoch_ms")
    base["connection"] = {
        "connection_name": "Omni",
        "connection_qualified_name": "default/omni/1733000000000",
    }
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1733000000000"


@pytest.mark.asyncio
async def test_connection_epoch_derived_from_stringified_connection_blob():
    # Argo parameters are strings — the connection widget delivers the
    # Connection asset as stringified JSON (observed on the saved tenant
    # workflow template).
    base = _make_base_args()
    base["payload"].pop("connection_epoch_ms")
    base["connection"] = json.dumps(
        {"attributes": {"qualifiedName": "default/omni/1733000000000"}}
    )
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1733000000000"


@pytest.mark.asyncio
async def test_connection_epoch_derived_from_metadata_connection_json():
    # Defensive fallback for payloads where create-configs preserved the raw
    # Atlas-shaped connection JSON under metadata.connection.
    base = _make_base_args()
    base["payload"].pop("connection_epoch_ms")
    base["metadata"] = {
        "connection": "{\"attributes\":{\"qualifiedName\":\"default/omni/1733000000000\"}}"
    }
    result = await _call_get_workflow_args({}, base)
    assert result["metadata"]["connection_epoch_ms"] == "1733000000000"


# ---------------------------------------------------------------------------
# Credential resolution (per-activity; secrets never returned from an activity)
# ---------------------------------------------------------------------------

RESOLVED_CREDENTIAL = {
    "host": "https://partneratlan.omniapp.co/api",
    "port": 443,
    "authType": "apikey",
    "password": "tok-secret",
    "extra": {},
}

EMPTY_SNAPSHOT = {
    "connections": [],
    "models": [],
    "topics": [],
    "folders": [],
    "documents": [],
}


def _contains_value(obj, needle: str) -> bool:
    """Recursively check a nested structure for a string value."""
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(_contains_value(v, needle) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_value(v, needle) for v in obj)
    return False


async def _call_extract(args: dict, handler) -> dict:
    from app.activities import ActivitiesClass

    activities = ActivitiesClass(handler=handler)
    writer = MagicMock()
    writer.write = AsyncMock()
    stats = MagicMock()
    stats.total_record_count = 0
    writer.close = AsyncMock(return_value=stats)
    with patch("app.activities.JsonFileWriter", return_value=writer):
        return await activities.extract_and_transform_metadata(args)


def _make_extract_args(overrides: dict | None = None) -> dict:
    args = {
        "workflow_id": "wf-123",
        "workflow_run_id": "run-456",
        "output_path": "./local/tmp/wf-123/run-456",
        "credential_guid": "",
        "credentials": {
            "omni_base_url": None,
            "omni_api_token": None,
            "verify_ssl": True,
            "timeout_seconds": 30,
            "rate_limit_rpm": 60,
        },
        "metadata": {
            "connection_epoch_ms": "1780514137",
            "page_size": 50,
            "max_pages": None,
            "output_file": "omni_entities.ndjson",
            "save_output_local": False,
            "max_concurrency": 10,
            "atlan_source_connection_map": {},
        },
    }
    if overrides:
        args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_get_workflow_args_passes_guid_and_never_secrets():
    # get_workflow_args must ferry credential_guid but never resolve it:
    # its return value is an activity result, persisted in Temporal history.
    base = _make_base_args()
    base["credential_guid"] = "b8b7ec20-ee9c-4ffc-8ddd-86c7809e1074"
    mock = AsyncMock(return_value=RESOLVED_CREDENTIAL)
    with patch("app.activities.SecretStore.get_credentials", new=mock):
        result = await _call_get_workflow_args({}, base)
    mock.assert_not_awaited()
    assert result["credential_guid"] == "b8b7ec20-ee9c-4ffc-8ddd-86c7809e1074"
    assert not _contains_value(result, "tok-secret")
    assert result["credentials"]["omni_base_url"] is None
    assert result["credentials"]["omni_api_token"] is None


@pytest.mark.asyncio
async def test_extract_resolves_credentials_from_guid():
    args = _make_extract_args(
        {"credential_guid": "b8b7ec20-ee9c-4ffc-8ddd-86c7809e1074"}
    )
    handler = MagicMock()
    handler.load = AsyncMock()
    handler.fetch_metadata = AsyncMock(return_value=EMPTY_SNAPSHOT)
    with patch(
        "app.activities.SecretStore.get_credentials",
        new=AsyncMock(return_value=RESOLVED_CREDENTIAL),
    ):
        result = await _call_extract(args, handler)
    loaded = handler.load.await_args.kwargs["credentials"]
    assert loaded["omni_base_url"] == "https://partneratlan.omniapp.co/api"
    assert loaded["omni_api_token"] == "tok-secret"
    assert loaded["verify_ssl"] is True
    # The activity result must not leak the secret either.
    assert not _contains_value(result, "tok-secret")


@pytest.mark.asyncio
async def test_extract_inline_credentials_skip_secretstore():
    # Form-provided credentials (playground) win; no store round-trip.
    args = _make_extract_args(
        {"credential_guid": "b8b7ec20-ee9c-4ffc-8ddd-86c7809e1074"}
    )
    args["credentials"]["omni_base_url"] = "https://org.omniapp.co/api"
    args["credentials"]["omni_api_token"] = "tok-inline"
    handler = MagicMock()
    handler.load = AsyncMock()
    handler.fetch_metadata = AsyncMock(return_value=EMPTY_SNAPSHOT)
    mock = AsyncMock()
    with patch("app.activities.SecretStore.get_credentials", new=mock):
        await _call_extract(args, handler)
    mock.assert_not_awaited()
    loaded = handler.load.await_args.kwargs["credentials"]
    assert loaded["omni_api_token"] == "tok-inline"


@pytest.mark.asyncio
async def test_extract_no_guid_passes_credentials_through():
    args = _make_extract_args()
    args["credentials"]["omni_base_url"] = "https://org.omniapp.co/api"
    args["credentials"]["omni_api_token"] = "tok-local"
    handler = MagicMock()
    handler.load = AsyncMock()
    handler.fetch_metadata = AsyncMock(return_value=EMPTY_SNAPSHOT)
    mock = AsyncMock()
    with patch("app.activities.SecretStore.get_credentials", new=mock):
        await _call_extract(args, handler)
    mock.assert_not_awaited()
    loaded = handler.load.await_args.kwargs["credentials"]
    assert loaded["omni_base_url"] == "https://org.omniapp.co/api"
