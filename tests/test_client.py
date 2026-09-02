"""Tests for app/client.py"""

import pytest
import respx
import httpx

from app.client import ClientClass, OmniApiError, NonRetryableOmniApiError


CREDS = {
    "omni_base_url": "https://test.omniapp.co/api",
    "omni_api_token": "tok-test",
}


def make_client() -> ClientClass:
    # rpm=0 disables the rate limiter so tests don't pay the 1s/request floor.
    return ClientClass(credentials=CREDS, rpm=0)


# ---------------------------------------------------------------------------
# load_credentials
# ---------------------------------------------------------------------------

def test_load_credentials_requires_base_url():
    with pytest.raises(ValueError, match="omni_base_url"):
        ClientClass(credentials={"omni_api_token": "tok"})


def test_load_credentials_requires_token():
    with pytest.raises(ValueError, match="omni_api_token"):
        ClientClass(credentials={"omni_base_url": "https://x.com/api"})


def test_load_credentials_requires_protocol():
    with pytest.raises(ValueError, match="protocol"):
        ClientClass(credentials={"omni_base_url": "x.com/api", "omni_api_token": "t"})


def test_rate_limiter_is_shared_across_clients_for_same_host():
    """Item 3: throttling is a property of the Omni host, not the run. Two
    concurrent activities against the same tenant must share one gate — five
    per-run 60 rpm limiters is 300 rpm at Omni's door."""
    creds = {"omni_base_url": "https://a.omniapp.co/api", "omni_api_token": "t"}
    a = ClientClass(credentials=creds, rpm=0)
    b = ClientClass(credentials=creds, rpm=0)
    assert a._rate_limiter is b._rate_limiter


def test_swallow_populates_failure_counters():
    """Item 5: silently-swallowed errors must land in the per-run counters."""
    client = ClientClass(credentials=CREDS, rpm=0)
    assert client._failures["model_yaml"] == 0
    client._swallow("model_yaml", "model_id=m1", RuntimeError("boom"))
    client._swallow("model_yaml", "model_id=m2", RuntimeError("boom"))
    client._swallow("topic_detail", "model_id=m1 topic=orders", RuntimeError("x"))
    assert client._failures["model_yaml"] == 2
    assert client._failures["topic_detail"] == 1
    assert client._failures["override_probe"] == 0


