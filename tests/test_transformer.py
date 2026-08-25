"""Tests for app/transformer.py — OmniV01* typedef alignment."""

import pytest

from app.transformer import OmniMetadataTransformer

EPOCH = "1747156800000"
CONN_QN = f"default/omni/{EPOCH}"

SNAPSHOT = {
    "connections": [
        {"id": "conn1", "name": "Snowflake", "dialect": "snowflake", "database": "analytics"},
    ],
    "models": [
        {
            "id": "mod1",
            "name": "Sales Model",
            "description": "All revenue topics",
            "modelKind": "SHARED",
            "connectionId": "conn1",
            "baseModelId": None,
            "updatedAt": "2024-01-01T00:00:00+00:00",
            "scope": "ORGANIZATION",
            "ownerName": "Alice Builder",
        },
        {
            "id": "mod2",
            "name": "Derived",
            "modelKind": "WORKBOOK",
            "connectionId": "conn1",
            "baseModelId": "mod1",
            "updatedAt": None,
        },
        # SCHEMA models are filtered out entirely.
        {"id": "mod3", "name": "Raw", "modelKind": "SCHEMA", "connectionId": "conn1"},
    ],
    "topics": [
        {
            "modelId": "mod1",
            "name": "orders",
            "label": "Orders",
            "baseViewName": "orders_view",
            "viewSources": [
                {"viewName": "orders_view", "tableName": "orders", "schema": "public", "catalog": "analytics"},
                {"viewName": "customers_view", "tableName": "customers", "schema": "public", "catalog": "analytics"},
                {"viewName": "products_view", "tableName": "products", "schema": "public", "catalog": None},
            ],
        },
        {"modelId": "mod1", "name": "customers", "label": None, "baseViewName": "customers_view"},
    ],
    "folders": [
        {
            "id": "fold1",
            "name": "Marketing",
            "path": "Acme/Marketing",
            "scope": "ORGANIZATION",
            "owner": {"id": "u1", "email": "alice@example.com", "name": "Alice"},
        },
        {"id": "fold2", "name": "Finance", "path": "Acme/Finance", "scope": "garbage-scope", "owner": None},
    ],
    "documents": [
        {
            "identifier": "doc1",
            "name": "Revenue Dashboard",
            "hasDashboard": True,
            "scope": "ORGANIZATION",
            "url": "https://app.omni.co/doc1",
            "updatedAt": "2024-06-01T00:00:00+00:00",
            "type": "WORKBOOK",
            "owner": {"id": "u2", "email": "bob@example.com", "name": "Bob"},
            "folder": {"id": "fold1", "path": "Acme/Marketing"},
            "tileTopics": [
                {"modelId": "mod1", "topicName": "orders"},
                {"modelId": "mod1", "topicName": "customers"},
                {"modelId": "mod1", "topicName": "orders"},  # duplicate -> deduped
            ],
        },
        {
            "identifier": "doc2",
            "name": "Data Workbook",
            "hasDashboard": False,
            "url": None,
            "updatedAt": None,
            "type": "WORKBOOK",
            "owner": None,
            "folder": None,
            "tileTopics": [],
        },
    ],
    "document_model_ids": [],
}


def transform(
    atlan_source_connection_map: dict[str, str] | None = None,
) -> list[dict]:
    t = OmniMetadataTransformer(
        connection_epoch_ms=EPOCH,
        atlan_source_connection_map=atlan_source_connection_map,
    )
    return t.transform(SNAPSHOT)


# ---------------------------------------------------------------------------
# Counts + type names
# ---------------------------------------------------------------------------

def test_type_name_counts():
    by_type: dict[str, int] = {}
    for e in transform():
        by_type[e["typeName"]] = by_type.get(e["typeName"], 0) + 1
    # SCHEMA model is filtered; no omni_connection / omni_dashboard / omni_workbook.
    assert by_type["OmniV01Model"] == 2
    assert by_type["OmniV01Topic"] == 2
    assert by_type["OmniV01Folder"] == 2
    assert by_type["OmniV01Document"] == 2
    assert by_type["Process"] == 2  # two unique (topic, doc) pairs on doc1
    assert "omni_connection" not in by_type
    assert "omni_dashboard" not in by_type
    assert "omni_workbook" not in by_type


