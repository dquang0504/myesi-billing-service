import pytest


@pytest.mark.asyncio
async def test_usage_requires_user_header(async_client):
    resp = await async_client.get("/api/billing/usage")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing X-User-ID header"
