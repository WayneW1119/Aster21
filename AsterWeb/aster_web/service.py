"""Two explicit provider adapters; no inference calls or automatic fallback."""
import asyncio
import ipaddress
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit

import aiohttp
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class RetrievalError(Exception):
    def __init__(self, code, message, *, status=None, retryable=False):
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, retryable

    def public(self, provider):
        return {"provider": provider, "error": {"code": self.code, "message": str(self),
                "upstream_status": self.status, "retryable": self.retryable}}


def validate_provider(provider):
    if provider not in ("exa", "deepseek"):
        raise RetrievalError("invalid_argument", "provider 必须为 exa 或 deepseek")


def public_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port
        if (not isinstance(value, str) or not value or len(value) > 8192
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or parsed.scheme not in ("http", "https") or not host
                or parsed.username is not None or parsed.password is not None
                or "." not in host and ":" not in host
                or host.lower().endswith((".localhost", ".local", ".internal"))):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RetrievalError("invalid_argument", "url 须为无账号凭据的公网 HTTP/HTTPS 地址") from None
    return value


def clipped(value, limit):
    if not isinstance(value, str):
        raise RetrievalError("upstream_format", "供应商返回了无效的文本格式")
    return value[:limit], len(value) > limit


def http_status(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    for nested in getattr(error, "exceptions", ()):
        if status := http_status(nested):
            return status
    return None


def exa_results(text):
    """Exa's anonymous MCP returns documented text blocks rather than JSON.

    Parse only metadata lines; fail explicitly if that wire format changes.
    Page text remains untrusted content, never instructions or authentication.
    """
    blocks = re.split(r"(?m)^Title: ", text)[1:]
    results = []
    for block in blocks:
        title, _, body = block.partition("\n")
        match = re.match(r"URL: (https?://[^\s]+)\n", body)
        if not match:
            raise RetrievalError("upstream_format", "Exa 搜索结果缺少有效来源链接")
        rest = body[match.end():]
        published = re.match(r"Published: ([^\n]+)\n", rest)
        if published:
            rest = rest[published.end():]
        rest = re.sub(r"\AAuthor: [^\n]*\n", "", rest)
        rest = re.sub(r"\A(?:Highlights|Text|Summary):\s*\n?", "", rest).strip()
        result = {"title": title.strip(), "url": public_url(match[1]), "summary": rest}
        if published and published[1] not in ("N/A", "Unknown"):
            result["published_at"] = published[1]
        results.append(result)
    if not results and not re.fullmatch(r"\s*No (?:search )?results(?: found)?[.!]?\s*", text, re.I):
        raise RetrievalError("upstream_format", "Exa 未返回可识别的搜索结果，请检查服务状态")
    return results


class Service:
    def __init__(self, *, deepseek_key="", exa_key="", deepseek_url="https://api.deepseek.com/v1/tools"):
        self.deepseek_key, self.exa_key = deepseek_key, exa_key
        self.deepseek_url = deepseek_url.rstrip("/")
        self.capacity = asyncio.Semaphore(4)

    async def _deepseek(self, action, body):
        if not self.deepseek_key:
            raise RetrievalError("not_configured", "宿主尚未配置 DeepSeek 搜索 API 凭据")
        headers = {"Authorization": "Bearer " + self.deepseek_key}
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as http:
                async with http.post(self.deepseek_url + "/" + action, json=body, allow_redirects=False) as response:
                    if response.status != 200:
                        status = response.status
                        code = {401: "authentication", 402: "quota", 403: "access_denied", 404: "not_found",
                                429: "rate_limited"}.get(status, "upstream_http")
                        raise RetrievalError(code, "DeepSeek 请求失败；未自动重试或切换供应商", status=status,
                                             retryable=status in (408, 429) or status >= 500)
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise RetrievalError("response_too_large", "供应商响应超过 2 MiB 限制")
                    return json.loads(raw)
        except RetrievalError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise RetrievalError("upstream_unavailable", "DeepSeek 网络连接失败或超时", retryable=True) from None
        except (ValueError, UnicodeError):
            raise RetrievalError("upstream_format", "DeepSeek 未返回有效 JSON") from None

    async def _exa(self, tool, arguments):
        headers = {"x-api-key": self.exa_key} if self.exa_key else {}
        try:
            async with asyncio.timeout(35):
                async with streamablehttp_client("https://mcp.exa.ai/mcp", headers=headers,
                                                timeout=timedelta(seconds=25)) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
                        if result.isError:
                            raise RetrievalError("upstream_error", "Exa 工具调用失败；未自动切换供应商")
                        return "\n".join(item.text for item in result.content if item.type == "text")
        except RetrievalError:
            raise
        except Exception as error:
            # SDK exceptions can contain request details; never relay them to agents.
            status = http_status(error)
            code = {401: "authentication", 403: "access_denied", 429: "rate_limited"}.get(status, "upstream_unavailable")
            raise RetrievalError(code, "Exa 连接失败、限流或超时", status=status,
                                 retryable=status is None or status in (408, 429) or status >= 500) from None

    async def search(self, query, provider, limit=5):
        validate_provider(provider)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise RetrievalError("invalid_argument", "query 须为 1–128 个字符")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise RetrievalError("invalid_argument", "limit 须为 1–10 的整数")
        query = query.strip()
        async with self.capacity:
            if provider == "exa":
                rows = exa_results(await self._exa("web_search_exa", {"query": query, "numResults": limit}))
            else:
                data = await self._deepseek("search", {"query": query, "count": limit, "need_content": False})
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise RetrievalError("upstream_format", "DeepSeek 搜索结果格式无效")
                rows = []
                for item in data["results"]:
                    if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                        raise RetrievalError("upstream_format", "DeepSeek 搜索条目格式无效")
                    row = {"title": item["title"], "url": public_url(item.get("url")),
                           "summary": item.get("summary") or item.get("snippet") or ""}
                    if item.get("date") is not None:
                        row["published_at"] = item["date"]
                    rows.append(row)
        truncated = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row["title"], shortened = clipped(row["title"], 500)
            truncated |= shortened
            row["summary"], shortened = clipped(row["summary"], 1500)
            truncated |= shortened
        return {"provider": provider, "query": query, "results": rows, "truncated": truncated}

    async def fetch(self, url, provider, max_characters=5000):
        validate_provider(provider)
        url = public_url(url)
        if type(max_characters) is not int or not 100 <= max_characters <= 20000:
            raise RetrievalError("invalid_argument", "max_characters 须为 100–20000 的整数")
        async with self.capacity:
            if provider == "exa":
                raw = await self._exa("web_fetch_exa", {"urls": [url], "maxCharacters": max_characters + 1})
                match = re.match(r"\A# ([^\n]*)\nURL: (https?://[^\s]+)\n", raw)
                if not match:
                    raise RetrievalError("upstream_format", "Exa 未返回可识别的网页正文")
                title, text = match[1], raw[match.end():].lstrip()
            else:
                data = await self._deepseek("fetch", {"url": url})
                if not isinstance(data, dict) or not isinstance(data.get("content"), str) or not data["content"].strip():
                    raise RetrievalError("upstream_format", "DeepSeek 未返回有效网页正文")
                title, text = data.get("title", ""), data["content"]
        title, _ = clipped(title, 500)
        text, truncated = clipped(text, max_characters)
        return {"provider": provider, "url": url, "title": title, "content": text, "truncated": truncated}