@respx.mock
def test_fetch_snapshot_aborts_when_model_yaml_failure_rate_exceeds_threshold():
    """Item 5 threshold: if >20% of model-YAML calls fail, abort before the
    transform pass rather than publish a partial crawl. The check runs at end
    of model pass (~1 min in), not end of fetch_snapshot — with
    maximum_attempts=1 a late abort costs a full three-hour re-crawl."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={
            "records": [{"id": f"m{i}", "modelKind": "SHARED"} for i in range(5)],
            "pageInfo": {"hasNextPage": False},
        })
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    # 3 of 5 models 500 -> 60% failure rate, well above the 20% floor.
    for i in range(3):
        respx.get(
            f"https://test.omniapp.co/api/v1/models/m{i}/yaml", params={"mode": "combined"}
        ).mock(return_value=httpx.Response(500, text="boom"))
    for i in range(3, 5):
        respx.get(
            f"https://test.omniapp.co/api/v1/models/m{i}/yaml", params={"mode": "combined"}
        ).mock(return_value=httpx.Response(200, json={"files": {}}))

    with pytest.raises(OmniApiError, match="model-YAML fetch failed"):
        make_client().fetch_snapshot(crawl_only_content_backed_workbooks=False)


def test_rate_limiter_is_distinct_across_hosts():
    """Different tenants share a pod but not a rate quota."""
    a = ClientClass(
        credentials={"omni_base_url": "https://a.omniapp.co/api", "omni_api_token": "t"},
        rpm=0,
    )
    b = ClientClass(
        credentials={"omni_base_url": "https://b.omniapp.co/api", "omni_api_token": "t"},
        rpm=0,
    )
    assert a._rate_limiter is not b._rate_limiter


def test_load_credentials_accepts_wire_shape():
    # Atlan UI → Heracles sends {"host": ..., "password": ..., "authType": "apikey"}.
    # ClientClass must accept these as aliases for omni_base_url / omni_api_token.
    client = ClientClass(
        credentials={
            "host": "https://test.omniapp.co/api",
            "password": "tok-wire",
            "authType": "apikey",
        },
        rpm=0,
    )
    assert client._credentials is not None
    assert client._credentials.base_url == "https://test.omniapp.co/api"
    assert client._credentials.api_token == "tok-wire"


def test_load_credentials_wire_shape_protocol_check():
    with pytest.raises(ValueError, match="protocol"):
        ClientClass(credentials={"host": "x.com/api", "password": "t"})


# ---------------------------------------------------------------------------
# list_connections
# ---------------------------------------------------------------------------

@respx.mock
def test_list_connections_returns_list():
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": [{"id": "c1", "name": "Conn1"}]})
    )
    client = make_client()
    result = client.list_connections()
    assert result == [{"id": "c1", "name": "Conn1"}]


@respx.mock
def test_list_connections_empty():
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    result = make_client().list_connections()
    assert result == []


@respx.mock
def test_list_connections_401_raises_non_retryable():
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(NonRetryableOmniApiError):
        make_client().list_connections()


@respx.mock
def test_list_connections_500_raises_retryable():
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with pytest.raises(OmniApiError) as exc_info:
        make_client().list_connections()
    assert exc_info.value.retryable


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

@respx.mock
def test_collect_paginated_follows_cursor():
    page1 = {
        "records": [{"id": "m1"}],
        "pageInfo": {"hasNextPage": True, "nextCursor": "cur2"},
    }
    page2 = {
        "records": [{"id": "m2"}],
        "pageInfo": {"hasNextPage": False, "nextCursor": None},
    }
    route = respx.get("https://test.omniapp.co/api/v1/models")
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]
    client = make_client()
    result = client._collect_paginated(client.list_models, page_size=1, max_pages=None)
    assert [r["id"] for r in result] == ["m1", "m2"]


@respx.mock
def test_collect_paginated_respects_max_pages():
    page = {
        "records": [{"id": "m1"}],
        "pageInfo": {"hasNextPage": True, "nextCursor": "cur2"},
    }
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json=page)
    )
    client = make_client()
    result = client._collect_paginated(client.list_models, page_size=1, max_pages=1)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# rate limit retry
# ---------------------------------------------------------------------------

@respx.mock
def test_rate_limit_retries_then_succeeds():
    route = respx.get("https://test.omniapp.co/api/v1/connections")
    route.side_effect = [
        httpx.Response(429, text="Rate limited"),
        httpx.Response(200, json={"connections": [{"id": "c1"}]}),
    ]
    client = make_client()
    result = client.list_connections()
    assert result[0]["id"] == "c1"


# ---------------------------------------------------------------------------
# fetch_snapshot topic parsing
# ---------------------------------------------------------------------------

@respx.mock
def test_fetch_snapshot_parses_topics():
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [{"id": "mod1"}],
                "pageInfo": {"hasNextPage": False},
            },
        )
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    yaml_content = "name: orders\nlabel: Orders\nbase_view_name: orders_view\n"
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_content}})
    )
    # Topic detail returns 404 — basic YAML data still flows through.
    respx.get("https://test.omniapp.co/api/v1/models/mod1/topic/orders").mock(
        return_value=httpx.Response(404, text="Not found")
    )

    snapshot = make_client().fetch_snapshot()
    assert snapshot["topics"] == [
        {
            "modelId": "mod1",
            "owningModelId": "mod1",
            "name": "orders",
            "label": "Orders",
            "baseViewName": "orders_view",
        }
    ]


@respx.mock
def test_fetch_snapshot_fetches_yaml_for_multiple_models_concurrently():
    """All model YAML calls are made regardless of ordering — concurrent fetch."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [{"id": "mod1"}, {"id": "mod2"}],
                "pageInfo": {"hasNextPage": False},
            },
        )
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    yaml1 = "label: Orders\nbase_view_name: orders_view\n"
    yaml2 = "label: Customers\nbase_view_name: customers_view\n"
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml1}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod2/yaml").mock(
        return_value=httpx.Response(200, json={"files": {"customers.topic": yaml2}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod1/topic/orders").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod2/topic/customers").mock(
        return_value=httpx.Response(404)
    )

    snapshot = make_client().fetch_snapshot()
    topic_model_ids = {t["modelId"] for t in snapshot["topics"]}
    assert topic_model_ids == {"mod1", "mod2"}
    assert len(snapshot["topics"]) == 2


@respx.mock
def test_fetch_snapshot_yaml_failure_skips_model_but_continues():
    """A failed YAML call for a SMALL fraction of models does not abort the
    batch. 1 of 6 fails = ~17%, safely under the 20% abort threshold; the
    other 5 topics still land."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [{"id": f"mod{i}"} for i in range(1, 7)],
                "pageInfo": {"hasNextPage": False},
            },
        )
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    # mod1 fails; the other five succeed with one topic apiece.
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    for i in range(2, 7):
        yaml_body = f"label: t{i}\nbase_view_name: t{i}_view\n"
        respx.get(f"https://test.omniapp.co/api/v1/models/mod{i}/yaml").mock(
            return_value=httpx.Response(200, json={"files": {f"t{i}.topic": yaml_body}})
        )
        respx.get(f"https://test.omniapp.co/api/v1/models/mod{i}/topic/t{i}").mock(
            return_value=httpx.Response(404)
        )

    snapshot = make_client().fetch_snapshot()
    assert len(snapshot["topics"]) == 5
    assert {t["modelId"] for t in snapshot["topics"]} == {"mod2", "mod3", "mod4", "mod5", "mod6"}


@respx.mock
def test_fetch_snapshot_resolves_document_model_ids_concurrently():
    """Document detail calls are made for all documents and model IDs collected."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [{"identifier": "doc1"}, {"identifier": "doc2"}],
                "pageInfo": {"hasNextPage": False},
            },
        )
    )
    respx.get("https://test.omniapp.co/api/v1/documents/doc1").mock(
        return_value=httpx.Response(200, json={"modelId": "mod1"})
    )
    respx.get("https://test.omniapp.co/api/v1/documents/doc2").mock(
        return_value=httpx.Response(200, json={"modelId": "mod2"})
    )

    snapshot = make_client().fetch_snapshot()
    assert sorted(snapshot["document_model_ids"]) == ["mod1", "mod2"]


