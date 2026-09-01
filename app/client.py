from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import random
import threading
import time
from typing import Any, Sequence

import httpx
import yaml
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Omni's documented API rate limit. Each request gates through a token bucket
# so concurrent threads collectively respect the cap.
OMNI_DEFAULT_RPM = 60

# Omni rejects anything larger with "Page size cannot exceed 100".
OMNI_MAX_PAGE_SIZE = 100


class _RateLimiter:
    """Thread-safe minimum-interval gate. With rpm=60 calls are spaced ≥1s apart."""

    def __init__(self, rpm: int):
        self.min_interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def tighten_to(self, rpm: int) -> None:
        """Adopt a MORE restrictive rate; never loosen.

        The limiter protects the Omni host, so the most conservative rpm any
        run asked for wins. Raising a host's rpm needs a worker restart. An
        operator who sets rate_limit_rpm low because Omni is 429-ing must not
        silently get an earlier run's higher rate.
        """
        interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        with self._lock:
            if interval > self.min_interval:
                self.min_interval = interval

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now >= self._next_allowed:
                self._next_allowed = now + self.min_interval
                wait = 0.0
            else:
                wait = self._next_allowed - now
                self._next_allowed += self.min_interval
        if wait > 0:
            time.sleep(wait)


# Rate limiting is a property of the Omni host, not of a run — five concurrent
# activities against the same tenant sharing five 60 rpm limiters is 300 rpm
# at Omni's door, which the API rejects. This cache hands each host a single
# limiter that all runs against it share. Keyed on `<base_url>|<rpm>` so a
# per-run rpm override doesn't clobber the default (paranoia: same tenant,
# different operator-configured rpm shouldn't be a mystery).
_HOST_RATE_LIMITERS: dict[str, _RateLimiter] = {}
_HOST_RATE_LIMITERS_LOCK = threading.Lock()


def _get_host_rate_limiter(base_url: str, rpm: int) -> _RateLimiter:
    # Keyed on the host alone: the limit belongs to the host, so two runs with
    # different operator-configured rpm must not each get their own limiter and
    # sum at Omni's door. The most restrictive rpm wins (see `tighten_to`).
    key = base_url
    with _HOST_RATE_LIMITERS_LOCK:
        limiter = _HOST_RATE_LIMITERS.get(key)
        if limiter is None:
            limiter = _RateLimiter(rpm)
            _HOST_RATE_LIMITERS[key] = limiter
        else:
            limiter.tighten_to(rpm)
    return limiter


