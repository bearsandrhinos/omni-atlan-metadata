"""Omni snapshot -> Atlan entity transformer.

Emits entities aligned with Atlan partner typedef reference v0 (2026-05-15):
- Four concrete types: OmniV01Model, OmniV01Topic, OmniV01Folder, OmniV01Document
- All extend abstract OmniV01 (which extends BI -> Catalog -> Asset)
- Standard Asset.* fields (name, description, sourceURL, sourceUpdatedAt,
  ownerUsers) are populated where Omni exposes the data
- Typed Atlas relationship edges (not string-QN attributes) for model->topic,
  model->baseModel, folder->document, and the built-in Connection edge
- Warehouse->Topic and Topic->Document lineage flow through standard Process
  entities so Atlan's lineage UI/SDK renders them out of the box

The previously-shipped omni_connection, omni_dashboard, and omni_workbook
custom types are retired. The Atlan-side Connection (created out-of-band by
the operator) is referenced via the canonical
default/omni/{connection_epoch_ms} qualifiedName.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Atlan-side enum value sets — defensive normalization for upstream Omni
# strings that may arrive in mixed casing. Values not in these sets are
# dropped rather than emitted as invalid enums. Document type is derived
# deterministically from `hasDashboard`, not normalized from a string.
_MODEL_KINDS = {"SHARED", "WORKBOOK"}
_SCOPES = {"ORGANIZATION", "WORKSPACE", "PRIVATE", "SHARED"}


def _epoch_ms(value: str | None) -> int | None:
    """Convert an ISO-8601 datetime string to epoch milliseconds.

    Atlan's date attributes (e.g. sourceUpdatedAt) are stored as epoch-ms
    integers. Passing an ISO string causes Atlas date validation to reject
    the entity on create.
    """
    if not value:
        return None
    return int(datetime.fromisoformat(value).timestamp() * 1000)


class OmniMetadataTransformer:
    def __init__(
        self,
        connection_epoch_ms: str,
        atlan_source_connection_map: dict[str, str] | None = None,
    ):
        if not connection_epoch_ms or not str(connection_epoch_ms).isdigit():
            raise ValueError(
                "connection_epoch_ms is required and must be a digit string."
            )
        self.connection_epoch_ms = str(connection_epoch_ms)
        self.connection_qn = f"default/omni/{self.connection_epoch_ms}"
        # Omni-connection-id -> Atlan source-connection qualifiedName (e.g.
        # the Snowflake/BigQuery connection that backs an Omni model).
        # Drives source-table -> topic Process emission only.
        self.atlan_source_connection_map = atlan_source_connection_map or {}

    # ------------------------------------------------------------------ #
    # Public entrypoint
    # ------------------------------------------------------------------ #

    def transform(
        self,
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        document_model_ids: list[str] = snapshot.get("document_model_ids", []) or []
        documents = snapshot.get("documents", [])
        connections = snapshot.get("connections", [])
        models = snapshot.get("models", [])
        topics = snapshot.get("topics", [])
        folders = snapshot.get("folders", [])

        # Lookups for source-table lineage resolution.
        model_to_connection: dict[str, str] = {
            m["id"]: m.get("connectionId")
            for m in models
            if m.get("id") and m.get("connectionId")
        }
        connection_to_database: dict[str, str] = {
            c["id"]: c.get("database")
            for c in connections
            if c.get("id") and c.get("database")
        }

        # (raw_modelId, topic_name) -> owningModelId. A workbook that merely
        # inherits a shared-model topic stamps owningModelId=baseModelId so all
        # entity + Process builders resolve to the shared canonical QN. Topics
        # the workbook actually redefines keep owning==modelId (their own).
        # Used by Process builders to canonicalize tile references pulled from
        # document detail (queryPresentations[].modelId).
        topic_owner: dict[tuple[str, str], str] = {
            (row["modelId"], row["name"]): row.get("owningModelId") or row["modelId"]
            for row in topics
            if row.get("modelId") and row.get("name")
        }

        entities: list[dict[str, Any]] = []
        entities.extend(self._models(models, document_model_ids))
        entities.extend(self._topics(topics))
        entities.extend(self._folders(folders))
        entities.extend(self._documents(documents))
        entities.extend(self._processes_topic_to_document(documents, topic_owner))
        entities.extend(
            self._processes_source_to_topic(
                topics, model_to_connection, connection_to_database
            )
        )
        return entities

    # ------------------------------------------------------------------ #
    # Qualified-name + relationship helpers
    # ------------------------------------------------------------------ #

    def _model_qn(self, model_id: str) -> str:
        return f"{self.connection_qn}/model/{model_id}"

    def _topic_qn(self, model_id: str, topic_name: str) -> str:
        return f"{self.connection_qn}/model/{model_id}/topic/{topic_name}"

    def _folder_qn(self, folder_id: str) -> str:
        return f"{self.connection_qn}/folder/{folder_id}"

    def _document_qn(self, identifier: str) -> str:
        return f"{self.connection_qn}/document/{identifier}"

    @staticmethod
    def _rel_ref(type_name: str, qualified_name: str) -> dict[str, Any]:
        return {
            "typeName": type_name,
            "uniqueAttributes": {"qualifiedName": qualified_name},
        }

    @staticmethod
    def _normalize_enum(value: Any, allowed: set[str]) -> str | None:
        if not value:
            return None
        normalized = str(value).strip().upper()
        return normalized if normalized in allowed else None

    @staticmethod
    def _owner_users(owner: dict[str, Any] | None) -> list[str] | None:
        if not owner:
            return None
        # Prefer email/username over display name for Atlan owner refs.
        candidate = owner.get("email") or owner.get("username") or owner.get("name")
        return [candidate] if candidate else None

    # ------------------------------------------------------------------ #
    # Entity builders
    # ------------------------------------------------------------------ #

    def _models(
        self,
        records: list[dict[str, Any]],
        document_model_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        for row in records:
            model_id = row.get("id")
            if not model_id:
                continue
            if row.get("modelKind") == "SCHEMA":
                continue
            # Exclude unnamed WORKBOOK models that aren't backing a document.
            if row.get("modelKind") == "WORKBOOK" and not row.get("name"):
                if document_model_ids is None or model_id not in document_model_ids:
                    continue

            model_kind = self._normalize_enum(row.get("modelKind"), _MODEL_KINDS)
            if not model_kind:
                # The typedef makes omniV01ModelKind required; without a valid
                # value we can't emit a conformant entity.
                continue

            qn = self._model_qn(model_id)
            attrs: dict[str, Any] = {
                "qualifiedName": qn,
                "name": row.get("name") or model_id,
                "connectorName": "omni",
                # Inherited plain-string attribute — access policies key on it.
                # There is no `connection` relationship on OmniV01 types; the
                # Connection linkage is expressed via this attribute alone.
                "connectionQualifiedName": self.connection_qn,
                "omniV01Id": model_id,
                "omniV01ModelKind": model_kind,
                "sourceUpdatedAt": _epoch_ms(row.get("updatedAt")),
            }
            description = row.get("description")
            if description:
                attrs["description"] = description
            # No `scope` on /v1/models records (verified against the live API:
            # baseModelId, connectionId, createdAt, deletedAt, id, modelKind,
            # name, updatedAt — that is the whole record). The read could only
            # ever yield None. Folders and documents keep theirs.
            owner_users = self._owner_users(row.get("owner") or {"name": row.get("ownerName")})
            if owner_users:
                attrs["ownerUsers"] = owner_users

            rel_attrs: dict[str, Any] = {}
            base_model_id = row.get("baseModelId")
            if base_model_id:
                # Relationship name is the typedef-declared key on this side of
                # the edge (omniV01BaseModel, not baseModel — Atlan drops
                # unknown relationship names silently on write).
                rel_attrs["omniV01BaseModel"] = self._rel_ref(
                    "OmniV01Model", self._model_qn(base_model_id)
                )

            entities.append(
                {
                    "typeName": "OmniV01Model",
                    "attributes": attrs,
                    "relationshipAttributes": rel_attrs,
                }
            )
        return entities

    def _topics(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Emit OmniV01Topic entities keyed on the topic's OWNING model.

        A workbook that inherits a shared-model topic without redefining it has
        `owningModelId == baseModelId` (set upstream in client._fetch_topics_for_model).
        All such rows collapse to a single QN; we prefer the row where
        `owningModelId == modelId` (the canonical shared-model source) so the
        Topic's `model` relationship points at the shared model. If the shared
        row is missing (partial fetch), fall back to the first workbook row.
        """
        # Dedup by canonical QN; prefer the row whose owning matches its modelId.
        chosen: dict[str, dict[str, Any]] = {}
        for row in records:
            model_id = row.get("modelId")
            topic_name = row.get("name")
            if not model_id or not topic_name:
                continue
            owning = row.get("owningModelId") or model_id
            qn = self._topic_qn(owning, topic_name)
            existing = chosen.get(qn)
            row_is_canonical = owning == model_id
            existing_is_canonical = existing and existing.get("owningModelId", existing.get("modelId")) == existing.get("modelId")
            if existing is None or (row_is_canonical and not existing_is_canonical):
                chosen[qn] = row

        entities: list[dict[str, Any]] = []
        for qn, row in chosen.items():
            topic_name = row["name"]
            owning = row.get("owningModelId") or row["modelId"]
            attrs: dict[str, Any] = {
                "qualifiedName": qn,
                "name": row.get("label") or topic_name,
                "connectorName": "omni",
                "connectionQualifiedName": self.connection_qn,
                "omniV01Id": topic_name,
                "omniV01BaseViewName": row.get("baseViewName"),
                "sourceUpdatedAt": _epoch_ms(row.get("updatedAt")),
            }
            description = row.get("description")
            if description:
                attrs["description"] = description

            entities.append(
                {
                    "typeName": "OmniV01Topic",
                    "attributes": attrs,
                    "relationshipAttributes": {
                        # omniV01Model, not `model` — the typedef's declared key.
                        "omniV01Model": self._rel_ref(
                            "OmniV01Model", self._model_qn(owning)
                        ),
                    },
                }
            )
        return entities

    def _folders(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        for row in records:
            folder_id = row.get("id")
            if not folder_id:
                continue
            qn = self._folder_qn(folder_id)
            owner = row.get("owner") or {}
            attrs: dict[str, Any] = {
                "qualifiedName": qn,
                "name": row.get("name") or folder_id,
                "connectorName": "omni",
                "connectionQualifiedName": self.connection_qn,
                "omniV01Id": folder_id,
                "omniV01Path": row.get("path"),
            }
            scope = self._normalize_enum(row.get("scope"), _SCOPES)
            if scope:
                attrs["omniV01Scope"] = scope
            owner_users = self._owner_users(
                owner if owner else {"name": row.get("ownerName")}
            )
            if owner_users:
                attrs["ownerUsers"] = owner_users

            # Empty dict, not omitted: without the key the serializer writes
            # `relationshipAttributes: null` and Atlan's calculate-diff step
            # crashes on subsequent runs (PART-1290).
            entities.append(
                {
                    "typeName": "OmniV01Folder",
                    "attributes": attrs,
                    "relationshipAttributes": {},
                }
            )
        return entities

    def _documents(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        for row in records:
            identifier = row.get("identifier")
            if not identifier:
                continue

            doc_type = "DASHBOARD" if row.get("hasDashboard") else "WORKBOOK"
            qn = self._document_qn(identifier)
            owner = row.get("owner") or {}
            folder = row.get("folder") or {}

            attrs: dict[str, Any] = {
                "qualifiedName": qn,
                "name": row.get("name") or identifier,
                "connectorName": "omni",
                "connectionQualifiedName": self.connection_qn,
                "omniV01Id": identifier,
                "omniV01DocumentType": doc_type,
                # omniV01Url is not on the typedef and is dropped; sourceURL
                # (inherited) carries the same value and renders as a link.
                "omniV01FolderPath": folder.get("path"),
                "sourceURL": row.get("url"),
                "sourceUpdatedAt": _epoch_ms(row.get("updatedAt")),
            }
            description = row.get("description")
            if description:
                attrs["description"] = description
            scope = self._normalize_enum(row.get("scope"), _SCOPES)
            if scope:
                attrs["omniV01Scope"] = scope
            owner_users = self._owner_users(
                owner if owner else {"name": row.get("ownerName")}
            )
            if owner_users:
                attrs["ownerUsers"] = owner_users

            rel_attrs: dict[str, Any] = {}
            folder_id = folder.get("id")
            if folder_id:
                # omniV01Folder, not `folder` — the typedef's declared key.
                rel_attrs["omniV01Folder"] = self._rel_ref(
                    "OmniV01Folder", self._folder_qn(folder_id)
                )

            entities.append(
                {
                    "typeName": "OmniV01Document",
                    "attributes": attrs,
                    "relationshipAttributes": rel_attrs,
                }
            )
        return entities

    # ------------------------------------------------------------------ #
    # Process entities (warehouse -> topic, topic -> document)
    # ------------------------------------------------------------------ #

    def _processes_topic_to_document(
        self,
        documents: list[dict[str, Any]],
        topic_owner: dict[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """Emit Process entities for each (topic -> document) lineage edge.

        Topic-to-document edges come from `tileTopics` (deduped upstream in
        client._fetch_document_detail). We canonicalize each tile's raw
        `modelId` to its `owningModelId` via `topic_owner` so an inherited
        workbook topic reuses the shared model's canonical topic QN. One
        Process per unique (owning_model, topic, doc).
        """
        entities: list[dict[str, Any]] = []
        for doc in documents:
            identifier = doc.get("identifier")
            if not identifier:
                continue
            doc_qn = self._document_qn(identifier)
            doc_label = doc.get("name") or identifier
            seen: set[tuple[str, str]] = set()
            for tile in doc.get("tileTopics") or []:
                model_id = tile.get("modelId")
                topic_name = tile.get("topicName")
                if not model_id or not topic_name:
                    continue
                # Canonicalize: prefer the owning model recorded upstream.
                # Fall back to the raw model_id if we didn't enumerate the
                # topic (filtered / snapshot mismatch).
                owning = topic_owner.get((model_id, topic_name), model_id)
                key = (owning, topic_name)
                if key in seen:
                    continue
                seen.add(key)
                topic_qn = self._topic_qn(owning, topic_name)
                process_qn = (
                    f"{self.connection_qn}/process/topic/{owning}/{topic_name}"
                    f"/document/{identifier}"
                )
                entities.append(
                    {
                        "typeName": "Process",
                        "attributes": {
                            "qualifiedName": process_qn,
                            "name": f"{topic_name} -> {doc_label}",
                            "connectorName": "omni",
                        },
                        "relationshipAttributes": {
                            "inputs": [self._rel_ref("OmniV01Topic", topic_qn)],
                            "outputs": [self._rel_ref("OmniV01Document", doc_qn)],
                        },
                    }
                )
        return entities

    def _processes_source_to_topic(
        self,
        topics: list[dict[str, Any]],
        model_to_connection: dict[str, str],
        connection_to_database: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Emit Process entities for each (source-table(s) -> topic) edge.

        Warehouse resolution keys on the ROW's own `modelId` (the workbook or
        shared model that produced the topic) so we resolve the correct
        atlan_source_connection_map entry / fallback database. The topic and
        process QNs, however, are keyed on `owningModelId` — matching the
        canonical OmniV01Topic emitted by `_topics`. Multiple workbook copies
        of the same inherited topic dedup to a single Process.
        """
        if not self.atlan_source_connection_map:
            return []

        entities: list[dict[str, Any]] = []
        seen_process_qns: set[str] = set()
        for row in topics:
            model_id = row.get("modelId")
            topic_name = row.get("name")
            if not model_id or not topic_name:
                continue
            connection_id = model_to_connection.get(model_id)
            if not connection_id:
                continue
            atlan_source_qn = self.atlan_source_connection_map.get(connection_id)
            if not atlan_source_qn:
                continue

            fallback_db = connection_to_database.get(connection_id)
            input_refs: list[dict[str, Any]] = []
            seen_qns: set[str] = set()
            for view in row.get("viewSources") or []:
                table_name = view.get("tableName")
                schema = view.get("schema")
                catalog = view.get("catalog") or fallback_db
                if not table_name or not schema or not catalog:
                    continue
                table_qn = f"{atlan_source_qn}/{catalog}/{schema}/{table_name}"
                if table_qn in seen_qns:
                    continue
                seen_qns.add(table_qn)
                input_refs.append(self._rel_ref("Table", table_qn))

            if not input_refs:
                continue

            owning = row.get("owningModelId") or model_id
            topic_qn = self._topic_qn(owning, topic_name)
            process_qn = (
                f"{self.connection_qn}/process/source/topic/{owning}/{topic_name}"
            )
            if process_qn in seen_process_qns:
                continue
            seen_process_qns.add(process_qn)
            topic_label = row.get("label") or topic_name
            entities.append(
                {
                    "typeName": "Process",
                    "attributes": {
                        "qualifiedName": process_qn,
                        "name": f"sources -> {topic_label}",
                        "connectorName": "omni",
                    },
                    "relationshipAttributes": {
                        "inputs": input_refs,
                        "outputs": [self._rel_ref("OmniV01Topic", topic_qn)],
                    },
                }
            )
        return entities
