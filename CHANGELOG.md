# Changelog

All notable changes to the Omni connector are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] - 2026-08-31

Follow-on fixes from Atlan's review of the v0.4.0 image on a large tenant
whose extract activity hit the 8h ceiling. Root cause was a topic-detail
fallback firing on ~all topics (20,692 of 20,693 on the affected org) and a
per-workbook topic-detail fetch that turned 93 distinct topics into 25,831
requests. Also fixes an Argo string-coercion bug that made
`crawl_only_content_backed_workbooks=false` a no-op — same coercion
silently ignored `verify_ssl=false`, which is security-relevant.

### Fixed

- **Topic detail is now derived locally on schema-namespaced orgs.** Topic YAML
  references views by their internal `<schema>__<view>` name, while the view
  files are keyed `<CATALOG>.<SCHEMA>/<view>.view`. `_views_from_payload`
  registered only the bare stem, so every lookup missed — and because one
  unresolvable reference gives up on the whole topic, and `base_view` is a
  required topic parameter, it fired on effectively every topic. A crawl of one
  large org fell back to the per-topic API for 20,692 of 20,693 topics, which at
  the client's 1 req/s gate is the difference between a crawl that finishes and
  one that does not. Views are now registered under every form a topic may
  reference them by; the change is additive, so orgs that reference views bare
  are unaffected.

- **Topic detail is fetched against the owning model and memoized.** The owning
  shared model was computed and then discarded, with the workbook's own id used
  for the fetch. Where the owning model does not carry the topic — a
  `BRANCH`-defined topic, or a failed override probe — the fetch falls back to
  the model itself rather than silently returning empty enrichment.

- **`crawl_only_content_backed_workbooks` can be turned off.** Argo passes
  workflow parameters as strings and `bool("false")` is `True`. The same
  coercion bug affected `save_output_local` and `verify_ssl`; a silently ignored
  `verify_ssl=false` is security-relevant.

- **Incomplete document evidence no longer deletes named workbooks.** When
  document-detail fetches fail beyond a threshold, `document_model_ids` is too
  incomplete to filter on, so the aggressive workbook filter degrades to the
  conservative one for that run instead of dropping real workbooks while
  reporting success. Thresholded so a single transient failure cannot widen the
  crawl from ~10% of models to 100%.

- **The shared host rate limiter can only ratchet down.** Keying it on
  `<base_url>|<rpm>` gave two runs with different configured rpm their own
  limiter, which summed at the host. It is now keyed on the host, and the most
  conservative rpm any run asked for wins.

### Changed

- Failure tallies now ride the periodic progress line. The end-of-pass
  `fetch_snapshot failures:` summary is never reached by a run that times out
  mid-pass, which is exactly the run whose failure counts are wanted.

## [0.4.0] - 2026-08-26

Six fixes surfaced by Atlan's live-run review of v0.3.0 on the customer
tenant. Every OmniV01 asset published in v0.3.0 was missing its parent /
model / folder linkage (the three relationship names were mis-spelled and
Atlan drops unknown relationship names silently on write), and every
publish failed on 1–3 dangling ATLAS-404 references from four distinct
reference-validity bugs. Two other classes of bug — shared-state
corruption across concurrent runs, and duplicate topics at ~170× on
deep-inheritance tenants — are also addressed here.

### Fixed

- **Relationship names + connection linkage (item 1).** Renamed the
  three relationship keys the transformer writes to match the typedef
  (`model` → `omniV01Model`, `baseModel` → `omniV01BaseModel`,
  `folder` → `omniV01Folder`). Dropped the `connection` relationship
  at three sites — there is no such relationship on the Omni types;
  the linkage now travels on the inherited plain-string attribute
  `connectionQualifiedName`, added on Model / Topic / Folder / Document.
  Dropped `omniV01Url` (not on the typedef; `sourceURL` already carries
  the same value and renders as a link).
