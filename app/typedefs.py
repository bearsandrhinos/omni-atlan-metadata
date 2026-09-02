"""Omni connector custom typedefs.

Aligned with Atlan partner typedef reference v0 (2026-05-15). The connector
emits four concrete entity types — OmniV01Model, OmniV01Topic, OmniV01Folder,
OmniV01Document — all extending an abstract OmniV01 supertype that itself
extends Atlan's built-in BI supertype (Referenceable -> Asset -> Catalog ->
BI -> OmniV01).

Atlan has already seeded these typedefs on the marketplace-partner canary;
the registration call below remains as the create-if-missing path for fresh
tenants.

Notes:
- The previously-shipped omni_connection, omni_dashboard, and omni_workbook
  custom types are retired. Connection uses the built-in Connection typedef
  with connectorName="omni"; Dashboard + Workbook collapse into the unified
  OmniV01Document with an omniV01DocumentType discriminator.
- Standard Asset.* fields carry name, description, sourceURL, sourceUpdatedAt,
  ownerUsers, ownerGroups — the connector does NOT redeclare these.
"""

from __future__ import annotations

from pyatlan.model.typedef import (
    AttributeDef,
    EntityDef,
    EnumDef,
    RelationshipDef,
)

# ---------------------------------------------------------------------------
# Attribute builders
# ---------------------------------------------------------------------------


def _str(
    name: str,
    *,
    required: bool = False,
    indexed: bool = False,
    description: str | None = None,
) -> AttributeDef:
    """Single-value string AttributeDef."""
    return AttributeDef(
        name=name,
        type_name="string",
        is_optional=not required,
        cardinality="SINGLE",
        is_indexable=indexed,
        description=description,
    )


def _enum(
    name: str,
    enum_name: str,
    *,
    required: bool = False,
    indexed: bool = False,
    description: str | None = None,
) -> AttributeDef:
    """Single-value enum AttributeDef. `enum_name` matches an EnumDef name."""
    return AttributeDef(
        name=name,
        type_name=enum_name,
        is_optional=not required,
        cardinality="SINGLE",
        is_indexable=indexed,
        description=description,
    )


# ---------------------------------------------------------------------------
# Enum typedefs
# ---------------------------------------------------------------------------

ENUM_DEFS: list[EnumDef] = [
    EnumDef.create(name="OmniV01ModelKind", values=["SHARED", "WORKBOOK"]),
    EnumDef.create(name="OmniV01DocumentType", values=["DASHBOARD", "WORKBOOK"]),
    EnumDef.create(
        name="OmniV01Scope",
        values=["ORGANIZATION", "WORKSPACE", "PRIVATE", "SHARED"],
    ),
]


# ---------------------------------------------------------------------------
# Abstract supertype
# ---------------------------------------------------------------------------

# Attributes carried by every concrete Omni asset. Declared once on the
# abstract OmniV01 supertype so subtypes don't redeclare them.
_OMNI_V01_ATTRS: list[AttributeDef] = [
    _str(
        "omniV01Id",
        required=True,
        indexed=True,
        description="Omni-side stable identifier. Required round-trip handle.",
    ),
    _str(
        "omniV01Url",
        indexed=False,
        description="Direct deep-link back to the asset in Omni's UI.",
    ),
    _enum(
        "omniV01Scope",
        "OmniV01Scope",
        indexed=True,
        description="Visibility scope of the asset in Omni.",
    ),
]

OMNI_V01_SUPERTYPE = EntityDef(
    name="OmniV01",
    super_types=["BI"],
    attribute_defs=_OMNI_V01_ATTRS,
    description=(
        "Abstract namespace-root for Omni connector assets. Carries the "
        "cross-cutting attributes that apply to every Omni asset so concrete "
        "subtypes do not re-declare them. Not instantiated directly."
    ),
)


# ---------------------------------------------------------------------------
# Concrete entity typedefs
# ---------------------------------------------------------------------------

