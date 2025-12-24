from starlette.requests import Request
from starlette.datastructures import Headers

from app.utils.extract_client_info import extract_client_info


def _build_request(headers=None, client_host="9.9.9.9"):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": Headers(headers or {}).raw,
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_extract_uses_forwarded_for():
    req = _build_request(
        headers={
            "x-forwarded-for": "1.2.3.4, 5.6.7.8",
            "user-agent": "test-agent",
        },
        client_host="7.7.7.7",
    )
    ip, ua = extract_client_info(req)
    assert ip == "1.2.3.4"
    assert ua == "test-agent"


def test_extract_fallback_to_client_host():
    req = _build_request(headers={"user-agent": "ua"}, client_host="8.8.8.8")
    ip, ua = extract_client_info(req)
    assert ip == "8.8.8.8"
    assert ua == "ua"


def test_extract_defaults_user_agent():
    req = _build_request(headers={}, client_host="8.8.4.4")
    ip, ua = extract_client_info(req)
    assert ip == "8.8.4.4"
    assert ua == "unknown"