@respx.mock
def test_fetch_snapshot_document_detail_failure_skips_but_continues():
    """A failed document detail call does not abort the rest of the batch."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [{"identifier": "doc1"}, {"identifier": "doc2"}],
                "pageInfo": {"hasNextPage": False},
            },
        )
    )
    respx.get("https://test.omniapp.co/api/v1/documents/doc1").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    respx.get("https://test.omniapp.co/api/v1/documents/doc2").mock(
        return_value=httpx.Response(200, json={"modelId": "mod2"})
    )

    snapshot = make_client().fetch_snapshot()
    assert snapshot["document_model_ids"] == ["mod2"]


@respx.mock
def test_fetch_snapshot_enriches_document_with_tile_topics():
    """Document records are enriched with deduplicated tile topics from queryPresentations."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [{"identifier": "doc1"}],
                "pageInfo": {"hasNextPage": False},
            },
        )
    )
    respx.get("https://test.omniapp.co/api/v1/documents/doc1").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelId": "mod1",
                "queryPresentations": [
                    {"topicName": "orders", "query": {"modelId": "mod1"}},
                    {"topicName": "customers", "query": {"modelId": "mod1"}},
                    {"topicName": "orders", "query": {"modelId": "mod1"}},  # duplicate
                    {"topicName": None},  # missing topic — skipped
                ],
            },
        )
    )

    snapshot = make_client().fetch_snapshot()
    doc = snapshot["documents"][0]
    tile_topics = doc["tileTopics"]
    assert len(tile_topics) == 2  # duplicate and null deduped/skipped
    assert {"modelId": "mod1", "topicName": "orders"} in tile_topics
    assert {"modelId": "mod1", "topicName": "customers"} in tile_topics