def test_no_connection_entity_emitted():
    """omni_connection retired; connector references built-in Connection via relationship edge."""
    assert not any(e["typeName"] == "Connection" for e in transform())


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

def test_constructor_rejects_empty_epoch():
    with pytest.raises(ValueError):
        OmniMetadataTransformer(connection_epoch_ms="")


def test_constructor_rejects_non_digit_epoch():
    with pytest.raises(ValueError):
        OmniMetadataTransformer(connection_epoch_ms="not-a-number")


# ---------------------------------------------------------------------------
# Qualified names use the default/omni/{epoch}/... pattern
# ---------------------------------------------------------------------------

def test_model_qualified_name():
    mod = next(e for e in transform() if e["typeName"] == "OmniV01Model" and e["attributes"]["omniV01Id"] == "mod1")
    assert mod["attributes"]["qualifiedName"] == f"{CONN_QN}/model/mod1"


def test_topic_qualified_name():
    orders = next(
        e for e in transform()
        if e["typeName"] == "OmniV01Topic" and e["attributes"]["omniV01Id"] == "orders"
    )
    assert orders["attributes"]["qualifiedName"] == f"{CONN_QN}/model/mod1/topic/orders"


def test_folder_qualified_name():
    f = next(e for e in transform() if e["typeName"] == "OmniV01Folder" and e["attributes"]["omniV01Id"] == "fold1")
    assert f["attributes"]["qualifiedName"] == f"{CONN_QN}/folder/fold1"


def test_document_qualified_name():
    d = next(e for e in transform() if e["typeName"] == "OmniV01Document" and e["attributes"]["omniV01Id"] == "doc1")
    assert d["attributes"]["qualifiedName"] == f"{CONN_QN}/document/doc1"


# ---------------------------------------------------------------------------
# Standard Asset.* fields + custom omniV01* attrs
# ---------------------------------------------------------------------------

def test_model_attributes_map_to_standard_asset_fields():
    mod = next(e for e in transform() if e["attributes"].get("omniV01Id") == "mod1")
    attrs = mod["attributes"]
    assert attrs["name"] == "Sales Model"
    assert attrs["description"] == "All revenue topics"
    assert attrs["connectorName"] == "omni"
    assert attrs["sourceUpdatedAt"] == 1704067200000
    assert attrs["omniV01ModelKind"] == "SHARED"
    assert "omniV01Scope" not in attrs  # /v1/models records carry no `scope`


def test_document_uses_source_url_and_source_updated_at():
    d = next(e for e in transform() if e["attributes"].get("omniV01Id") == "doc1")
    assert d["attributes"]["sourceURL"] == "https://app.omni.co/doc1"
    assert d["attributes"]["sourceUpdatedAt"] == 1717200000000
    # omniV01Url was a duplicate of sourceURL; not on the typedef and dropped.
    assert "omniV01Url" not in d["attributes"]


def test_dashboard_discriminator():
    d = next(e for e in transform() if e["attributes"].get("omniV01Id") == "doc1")
    assert d["attributes"]["omniV01DocumentType"] == "DASHBOARD"


def test_workbook_discriminator():
    d = next(e for e in transform() if e["attributes"].get("omniV01Id") == "doc2")
    assert d["attributes"]["omniV01DocumentType"] == "WORKBOOK"


def test_folder_path_and_owner():
    f = next(e for e in transform() if e["attributes"].get("omniV01Id") == "fold1")
    assert f["attributes"]["omniV01Path"] == "Acme/Marketing"
    assert f["attributes"]["ownerUsers"] == ["alice@example.com"]


def test_topic_does_not_carry_source_table_attrs():
    """Per typedef ref §5.3: warehouse lineage lives on Process, not on Topic."""
    orders = next(
        e for e in transform()
        if e["typeName"] == "OmniV01Topic" and e["attributes"]["omniV01Id"] == "orders"
    )
    attrs = orders["attributes"]
    assert "sourceTableName" not in attrs
    assert "sourceSchema" not in attrs
    assert "sourceCatalog" not in attrs


