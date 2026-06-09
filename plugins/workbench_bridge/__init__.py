"""Hermes gateway bridge for 91 AI Workbench.

This plugin is intentionally thin: Hermes remains the messaging gateway,
while 91 AI Workbench owns capture-only routing, /tower commands, review,
memory, and worker orchestration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WORKBENCH_URL = "http://127.0.0.1:8000"
URL_RE = re.compile(r"https?://\S+")
MEDIA_MESSAGE_TYPES = {"photo", "image", "document", "video", "audio", "voice"}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)


def _pre_gateway_dispatch(event, gateway, **_kwargs) -> dict | None:
    if getattr(event, "internal", False):
        return None

    text = str(getattr(event, "text", "") or "").strip()
    media_urls = list(getattr(event, "media_urls", []) or [])
    message_type = _message_type(event)
    if not text and not media_urls and message_type not in MEDIA_MESSAGE_TYPES:
        return None

    source = getattr(event, "source", None)
    platform = _platform_value(getattr(source, "platform", None))
    if not _platform_enabled(platform):
        return None

    payload = _build_payload(event, source, platform, text)
    workbench_url = _workbench_url()
    timeout = _timeout_seconds()

    async def _forward_and_reply() -> None:
        try:
            response = await asyncio.to_thread(_post_ingress, workbench_url, payload, timeout)
            message = str(response.get("message") or "已转发到 91 AI Workbench。")
        except Exception as exc:
            logger.warning("Workbench bridge forwarding failed after %.1fs: %s", timeout, exc)
            message = f"⚠️ Workbench 转发失败：{exc}"
        await _send_reply(gateway, event, message)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_forward_and_reply())
    except RuntimeError:
        logger.warning("Workbench bridge has no running event loop; skipping")
        return None

    return {"action": "skip", "reason": "workbench_bridge"}


def _build_payload(event, source, platform: str, text: str) -> dict[str, Any]:
    raw_url = _first_url(text)
    media_urls = list(getattr(event, "media_urls", []) or [])
    media_types = list(getattr(event, "media_types", []) or [])
    message_type = _message_type(event)
    raw_text = text or _media_placeholder(message_type, media_urls)
    return {
        "text": raw_text,
        "url": raw_url,
        "content_type": "link" if raw_url else message_type,
        "source": f"hermes.{platform}" if platform else "hermes",
        "source_message_id": getattr(event, "message_id", None),
        "sender": {
            "user_id": getattr(source, "user_id", None),
            "user_name": getattr(source, "user_name", None),
        },
        "context": {
            "chat_id": getattr(source, "chat_id", None),
            "chat_type": getattr(source, "chat_type", None),
            "thread_id": getattr(source, "thread_id", None),
            "platform": platform,
        },
        "adapter_metadata": {
            "bridge": "hermes.workbench_bridge",
            "message_type": message_type,
            "media_urls": media_urls,
            "media_types": media_types,
            "media_cache_status": "cached" if media_urls else "missing",
            "reply_to_message_id": getattr(event, "reply_to_message_id", None),
        },
    }


def _post_ingress(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/api/adapters/hermes/ingress"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:200]}") from exc
    return json.loads(raw)


async def _send_reply(gateway, event, message: str) -> None:
    source = getattr(event, "source", None)
    adapter = getattr(gateway, "adapters", {}).get(getattr(source, "platform", None))
    if adapter is None:
        logger.warning("Workbench bridge reply skipped: adapter missing")
        return
    metadata = None
    if hasattr(gateway, "_thread_metadata_for_source"):
        try:
            metadata = gateway._thread_metadata_for_source(source)
        except Exception:
            metadata = None
    chat_id = getattr(source, "chat_id", None)
    attempts = _reply_attempts()
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            result = await adapter.send(chat_id, message, metadata=metadata)
            if getattr(result, "success", True) is False:
                error = getattr(result, "error", "unknown send failure")
                raise RuntimeError(str(error))
            if attempt > 1:
                logger.info("Workbench bridge reply sent after retry %s/%s", attempt, attempts)
            return
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = _reply_retry_delay(attempt)
            logger.warning(
                "Workbench bridge reply failed (attempt %s/%s), retrying in %.1fs: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    logger.error("Workbench bridge reply failed after %s attempt(s): %s", attempts, last_error)


def _workbench_url() -> str:
    return os.getenv("HERMES_WORKBENCH_URL", DEFAULT_WORKBENCH_URL).strip() or DEFAULT_WORKBENCH_URL


def _timeout_seconds() -> float:
    raw = os.getenv("HERMES_WORKBENCH_TIMEOUT", "10").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 10.0


def _reply_attempts() -> int:
    raw = os.getenv("HERMES_WORKBENCH_REPLY_ATTEMPTS", "6").strip()
    try:
        return max(1, min(int(raw), 12))
    except ValueError:
        return 6


def _reply_retry_delay(attempt: int) -> float:
    return min(60.0, 5.0 * (2 ** max(0, attempt - 1)))


def _platform_enabled(platform: str) -> bool:
    raw = os.getenv("HERMES_WORKBENCH_PLATFORMS", "").strip()
    if not raw:
        return True
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return platform in allowed


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _message_type(event) -> str:
    message_type = getattr(event, "message_type", None)
    return str(getattr(message_type, "value", message_type) or "text").strip().lower()


def _first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    return match.group(0).rstrip(").,，。") if match else None


def _media_placeholder(message_type: str, media_urls: list[Any]) -> str:
    count = len(media_urls) or (1 if message_type in MEDIA_MESSAGE_TYPES else 0)
    if count <= 0:
        return ""
    label = {
        "photo": "图片",
        "image": "图片",
        "document": "文件",
        "video": "视频",
        "audio": "音频",
        "voice": "语音",
    }.get(message_type, "媒体")
    return f"[Hermes {label}消息：{count} 个附件]"
