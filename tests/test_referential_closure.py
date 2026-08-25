"""Reference-validity contract for the transformer's output.

Every internal reference emitted by the transformer must point at a
qualifiedName the same run also emits, or Atlan's publish step raises
ATLAS-404-00-00A and fails the entire publish (one rejected record
poisons the whole batch of ~13k assets).

This file is the gate for that invariant. It was authored by Atlan's
partner-engineering review; do not weaken the checks without discussing
with them first — the four cases below map to real publish failures
observed on live tenants.
"""

from typing import Any, Iterator

import pytest

from app.transformer import OmniMetadataTransformer

EPOCH = "1747156800000"
CONN_QN = f"default/omni/{EPOCH}"
SOURCE_CONN_QN = "default/snowflake/1700000000"

# Reference types that legitimately live outside this payload.
EXTERNAL_TYPES = frozenset({"Connection", "Table"})


# --------------------------------------------------------------------------
# The invariant, expressed once and reused by every case.
# --------------------------------------------------------------------------

def _referenced(entity: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield (relationship_name, referenced_typeName, referenced_qualifiedName).

    Covers both shapes the transformer emits: a single ref dict, and a list of
    ref dicts (Process `inputs` / `outputs`). Yields refs with a missing or empty
    qualifiedName as well — a malformed ref is a defect, not something to skip.
    """
    for rel_name, ref in (entity.get("relationshipAttributes") or {}).items():
        for candidate in (ref if isinstance(ref, list) else [ref]):
            if not isinstance(candidate, dict):
                continue
            yield (
                rel_name,
                str(candidate.get("typeName") or "<no-typeName>"),
                str((candidate.get("uniqueAttributes") or {}).get("qualifiedName") or ""),
            )


def assert_referentially_closed(entities: list[dict[str, Any]]) -> None:
    """Fail with a readable report if any internal reference dangles."""
    emitted = {
        str(e["attributes"]["qualifiedName"])
        for e in entities
        if (e.get("attributes") or {}).get("qualifiedName")
    }

    problems: list[str] = []
    for entity in entities:
        source_qn = (entity.get("attributes") or {}).get("qualifiedName", "<no-qn>")
        for rel_name, type_name, target_qn in _referenced(entity):
            if not target_qn:
                problems.append(
                    f"  {entity.get('typeName')} {source_qn}\n"
                    f"    --{rel_name}--> {type_name} with EMPTY qualifiedName"
                )
                continue
            if type_name in EXTERNAL_TYPES:
                continue
            if target_qn not in emitted:
                problems.append(
                    f"  {entity.get('typeName')} {source_qn}\n"
                    f"    --{rel_name}--> {type_name} {target_qn}   (never emitted)"
                )

    if problems:
        raise AssertionError(
            f"{len(problems)} dangling reference(s) — each is an ATLAS-404 on "
            f"publish, and one rejected record fails the entire publish:\n"
            + "\n".join(problems)
        )


def assert_no_duplicate_qualified_names(entities: list[dict[str, Any]]) -> None:
    """Two entities sharing a qualifiedName is an ambiguous upsert."""
    seen: dict[str, dict[str, Any]] = {}
    clashes: list[str] = []
    for entity in entities:
        qn = (entity.get("attributes") or {}).get("qualifiedName")
        if not qn:
            continue
        prior = seen.get(qn)
        if prior is None:
            seen[qn] = entity
            continue
        if prior.get("attributes") != entity.get("attributes"):
            clashes.append(
                f"  {qn}\n"
                f"    first: {prior.get('attributes')}\n"
                f"    again: {entity.get('attributes')}"
            )
    if clashes:
        raise AssertionError(
            f"{len(clashes)} qualifiedName(s) emitted twice with conflicting "
            f"attributes — the later write silently wins:\n" + "\n".join(clashes)
        )


def _transform(snapshot: dict[str, Any], source_map: dict[str, str] | None = None):
    return OmniMetadataTransformer(
        connection_epoch_ms=EPOCH,
        atlan_source_connection_map=source_map or {},
    ).transform(snapshot=snapshot)


# --------------------------------------------------------------------------
# Fixture helpers — shapes match what client.fetch_snapshot actually returns.
# Note `folder` is a NESTED dict on a document, not a flat `folderId`.
# --------------------------------------------------------------------------

def _snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connections": [{"id": "conn1", "name": "Snowflake", "dialect": "snowflake"}],
        "models": [],
        "topics": [],
        "folders": [],
        "documents": [],
        "document_model_ids": [],
    }
    base.update(overrides)
    return base


SHARED_MODEL = {
    "id": "shared1",
    "name": "Sales",
    "modelKind": "SHARED",
    "connectionId": "conn1",
    "baseModelId": None,
}


# --------------------------------------------------------------------------
# Case 1 — baseline. Exercises every reference class, including the folder edge
# and warehouse lineage, so it is a real reference point for the others.
# --------------------------------------------------------------------------

def test_baseline_snapshot_is_referentially_closed():
    snapshot = _snapshot(
        models=[SHARED_MODEL],
        topics=[
            {
                "modelId": "shared1",
                "owningModelId": "shared1",
                "name": "orders",
                "baseViewName": "orders_view",
                "viewSources": [
                    {
                        "viewName": "orders_view",
                        "tableName": "orders",
                        "schema": "public",
                        "catalog": "analytics",
                    }
                ],
            }
        ],
        folders=[{"id": "fold1", "name": "Marketing", "path": "Acme/Marketing"}],
        documents=[
            {
                "identifier": "doc1",
                "name": "Revenue",
                "folder": {"id": "fold1", "path": "Acme/Marketing"},
                "hasDashboard": True,
                "tileTopics": [{"modelId": "shared1", "topicName": "orders"}],
            }
        ],
        document_model_ids=["shared1"],
    )
    entities = _transform(snapshot, source_map={"conn1": SOURCE_CONN_QN})

    # Guard against a vacuous pass: the baseline must actually emit references,
    # including a folder edge and a warehouse Table edge. Relationship key is
    # `omniV01Folder` (the typedef-declared name); `folder` was the pre-fix
    # name that Atlan silently dropped on write.
    rels = [r for e in entities for r in _referenced(e)]
    assert any(rel == "omniV01Folder" for rel, _, _ in rels), "baseline emits no folder edge"
    assert any(t == "Table" for _, t, _ in rels), "baseline emits no warehouse edge"

    assert_referentially_closed(entities)
    assert_no_duplicate_qualified_names(entities)


# --------------------------------------------------------------------------
# Case 2 — a topic owned by a model the transformer filters out.
#
# Only two distinct transformer branches exist: SCHEMA is dropped by an explicit
# check, and any modelKind outside {SHARED, WORKBOOK} is dropped by the enum
# guard. One representative of each; the kind names come from the Omni API review
# from the Omni API model-kind list.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model_kind", ["SCHEMA", "SHARED_EXTENSION"])
def test_topic_owned_by_filtered_model_kind(model_kind):
    snapshot = _snapshot(
        models=[{**SHARED_MODEL, "id": "other1", "modelKind": model_kind}],
        topics=[{"modelId": "other1", "owningModelId": "other1", "name": "orders"}],
    )
    assert_referentially_closed(_transform(snapshot))


# --------------------------------------------------------------------------
# Case 3 — a SHARED model whose base is a SCHEMA. The ordinary Omni shape:
# SHARED models sit on a SCHEMA base, and SCHEMA models are filtered out.
# --------------------------------------------------------------------------

def test_shared_model_with_schema_base():
    snapshot = _snapshot(
        models=[
            {"id": "schema1", "name": "Raw", "modelKind": "SCHEMA", "connectionId": "conn1"},
            {**SHARED_MODEL, "baseModelId": "schema1"},
        ]
    )
    assert_referentially_closed(_transform(snapshot))


# --------------------------------------------------------------------------
# Case 4 — a dashboard tile naming a topic the crawl never produced. This is the
# exact shape that failed the real publish: the lookup misses and the Process
# builder falls back to the raw model id.
# --------------------------------------------------------------------------

def test_dashboard_tile_referencing_uncrawled_topic():
    snapshot = _snapshot(
        models=[SHARED_MODEL],
        topics=[],  # the topic pass produced nothing
        folders=[{"id": "fold1", "name": "Marketing", "path": "Acme/Marketing"}],
        documents=[
            {
                "identifier": "doc1",
                "name": "Revenue",
                "folder": {"id": "fold1", "path": "Acme/Marketing"},
                "hasDashboard": True,
                "tileTopics": [{"modelId": "shared1", "topicName": "never_crawled"}],
            }
        ],
        document_model_ids=["shared1"],
    )
    assert_referentially_closed(_transform(snapshot))