def test_document_does_not_carry_topic_qualified_names():
    """Per typedef ref §5.5: topic->document lineage lives on Process, not on Document."""
    d = next(e for e in transform() if e["attributes"].get("omniV01Id") == "doc1")
    assert "topicQualifiedNames" not in d["attributes"]


def test_legacy_custom_sync_attrs_not_emitted():
    """Custom last_sync_* triple retired; Atlan handles sync tracking via standard Asset.*"""
    for e in transform():
        attrs = e["attributes"]
        assert "last_sync_workflow_name" not in attrs
        assert "last_sync_run" not in attrs
        assert "connector_name" not in attrs


# ---------------------------------------------------------------------------
# Enum normalization
# ---------------------------------------------------------------------------

def test_invalid_model_kind_drops_entity():
    snap = {
        "connections": [],
        "models": [{"id": "m1", "name": "M", "modelKind": "garbage", "connectionId": None}],
        "topics": [],
        "folders": [],
        "documents": [],
        "document_model_ids": [],
    }
    out = OmniMetadataTransformer(connection_epoch_ms=EPOCH).transform(snap)
    assert not any(e["typeName"] == "OmniV01Model" for e in out)


def test_invalid_scope_dropped_silently():
    """Invalid scopes are stripped from attrs; the rest of the entity is still emitted."""
    f = next(e for e in transform() if e["attributes"].get("omniV01Id") == "fold2")
    assert "omniV01Scope" not in f["attributes"]


def test_scope_case_normalized():
    f = next(e for e in transform() if e["attributes"].get("omniV01Id") == "fold1")
    assert f["attributes"]["omniV01Scope"] == "ORGANIZATION"


# ---------------------------------------------------------------------------
# Relationships (typed Atlas edges)
# ---------------------------------------------------------------------------

def test_all_entities_carry_connection_qualified_name():
    """`connectionQualifiedName` (inherited plain-string attr) is what Atlan
    uses for access policies and the connection facet in discovery. There is
    no `connection` relationship on OmniV01 types — writes with that name are
    silently dropped."""
    for e in transform():
        if e["typeName"] == "Process":
            continue  # Process has its own I/O edges, not the connection attr
        assert e["attributes"].get("connectionQualifiedName") == CONN_QN, (
            f"{e['typeName']} {e['attributes'].get('qualifiedName')} missing "
            f"connectionQualifiedName"
        )


def test_no_entity_writes_connection_relationship():
    """Regression fence: `connection` was written at three sites and dropped
    silently on write. The attribute above is the only correct mechanism."""
    for e in transform():
        rels = e.get("relationshipAttributes") or {}
        assert "connection" not in rels, (
            f"{e['typeName']} {e['attributes'].get('qualifiedName')} still "
            f"writes a `connection` relationship (Atlan drops it silently)"
        )


def test_derived_model_has_omniV01BaseModel_relationship():
    """Typedef-declared key is omniV01BaseModel, not baseModel."""
    derived = next(e for e in transform() if e["attributes"].get("omniV01Id") == "mod2")
    rel = derived["relationshipAttributes"]["omniV01BaseModel"]
    assert rel == {
        "typeName": "OmniV01Model",
        "uniqueAttributes": {"qualifiedName": f"{CONN_QN}/model/mod1"},
    }
    # Guard the old key too — Atlan drops it silently, so a regression here
    # would be invisible in production.
    assert "baseModel" not in derived["relationshipAttributes"]


def test_base_model_has_no_baseModel_relationship():
    base = next(e for e in transform() if e["attributes"].get("omniV01Id") == "mod1")
    rels = base["relationshipAttributes"]
    assert "omniV01BaseModel" not in rels
    assert "baseModel" not in rels


def test_topic_has_omniV01Model_relationship():
    """Typedef-declared key is omniV01Model, not model."""
    orders = next(
        e for e in transform()
        if e["typeName"] == "OmniV01Topic" and e["attributes"]["omniV01Id"] == "orders"
    )
    rel = orders["relationshipAttributes"]["omniV01Model"]
    assert rel == {
        "typeName": "OmniV01Model",
        "uniqueAttributes": {"qualifiedName": f"{CONN_QN}/model/mod1"},
    }
    assert "model" not in orders["relationshipAttributes"]