- **Emit-set gating on every internal reference (item 2).** Every
  publish failure Atlan observed was ATLAS-404-00-00A — one to three
  refs pointing at qualifiedNames the run wasn't also emitting.
  `transform()` now pre-computes three emit-sets (`emitted_model_ids`,
  `emitted_folder_ids`, `emitted_topic_qns`) before any relationship is
  written; each entity builder gates its outgoing references against
  them. Fixes topic→filtered-model, model→SCHEMA-base, doc→private-folder,
  and the dashboard-tile fallback that fabricated dangling QNs when the
  topic lookup missed.
- **Per-run client + per-host shared rate limiter (item 3).** The SDK
  creates one ActivitiesClass per pod and runs up to five activities
  concurrently. `self.handler.client` was shared across all of them, so
  credentials, HTTP client, cancel flag and rate limiter leaked between
  runs — the 29-minute run whose topic counter froze at model 95 was
  the sentinel case. Worse: `load_credentials` overwrote shared creds,
  so a two-tenant customer could have one run crawl the other's org
  with the other's token. Now `extract_and_transform_metadata` builds a
  fresh ClientClass + HandlerClass per invocation and closes the client
  in `finally`. The rate limiter alone stays process-wide (per Omni
  host, via `_HOST_RATE_LIMITERS`) — throttling is a property of the
  API, not of the run.
- **Deeper baseModelId chain walk (items 4a + 4b + 4c).**
  v0.3.0 filtered only UNNAMED workbook models absent from
  /v1/documents. The customer's tenant had 19,937 workbook models most
  of which were named ephemeral sessions Omni auto-creates when a user
  opens a workbook; ~90% of the API traffic on that tenant was fetching
  YAML for models that would never publish. The filter now catches any
  WORKBOOK not in /v1/documents (apps, saved workbooks and dashboards
  all surface through that endpoint — confirmed with the customer);
  the escape hatch is the new `crawl_only_content_backed_workbooks`
  config flag (default `true`).
  v0.3.0's topic canonicalization also climbed exactly one hop from
  workbook to baseModelId. Real Omni inheritance is deeper
  (schema → shared → extension/branch → workbook), so a topic still
  emitted a copy at each intermediate layer; the widest topic on the
  customer's tenant had ~493 copies. The chain now climbs to the SHARED
  ancestor, skipping SHARED_EXTENSION and BRANCH kinds that the typedef
  can't represent. The extension-probe failure default also flips: an
  unreadable probe now COLLAPSES the topic (inherited) rather than
  keeping it (workbook) — the old default failed toward duplication.
- **Silent failures now count, log, and abort past threshold (item 5).**
  Six exception handlers in client.py returned an empty value with no
  log and no counter, so the 29-minute run that lost 88% of its topics
  reported `success: True`. Each handler now records the swallow via
  `_swallow(category, context, exc)`, grouped by blast radius. If
  >20% of model-YAML fetches fail, `fetch_snapshot` raises before the
  transform pass — the check runs at end of model pass (~1 min in)
  rather than end of `fetch_snapshot`, because with
  `maximum_attempts=1` a late abort costs a full three-hour re-crawl.
  Failure tallies are also surfaced in the activity result dict so they
  land in Temporal history alongside the entity count.
  Also fixed: `workflow.logger.info("Omni extraction completed: %s", …)`
  never interpolated (the SDK's logger adapter doesn't do %-formatting)
  so no run's entity count ever reached its own logs.
- **One bad date no longer throws away a finished crawl (item 6).**
  `_epoch_ms` raised on unparseable input; with `maximum_attempts=1`
  that lost three hours of work to one malformed field. Now returns
  None on parse failure and tolerates non-string values via `str()`.
  Naive datetimes are treated as UTC (was container-local, which
  drifted by whatever offset the pod happened to run in). `_parse_dt`
  also returns None on failure so the bad value never reaches
  `_epoch_ms`. Trailing slashes and whitespace are stripped from
  operator-supplied `atlan_source_connection_map` paths so a pasted
  `default/snowflake/1700000000/` doesn't produce a double slash in the
  table qualifiedName. Deliberately no case normalisation — Atlan
  warehouse qualifiedNames are case-sensitive and vary by warehouse.

