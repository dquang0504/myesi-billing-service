from fastapi import Request


def extract_client_info(request: Request):
    """Lấy IP và User-Agent thật, có xử lý X-Forwarded-For."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    else:
        client_ip = request.client.host
    user_agent = request.headers.get("user-agent", "unknown")
    return client_ip, user_agent