def test_folder_relationship_attributes_is_empty_dict_not_null():
    """PART-1290: folders must emit an empty `relationshipAttributes` dict
    (not omit the key) so the serializer doesn't write null and crash Atlan's
    calculate-diff step on re-runs. Folders have no relationships on the type."""
    f = next(e for e in transform() if e["attributes"].get("omniV01Id") == "fold1")
    assert "relationshipAttributes" in f
    assert f["relationshipAttributes"] == {}


def test_document_has_omniV01Folder_relationship():
    """Typedef-declared key is omniV01Folder, not folder."""
    d = next(e for e in transform() if e["attributes"].get("omniV01Id") == "doc1")
    rels = d["relationshipAttributes"]
    assert rels["omniV01Folder"] == {
        "typeName": "OmniV01Folder",
        "uniqueAttributes": {"qualifiedName": f"{CONN_QN}/folder/fold1"},
    }
    assert "folder" not in rels


def test_document_without_folder_has_empty_relationships():
    """Document with no folder emits `relationshipAttributes: {}` — no
    `connection` relationship, no dangling omniV01Folder key."""
    d = next(e for e in transform() if e["attributes"].get("omniV01Id") == "doc2")
    assert d["relationshipAttributes"] == {}


# ---------------------------------------------------------------------------
# Topic -> Document Process lineage
# ---------------------------------------------------------------------------

def test_topic_document_process_deduped():
    processes = [e for e in transform() if e["typeName"] == "Process"]
    qns = {p["attributes"]["qualifiedName"] for p in processes}
    assert f"{CONN_QN}/process/topic/mod1/orders/document/doc1" in qns
    assert f"{CONN_QN}/process/topic/mod1/customers/document/doc1" in qns


def test_topic_document_process_io_types():
    process = next(
        e for e in transform()
        if e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"] == f"{CONN_QN}/process/topic/mod1/orders/document/doc1"
    )
    rel = process["relationshipAttributes"]
    assert rel["inputs"] == [{
        "typeName": "OmniV01Topic",
        "uniqueAttributes": {"qualifiedName": f"{CONN_QN}/model/mod1/topic/orders"},
    }]
    assert rel["outputs"] == [{
        "typeName": "OmniV01Document",
        "uniqueAttributes": {"qualifiedName": f"{CONN_QN}/document/doc1"},
    }]


def test_workbook_with_no_tile_topics_emits_no_process():
    processes = [
        e for e in transform()
        if e["typeName"] == "Process"
        and e["relationshipAttributes"]["outputs"][0]["uniqueAttributes"]["qualifiedName"]
        == f"{CONN_QN}/document/doc2"
    ]
    assert processes == []


def test_workbook_process_outputs_document_typename():
    """Both DASHBOARD and WORKBOOK collapsed to OmniV01Document — Process I/O typeName is uniform."""
    snapshot = dict(SNAPSHOT)
    snapshot["documents"] = [
        {
            "identifier": "wb1",
            "name": "Some Workbook",
            "hasDashboard": False,
            "tileTopics": [{"modelId": "mod1", "topicName": "orders"}],
        }
    ]
    t = OmniMetadataTransformer(connection_epoch_ms=EPOCH)
    result = t.transform(snapshot)
    process = next(e for e in result if e["typeName"] == "Process")
    assert process["relationshipAttributes"]["outputs"][0]["typeName"] == "OmniV01Document"


# ---------------------------------------------------------------------------
# Source-Table -> Topic Process lineage
# ---------------------------------------------------------------------------

SOURCE_MAP = {"conn1": "default/snowflake/1700000000"}


def test_no_source_processes_when_map_missing():
    assert not any(
        e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"].startswith(f"{CONN_QN}/process/source/")
        for e in transform()
    )