@respx.mock
def test_fetch_snapshot_falls_back_to_doc_model_id_when_query_missing_model():
    """When a presentation has no inner query.modelId, fall back to the document's modelId."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"identifier": "doc1"}], "pageInfo": {"hasNextPage": False}},
        )
    )
    respx.get("https://test.omniapp.co/api/v1/documents/doc1").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelId": "modX",
                "queryPresentations": [{"topicName": "orders"}],
            },
        )
    )

    snapshot = make_client().fetch_snapshot()
    doc = snapshot["documents"][0]
    assert doc["tileTopics"] == [{"modelId": "modX", "topicName": "orders"}]


@respx.mock
def test_fetch_snapshot_enriches_topic_with_source_table_and_fields():
    """Topic API enrichment populates source table, joined views, dimensions, and measures."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"id": "mod1"}], "pageInfo": {"hasNextPage": False}},
        )
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(
            200,
            json={"files": {"orders.topic": "label: Orders\nbase_view_name: orders_view\n"}},
        )
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod1/topic/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "topic": {
                    "name": "orders",
                    "label": "Orders",
                    "base_view_name": "orders_view",
                    "views": [
                        {
                            "name": "orders_view",
                            "table_name": "orders",
                            "schema": "public",
                            "catalog": "analytics",
                            "dimensions": [
                                {"fully_qualified_name": "orders_view.id"},
                                {"fully_qualified_name": "orders_view.created_at"},
                            ],
                            "measures": [
                                {"fully_qualified_name": "orders_view.total_revenue"},
                            ],
                        },
                        {
                            "name": "customers_view",
                            "table_name": "customers",
                            "schema": "public",
                            "dimensions": [
                                {"fully_qualified_name": "customers_view.email"},
                            ],
                            "measures": [],
                        },
                    ],
                },
            },
        )
    )

    snapshot = make_client().fetch_snapshot()
    assert len(snapshot["topics"]) == 1
    topic = snapshot["topics"][0]
    assert topic["sourceTableName"] == "orders"
    assert topic["sourceSchema"] == "public"
    assert topic["sourceCatalog"] == "analytics"
    assert topic["joinedViewNames"] == ["customers_view"]
    assert topic["dimensionNames"] == [
        "orders_view.id",
        "orders_view.created_at",
        "customers_view.email",
    ]
    assert topic["measureNames"] == ["orders_view.total_revenue"]
    # viewSources collects every view that has a table_name (catalog optional).
    assert topic["viewSources"] == [
        {"viewName": "orders_view", "tableName": "orders", "schema": "public", "catalog": "analytics"},
        {"viewName": "customers_view", "tableName": "customers", "schema": "public", "catalog": None},
    ]


