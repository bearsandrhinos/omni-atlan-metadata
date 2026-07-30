from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import random
import threading
import time
from typing import Any

import httpx
import yaml
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Omni's documented API rate limit. Each request gates through a token bucket
# so concurrent threads collectively respect the cap.
OMNI_DEFAULT_RPM = 60


class _RateLimiter:
    """Thread-safe minimum-interval gate. With rpm=60 calls are spaced ≥1s apart."""

    def __init__(self, rpm: int):
        self.min_interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

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


def _parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


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


class ClientClass:
    def __init__(
        self,
        credentials: dict[str, Any] | None = None,
        rpm: int = OMNI_DEFAULT_RPM,
    ):
        self._credentials: OmniCredentials | None = None
        self._http_client: httpx.Client | None = None
        self._rate_limiter = _RateLimiter(rpm)
        if credentials:
            self.load_credentials(credentials)

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

        # Reconfigure the rate limiter if the operator passed a custom rpm.
        rpm_raw = credentials.get("rate_limit_rpm")
        if rpm_raw not in (None, "", 0):
            self._rate_limiter = _RateLimiter(int(rpm_raw))
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
            if status < 400:
                data = response.json()
                if not isinstance(data, dict):
                    raise NonRetryableOmniApiError(
                        f"GET {path} returned non-object response.",
                        status_code=status,
                        retryable=False,
                    )
                return data

            if status == 429:
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

    def list_models(self, page_size: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"pageSize": page_size}
        if cursor:
            params["cursor"] = cursor
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
        while True:
            response = list_fn(page_size=page_size, cursor=cursor)
            records, cursor = self._paginate(response)
            rows.extend(records)
            page += 1
            if not cursor:
                break
            if max_pages is not None and page >= max_pages:
                break
        return rows

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
        except Exception:
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
        except Exception:
            return []

        kind = str(model.get("modelKind") or "").upper()
        base_model_id = model.get("baseModelId")
        is_workbook = kind == "WORKBOOK" and bool(base_model_id)
        overridden = self._overridden_topic_names(model_id) if is_workbook else None

        topics: list[dict[str, Any]] = []
        files = payload.get("files", {}) or {}
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

            # Determine canonical owning model. For an inherited (non-overridden)
            # workbook topic, anchor to the base shared model so the transformer
            # emits one OmniV01Topic per logical topic rather than one per workbook.
            # If the extension-YAML fetch failed (overridden is None), be
            # conservative and keep the workbook as owner (no canonicalization).
            if is_workbook and overridden is not None and topic_name not in overridden:
                owning_model_id = base_model_id
            else:
                owning_model_id = model_id

            topic = {
                "modelId": model_id,
                "owningModelId": owning_model_id,
                "name": topic_name,
                "label": parsed.get("label"),
                "baseViewName": parsed.get("base_view") or parsed.get("base_view_name"),
            }
            topic.update(self._fetch_topic_detail(model_id, topic_name))
            topics.append(topic)
        return topics

    def _fetch_topic_detail(self, model_id: str, topic_name: str) -> dict[str, Any]:
        """Fetch enriched topic data via the topic API. Returns {} on any error.

        Pulls from `GET /v1/models/{modelId}/topic/{topicName}`:
        - base view's `table_name` / `schema` / `catalog` for source lineage
        - names of joined views (excluding the base view)
        - fully-qualified dimension and measure names across all included views
        """
        try:
            payload = self.get_topic(model_id, topic_name)
        except Exception:
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
        except Exception:
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
        except Exception:
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
        except Exception:
            return {}

    def fetch_snapshot(
        self,
        page_size: int = 50,
        max_pages: int | None = None,
        max_concurrency: int = 10,
    ) -> dict[str, Any]:
        connections = self.list_connections()
        logger.info(f"fetch_snapshot: {len(connections)} connections")

        models = self._collect_paginated(self.list_models, page_size, max_pages)
        logger.info(f"fetch_snapshot: {len(models)} models listed")

        folders = self._collect_paginated(self.list_folders, page_size, max_pages)
        logger.info(f"fetch_snapshot: {len(folders)} folders listed")

        documents = self._collect_paginated(self.list_documents, page_size, max_pages)
        logger.info(f"fetch_snapshot: {len(documents)} documents listed")

        # Fetch model YAMLs and document details concurrently in a shared pool.
        # Both batches share workers; `as_completed` drives progress logging so
        # operators can see how far along long-running tenants are.
        topics: list[dict[str, Any]] = []
        _seen_model_ids: set[str] = set()
        document_model_ids: list[str] = []
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            model_futures = {
                executor.submit(self._fetch_topics_for_model, m): m for m in models
            }
            doc_futures = {
                executor.submit(self._fetch_document_detail, d): d for d in documents
            }
            total_models = len(model_futures)
            total_docs = len(doc_futures)
            done_models = 0
            done_docs = 0

            for future in as_completed({**model_futures, **doc_futures}):
                if future in model_futures:
                    topics.extend(future.result())
                    done_models += 1
                    if done_models % 5 == 0 or done_models == total_models:
                        logger.info(
                            f"fetch_snapshot progress: models {done_models}/{total_models}, "
                            f"docs {done_docs}/{total_docs}, topics_so_far={len(topics)}"
                        )
                else:
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
                            f"fetch_snapshot progress: models {done_models}/{total_models}, "
                            f"docs {done_docs}/{total_docs}, topics_so_far={len(topics)}"
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
        }