def test_source_to_topic_process_emitted():
    entities = transform(atlan_source_connection_map=SOURCE_MAP)
    sources = [
        e for e in entities
        if e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"].startswith(f"{CONN_QN}/process/source/")
    ]
    assert len(sources) == 1
    p = sources[0]
    assert p["attributes"]["qualifiedName"] == f"{CONN_QN}/process/source/topic/mod1/orders"
    assert p["attributes"]["connectorName"] == "omni"
    assert p["attributes"]["name"] == "sources -> Orders"


def test_source_process_inputs_use_table_typename():
    entities = transform(atlan_source_connection_map=SOURCE_MAP)
    p = next(
        e for e in entities
        if e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"] == f"{CONN_QN}/process/source/topic/mod1/orders"
    )
    inputs = p["relationshipAttributes"]["inputs"]
    assert all(i["typeName"] == "Table" for i in inputs)
    qns = {i["uniqueAttributes"]["qualifiedName"] for i in inputs}
    assert qns == {
        "default/snowflake/1700000000/analytics/public/orders",
        "default/snowflake/1700000000/analytics/public/customers",
        # products_view's catalog is None → falls back to conn1.database = "analytics".
        "default/snowflake/1700000000/analytics/public/products",
    }


def test_source_process_output_is_omni_v01_topic():
    entities = transform(atlan_source_connection_map=SOURCE_MAP)
    p = next(
        e for e in entities
        if e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"] == f"{CONN_QN}/process/source/topic/mod1/orders"
    )
    assert p["relationshipAttributes"]["outputs"] == [{
        "typeName": "OmniV01Topic",
        "uniqueAttributes": {"qualifiedName": f"{CONN_QN}/model/mod1/topic/orders"},
    }]


def test_source_process_skipped_when_connection_not_in_map():
    entities = transform(atlan_source_connection_map={"some-other-conn": "default/redshift/x"})
    assert not any(
        e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"].startswith(f"{CONN_QN}/process/source/")
        for e in entities
    )


def test_source_process_skipped_when_view_missing_schema_or_catalog():
    snap = {
        "connections": [{"id": "c1", "name": "C", "database": None}],
        "models": [{"id": "m1", "name": "M", "modelKind": "SHARED", "connectionId": "c1"}],
        "topics": [{
            "modelId": "m1",
            "name": "t1",
            "label": "T1",
            "baseViewName": "v1",
            "viewSources": [{"viewName": "v1", "tableName": "t", "schema": None, "catalog": None}],
        }],
        "folders": [],
        "documents": [],
        "document_model_ids": [],
    }
    t = OmniMetadataTransformer(
        connection_epoch_ms=EPOCH,
        atlan_source_connection_map={"c1": "default/postgres/1"},
    )
    result = t.transform(snap)
    assert not any(
        e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"].startswith(f"{CONN_QN}/process/source/")
        for e in result
    )


# ---------------------------------------------------------------------------
# Workbook / shared-model topic canonicalization (PART-1355)
# ---------------------------------------------------------------------------

def _wb_snapshot(overridden: bool) -> dict:
    """Shared model + workbook that either inherits or overrides `orders`."""
    return {
        "connections": [{"id": "c1", "name": "SF", "database": "analytics"}],
        "models": [
            {"id": "shared1", "name": "Shared", "modelKind": "SHARED", "connectionId": "c1"},
            {"id": "wb1", "name": "WB", "modelKind": "WORKBOOK", "connectionId": "c1",
             "baseModelId": "shared1"},
        ],
        "topics": [
            {
                "modelId": "shared1", "owningModelId": "shared1",
                "name": "orders", "label": "Orders",
                "viewSources": [{"viewName": "orders_view", "tableName": "orders",
                                 "schema": "public", "catalog": "analytics"}],
            },
            {
                "modelId": "wb1",
                "owningModelId": "wb1" if overridden else "shared1",
                "name": "orders",
                "label": "Orders (WB override)" if overridden else "Orders",
                "viewSources": [{"viewName": "orders_view", "tableName": "orders",
                                 "schema": "public", "catalog": "analytics"}],
            },
        ],
        "folders": [],
        "documents": [
            {"identifier": "doc1", "name": "Rev", "hasDashboard": True,
             "tileTopics": [{"modelId": "wb1", "topicName": "orders"}]},
        ],
        "document_model_ids": [],
    }