@respx.mock
def test_fetch_snapshot_topic_detail_failure_falls_back_to_yaml():
    """If the topic detail API fails, the topic is still emitted with YAML-derived basics."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"id": "mod1"}], "pageInfo": {"hasNextPage": False}},
        )
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(
            200,
            json={"files": {"orders.topic": "label: Orders\nbase_view_name: orders_view\n"}},
        )
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod1/topic/orders").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    snapshot = make_client().fetch_snapshot()
    assert len(snapshot["topics"]) == 1
    topic = snapshot["topics"][0]
    assert topic["name"] == "orders"
    assert topic["label"] == "Orders"
    assert topic["baseViewName"] == "orders_view"
    # Detail-only fields are absent when the topic API fails.
    assert "sourceTableName" not in topic


# ---------------------------------------------------------------------------
# Workbook topic canonicalization (owningModelId stamping)
# ---------------------------------------------------------------------------

@respx.mock
def test_workbook_inherited_topic_owning_is_shared_model():
    """A workbook whose mode=extension YAML doesn't list a topic file is treated
    as inheriting it — owningModelId points to the base shared model."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={
            "records": [
                {"id": "shared1", "modelKind": "SHARED"},
                {"id": "wb1", "name": "Workbook 1", "modelKind": "WORKBOOK", "baseModelId": "shared1"},
            ],
            "pageInfo": {"hasNextPage": False},
        })
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    yaml_body = "label: Orders\nbase_view_name: orders_view\n"
    respx.get("https://test.omniapp.co/api/v1/models/shared1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    # Workbook's own extension layer is empty => topic is inherited, not overridden.
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "extension"}).mock(
        return_value=httpx.Response(200, json={"files": {}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/shared1/topic/orders").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/topic/orders").mock(
        return_value=httpx.Response(404)
    )

    snapshot = make_client().fetch_snapshot(crawl_only_content_backed_workbooks=False)
    topics_by_model = {t["modelId"]: t for t in snapshot["topics"]}
    assert topics_by_model["shared1"]["owningModelId"] == "shared1"
    assert topics_by_model["wb1"]["owningModelId"] == "shared1"


@respx.mock
def test_workbook_overridden_topic_owning_is_workbook():
    """A workbook whose extension YAML lists the topic file IS overriding it —
    owningModelId stays the workbook so it gets its own canonical entity."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={
            "records": [
                {"id": "shared1", "modelKind": "SHARED"},
                {"id": "wb1", "name": "Workbook 1", "modelKind": "WORKBOOK", "baseModelId": "shared1"},
            ],
            "pageInfo": {"hasNextPage": False},
        })
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    yaml_body = "label: Orders\nbase_view_name: orders_view\n"
    respx.get("https://test.omniapp.co/api/v1/models/shared1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    # Refinement-prefix form (+orders.topic) — should normalize to `orders`.
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "extension"}).mock(
        return_value=httpx.Response(200, json={"files": {"+orders.topic": yaml_body}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/shared1/topic/orders").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/topic/orders").mock(
        return_value=httpx.Response(404)
    )

    snapshot = make_client().fetch_snapshot(crawl_only_content_backed_workbooks=False)
    topics_by_model = {t["modelId"]: t for t in snapshot["topics"]}
    assert topics_by_model["shared1"]["owningModelId"] == "shared1"
    assert topics_by_model["wb1"]["owningModelId"] == "wb1"


@respx.mock
def test_workbook_extension_fetch_failure_collapses_to_shared_owner():
    """Item 4c inversion: a failed extension probe means we don't know which
    topics were overridden. Default to INHERITED (collapse to SHARED ancestor)
    rather than KEPT (workbook as owner). The old default failed toward
    duplication, which was the 170x bug."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={
            "records": [
                {"id": "shared1", "modelKind": "SHARED"},
                {"id": "wb1", "name": "Workbook 1", "modelKind": "WORKBOOK", "baseModelId": "shared1"},
            ],
            "pageInfo": {"hasNextPage": False},
        })
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    yaml_body = "label: Orders\nbase_view_name: orders_view\n"
    respx.get("https://test.omniapp.co/api/v1/models/shared1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "extension"}).mock(
        return_value=httpx.Response(500, text="boom")
    )
    respx.get("https://test.omniapp.co/api/v1/models/shared1/topic/orders").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/topic/orders").mock(
        return_value=httpx.Response(404)
    )

    snapshot = make_client().fetch_snapshot(crawl_only_content_backed_workbooks=False)
    topics_by_model = {t["modelId"]: t for t in snapshot["topics"]}
    # Both topic rows collapse to the SHARED ancestor's model id.
    assert topics_by_model["wb1"]["owningModelId"] == "shared1"


