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
    """A failed YAML call for one model does not abort the rest of the batch."""
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
    respx.get("https://test.omniapp.co/api/v1/models/mod1/yaml").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    yaml2 = "label: Customers\nbase_view_name: customers_view\n"
    respx.get("https://test.omniapp.co/api/v1/models/mod2/yaml").mock(
        return_value=httpx.Response(200, json={"files": {"customers.topic": yaml2}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/mod2/topic/customers").mock(
        return_value=httpx.Response(404)
    )

    snapshot = make_client().fetch_snapshot()
    assert len(snapshot["topics"]) == 1
    assert snapshot["topics"][0]["modelId"] == "mod2"


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

    snapshot = make_client().fetch_snapshot()
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

    snapshot = make_client().fetch_snapshot()
    topics_by_model = {t["modelId"]: t for t in snapshot["topics"]}
    assert topics_by_model["shared1"]["owningModelId"] == "shared1"
    assert topics_by_model["wb1"]["owningModelId"] == "wb1"


@respx.mock
def test_workbook_extension_fetch_failure_keeps_workbook_owning():
    """If the extension YAML fetch fails, we can't tell inherited from overridden —
    fall back to owning==modelId (no canonicalization) to avoid dropping data."""
    respx.get("https://test.omniapp.co/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"connections": []})
    )
    respx.get("https://test.omniapp.co/api/v1/models").mock(
        return_value=httpx.Response(200, json={
            "records": [
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
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "combined"}).mock(
        return_value=httpx.Response(200, json={"files": {"orders.topic": yaml_body}})
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/yaml", params={"mode": "extension"}).mock(
        return_value=httpx.Response(500, text="boom")
    )
    respx.get("https://test.omniapp.co/api/v1/models/wb1/topic/orders").mock(
        return_value=httpx.Response(404)
    )

    snapshot = make_client().fetch_snapshot()
    assert snapshot["topics"][0]["owningModelId"] == "wb1"


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