### Added

- `tests/test_referential_closure.py` — Atlan's referential-closure
  contract (5 cases) as the primary gate for item 2.
- ~30 new regression fences across the six items.

## [0.3.0] - 2026-08-20

Extraction performance and run observability, from an Atlan investigation of a
large-tenant run that could not complete (19,937 models; ~928,000 API requests;
timed out at 0.6% and then kept calling the Omni API for 9h44m unobserved).

### Changed

- **Topic enrichment no longer costs an HTTP call per topic.** The combined-mode
  model YAML already carries the model's `.view` files, so `viewSources` (and the
  source table/schema/catalog) are derived locally. The `/v1/models/{id}/topic/
  {name}` endpoint is now only called when a topic's joins cannot be read from
  the payload. Largest single saving: ~897,000 requests on the tenant above.
- **Shadow workbook models no longer pay the YAML fan-out.** An unnamed
  `WORKBOOK` that no document points at is skipped — the same predicate
  `transformer._models` already applies when deciding which models become
  entities. Their topics are byte-identical copies of the `SHARED` parent's.
- **Document details are fetched before model YAMLs.** Previously both batches
  shared one FIFO pool with models submitted first, so on a large tenant the
  document futures never ran (`docs 0/264` after 12 hours).
- **`page_size` default 50 -> 100** (Omni's documented maximum).
- **`extract_and_transform_metadata` timeout restored to 8h** (was 2h). The
  heartbeat added below is what surfaces a hung run now, so the ceiling's only
  job is to bound total work.
- **`extract_retry_policy` `maximum_attempts` 2 -> 1.** A retry restarts the
  crawl from zero, so on a multi-hour extraction it costs a second full window
  of source-API load for little benefit. Worth raising again once the fetch can
  checkpoint and resume.
- **A burst `403` is now retried rather than fatal.** The API answers a rapid
  burst with `403 "Invalid bearer token"` rather than `429`, which the client
  was classifying as non-retryable.

### Fixed

- **`joins` is parsed recursively.** The topic YAML's `joins` block nests views
  under the view they join through, to arbitrary depth. Reading only the top
  level captured a subset (2 of 4 views in Omni's own example) and emitted
  partial source lineage without falling back. Every key at every depth is now
  collected. Thanks to Omni for confirming the shape.
- **Relationship aliases resolve locally.** `joins` names a relationship's alias
  where one is defined (`join_from_view_as`) rather than the underlying view
  (`join_from_view`). Those definitions ride in the same combined-mode payload's
  `.relationships` file, so the alias map is built from it and aliased joins
  resolve with no extra request. An alias we still cannot map falls back rather
  than dropping a table.
- **An unresolvable view name in `joins` falls back instead of dropping a table.**
  Where a relationship is aliased, `joins` names the alias rather than the
  underlying view, so it will not match a `.view` in the payload. Deriving
  locally would have emitted source lineage silently missing that table; the
  topic now falls back to the topic-detail API. A view that *does* resolve but
  carries no `table_name` is a derived view and is still skipped, not treated as
  a miss. Also de-duplicates the base view, which is normally listed in `joins`
  too, so `viewSources` matches the topic API's one-row-per-view shape.
- **A listing call that rejects `pageSize` halves and retries.** The cap is
  per-endpoint: `/v1/documents` and `/v1/models` document a max of 100, but
  `/v1/folders` documents none. On a 400 the page size halves rather than
  failing the run at the top. Suggested by Omni.
- **The extract activity could not observe its own timeout.** `fetch_metadata`
  is `async def` but called the synchronous `fetch_snapshot` directly, blocking
  the worker's event loop for the whole crawl — so no heartbeat could fire and
  the Temporal timeout was never delivered. It now runs via `asyncio.to_thread`
  with a cooperative abort so cancellation drains within one in-flight request.
- **Added `heartbeat_timeout` (5 min) and `@auto_heartbeater`** to the extract
  activity, matching the pattern used by other connectors on this SDK.
- **Removed a dead `scope` read on model records.** `/v1/models` returns
  `baseModelId, connectionId, createdAt, deletedAt, id, modelKind, name,
  updatedAt` — there is no `scope` field, so `omniV01Scope` could only ever be
  unset on models. Folders and documents are unaffected.

## [0.2.9] - 2026-07-29

Four issues surfaced by Atlan's demo-curation testing (PART-1290, PART-1355,
PART-1221, PART-1079).

### Fixed

- **PART-1290 — `OmniV01Folder` publish crash on re-run.** Folders were the
  only entity type emitted without a `relationshipAttributes` key, so the
  serializer wrote `relationshipAttributes: null` and Atlan's calculate-diff
  step choked on subsequent runs. Now emitted symmetrically with a
  `connection` relationship — also parents folders to the Omni Connection.
- **PART-1355 — Duplicate topics across shared + workbook models.** Omni's
  combined-YAML endpoint merges a workbook's shared parent into its own
  layer, so an inherited topic like `orders` was landing under 2–3
  OmniV01Model QNs (once per workbook + the shared model). Per Omni
  modeling docs, workbook topics *can* legitimately override inherited
  ones via `extends`, so we can't blindly canonicalize. Fix:
  - `client.py` — new `_overridden_topic_names(model_id)` uses
    `mode=extension` (the workbook's own YAML layer) to detect real
    overrides. Normalizes both plain (`orders.topic`) and refinement-prefixed
    (`+orders.topic`) filenames.
  - `client.py::_fetch_topics_for_model` — for WORKBOOK models, stamp
    `owningModelId=baseModelId` on inherited topics and
    `owningModelId=modelId` on overridden ones. If the extension fetch
    fails, keep the workbook as owning (fail-safe: don't canonicalize).
  - `transformer.py::_topics` — key OmniV01Topic QN off `owningModelId`,
    dedup by QN, prefer the canonical (owning==modelId) row.
  - `transformer.py::_processes_topic_to_document` — canonicalize each
    tile's `modelId` through a `(modelId, topic) -> owningModelId` lookup
    so Processes point at the canonical topic QN.
  - `transformer.py::_processes_source_to_topic` — key process QN + topic
    output on `owningModelId`, dedup by process QN. Warehouse-connection
    resolution stays on the row's original `modelId` (that's where the
    `viewSources` payload came from).
- **PART-1221 — Preflight UI shows "checks failed" cosmetically.**
  `handler.preflight_check` was returning a flat `{success, message, data}`;
  the setup UI iterates top-level keys expecting each to carry `.success`.
  Now returns the SDK's per-check shape:
  `{"connection": {success, successMessage, failureMessage}}`.
- **PART-1079 — Runtime tightening.** Removed the dead `handler.get_configmap()`
  method (opened `app/frontend/workflow.json`, which the marketplace no longer
  serves via this route — the form schema lives in marketplace-packages).
  Tightened `extract_and_transform_metadata` `start_to_close_timeout` from
  8h to 2h so hung runs surface faster.

### Added

- Client tests covering the three workbook-topic scenarios (inherited,
  overridden with `+` prefix, extension-fetch failure fallback).
- Transformer tests covering topic dedup to shared QN, override retention,
  tile canonicalization, source-process dedup, and unenumerated-topic
  fallback.
- Handler test file (new) covering the per-check preflight shape.

## [0.2.8] - 2026-06-24

**Date attribute fix.** v0.2.7 wrote `sourceUpdatedAt` as ISO-8601 strings
(`2026-05-27T19:24:23.616000+00:00`). Atlan's Atlas store requires date
attributes to be epoch milliseconds (`1782749725000`); the ISO string
failed date validation and all OmniV01* entities were rejected on create.
The Connection has no date field, which is why it created fine.

### Fixed

- `app/transformer.py` — added `_epoch_ms()` helper that converts an
  ISO-8601 string to epoch milliseconds via `datetime.fromisoformat`.
  Applied at all three `sourceUpdatedAt` sites (Model, Topic, Document).
  `None` input passes through as `None` unchanged.

## [0.2.7] - 2026-06-22

**Publishing fix.** v0.2.6 ran green but no assets landed in Atlan's
catalog: `JsonFileWriter` was writing to the root of `output_path`,
while Atlan's convert/publish step reads from a `transformed/`
subdirectory (the SDK's own convention, mirroring how `sql.py` does
`os.path.join(output_path, "transformed")`). One-line fix.

### Fixed

- `app/activities.py::extract_and_transform_metadata` — point
  `JsonFileWriter` at `os.path.join(args["output_path"], "transformed")`
  so the publish step finds the NDJSON files.

## [0.2.6] - 2026-06-05

**Connection-shape tolerance + runtime credential resolution.** Two
follow-on issues surfaced from Atlan's deeper review of v0.2.5. Patches
ready-to-apply, validated against v0.2.5 by the Atlan partner team.

### Changed

- `app/activities.py::get_workflow_args` — Connection lookup is now
  tolerant of all three shapes the platform actually delivers: the flat
  Atlan-normalized `connection_qualified_name`, the Atlas-shaped
  `attributes.qualifiedName`, and the stringified-JSON form that Argo
  parameter passing produces. Falls back across `base_args["connection"]`
  and `metadata_in["connection"]`.
- `app/activities.py::get_workflow_args` — ferries `credential_guid`
  through to the next activity but never resolves it. The return value
  is a Temporal activity result and would otherwise persist secrets in
  workflow history.
- `app/activities.py::extract_and_transform_metadata` — when a
  `credential_guid` is present and no inline credentials are provided
  (the production path), resolve `omni_base_url` / `omni_api_token`
  from `SecretStore.get_credentials(credential_guid)` immediately
  before `handler.load`. Accepts both the wire-shape `host` / `password`
  and the semantic key names from the SecretStore. Inline credentials
  (local playground) skip the SecretStore round-trip entirely.

### Added

- 7 tests covering the three Connection shapes (snake_case flat,
  stringified-JSON Atlas, `metadata.connection` JSON) and the four
  credential-resolution paths (guid ferried but not awaited in
  `get_workflow_args`, guid resolved in `extract_and_transform_metadata`,
  inline credentials skip the store, no-guid passthrough).

## [0.2.5] - 2026-06-05

**Form-field cleanup.** v0.2.4's workflow registration fix unblocked
end-to-end dispatch, but the first real run on
`marketplace-partner.atlan.com` failed in `get_workflow_args` because
the activity required `connection_epoch_ms` as a form field — and the
Atlan UI doesn't collect it (the epoch is already embedded in the
Connection's qualifiedName at `base_args["connection"]`).

### Changed

- `app/activities.py::get_workflow_args` derives `connection_epoch_ms`
  from `base_args["connection"].attributes.qualifiedName` (third segment
  of `default/omni/<epoch>/...`) before falling back to a form field.
  The form-field path remains so the local playground keeps working.
  The 13-digit validation is unchanged so malformed connections still
  fail loud.
- `app/frontend/workflow.json` — `connection_epoch_ms` is now
  `required: false` with help text noting it's auto-derived in
  production and only relevant for local runs.

### Added

- Tests covering Connection-QN derivation, Connection-QN precedence
  over a stale form field, malformed-QN fallback to the form field,
  and asset-style QNs with extra segments.

## [0.2.4] - 2026-06-04

**Workflow registration fix.** v0.2.3 resolved Test Authentication on
`marketplace-partner.atlan.com` but the first end-to-end workflow run
sat idle: Atlan's launcher dispatches workflows by the wire-level type
name `OmniMetadataExtractionWorkflow` (mirroring the SDK's
`BaseSQLMetadataExtractionWorkflow` convention), and our class was
registered under its Python symbol `WorkflowClass`. The Temporal worker
reported class-not-found and never picked up the run. Latent in every
prior image — the synchronous `/auth` route doesn't go through the
workflow registry, so Test Authentication never exercised it.

### Fixed

- `app/workflow.py:15` — override the registered name on `@workflow.defn`
  to `OmniMetadataExtractionWorkflow`. The Python class symbol
  `WorkflowClass` is unchanged so `main.py` imports and other internal
  references stay intact.

## [0.2.3] - 2026-06-02

**Auth fix, round 2.** v0.2.2 unwrapped the SDK's wrapped auth body but
`client.load_credentials` still only recognized the semantic key names
`omni_base_url` / `omni_api_token`. The Atlan UI sends wire-shape keys
`host` / `password` / `authType` through Heracles to
`POST /workflows/v1/auth`, so v0.2.2 raised `ValueError` again — surfaced
as Heracles' generic "App service returned an internal error" 400 in the
Test Authentication form on `marketplace-partner.atlan.com`. Same
PART-1112 ticket.

### Fixed

- `app/client.py::ClientClass.load_credentials` now accepts both
  credential shapes: wire (`host`, `password`, `authType`) and semantic
  (`omni_base_url`, `omni_api_token`). Wire keys are read as aliases for
  the semantic keys; `authType` is ignored (only API-key auth is
  supported). The protocol check applies to whichever base URL is
  provided.

### Added

- Tests covering the wire-shape path and the protocol check against the
  wire-shape base URL.

## [0.2.2] - 2026-05-31

**Auth fix.** v0.2.1's `/workflows/v1/auth` route raised `ValueError: Both
omni_base_url and omni_api_token are required.` on every credential load.
The Atlan SDK wraps the auth body as `{"credentials": {...}, "metadata":
{...}}` before calling `handler.load(body.model_dump())`, but `load()`
passed the outer wrapper straight to `client.load_credentials`, which
looks for `omni_base_url` at the top level — so it never found it.
Reproduced on `marketplace-partner.atlan.com` (PART-1112) across four
identical-fingerprint failures from pod `omni-7874b49895-v65xk`.

### Fixed

- `app/handler.py::HandlerClass.load` now unwraps the `credentials` key
  out of the wrapped body before forwarding to the client, mirroring the
  same logic already used in `preflight_check`. Falls back to the raw
  `args[0]` so the activities path (which passes a flat dict) keeps
  working.

## [0.2.1] - 2026-05-22

**Security fix.** v0.2.0 inadvertently shipped sensitive local files inside
the Docker image because the `COPY . .` step in the Dockerfile did not honor
`.gitignore`. Atlan partner review flagged:

- `app/.env` containing a live Omni API token bound to `peter.omniapp.co`
- `omni_entities.ndjson` — a 4.4 MB metadata snapshot of the same tenant
- `.git/` history

### Added

- `.dockerignore` — authoritative exclusion list for the Docker build context:
  `.env`/`.env.*` (except `.env.example`), `.git/`, `.venv/`, `local/`,
  `components/`, `temporal.db*`, `omni_entities.ndjson`, `*.ndjson`, Python
  caches, frontend playground assets, IDE files, `.github/`.

### Fixed

- v0.2.0 image leaked the Omni API token, a tenant metadata snapshot, and
  `.git/`. v0.2.1 builds without any of these in the layer. The leaked token
  was revoked on the Omni side; the v0.2.0 / `c95349` tags must not be
  deployed.

## [0.2.0] - 2026-05-20

Aligns the connector image with Atlan's v0 partner typedef reference, clears
the quality flags raised in the va3aeae codebase analysis, and adds the
operational hardening needed to run against real-world Omni tenants
(rate-limit honoring, progress visibility, recoverable timeouts).

Validated end-to-end against `peter.omniapp.co` on 2026-05-20: 36 models /
8915 topics / 28 documents / 32 lineage processes, no red flags from the
`scripts/inspect_dryrun.py` contract checks.

### Added

- Abstract `OmniV01` supertype (extends `BI`) plus four concrete entity
  typedefs: `OmniV01Model`, `OmniV01Topic`, `OmniV01Folder`, `OmniV01Document`.
- Three enum typedefs: `OmniV01ModelKind`, `OmniV01DocumentType`,
  `OmniV01Scope`.
- Three internal typed relationships: model→topics, model→baseModel/derived
  models, folder→documents.
- `connection_epoch_ms` workflow form field; qualified names now follow
  `default/omni/{connection_epoch_ms}/{rest}`.
- Thread-safe API rate limiter on the Omni client (default 60 rpm, exposed as
  a `rate_limit_rpm` form field). Spaces requests evenly regardless of
  `max_concurrency`.
- Honor `Retry-After` header on 429 responses; fall back to jittered
  exponential backoff if Omni doesn't send one.
- Progress logging during `fetch_snapshot` — model/document completion
  counters via `as_completed`, plus summary line on snapshot done.
- `OMNI_LOCAL_UI=1` env flag for local development. When set, the SDK's UI
  routes are mounted at `http://localhost:8000`, and `save_output_local` is
  forced on so dry-runs leave `omni_entities.ndjson` on disk for the
  `scripts/inspect_dryrun.py` validator.
- Omni Deep Pink monogram logo, inlined as a base64 data URI in
  `workflow.json` and checked in as `app/frontend/omni-logo.svg`.

### Changed

- All entity payloads map to standard `Asset.*` fields where applicable
  (`ownerUsers`, `sourceURL`, `sourceUpdatedAt`).
- Cross-references promoted from string-QN attributes to typed Atlas
  relationship edges.
- `Process` lineage entities now reference `OmniV01Topic` and `OmniV01Document`
  as inputs/outputs (Snowflake → Topic → Document chain).
- Base image upgraded to `app-runtime-base:2.8.7-6` (was the legacy
  `application-sdk:main-2.3.1`); picks up CVE remediation.
- `extract_and_transform_metadata` activity timeout raised from 20 min to 8 h
  (large-tenant runs at the 60 rpm cap can exceed 4 h).
- Typedef-registration failure now surfaces as `ERROR` (was `WARN`) so the
  silent-bad-startup mode is no longer possible.
- `get_workflow_args` now sources every form field from
  `payload`/`metadata`/`credentials`/`base_args` so it handles both the
  marketplace-packages nesting and the playground's flat layout.

### Removed

- Custom `omni_connection`, `omni_dashboard`, `omni_workbook` typedefs. Use the
  built-in `Connection` with `connectorName: "omni"` for the warehouse anchor;
  the unified `OmniV01Document` carries an `omniV01DocumentType` discriminator.
- `Asset.*`-overlapping attributes from the custom typedefs (`url`, `updatedAt`,
  `ownerId/ownerName`, custom `last_sync_*` triple).

### Fixed

- `application.start()` now passes `ui_enabled=False` in production (and `True`
  when `OMNI_LOCAL_UI=1`) so the SDK does not try to mount the empty
  `frontend/static/` stub in production while still serving the form locally.
- `snapshot["document_model_ids"]` is now a JSON-serializable `list` (was a
  `set`), unblocking any future activity-to-activity hand-off of the snapshot.
- `connection_epoch_ms` validation failure now raises
  `ApplicationError(non_retryable=True)` instead of `ValueError`, so misconfig
  fails fast without burning Temporal retries.

## [0.1.0] - 2026-03-10

Initial Harbor push (`a3aeae`). Six custom typedefs, file-output +
publish-app delivery, full Dapr + Temporal integration, source-table-to-topic
and topic-to-dashboard Process lineage.
