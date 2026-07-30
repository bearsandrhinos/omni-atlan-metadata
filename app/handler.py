from typing import Any

from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

from .client import ClientClass

logger = get_logger(__name__)


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
        page_size = int(metadata.get("page_size", 50))
        max_pages = metadata.get("max_pages")
        max_pages = int(max_pages) if max_pages not in (None, "", "null") else None
        max_concurrency = int(metadata.get("max_concurrency", 10))
        return self.client.fetch_snapshot(
            page_size=page_size,
            max_pages=max_pages,
            max_concurrency=max_concurrency,
        )
