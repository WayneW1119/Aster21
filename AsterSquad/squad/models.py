"""Host-owned model routes; browser/Agent input selects an allowlisted ID only."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def load_catalog(path):
    if not path:
        return {}
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ValueError("Model catalog must be a nonempty object")
    result = {}
    allowed = {"name", "url", "auth", "api_key_env", "auth_home", "auth_profile", "allow_http"}
    for ident, route in value.items():
        if (not isinstance(ident, str) or not ident.strip() or not isinstance(route, dict)
                or set(route) - allowed or not isinstance(route.get("name", ident), str)):
            raise ValueError("Invalid model catalog entry")
        route = {"name": ident, "auth": "api_key", "allow_http": False, **route}
        if route["auth"] not in {"api_key", "chatgpt"} or type(route["allow_http"]) is not bool:
            raise ValueError("Invalid model authentication")
        url = urlsplit(route.get("url", ""))
        if not url.hostname or url.username or url.password or url.query or url.fragment or (
                url.scheme != "https" and not (route["allow_http"] and url.scheme == "http")):
            raise ValueError("Model endpoint requires HTTPS, or explicit allow_http")
        if route["auth"] == "chatgpt":
            if route["url"] != "https://api.openai.com/v1/responses":
                raise ValueError("ChatGPT requires the official Responses endpoint")
            if not route.get("auth_home"):
                raise ValueError("ChatGPT route requires an explicit host auth_home")
            route["auth_home"] = str(Path(route["auth_home"]).expanduser().resolve(strict=True))
            if "api_key_env" in route:
                raise ValueError("ChatGPT route must not specify API credentials")
        else:
            import re
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", route.get("api_key_env", "")):
                raise ValueError("API route requires api_key_env")
            if route.get("auth_home") or route.get("auth_profile"):
                raise ValueError("API route cannot specify ChatGPT credentials")
        result[ident] = route
    return result


def worker_model(route):
    """Workers receive their selected key through one fixed environment slot."""
    result = dict(route)
    if result["auth"] == "api_key":
        result["api_key_env"] = "SQUAD_MODEL_KEY"
    return result