def _parse_dt(value: Any) -> str | None:
    """Normalise an ISO-8601 string to canonical isoformat.

    Returns None on parse failure so the bad value never reaches the
    transformer's _epoch_ms (which would otherwise raise and take down
    the whole run — see PART-1079 rev 2 in the Atlan review).
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except (ValueError, TypeError):
        return None


@dataclass
class OmniCredentials:
    base_url: str
    api_token: str
    verify_ssl: bool = True
    timeout_seconds: int = 30

    def __repr__(self) -> str:
        return (
            f"OmniCredentials(base_url={self.base_url!r}, api_token='***', "
            f"verify_ssl={self.verify_ssl}, timeout_seconds={self.timeout_seconds})"
        )


class OmniApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class NonRetryableOmniApiError(OmniApiError):
    pass


# Per-run tallies of silently-swallowed errors, grouped by blast radius.
# One model-YAML failure loses that whole model's topics; one topic-detail
# failure loses one topic's enrichment; one document-detail failure loses one
# doc's tile-topic edges. Emitting these blended into one pooled ratio hides
# whichever is dominating, so each category is counted separately.
_FAILURE_CATEGORIES = (
    "model_yaml",
    "override_probe",
    "topic_detail",
    "document_detail",
)

# If more than this fraction of model-YAML fetches fail, abort after the
# model pass — before we spend three hours on a run whose topic side will
# be inconsistent anyway. Checked in fetch_snapshot with maximum_attempts=1
# in mind: a late abort costs a full re-crawl.
_MODEL_YAML_FAILURE_ABORT_THRESHOLD = 0.20


class ClientClass:
    def __init__(
        self,
        credentials: dict[str, Any] | None = None,
        rpm: int = OMNI_DEFAULT_RPM,
    ):
        self._credentials: OmniCredentials | None = None
        self._http_client: httpx.Client | None = None
        # Placeholder until load_credentials resolves the host and swaps in the
        # shared per-host limiter. Used only for the pre-load state.
        self._rate_limiter = _RateLimiter(rpm)
        # Set by fetch_snapshot; lets a Temporal cancellation drain the thread
        # pool instead of the pool outliving the activity (see handler).
        self._abort: threading.Event | None = None
        # Per-run failure tallies. Reset at fetch_snapshot start.
        self._failures: dict[str, int] = {k: 0 for k in _FAILURE_CATEGORIES}
        # Topic detail is a property of the OWNING model, not of each workbook
        # that inherits it, so it is fetched once per (owning model, topic) and
        # reused. Without this, every workbook re-fetches the same shared topic:
        # one live run issued 25,831 topic-detail requests that resolved to 93
        # distinct topics. Reset at fetch_snapshot start.
        self._topic_detail_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._topic_detail_cache_lock = threading.Lock()
        if credentials:
            self.load_credentials(credentials)

    def _swallow(self, category: str, context: str, exc: BaseException) -> None:
        """Record + log a silently-caught error so it isn't invisible.

        The bare `except Exception: return {}` pattern was correct in intent
        (one bad topic shouldn't fail the crawl) but wrong in execution:
        with no counter and no log, the 29-minute run that lost 88% of its
        topics still reported success. Callers still swallow; this method
        just makes the swallow observable.
        """
        self._failures[category] = self._failures.get(category, 0) + 1
        logger.warning(f"omni fetch failure ({category}) {context}: {exc!r}")

    def load_credentials(self, credentials: dict[str, Any]) -> None:
        # Accept both credential shapes:
        # - Wire shape (Atlan UI → Heracles → /workflows/v1/auth): host, password, authType
        # - Semantic shape (this app's own UI form, activities path): omni_base_url, omni_api_token
        base_url = str(
            credentials.get("omni_base_url")
            or credentials.get("host")
            or ""
        ).strip().rstrip("/")
        token = str(
            credentials.get("omni_api_token")
            or credentials.get("password")
            or ""
        ).strip()
        if not base_url or not token:
            raise ValueError(
                "Credentials must include a base URL (omni_base_url or host) "
                "and an API token (omni_api_token or password)."
            )
        if not (base_url.startswith("https://") or base_url.startswith("http://")):
            raise ValueError(
                "omni_base_url must include protocol, for example "
                "'https://your-org.omniapp.co/api'."
            )

        verify_ssl = bool(credentials.get("verify_ssl", True))
        timeout_seconds = int(credentials.get("timeout_seconds", 30))

        # Swap in the process-wide per-host limiter now that the base URL is
        # known. All concurrent activities against the same tenant share one
        # gate — otherwise five 60 rpm limiters is 300 rpm at Omni's door.
        rpm_raw = credentials.get("rate_limit_rpm")
        effective_rpm = int(rpm_raw) if rpm_raw not in (None, "", 0) else OMNI_DEFAULT_RPM
        self._rate_limiter = _get_host_rate_limiter(base_url, effective_rpm)
        self._credentials = OmniCredentials(
            base_url=base_url,
            api_token=token,
            verify_ssl=verify_ssl,
            timeout_seconds=timeout_seconds,
        )

        self.close()
        self._http_client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            verify=verify_ssl,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def _client(self) -> httpx.Client:
        if not self._http_client:
            raise OmniApiError("Omni client is not initialized. Call load_credentials first.")
        return self._http_client

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        max_rate_limit_retries = 3
        max_server_error_retries = 2

        for attempt in range(max_rate_limit_retries + 1):
            # Single choke point for cooperative cancellation: once the activity
            # is cancelled no queued worker issues another customer API call.
            if self._abort is not None and self._abort.is_set():
                raise OmniApiError(
                    f"GET {path} aborted: extraction was cancelled.",
                    retryable=True,
                )
            self._rate_limiter.acquire()
            try:
                response = self._client().get(path, params=params or {})
            except httpx.HTTPError as exc:
                if attempt < max_server_error_retries:
                    delay = (2**attempt) + random.uniform(0.0, 0.25)
                    time.sleep(delay)
                    continue
                raise OmniApiError(
                    f"GET {path} failed due to network error: {exc}",
                    retryable=True,
                ) from exc

            status = response.status_code
            if 300 <= status < 400:
                # A 3xx from Omni's API almost always means the base URL is
                # wrong (typically missing the `/api` suffix — the marketing
                # site 302s to docs). Fail loud with the target URL so the
                # operator gets a useful message rather than a JSONDecodeError.
                location = response.headers.get("location") or "(no Location header)"
                raise NonRetryableOmniApiError(
                    f"GET {path} redirected ({status} -> {location}). Check "
                    f"omni_base_url — Omni's REST API lives under `/api`.",
                    status_code=status,
                    retryable=False,
                )
            if status < 400:
                data = response.json()
                if not isinstance(data, dict):
                    raise NonRetryableOmniApiError(
                        f"GET {path} returned non-object response.",
                        status_code=status,
                        retryable=False,
                    )
                return data

            # Omni answers a rapid burst with 403 "Invalid bearer token", not
            # 429, so 403 must back off and retry rather than hard-fail the run.
            if status in (403, 429):
                if attempt < max_rate_limit_retries:
                    # Honor Retry-After if Omni sends it; else fall back to
                    # jittered exponential backoff.
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else (
                            (2**attempt) + random.uniform(0.0, 0.5)
                        )
                    except ValueError:
                        delay = (2**attempt) + random.uniform(0.0, 0.5)
                    logger.warning(
                        f"Omni rate limit hit for {path}. Retrying in {delay:.2f}s "
                        f"(attempt {attempt + 1}/{max_rate_limit_retries}, "
                        f"retry_after={retry_after!r})."
                    )
                    time.sleep(delay)
                    continue
                raise OmniApiError(
                    f"GET {path} rate-limited after retries: {status}",
                    status_code=status,
                    retryable=True,
                )

            if 500 <= status < 600:
                if attempt < max_server_error_retries:
                    delay = (2**attempt) + random.uniform(0.0, 0.25)
                    time.sleep(delay)
                    continue
                raise OmniApiError(
                    f"GET {path} failed after server retries: {status}",
                    status_code=status,
                    retryable=True,
                )

            raise NonRetryableOmniApiError(
                f"GET {path} failed: {status}",
                status_code=status,
                retryable=False,
            )

        raise OmniApiError(f"GET {path} failed unexpectedly.", retryable=True)

    def list_connections(self) -> list[dict[str, Any]]:
        data = self._get_json("/v1/connections")
        return data.get("connections", []) or []

    def list_models(
        self,
        page_size: int = 50,
        cursor: str | None = None,
        model_kind: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"pageSize": page_size}
        if cursor:
            params["cursor"] = cursor
        # Omni filters server-side on `modelKind` (note: `kind` is rejected with
        # "Unrecognized key"). Left unset the listing returns every kind, which
        # is the default so a new model kind can never be silently dropped.
        if model_kind:
            params["modelKind"] = model_kind
        return self._get_json("/v1/models", params=params)

    def list_folders(self, page_size: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"pageSize": page_size, "include": "labels,_count"}
        if cursor:
            params["cursor"] = cursor
        return self._get_json("/v1/folders", params=params)

    def list_documents(self, page_size: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"pageSize": page_size, "include": "labels,_count"}
        if cursor:
            params["cursor"] = cursor
        return self._get_json("/v1/documents", params=params)

    def get_model_yaml(self, model_id: str, mode: str = "combined") -> dict[str, Any]:
        return self._get_json(f"/v1/models/{model_id}/yaml", params={"mode": mode})

    def get_document(self, identifier: str) -> dict[str, Any]:
        return self._get_json(f"/v1/documents/{identifier}")

    def get_topic(self, model_id: str, topic_name: str) -> dict[str, Any]:
        return self._get_json(f"/v1/models/{model_id}/topic/{topic_name}")

    @staticmethod
    def _paginate(response: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        records = response.get("records", []) or []
        page_info = response.get("pageInfo", {}) or {}
        has_next = bool(page_info.get("hasNextPage"))
        next_cursor = page_info.get("nextCursor")
        return records, (next_cursor if has_next else None)

    def _collect_paginated(
        self,
        list_fn,
        page_size: int,
        max_pages: int | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        page = 0
        # Omni's pageSize cap is per-endpoint, not global: /v1/documents and
        # /v1/models document a max of 100, but /v1/folders documents none — 100
        # works today, so the ceiling there is whatever the implementation
        # currently allows (confirmed by Omni, 2026-08-19, who suggested this
        # guard). On a 400 from a listing call, halve pageSize and retry rather
        # than failing the whole run at the top.
        effective_page_size = page_size
        while True:
            try:
                response = list_fn(page_size=effective_page_size, cursor=cursor)
            except NonRetryableOmniApiError as exc:
                if exc.status_code != 400 or effective_page_size <= 1:
                    raise
                effective_page_size = max(1, effective_page_size // 2)
                logger.warning(
                    f"Listing call rejected pageSize; halving to "
                    f"{effective_page_size} and retrying ({exc})."
                )
                continue
            records, cursor = self._paginate(response)
            rows.extend(records)
            page += 1
            if not cursor:
                break
            if max_pages is not None and page >= max_pages:
                break
        return rows

    def _walk_to_shared_owner(self, model_id: str) -> str:
        """Climb baseModelId until we hit a SHARED ancestor.

        Real Omni inheritance is deeper than one hop: schema -> shared ->
        extension/branch -> workbook. v0.3.0 canonicalized one level up, so an
        inherited topic still emitted a copy for every intermediate layer.
        On the customer's tenant this produced ~493 copies of the same topic.

        Also handles the SHARED_EXTENSION / BRANCH case: those kinds are not
        representable as OmniV01Model, so climbing past them lands on the
        SHARED ancestor that IS representable.

        Falls back to `model_id` if the chain doesn't reach a SHARED (chain
        broken, or terminates in a non-SHARED). The referential-closure gate
        will flag any dangling relationship this creates.
        """
        lookup = getattr(self, "_model_lookup", None) or {}
        seen: set[str] = set()
        current = model_id
        while current and current not in seen:
            seen.add(current)
            model = lookup.get(current)
            if not model:
                break
            if str(model.get("modelKind") or "").upper() == "SHARED":
                return current
            base = model.get("baseModelId")
            if not base:
                break
            current = base
        return model_id

    def _overridden_topic_names(self, model_id: str) -> set[str] | None:
        """Return the set of topic names that a workbook REDEFINES in its own layer.

        Uses mode=extension (the workbook's own YAML layer, not the merged combined
        view) so we can tell inherited topics from actually-overridden ones. Both
        plain filenames (order_analytics.topic) and refinement-prefixed ones
        (+order_analytics.topic) are normalised to the bare stem.

        Returns None on any error so callers can treat a failed fetch as
        "don't canonicalize" (fail-safe).
        """
        try:
            payload = self.get_model_yaml(model_id, mode="extension")
            files = payload.get("files", {}) or {}
            return {
                f.removesuffix(".topic").split("/")[-1].lstrip("+")
                for f in files
                if f.endswith(".topic")
            }
        except Exception as exc:
            self._swallow("override_probe", f"model_id={model_id}", exc)
            return None

    def _fetch_topics_for_model(self, model: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch and parse topics from a single model's YAML, enriched via the topic API.

        The model YAML is the source for enumerating topic names (no list endpoint
        exists). For each topic, we additionally call the topic detail API to pull
        source-table/schema/catalog, joined views, and dimension/measure names.
        If the topic detail call fails, we still emit the basic topic from YAML.

        For WORKBOOK models: topics that the workbook merely inherits from its
        base shared model are stamped with owningModelId=baseModelId so the
        transformer can collapse them to the shared-model's canonical QN. Topics
        the workbook actually redefines keep owningModelId=model_id (their own).
        """
        model_id = model.get("id")
        if not model_id:
            return []
        try:
            payload = self.get_model_yaml(model_id, mode="combined")
        except Exception as exc:
            self._swallow("model_yaml", f"model_id={model_id}", exc)
            return []

        kind = str(model.get("modelKind") or "").upper()
        base_model_id = model.get("baseModelId")
        is_workbook = kind == "WORKBOOK" and bool(base_model_id)
        overridden = self._overridden_topic_names(model_id) if is_workbook else None

        topics: list[dict[str, Any]] = []
        files = payload.get("files", {}) or {}
        # The combined-mode payload already contains this model's `.view` files
        # alongside its `.topic` files, so every field the transformer consumes
        # can be derived locally instead of costing one HTTP call per topic.
        views = self._views_from_payload(files)
        # Aliases ride in the same payload, so resolve them locally rather than
        # falling back to the topic API for every aliased join.
        view_aliases = self._view_aliases_from_payload(files)
        for file_name, file_content in files.items():
            if not file_name.endswith(".topic"):
                continue
            try:
                parsed = yaml.safe_load(file_content) or {}
            except yaml.YAMLError:
                continue
            # Omni topic YAML has no "name" field; derive it from the filename stem.
            # Filenames may include a group prefix (e.g. "COCO_DEMO/podcast_streaming.topic");
            # the topic API only accepts the bare stem without the directory prefix.
            stem = file_name.removesuffix(".topic").split("/")[-1]
            topic_name = parsed.get("name") or stem
            if not topic_name:
                continue

            # Determine canonical owning model. A workbook topic is either:
            #   - genuinely overridden (probe found `<name>.topic` or `+<name>.topic`
            #     in the extension YAML) -> owning is the workbook itself
            #   - inherited from the SHARED ancestor -> walk baseModelId until
            #     we hit that SHARED, so all N copies collapse to one QN
            # A failed extension probe (overridden is None) means "we don't know
            # which topics were overridden." Default to INHERITED (collapse) —
            # the alternative default (keep as workbook) fails toward duplication,
            # which is exactly the 170x bug we're fixing.
            genuinely_overridden = (
                is_workbook and overridden is not None and topic_name in overridden
            )
            if is_workbook and not genuinely_overridden:
                owning_model_id = self._walk_to_shared_owner(model_id)
            else:
                owning_model_id = model_id

            topic = {
                "modelId": model_id,
                "owningModelId": owning_model_id,
                "name": topic_name,
                "label": parsed.get("label"),
                "baseViewName": parsed.get("base_view") or parsed.get("base_view_name"),
            }
            # Derive the enrichment fields from the `.view` files in hand. Only
            # if that is impossible for this topic (no `.view` files in the
            # payload, or a `joins` block we cannot read) do we spend a request
            # on the topic-detail endpoint — so no consumed field is ever
            # silently dropped.
            detail = (
                self._topic_detail_from_views(
                    parsed, topic["baseViewName"], views, view_aliases
                )
                if views
                else None
            )
            if detail is None:
                # `owning_model_id` — not `model_id`. When a workbook genuinely
                # overrides the topic these are equal (see above), so overrides
                # still fetch their own copy; when it merely inherits, every
                # inheriting workbook shares one fetch.
                detail = self._fetch_topic_detail_cached(owning_model_id, topic_name)
            topic.update(detail)
            topics.append(topic)
        return topics

    @staticmethod
    def _view_aliases_from_payload(files: dict[str, Any]) -> dict[str, str]:
        """Map a relationship alias -> the underlying view name.

        `joins` names the relationship's ALIAS where one is defined, not the
        view (confirmed by Omni, 2026-08-19). The alias is declared on the
        relationship as `join_from_view_as`, alongside the real view in
        `join_from_view`:

            - join_from_view: users
              join_from_view_as: buyers        # `joins` will say "buyers"
              join_from_view_as_label: Buyers
              join_to_view: user_facts
              join_type: always_left
              on_sql: ${users.id} = ${user_facts.id}
              relationship_type: one_to_one

        Relationship definitions ride along in the same combined-mode payload
        (the `.relationships` file), so aliases resolve locally with no extra
        request. Accepts either a bare list of relationship mappings or a dict
        wrapping one under a `relationships`-style key, and ignores anything it
        does not recognise — an unresolved alias simply falls through to the
        topic-detail fallback rather than dropping a table.
        """
        aliases: dict[str, str] = {}

        def _absorb(entry: Any) -> None:
            if not isinstance(entry, dict):
                return
            alias = entry.get("join_from_view_as")
            actual = entry.get("join_from_view")
            if alias and actual:
                aliases.setdefault(str(alias), str(actual))

        for file_name, file_content in files.items():
            if not file_name.endswith(".relationships"):
                continue
            try:
                parsed = yaml.safe_load(file_content) or {}
            except yaml.YAMLError:
                continue
            if isinstance(parsed, list):
                for entry in parsed:
                    _absorb(entry)
            elif isinstance(parsed, dict):
                for value in parsed.values():
                    if isinstance(value, list):
                        for entry in value:
                            _absorb(entry)
                    else:
                        _absorb(value)
                _absorb(parsed)
        return aliases

    @staticmethod
    def _views_from_payload(files: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Parse the `.view` files carried in a combined-mode YAML payload.

        Keyed on the bare view name. Observed filename convention is
        "<CATALOG>.<SCHEMA>/<view_name>.view" (e.g.
        "WIDE_WORLD_IMPORTERS.PROCESSED_GOLD/dim_customer.view"); the directory
        prefix only restates catalog/schema, both of which the view body also
        carries, so the stem is all we need for lookup.
        """
        views: dict[str, dict[str, Any]] = {}
        for file_name, file_content in files.items():
            if not file_name.endswith(".view"):
                continue
            try:
                parsed = yaml.safe_load(file_content) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(parsed, dict):
                continue
            stem = file_name.removesuffix(".view").split("/")[-1]
            views[str(parsed.get("name") or stem)] = parsed
        return views

    @staticmethod
    def _joined_view_names(parsed_topic: dict[str, Any]) -> list[str] | None:
        """View names a topic joins, read from the topic YAML's `joins` block.

        `joins` is a RECURSIVELY NESTED mapping: each view is nested under the
        view it joins through, and a leaf ends with an empty object. Confirmed
        by Omni, 2026-08-19:

            joins:
              inventory_items:            # included in the topic
                products:                 # joined to inventory_items
                  distribution_centers: {}  # joined to products
              users: {}                   # included in the topic

        All four of those views belong in the topic, so every key at every
        depth is collected — reading only the top level would silently emit
        partial source lineage (2 of the 4 above), which is worse than falling
        back to the topic-detail call.

        Returns [] when the topic declares no joins. Returns None when `joins`
        is present in a shape we do not recognise, which the caller treats as
        "cannot derive locally" and answers with the topic-detail call for that
        one topic rather than emitting incomplete lineage.

        Note: a topic may also redefine relationships scoped to itself. That
        changes how views join, not WHICH views are included, so it does not
        affect the view set this returns (and we model only table-level
        lineage, not join semantics).
        """
        if "joins" not in parsed_topic:
            return []
        joins = parsed_topic.get("joins")
        if joins in (None, {}, []):
            return []

        names: list[str] = []
        seen: set[str] = set()

        def _walk(node: Any) -> bool:
            """Collect view names depth-first. False = unrecognised shape."""
            if node in (None, {}, []):
                return True
            if isinstance(node, dict):
                for key, child in node.items():
                    if not key:
                        continue
                    name = str(key)
                    if name not in seen:
                        seen.add(name)
                        names.append(name)
                    if not _walk(child):
                        return False
                return True
            # A list of bare view names is accepted as a leaf-only shape.
            if isinstance(node, list) and all(isinstance(j, str) for j in node):
                for j in node:
                    if j and j not in seen:
                        seen.add(j)
                        names.append(j)
                return True
            return False

        if not _walk(joins):
            return None
        return names

    def _topic_detail_from_views(
        self,
        parsed_topic: dict[str, Any],
        base_view_name: str | None,
        views: dict[str, dict[str, Any]],
        view_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Build `_fetch_topic_detail`'s exact output shape from local `.view` data.

        Returns None when the topic's joins cannot be read, OR when any view the
        topic references cannot be resolved from the payload — so the caller
        falls back to the HTTP call for that topic alone.

        The unresolvable-name case is not hypothetical: `joins` uses the
        RELATIONSHIP'S ALIAS where a relationship is aliased, not the underlying
        view name (confirmed by Omni, 2026-08-19). The alias mapping is declared
        somewhere we do not yet parse, so an aliased join simply will not match a
        `.view` we hold. Skipping it would emit source lineage that is silently
        missing a table; falling back fetches complete detail from the topic API
        for that topic instead. Fail safe rather than guess at the alias schema.

        `dimensionNames` / `measureNames` are emitted empty: their only source
        is the topic API's per-field `fully_qualified_name`, which `.view`
        bodies do not carry. Neither field has a consumer anywhere in `app/`
        and `typedefs.py` declares no dimension/measure attribute, so nothing
        downstream observes the difference. Flagged rather than reconstructed —
        we will not invent an FQN format we have not verified.
        """
        joined = self._joined_view_names(parsed_topic)
        if joined is None:
            return None
        # Base view first, then joins, each view once. The base view is normally
        # also listed in `joins` (it is "included in the topic"), and the topic
        # API returns each view exactly once — so de-dupe to match that shape
        # rather than emitting a duplicate viewSources row for it.
        ordered: list[str] = []
        for name in ([base_view_name] if base_view_name else []) + joined:
            if name and name not in ordered:
                ordered.append(name)
        aliases = view_aliases or {}
        view_sources: list[dict[str, Any]] = []
        for view_name in ordered:
            view = views.get(view_name)
            if not isinstance(view, dict) and view_name in aliases:
                # `joins` named a relationship alias; resolve to the real view.
                view = views.get(aliases[view_name])
            if not isinstance(view, dict):
                # Still unresolvable — an alias we could not map, or a view absent
                # from the payload. Give up on local derivation for this topic so
                # the caller fetches complete detail from the API, rather than
                # emitting lineage that is silently missing a table.
                return None
            # A view we DID resolve but that carries no `table_name` is legitimate
            # (a derived view with no physical table); skip it, do not fall back.
            table_name = view.get("table_name")
            if not table_name:
                continue
            view_sources.append(
                {
                    "viewName": view_name,
                    "tableName": table_name,
                    "schema": view.get("schema"),
                    "catalog": view.get("catalog"),
                }
            )
        base_view = (
            views.get(base_view_name)
            or views.get(aliases.get(base_view_name or "", ""))
            or {}
        ) if base_view_name else {}
        return {
            "sourceTableName": base_view.get("table_name"),
            "sourceSchema": base_view.get("schema"),
            "sourceCatalog": base_view.get("catalog"),
            "joinedViewNames": joined,
            "dimensionNames": [],
            "measureNames": [],
            "viewSources": view_sources,
        }

    def _fetch_topic_detail_cached(
        self, owning_model_id: str, topic_name: str
    ) -> dict[str, Any]:
        """Memoized `_fetch_topic_detail`, keyed on the OWNING model.

        Only successful (non-empty) results are cached: caching `{}` would let
        one transient failure permanently blank that topic for every workbook
        in the run, which is a worse trade than re-attempting it.

        A benign race can let two threads fetch the same key concurrently; the
        lock is not held across the request, because doing so would serialise
        the whole pool behind one HTTP call.
        """
        key = (owning_model_id, topic_name)
        with self._topic_detail_cache_lock:
            hit = self._topic_detail_cache.get(key)
        if hit is not None:
            return hit
        detail = self._fetch_topic_detail(owning_model_id, topic_name)
        if detail:
            with self._topic_detail_cache_lock:
                self._topic_detail_cache[key] = detail
        return detail

    def _fetch_topic_detail(self, model_id: str, topic_name: str) -> dict[str, Any]:
        """Fetch enriched topic data via the topic API. Returns {} on any error.

        Pulls from `GET /v1/models/{modelId}/topic/{topicName}`:
        - base view's `table_name` / `schema` / `catalog` for source lineage
        - names of joined views (excluding the base view)
        - fully-qualified dimension and measure names across all included views
        """
        try:
            payload = self.get_topic(model_id, topic_name)
        except Exception as exc:
            self._swallow(
                "topic_detail",
                f"model_id={model_id} topic={topic_name}",
                exc,
            )
            return {}
        try:
            topic = payload.get("topic") or {}
            views = topic.get("views") or []
            base_view_name = topic.get("base_view_name")

            base_view: dict[str, Any] = {}
            joined_view_names: list[str] = []
            dimension_names: list[str] = []
            measure_names: list[str] = []
            view_sources: list[dict[str, Any]] = []

            for view in views:
                if not isinstance(view, dict):
                    continue
                view_name = view.get("name")
                if view_name == base_view_name:
                    base_view = view
                elif view_name:
                    joined_view_names.append(view_name)
                table_name = view.get("table_name")
                if table_name:
                    view_sources.append(
                        {
                            "viewName": view_name,
                            "tableName": table_name,
                            "schema": view.get("schema"),
                            "catalog": view.get("catalog"),
                        }
                    )
                for dim in view.get("dimensions") or []:
                    if isinstance(dim, dict):
                        fqn = dim.get("fully_qualified_name")
                        if fqn:
                            dimension_names.append(fqn)
                for meas in view.get("measures") or []:
                    if isinstance(meas, dict):
                        fqn = meas.get("fully_qualified_name")
                        if fqn:
                            measure_names.append(fqn)

            return {
                "sourceTableName": base_view.get("table_name"),
                "sourceSchema": base_view.get("schema"),
                "sourceCatalog": base_view.get("catalog"),
                "joinedViewNames": joined_view_names,
                "dimensionNames": dimension_names,
                "measureNames": measure_names,
                "viewSources": view_sources,
            }
        except Exception as exc:
            self._swallow(
                "topic_detail",
                f"model_id={model_id} topic={topic_name} (parse)",
                exc,
            )
            return {}

    def _fetch_document_detail(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Fetch document detail and return enrichment fields. Returns {} on any error.

        The Omni `GET /v1/documents/{id}` endpoint returns the backing modelId
        and a `queryPresentations` array (one per dashboard tile/tab). Each
        presentation may carry a `topicName` and a `query` object containing
        its own `modelId`. We collect unique (modelId, topicName) pairs across
        all presentations so the dashboard can be linked to the topics it uses.
        """
        identifier = doc.get("identifier")
        if not identifier:
            return {}
        try:
            detail = self.get_document(identifier)
        except Exception as exc:
            self._swallow("document_detail", f"identifier={identifier}", exc)
            return {}

        try:
            doc_model_id = detail.get("modelId")

            tile_topics: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for presentation in detail.get("queryPresentations") or []:
                if not isinstance(presentation, dict):
                    continue
                topic_name = presentation.get("topicName")
                if not topic_name:
                    continue
                inner_query = presentation.get("query") or {}
                model_id = (
                    inner_query.get("modelId") if isinstance(inner_query, dict) else None
                ) or doc_model_id
                if not model_id:
                    continue
                key = (model_id, topic_name)
                if key not in seen:
                    seen.add(key)
                    tile_topics.append({"modelId": model_id, "topicName": topic_name})

            return {
                "modelId": doc_model_id,
                "tileTopics": tile_topics,
            }
        except Exception as exc:
            self._swallow(
                "document_detail",
                f"identifier={identifier} (parse)",
                exc,
            )
            return {}

    @staticmethod
    def _is_workbook_without_content(
        model: dict[str, Any],
        document_model_ids: set[str],
        aggressive: bool = True,
    ) -> bool:
        """True for a WORKBOOK model no document / dashboard / app references.

        Apps and saved workbooks both surface through `/v1/documents` (confirmed
        with the customer), so `document_model_ids` is the complete content
        set — a workbook model absent from it is an ephemeral session Omni
        auto-creates when a user opens a workbook. On a large tenant these
        outnumber real workbooks by roughly two orders of magnitude.

        `aggressive` is the config-flag escape hatch. When True (default),
        catches every content-less workbook, named or not. When False, only
        UNNAMED ones — the pre-fix v0.3.0 behaviour, kept for the rare tenant
        whose named workbooks legitimately aren't in the document set.
        """
        if str(model.get("modelKind") or "").upper() != "WORKBOOK":
            return False
        if str(model.get("id") or "") in document_model_ids:
            return False
        if not aggressive and model.get("name"):
            return False
        return True

    def fetch_snapshot(
        self,
        page_size: int = 50,
        max_pages: int | None = None,
        max_concurrency: int = 10,
        model_kinds: Sequence[str] | None = None,
        crawl_only_content_backed_workbooks: bool = True,
        abort: threading.Event | None = None,
    ) -> dict[str, Any]:
        # Cap rather than reject: an operator page_size of 500 would otherwise
        # 400 on every listing call.
        page_size = min(int(page_size or 50), OMNI_MAX_PAGE_SIZE)
        self._abort = abort
        # Reset per-run failure counters.
        self._failures = {k: 0 for k in _FAILURE_CATEGORIES}
        self._topic_detail_cache = {}
        connections = self.list_connections()
        logger.info(f"fetch_snapshot: {len(connections)} connections")

        if model_kinds:
            models = []
            _listed_ids: set[str] = set()
            for kind in model_kinds:
                for row in self._collect_paginated(
                    partial(self.list_models, model_kind=kind), page_size, max_pages
                ):
                    row_id = str(row.get("id") or "")
                    if row_id and row_id in _listed_ids:
                        continue
                    _listed_ids.add(row_id)
                    models.append(row)
        else:
            models = self._collect_paginated(self.list_models, page_size, max_pages)
        logger.info(f"fetch_snapshot: {len(models)} models listed")

        # Populate the per-run lookup used by _walk_to_shared_owner to climb
        # the baseModelId chain from an inherited topic to its true SHARED
        # ancestor (real Omni inheritance is deeper than one hop). Per-run
        # storage is safe: item 3 gives each activity its own ClientClass.
        self._model_lookup = {str(m["id"]): m for m in models if m.get("id")}

        folders = self._collect_paginated(self.list_folders, page_size, max_pages)
        logger.info(f"fetch_snapshot: {len(folders)} folders listed")

        documents = self._collect_paginated(self.list_documents, page_size, max_pages)
        logger.info(f"fetch_snapshot: {len(documents)} documents listed")

        # Document details run FIRST: they name the models a dashboard tile
        # actually reads, which is what lets us skip the YAML fan-out for every
        # other unnamed workbook without dropping a tile's topic reference.
        _seen_model_ids: set[str] = set()
        document_model_ids: list[str] = []
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            doc_futures = {
                executor.submit(self._fetch_document_detail, d): d for d in documents
            }
            total_docs = len(doc_futures)
            done_docs = 0
            for future in as_completed(doc_futures):
                doc = doc_futures[future]
                detail = future.result()
                model_id = detail.get("modelId")
                if model_id and model_id not in _seen_model_ids:
                    _seen_model_ids.add(model_id)
                    document_model_ids.append(model_id)
                doc.update(detail)
                done_docs += 1
                if done_docs % 10 == 0 or done_docs == total_docs:
                    logger.info(
                        f"fetch_snapshot progress: docs {done_docs}/{total_docs}"
                    )

        # Drop content-less workbook models before the YAML fan-out. Every
        # model still lands in `models` (so `model_to_connection` and the
        # `omniV01BaseModel` edge behave exactly as before); the transformer
        # is the second gate that keeps content-less workbook ENTITIES out of
        # the catalog. Both gates share the same predicate for consistency.
        # Document-detail failures leave document_model_ids INCOMPLETE, and the
        # aggressive filter deletes any WORKBOOK absent from that set — so an
        # incomplete set silently drops real, named workbooks while the run still
        # reports success. Fail open: fall back to the conservative filter
        # (unnamed content-less workbooks only) rather than deleting models on
        # incomplete evidence.
        doc_failures = self._failures.get("document_detail", 0)
        if doc_failures and crawl_only_content_backed_workbooks:
            logger.warning(
                f"document-detail fetch failed for {doc_failures}/{len(documents)} "
                f"documents; document_model_ids is incomplete. Degrading "
                f"crawl_only_content_backed_workbooks to False for this run so "
                f"named workbooks are not dropped on incomplete evidence."
            )
            crawl_only_content_backed_workbooks = False

        topic_models = [
            m for m in models
            if not self._is_workbook_without_content(
                m, _seen_model_ids, aggressive=crawl_only_content_backed_workbooks,
            )
        ]
        logger.info(
            f"fetch_snapshot: fetching topics for {len(topic_models)}/{len(models)} "
            f"models ({len(models) - len(topic_models)} content-less workbooks skipped)"
        )

        topics: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            model_futures = {
                executor.submit(self._fetch_topics_for_model, m): m
                for m in topic_models
            }
            total_models = len(model_futures)
            done_models = 0
            for future in as_completed(model_futures):
                topics.extend(future.result())
                done_models += 1
                if done_models % 5 == 0 or done_models == total_models:
                    # Failure tallies ride the progress line. The end-of-pass
                    # `fetch_snapshot failures:` summary below is never reached
                    # by a run that times out mid-pass — which is exactly the
                    # run whose failure counts you need.
                    logger.info(
                        f"fetch_snapshot progress: models {done_models}/{total_models}, "
                        f"topics_so_far={len(topics)}, "
                        f"topic_detail_cached={len(self._topic_detail_cache)}, "
                        f"failures={{"
                        f"model_yaml={self._failures['model_yaml']}, "
                        f"topic_detail={self._failures['topic_detail']}, "
                        f"document_detail={self._failures['document_detail']}}}"
                    )

        # Threshold check: abort BEFORE the transformer runs if the model pass
        # lost too much. Prior behaviour was to swallow every failure silently
        # and report success — the 29-minute run that lost 88% of its topics
        # was the sentinel case. Checked here rather than at the end of
        # fetch_snapshot because with maximum_attempts=1 (see workflow.py) a
        # late abort costs a full three-hour re-crawl.
        model_failures = self._failures.get("model_yaml", 0)
        if total_models > 0:
            failure_rate = model_failures / total_models
            if failure_rate > _MODEL_YAML_FAILURE_ABORT_THRESHOLD:
                raise OmniApiError(
                    f"model-YAML fetch failed for {model_failures}/{total_models} "
                    f"models ({failure_rate:.0%} > "
                    f"{_MODEL_YAML_FAILURE_ABORT_THRESHOLD:.0%}). Aborting before "
                    f"transform to avoid publishing a partial crawl.",
                    retryable=True,
                )
        logger.info(
            f"fetch_snapshot failures: "
            f"model_yaml={self._failures['model_yaml']}/{total_models} "
            f"override_probe={self._failures['override_probe']} "
            f"topic_detail={self._failures['topic_detail']} "
            f"document_detail={self._failures['document_detail']}/{len(documents)}"
        )

        for model in models:
            model["updatedAt"] = _parse_dt(model.get("updatedAt"))
        for doc in documents:
            doc["updatedAt"] = _parse_dt(doc.get("updatedAt"))

        logger.info(
            f"fetch_snapshot done: connections={len(connections)} models={len(models)} "
            f"folders={len(folders)} documents={len(documents)} topics={len(topics)}"
        )

        return {
            "connections": connections,
            "models": models,
            "folders": folders,
            "documents": documents,
            "topics": topics,
            "document_model_ids": document_model_ids,
            # Threaded to the transformer so its entity filter matches the
            # client's YAML pre-filter. Without this the transformer would
            # emit OmniV01Model entities for workbooks whose topics we
            # deliberately never fetched.
            "crawl_only_content_backed_workbooks": crawl_only_content_backed_workbooks,
        }
