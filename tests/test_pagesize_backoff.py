import httpx, respx, pytest
from app.client import ClientClass

BASE = "https://t.omniapp.co/api"

def _client():
    c = ClientClass()
    c.load_credentials({"omni_base_url": BASE, "omni_api_token": "t", "rate_limit_rpm": 0})
    return c

@respx.mock
def test_folders_400_on_page_100_halves_and_succeeds():
    """Omni does not document a /v1/folders cap; if it ever tightens, halve and retry."""
    seen = []
    def handler(request):
        ps = int(httpx.URL(str(request.url)).params["pageSize"])
        seen.append(ps)
        if ps > 50:
            return httpx.Response(400, json={"detail": "Bad Request: pageSize: Page size cannot exceed 50"})
        return httpx.Response(200, json={"records": [{"id": "f1"}], "pageInfo": {"hasNextPage": False}})
    respx.get(url__startswith=f"{BASE}/v1/folders").mock(side_effect=handler)
    rows = _client()._collect_paginated(ClientClass.list_folders.__get__(_client()), 100, None)
    assert seen == [100, 50], seen          # tried 100, halved to 50
    assert [r["id"] for r in rows] == ["f1"]

@respx.mock
def test_persistent_400_still_raises_rather_than_looping():
    respx.get(url__startswith=f"{BASE}/v1/folders").mock(
        return_value=httpx.Response(400, json={"detail": "always"})
    )
    c = _client()
    with pytest.raises(Exception):
        c._collect_paginated(ClientClass.list_folders.__get__(c), 100, None)