def test_workbook_inherited_topic_deduped_to_shared_qn():
    """Inherited workbook topic + its shared parent should collapse to one
    OmniV01Topic under the shared model's QN."""
    result = OmniMetadataTransformer(connection_epoch_ms=EPOCH).transform(_wb_snapshot(overridden=False))
    topics = [e for e in result if e["typeName"] == "OmniV01Topic"]
    assert len(topics) == 1
    assert topics[0]["attributes"]["qualifiedName"] == f"{CONN_QN}/model/shared1/topic/orders"
    # omniV01Model relationship points at the shared model, not the workbook.
    assert topics[0]["relationshipAttributes"]["omniV01Model"]["uniqueAttributes"]["qualifiedName"] \
        == f"{CONN_QN}/model/shared1"


def test_workbook_overridden_topic_kept_separate():
    """An actually-overridden workbook topic keeps its own QN — no dedup."""
    result = OmniMetadataTransformer(connection_epoch_ms=EPOCH).transform(_wb_snapshot(overridden=True))
    topic_qns = {
        e["attributes"]["qualifiedName"]
        for e in result if e["typeName"] == "OmniV01Topic"
    }
    assert topic_qns == {
        f"{CONN_QN}/model/shared1/topic/orders",
        f"{CONN_QN}/model/wb1/topic/orders",
    }


def test_tile_topic_from_workbook_canonicalizes_to_shared_process():
    """A tile whose modelId is the workbook must have its Process point at the
    shared-model topic QN when the topic is inherited (not overridden)."""
    result = OmniMetadataTransformer(connection_epoch_ms=EPOCH).transform(_wb_snapshot(overridden=False))
    processes = [
        e for e in result if e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"].startswith(f"{CONN_QN}/process/topic/")
    ]
    assert len(processes) == 1
    p = processes[0]
    assert p["attributes"]["qualifiedName"] == f"{CONN_QN}/process/topic/shared1/orders/document/doc1"
    assert p["relationshipAttributes"]["inputs"][0]["uniqueAttributes"]["qualifiedName"] \
        == f"{CONN_QN}/model/shared1/topic/orders"


def test_source_process_deduped_to_shared_topic():
    """Warehouse -> topic Process emitted once per (owning_model, topic), not per
    workbook copy."""
    result = OmniMetadataTransformer(
        connection_epoch_ms=EPOCH,
        atlan_source_connection_map={"c1": "default/snowflake/1700"},
    ).transform(_wb_snapshot(overridden=False))
    source_procs = [
        e for e in result if e["typeName"] == "Process"
        and e["attributes"]["qualifiedName"].startswith(f"{CONN_QN}/process/source/")
    ]
    assert len(source_procs) == 1
    assert source_procs[0]["attributes"]["qualifiedName"] \
        == f"{CONN_QN}/process/source/topic/shared1/orders"
    assert source_procs[0]["relationshipAttributes"]["outputs"][0]["uniqueAttributes"]["qualifiedName"] \
        == f"{CONN_QN}/model/shared1/topic/orders"


def test_tile_topic_uncatalogued_falls_back_to_raw_model_id():
    """If the tile references a topic we never enumerated (filtered/absent from
    the snapshot), the Process still emits using the raw model_id — no crash."""
    snap = {
        "connections": [],
        "models": [{"id": "wb1", "name": "WB", "modelKind": "SHARED"}],
        "topics": [],  # no topic enumerated
        "folders": [],
        "documents": [
            {"identifier": "d1", "name": "D", "hasDashboard": True,
             "tileTopics": [{"modelId": "wb1", "topicName": "unknown"}]},
        ],
        "document_model_ids": [],
    }
    result = OmniMetadataTransformer(connection_epoch_ms=EPOCH).transform(snap)
    procs = [e for e in result if e["typeName"] == "Process"]
    assert len(procs) == 1
    assert procs[0]["attributes"]["qualifiedName"] \
        == f"{CONN_QN}/process/topic/wb1/unknown/document/d1"
