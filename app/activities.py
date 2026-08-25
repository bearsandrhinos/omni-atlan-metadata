import json
import os
from pathlib import Path
from typing import Any, Dict

from app.client import ClientClass, NonRetryableOmniApiError, OmniApiError
from app.handler import HandlerClass
from app.transformer import OmniMetadataTransformer
from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.io.json import JsonFileWriter
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.secretstore import SecretStore
from temporalio import activity
from temporalio.exceptions import ApplicationError

logger = get_logger(__name__)
activity.logger = logger


class ActivitiesClass(ActivitiesInterface):
    def __init__(self, handler: HandlerClass | None = None):
        self.handler = handler or HandlerClass()

    @activity.defn
    async def get_workflow_args(
        self, workflow_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        base_args = await super().get_workflow_args(workflow_config)

        # The SDK lays out form fields differently depending on the caller:
        # - Production marketplace UI nests under payload/metadata/credentials
        # - The local playground flattens fields onto base_args itself
        # Look in all four locations and use whichever populated first.
        payload = base_args.get("payload", {}) or {}
        metadata_in = base_args.get("metadata", {}) or {}
        credentials_in = base_args.get("credentials", {}) or {}

        def _form_value(key: str) -> Any:
            for src in (payload, metadata_in, credentials_in, base_args):
                if key in src and src[key] not in (None, ""):
                    return src[key]
            return None

        # connection_epoch_ms is the third segment of the Atlan Connection's
        # qualifiedName (default/omni/<epoch_ms>). The marketplace UI doesn't
        # collect it as a form field — the SDK hands the resolved Connection
        # to us via base_args["connection"]. Local playground runs don't have
        # that blob, so fall back to the form field there.
        def _parse_connection(value: Any) -> dict:
            # Argo parameters are always strings — the connection widget
            # delivers stringified JSON; state-store paths deliver dicts.
            if isinstance(value, str) and value.strip():
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    logger.warning("connection is not valid JSON; ignoring.")
                    return {}
            return value if isinstance(value, dict) else {}

        def _connection_qualified_name() -> str:
            # Platform-normalized flat key first, Atlas-shaped attributes
            # fallback, metadata.connection as a last resort.
            for source in (base_args.get("connection"), metadata_in.get("connection")):
                blob = _parse_connection(source)
                qn = blob.get("connection_qualified_name") or (
                    blob.get("attributes") or {}
                ).get("qualifiedName")
                if qn:
                    return str(qn)
            return ""

        qn = _connection_qualified_name()
        connection_epoch_ms = qn.split("/")[2] if qn.count("/") >= 2 else ""
        if not connection_epoch_ms:
            connection_epoch_ms = str(_form_value("connection_epoch_ms") or "").strip()
        if not connection_epoch_ms or not connection_epoch_ms.isdigit():
            logger.error(
                f"connection_epoch_ms validation failed. "
                f"base_keys={sorted(base_args.keys())} "
                f"connection_qn={qn!r} "
                f"form_value={_form_value('connection_epoch_ms')!r}"
            )
            raise ApplicationError(
                "Could not resolve a numeric connection_epoch_ms (13-digit "
                "millisecond epoch). Expected to derive it from the third "
                "segment of base_args['connection'].attributes.qualifiedName "
                "(default/omni/<epoch_ms>) or from a connection_epoch_ms "
                "form field for local playground runs.",
                non_retryable=True,
            )
        page_size_raw = _form_value("page_size") or 100
        max_pages_raw = _form_value("max_pages")
        timeout_raw = _form_value("timeout_seconds") or 30
        max_concurrency_raw = _form_value("max_concurrency") or 10

        def _to_int(value: Any, default: int | None = None) -> int | None:
            if value in (None, "", "null"):
                return default
            return int(value)

        def _to_str_str_map(value: Any) -> dict[str, str]:
            """Coerce a JSON string or dict into {str: str}; tolerate empties."""
            if not value:
                return {}
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    logger.warning("atlan_source_connection_map is not valid JSON; ignoring.")
                    return {}
            if not isinstance(value, dict):
                return {}
            return {str(k): str(v) for k, v in value.items() if k and v}

        atlan_source_connection_map = _to_str_str_map(
            _form_value("atlan_source_connection_map")
        )

        save_output_raw = _form_value("save_output_local")
        verify_ssl_raw = _form_value("verify_ssl")

        # Local dev convenience: when the operator launched the app via
        # OMNI_LOCAL_UI=1, force the local NDJSON dump on so dry-runs leave
        # a file on disk the inspector can read. In production OMNI_LOCAL_UI
        # is unset, so this has no effect.
        if os.environ.get("OMNI_LOCAL_UI", "").lower() in ("1", "true", "yes"):
            save_output_raw = True

        # Escape hatch: `crawl_only_content_backed_workbooks=false` restores the
        # v0.3.0 behaviour of crawling every workbook, named or not. Default
        # True — the aggressive filter (drop WORKBOOK models absent from
        # /v1/documents) cuts ~90% of API traffic on tenants like the one
        # with 19,937 models where most are ephemeral sessions.
        aggressive_raw = _form_value("crawl_only_content_backed_workbooks")
        aggressive_workbook_filter = True if aggressive_raw is None else bool(aggressive_raw)

        metadata: dict[str, Any] = {
            "page_size": _to_int(page_size_raw, 100),
            "max_pages": _to_int(max_pages_raw, None),
            "connection_epoch_ms": connection_epoch_ms,
            "output_file": _form_value("output_file") or "omni_entities.ndjson",
            "save_output_local": False if save_output_raw is None else bool(save_output_raw),
            "max_concurrency": _to_int(max_concurrency_raw, 10),
            "atlan_source_connection_map": atlan_source_connection_map,
            "crawl_only_content_backed_workbooks": aggressive_workbook_filter,
        }

        credentials = {
            "omni_base_url": _form_value("omni_base_url"),
            "omni_api_token": _form_value("omni_api_token"),
            "verify_ssl": True if verify_ssl_raw is None else bool(verify_ssl_raw),
            "timeout_seconds": _to_int(timeout_raw, 30),
            "rate_limit_rpm": _to_int(_form_value("rate_limit_rpm"), 60),
        }

        # output_path is set by the SDK's get_workflow_args; fall back to a local temp dir.
        output_path = base_args.get("output_path") or os.path.join(
            TEMPORARY_PATH,
            base_args.get("workflow_id", "omni-extraction"),
            base_args.get("workflow_run_id", "local-run"),
        )

        return {
            "workflow_id": base_args.get("workflow_id", "omni-extraction"),
            "workflow_run_id": base_args.get("workflow_run_id", "local-run"),
            "output_path": output_path,
            # Pass the guid, never the resolved secrets: this dict is an
            # activity result, which Temporal persists in event history.
            "credential_guid": str(base_args.get("credential_guid") or "").strip(),
            "credentials": credentials,
            "metadata": metadata,
        }

    @activity.defn
    @auto_heartbeater
    async def extract_and_transform_metadata(
        self, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        credentials = dict(args.get("credentials") or {})
        credential_guid = str(args.get("credential_guid") or "").strip()
        if credential_guid and not (
            credentials.get("omni_base_url") and credentials.get("omni_api_token")
        ):
            # Production path: the operator's credential values live in the
            # credential store keyed by credential_guid (stored as host /
            # password). Resolve them here, inside the activity, so the
            # secrets never appear in an activity result.
            resolved = await SecretStore.get_credentials(credential_guid)
            credentials["omni_base_url"] = (
                credentials.get("omni_base_url")
                or resolved.get("host")
                or resolved.get("omni_base_url")
            )
            credentials["omni_api_token"] = (
                credentials.get("omni_api_token")
                or resolved.get("password")
                or resolved.get("omni_api_token")
            )
        # Build a per-run client so credentials, HTTP client, cancel flag, etc.
        # never leak between concurrent activities (the SDK creates one
        # ActivitiesClass per pod and runs up to five activities concurrently
        # — a shared self.handler.client corrupts runs and, worse, can pipe
        # one tenant's request through another tenant's token). The rate
        # limiter itself remains process-wide via _get_host_rate_limiter,
        # since throttling belongs to the Omni host, not the run.
        run_client = ClientClass(credentials=credentials)
        run_handler = HandlerClass(client=run_client)
        try:
            snapshot = await run_handler.fetch_metadata(metadata=args["metadata"])
        except NonRetryableOmniApiError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        except OmniApiError as exc:
            raise ApplicationError(str(exc), non_retryable=not exc.retryable) from exc
        finally:
            run_client.close()

        transformer = OmniMetadataTransformer(
            connection_epoch_ms=args["metadata"]["connection_epoch_ms"],
            atlan_source_connection_map=args["metadata"].get("atlan_source_connection_map", {}),
        )
        entities = transformer.transform(snapshot=snapshot)

        # Write entities via the SDK writer, which uploads to the Atlan object store
        # (when ENABLE_ATLAN_UPLOAD=true in production) or writes locally (in dev).
        writer = JsonFileWriter(
            path=os.path.join(args["output_path"], "transformed"),
            typename="omni_entities",
            retain_local_copy=args["metadata"].get("save_output_local", False),
        )
        await writer.write(entities)
        stats = await writer.close()

        # Optional additional local debug write at the user-specified output_file path.
        if args["metadata"].get("save_output_local"):
            import json
            output_path_local = Path(args["metadata"]["output_file"])
            with output_path_local.open("w", encoding="utf-8") as handle:
                for entity in entities:
                    handle.write(json.dumps(entity))
                    handle.write("\n")

        return {
            "success": True,
            "entity_count": stats.total_record_count,
            "output_path": args["output_path"],
            "snapshot_counts": {
                "connections": len(snapshot.get("connections", [])),
                "models": len(snapshot.get("models", [])),
                "topics": len(snapshot.get("topics", [])),
                "folders": len(snapshot.get("folders", [])),
                "documents": len(snapshot.get("documents", [])),
            },
        }
