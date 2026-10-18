"""Provider setup templates and endpoint-specific Chat Completions profiles."""
import re
from urllib.parse import urlsplit


def cloudflare_base_url(account_id):
    account_id = account_id.strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
        raise ValueError("أرسل Cloudflare Account ID المكوّن من 32 خانة hexadecimal، وليس API Token")
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id.lower()}/ai/v1"


def provider_preset(service, *, account_id=None):
    common = {"transport": "openai_compatible", "is_enabled": True}
    if service == "openrouter":
        return {**common, "id": "openrouter", "label": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1", "api_keys_env": "OPENROUTER_API_KEY"}
    if service == "cloudflare":
        return {**common, "id": "cloudflare", "label": "Cloudflare Workers AI",
                "base_url": cloudflare_base_url(account_id or ""), "api_keys_env": "CLOUDFLARE_API_TOKEN"}
    raise ValueError("Unknown provider preset")


def chat_service(base_url):
    """Match the actual destination, never a mutable provider identifier/label."""
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        return "generic"
    if url.scheme != "https" or port not in (None, 443):
        return "generic"
    if url.hostname == "openrouter.ai" and url.path.rstrip("/") == "/api/v1":
        return "openrouter"
    if url.hostname == "api.cloudflare.com" and re.fullmatch(
            r"/client/v4/accounts/[a-fA-F0-9]{32}/ai/v1/?", url.path):
        return "cloudflare"
    return "generic"