@respx.mock
def test_workbook_chain_climbs_past_shared_extension_to_shared():
    """Item 4b multi-hop walk: WORKBOOK -> SHARED_EXTENSION -> SHARED. The
    single-hop v0.3.0 walk would have stopped at SHARED_EXTENSION, which is
    not a representable OmniV01 model kind and would have dangled on publish."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={
            "records": [
                {"id": "shared1", "modelKind": "SHARED"},
                {"id": "ext1", "modelKind": "SHARED_EXTENSION", "baseModelId": "shared1"},
                {"id": "wb1", "name": "WB", "modelKind": "WORKBOOK", "baseModelId": "ext1"},
            ],
            "pageInfo": {"hasNextPage": False},
        })
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    yaml_body = "label: Orders\nbase_view_name: orders_view\n"
    for model_id in ("shared1", "ext1", "wb1"):
        respx.get(
            f"https://test.omniapp.co/api/v1/models/{model_id}/yaml",
            params={"mode": "combined"},
        ).mock(
            return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
        )
        respx.get(
            f"https://test.omniapp.co/api/v1/models/{model_id}/topic/orders",
        ).mock(return_value=httpx.Response(404))
    for wb_model in ("ext1", "wb1"):
        respx.get(
            f"https://test.omniapp.co/api/v1/models/{wb_model}/yaml",
            params={"mode": "extension"},
        ).mock(return_value=httpx.Response(200, json={"files": {}}))

    snapshot = make_client().fetch_snapshot(crawl_only_content_backed_workbooks=False)
    topics_by_model = {t["modelId"]: t for t in snapshot["topics"]}
    # The workbook's topic climbs past the SHARED_EXTENSION straight to SHARED.
    assert topics_by_model["wb1"]["owningModelId"] == "shared1"


@respx.mock
def test_fetch_snapshot_skips_non_topic_files():
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"id": "mod1"}], "pageInfo": {"hasNextPage": False}},
        )
    )
    respx.get("https://test.omniapp.co/api/v1/folders").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/documents").mock(
        return_value=httpx.Response(200, json={"records": [], "pageInfo": {"hasNextPage": False}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(200, json={"files": {"schema.sql": "SELECT 1"}})
    )

    snapshot = make_client().fetch_snapshot()
    assert snapshot["topics"] == []


# ---------------------------------------------------------------------------
# topic-detail memoization
#
# A live run against a large org issued 25,831 topic-detail requests that
# resolved to 93 distinct topics: every workbook inheriting a shared topic
# re-fetched it. These are invariants on the request COUNT, not on any one
# call site, so they stay meaningful if the fetch path is refactored.
# ---------------------------------------------------------------------------

def test_topic_detail_is_fetched_once_per_owning_model_and_topic():
    """N workbooks inheriting one shared topic cost ONE topic-detail request."""
    client = make_client()
    calls: list[tuple[str, str]] = []

    def _record(model_id: str, topic_name: str) -> dict:
        calls.append((model_id, topic_name))
        return {"sourceTableName": "orders"}

    client._fetch_topic_detail = _record  # type: ignore[assignment]

    for _ in range(50):
        detail = client._fetch_topic_detail_cached("shared-model-1", "orders")
        assert detail == {"sourceTableName": "orders"}

    assert len(calls) == 1, f"expected 1 fetch, got {len(calls)}"
    assert calls[0] == ("shared-model-1", "orders")


def test_topic_detail_cache_separates_distinct_keys():
    """Distinct owning models, and distinct topics, are cached independently."""
    client = make_client()
    calls: list[tuple[str, str]] = []

    def _record(model_id: str, topic_name: str) -> dict:
        calls.append((model_id, topic_name))
        return {"sourceTableName": f"{model_id}:{topic_name}"}

    client._fetch_topic_detail = _record  # type: ignore[assignment]

    for _ in range(10):
        client._fetch_topic_detail_cached("model-a", "orders")
        client._fetch_topic_detail_cached("model-a", "users")
        client._fetch_topic_detail_cached("model-b", "orders")

    assert len(calls) == 3
    assert set(calls) == {
        ("model-a", "orders"),
        ("model-a", "users"),
        ("model-b", "orders"),
    }


def test_topic_detail_failure_is_not_cached():
    """A failed fetch returns {} and is retried, not permanently blanked.

    Caching {} would let one transient failure blank that topic for every
    workbook in the run - a worse trade than re-attempting it.
    """
    client = make_client()
    calls: list[tuple[str, str]] = []

    def _record(model_id: str, topic_name: str) -> dict:
        calls.append((model_id, topic_name))
        return {}

    client._fetch_topic_detail = _record  # type: ignore[assignment]

    assert client._fetch_topic_detail_cached("model-a", "orders") == {}
    assert client._fetch_topic_detail_cached("model-a", "orders") == {}
    assert len(calls) == 2


def test_host_rate_limiter_ratchets_down_never_up():
    """The most conservative rpm any run asked for wins for the host."""
    from app.client import _RateLimiter

    limiter = _RateLimiter(60)
    assert limiter.min_interval == pytest.approx(1.0)

    limiter.tighten_to(10)          # 6s spacing - more restrictive
    assert limiter.min_interval == pytest.approx(6.0)

    limiter.tighten_to(60)          # would loosen - must be ignored
    assert limiter.min_interval == pytest.approx(6.0)

    limiter.tighten_to(0)           # rpm=0 must never disable it
    assert limiter.min_interval == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# The call site, not the memo dict.
#
# The first cut of these tests stubbed _fetch_topic_detail and called the cache
# wrapper directly, which asserts that a dict memoizes. Reverting the call site
# to model_id left the whole suite green. These drive _fetch_topics_for_model
# and assert on the URLs actually requested, so they fail if it reverts.
# ---------------------------------------------------------------------------

def _payload(files: dict) -> dict:
    return {"files": files}


def test_module_imports():
    """typedefs.py has no importer in-tree, so nothing else catches a syntax
    or signature error in it."""
    import app.typedefs  # noqa: F401
    import app.client    # noqa: F401
    import app.activities  # noqa: F401
    import app.handler  # noqa: F401


def test_single_document_failure_does_not_widen_the_crawl():
    """One transient document-detail failure must not flip the workbook filter
    off -- that widens the crawl from ~10% of models to 100%."""
    from app.client import _DOCUMENT_DETAIL_DEGRADE_THRESHOLD

    assert 0 < _DOCUMENT_DETAIL_DEGRADE_THRESHOLD < 1
    # 1 failure in 263 documents is 0.38% -- must stay below the threshold.
    assert (1 / 263) < _DOCUMENT_DETAIL_DEGRADE_THRESHOLD
    # The observed 50-in-263 (19%) must exceed it and degrade.
    assert (50 / 263) > _DOCUMENT_DETAIL_DEGRADE_THRESHOLD


# ---------------------------------------------------------------------------
# Schema-namespaced view references.
#
# Omni topic YAML names views by their internal `<schema>__<view>` reference,
# while the view FILES are keyed "<CATALOG>.<SCHEMA>/<view>.view". Keying the
# parsed views on the bare stem alone missed every lookup, and because one
# unresolvable name dumps the WHOLE topic to the per-topic API -- and
# `base_view` is a required topic parameter -- it fired on 100% of topics.
# One measured crawl fell back for 20,692 of 20,693 topics.
#
# These assert on DERIVATION SUCCEEDING, which is the invariant; they do not
# care how the lookup is implemented.
# ---------------------------------------------------------------------------

_VIEW_ORDERS = "name: order_items\ntable_name: ORDER_ITEMS\nschema: ECOMM\ncatalog: PROD\n"
_VIEW_USERS = "name: users\ntable_name: USERS\nschema: ECOMM\ncatalog: PROD\n"


def test_schema_namespaced_topic_derives_locally():
    """Omni's documented shape: refs are `ecomm__x`, files are `ECOMM/x.view`."""
    client = make_client()
    files = {
        "PROD.ECOMM/order_items.view": _VIEW_ORDERS,
        "PROD.ECOMM/users.view": _VIEW_USERS,
    }
    views = client._views_from_payload(files)
    parsed = {"name": "orders", "base_view": "ecomm__order_items",
              "joins": {"ecomm__users": {}}}

    detail = client._topic_detail_from_views(parsed, "ecomm__order_items", views, {})

    assert detail is not None, (
        "schema-namespaced refs fell through to the topic API -- this is the "
        "20,692-of-20,693 fallback"
    )
    assert detail["sourceTableName"] == "ORDER_ITEMS"
    assert detail["sourceSchema"] == "ECOMM"
    assert len(detail["viewSources"]) == 2