ENTITY_DEFS: list[EntityDef] = [
    OMNI_V01_SUPERTYPE,
    EntityDef(
        name="OmniV01Model",
        super_types=["OmniV01"],
        attribute_defs=[
            _enum(
                "omniV01ModelKind",
                "OmniV01ModelKind",
                required=True,
                indexed=True,
                description="SHARED or WORKBOOK — Omni model kind.",
            ),
        ],
        description=(
            "Omni Model — the workspace layer that aggregates Topics, "
            "references one upstream Connection, and may inherit from a base "
            "Model (shared model pattern)."
        ),
    ),
    EntityDef(
        name="OmniV01Topic",
        super_types=["OmniV01"],
        attribute_defs=[
            _str(
                "omniV01BaseViewName",
                indexed=True,
                description="The base view this Topic projects from, if applicable.",
            ),
        ],
        description=(
            "Omni Topic — presentation projection over one or more warehouse "
            "tables. Warehouse-to-Topic lineage is emitted at connector "
            "runtime as Process entities, not via a typed relationship here."
        ),
    ),
    EntityDef(
        name="OmniV01Folder",
        super_types=["OmniV01"],
        attribute_defs=[
            _str(
                "omniV01Path",
                indexed=True,
                description="Fully qualified folder path within Omni (e.g. Org/Team/Subfolder).",
            ),
        ],
        description="Organizational hierarchy node for Omni Documents.",
    ),
    EntityDef(
        name="OmniV01Document",
        super_types=["OmniV01"],
        attribute_defs=[
            _enum(
                "omniV01DocumentType",
                "OmniV01DocumentType",
                required=True,
                indexed=True,
                description="DASHBOARD or WORKBOOK — discriminator for the unified Document type.",
            ),
            _str(
                "omniV01FolderPath",
                indexed=True,
                description="Denormalized folder path for fast UI display.",
            ),
        ],
        description=(
            "Omni Document — the unified representation of both Dashboards "
            "and Workbooks, distinguished by omniV01DocumentType."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Relationship typedefs
# ---------------------------------------------------------------------------


def _end_def(
    type_name: str,
    attr_name: str,
    cardinality: str,
    is_container: bool = False,
) -> dict[str, object]:
    return {
        "type": type_name,
        "name": attr_name,
        "cardinality": cardinality,
        "isContainer": is_container,
        "isLegacyAttribute": False,
    }


RELATIONSHIP_DEFS: list[RelationshipDef] = [
    RelationshipDef(
        name="omni_v01model_omni_v01topics",
        relationship_category="AGGREGATION",
        relationship_label="__OmniV01Model.topics",
        end_def1=_end_def("OmniV01Model", "topics", "SET", is_container=True),
        end_def2=_end_def("OmniV01Topic", "model", "SINGLE"),
        description="OmniV01Model owns one-to-many OmniV01Topic.",
    ),
    RelationshipDef(
        name="omni_v01base_model_omni_v01derived_models",
        relationship_category="ASSOCIATION",
        relationship_label="__OmniV01Model.baseModel",
        end_def1=_end_def("OmniV01Model", "derivedModels", "SET"),
        end_def2=_end_def("OmniV01Model", "baseModel", "SINGLE"),
        description=(
            "Self-referential model inheritance: a derived model points at "
            "its shared base model via baseModel; the base side exposes "
            "derivedModels."
        ),
    ),
    RelationshipDef(
        name="omni_v01folder_omni_v01documents",
        relationship_category="AGGREGATION",
        relationship_label="__OmniV01Folder.documents",
        end_def1=_end_def("OmniV01Folder", "documents", "SET", is_container=True),
        end_def2=_end_def("OmniV01Document", "folder", "SINGLE"),
        description="OmniV01Folder contains one-to-many OmniV01Document.",
    ),
]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_typedefs(skip_existing: bool = True) -> None:
    """Register Omni custom typedefs with Atlan.

    Creates enums, then the abstract supertype + concrete entity types, then
    the relationship typedefs. Each type is checked for existence first so
    that calls are idempotent on tenants where Atlan has pre-seeded the
    typedef set (e.g. marketplace-partner.atlan.com).

    Requires ATLAN_BASE_URL and ATLAN_API_KEY in the environment.
    """
    from application_sdk.clients.atlan import get_client
    from application_sdk.observability.logger_adaptor import get_logger

    log = get_logger(__name__)
    client = get_client()

    # Order matters: enums must exist before attributes reference them; the
    # abstract supertype must exist before its subtypes; concrete entities
    # must exist before relationships that reference them.
    all_defs: list[tuple[str, object]] = []
    all_defs.extend(("enum", d) for d in ENUM_DEFS)
    all_defs.extend(("entity", d) for d in ENTITY_DEFS)
    all_defs.extend(("relationship", d) for d in RELATIONSHIP_DEFS)

    for kind, type_def in all_defs:
        type_name = type_def.name
        try:
            client.typedef.get_by_name(type_name)
            log.info("Omni %s typedef '%s' already registered, skipping.", kind, type_name)
            continue
        except Exception:
            pass

        try:
            client.typedef.create(type_def)
            log.info("Registered Omni %s typedef '%s'.", kind, type_name)
        except Exception as exc:
            if skip_existing:
                log.warning(
                    "Failed to register Omni %s typedef '%s': %s",
                    kind,
                    type_name,
                    exc,
                )
            else:
                raise
