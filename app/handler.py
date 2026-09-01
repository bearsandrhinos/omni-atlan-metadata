import asyncio
import threading
from typing import Any

from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

from .client import ClientClass

logger = get_logger(__name__)


# Argo passes workflow parameters as STRINGS, so the operator setting a flag to
# "false" arrives as the string "false" — and bool("false") is True. Every
# boolean read off the form/state path must go through this. "null" is in the
# falsey set because the app already treats the literal string "null" as a
# sentinel elsewhere (activities.py, handler.py).
_FALSEY = ("false", "0", "no", "off", "null", "none", "")


def _to_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSEY


class HandlerClass(HandlerInterface):
    def __init__(self, client: ClientClass | None = None):
        self.client = client or ClientClass()

    async def load(self, *args: Any, **kwargs: Any) -> None:
        # Server path: args[0] = body.model_dump() = {"credentials": {...}, "metadata": {...}}
        # Activities path: kwargs = {"credentials": {...}, ...}
        credentials = kwargs.get("credentials") or {}
        if args and isinstance(args[0], dict):
            credentials = args[0].get("credentials") or args[0]
        self.client.load_credentials(credentials)

    async def test_auth(self, *args: Any, **kwargs: Any) -> bool:
        # load() has already initialized the client with credentials.
        self.client.list_connections()
        return True

    async def preflight_check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Server path: args[0] = body.model_dump() = {"credentials": {...}, "metadata": {...}}
        # Activities path: kwargs = {"credentials": {...}, "metadata": {...}}
        # Return the SDK's per-check shape ({<checkName>: {success, successMessage,
        # failureMessage}}) — the setup UI iterates top-level keys expecting each
        # to carry `.success`. A flat {success, message, data} caused the UI to
        # render Data/Message/Success as "failed" even though the HTTP call was 200.
        self.client.list_connections()
        return {
            "connection": {
                "success": True,
                "successMessage": "Omni connection validated.",
                "failureMessage": "Could not reach Omni with the provided base URL / token.",
            }
        }

    async def fetch_metadata(self, *args: Any, **kwargs: Any) -> Any:
        metadata = kwargs.get("metadata") or {}
        page_size = int(metadata.get("page_size", 100))
        max_pages = metadata.get("max_pages")
        max_pages = int(max_pages) if max_pages not in (None, "", "null") else None
        max_concurrency = int(metadata.get("max_concurrency", 10))
        model_kinds = metadata.get("model_kinds") or None
        # Default True: on a large tenant most WORKBOOK models are ephemeral
        # sessions Omni auto-creates and are not worth crawling. Escape hatch
        # is the config flag `crawl_only_content_backed_workbooks=false`.
        raw_flag = metadata.get("crawl_only_content_backed_workbooks")
        crawl_only_content_backed_workbooks = _to_bool(raw_flag, True)
        # fetch_snapshot is synchronous and blocks for hours on a large tenant.
        # Calling it directly wedged the worker's event loop, so the activity's
        # own heartbeat could not fire and the worker never observed its
        # start_to_close timeout — it kept calling Omni for 9h44m after Temporal
        # had already timed the activity out. Off-loading to a thread keeps the
        # loop free to heartbeat and to receive the cancellation.
        abort = threading.Event()
        try:
            return await asyncio.to_thread(
                self.client.fetch_snapshot,
                page_size=page_size,
                max_pages=max_pages,
                max_concurrency=max_concurrency,
                model_kinds=model_kinds,
                crawl_only_content_backed_workbooks=crawl_only_content_backed_workbooks,
                abort=abort,
            )
        except asyncio.CancelledError:
            # asyncio.to_thread cannot kill the worker threads, so signal them
            # cooperatively: _get_json checks `abort` before every request and
            # the pool drains within one in-flight request per worker.
            abort.set()
            raise