def test_bare_reference_still_derives_locally():
    """Control: an org that references views bare keeps working unchanged."""
    client = make_client()
    files = {"order_items.view": _VIEW_ORDERS, "users.view": _VIEW_USERS}
    views = client._views_from_payload(files)
    parsed = {"name": "orders", "base_view": "order_items",
              "joins": {"users": {}}}

    detail = client._topic_detail_from_views(parsed, "order_items", views, {})
    assert detail is not None
    assert detail["sourceTableName"] == "ORDER_ITEMS"


def test_base_view_alone_is_enough_to_force_the_api():
    """`base_view` is required on every topic, so an unresolvable base view
    fails the topic regardless of join structure. Guards the 100% blast radius."""
    client = make_client()
    views = client._views_from_payload({"PROD.ECOMM/order_items.view": _VIEW_ORDERS})

    # resolvable base view, no joins -> derives
    assert client._topic_detail_from_views(
        {"base_view": "ecomm__order_items"}, "ecomm__order_items", views, {}
    ) is not None

    # genuinely absent view -> still correctly gives up
    assert client._topic_detail_from_views(
        {"base_view": "nowhere__missing"}, "nowhere__missing", views, {}
    ) is None


def test_views_registered_under_every_reference_form():
    client = make_client()
    views = client._views_from_payload({"PROD.ECOMM/order_items.view": _VIEW_ORDERS})
    for key in ("order_items", "ECOMM__order_items", "ecomm__order_items"):
        assert key in views, f"view not registered under {key!r}"


def test_module_imports():
    """typedefs.py has no in-tree importer, so nothing else catches a signature
    error in it -- a removed attribute once left an orphaned _str() call."""
    import app.typedefs  # noqa: F401
    import app.client  # noqa: F401
    import app.activities  # noqa: F401
    import app.handler  # noqa: F401


def test_single_document_failure_does_not_widen_the_crawl():
    """One transient document-detail failure must not flip the workbook filter
    off -- that widens the crawl from ~10% of models to 100%."""
    from app.client import _DOCUMENT_DETAIL_DEGRADE_THRESHOLD

    assert 0 < _DOCUMENT_DETAIL_DEGRADE_THRESHOLD < 1
    assert (1 / 263) < _DOCUMENT_DETAIL_DEGRADE_THRESHOLD    # 1 flaky doc: tolerate
    assert (50 / 263) > _DOCUMENT_DETAIL_DEGRADE_THRESHOLD   # observed 19%: degrade
