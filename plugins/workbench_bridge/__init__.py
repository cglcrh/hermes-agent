"""Hermes gateway bridge for 91 AI Workbench.

This plugin is intentionally thin: Hermes remains the messaging gateway,
while 91 AI Workbench owns capture-only routing, /tower commands, review,
memory, and worker orchestration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WORKBENCH_URL = "http://127.0.0.1:8000"
DEFAULT_WORKBENCH_TARGET = "v1"
DEFAULT_V2_RELAY_PATH = "/hermes-relay"
DEFAULT_V2_RELAY_URL = f"http://127.0.0.1:8790{DEFAULT_V2_RELAY_PATH}"
DEFAULT_V2_SECRET_ENV = "AIWB_V2_HERMES_RELAY_SECRET"
HERMES_DELIVERY_SCHEMA_VERSION = 1
URL_RE = re.compile(r"https?://\S+")
MEDIA_MESSAGE_TYPES = {"photo", "image", "document", "video", "audio", "voice"}
V2_GATEWAYS = {"telegram", "feishu", "wechat", "web", "cli"}
V2_ATTACHMENT_MESSAGE_TYPES = {"photo", "image", "document", "video", "audio", "voice"}
V2_DELIVERY_GATEWAYS = {"telegram", "feishu", "wechat"}
V2_DELIVERY_REQUIRED_FIELDS = (
    "delivery_request_id",
    "delivery_id",
    "ticket_ref",
    "event_session_id",
    "action_set_id",
    "gateway",
    "conversation_ref",
    "message_text",
    "copy_block",
    "idempotency_key",
)
V2_DELIVERY_FORBIDDEN_KEYS = {
    "active_prompt",
    "agent_loop",
    "authorization",
    "cookie",
    "headers",
    "memory_write",
    "provider_dispatch",
    "raw_body",
    "raw_event",
    "raw_headers",
    "raw_payload",
    "raw_response",
    "raw_update",
    "secret",
    "set_cookie",
    "telegram_bot_token",
    "token",
    "webhook_secret",
}
SECRET_TEXT_RE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+\S+|token\s*[:=]\s*[^&\s;,]+|secret\s*[:=]\s*[^&\s;,]+|session=|xox[baprs]-|bot\d+:)"
)
SAFE_DELIVERY_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,160}$")
SAFE_TICKET_REF_RE = re.compile(r"^[a-z0-9]{12}$")
_V2_DELIVERY_ATTEMPTS: set[str] = set()


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

    target = _workbench_target()
    payload = _build_v2_relay_payload(event, source, platform, text) if target == "v2" else _build_payload(
        event,
        source,
        platform,
        text,
    )
    timeout = _timeout_seconds()

    async def _forward_and_reply() -> None:
        metadata = None
        try:
            response = await asyncio.to_thread(_post_target, target, payload, timeout)
            message = str(response.get("message") or "已转发到 91 AI Workbench。")
            if target == "v2":
                metadata = _v2_reply_metadata(response, platform=platform)
        except Exception as exc:
            logger.warning("Workbench bridge forwarding failed after %.1fs: %s", timeout, exc)
            message = f"⚠️ Workbench 转发失败：{exc}"
        await _send_reply(gateway, event, message, extra_metadata=metadata)

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
    reply_to_message_id = getattr(event, "reply_to_message_id", None)
    reply_to_text = _safe_text(getattr(event, "reply_to_text", None))
    reply_to = _reply_to_metadata(reply_to_message_id, reply_to_text)
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
            "reply_to_message_id": reply_to_message_id,
            "reply_to_text": reply_to_text,
            **({"reply_to": reply_to} if reply_to else {}),
        },
    }


def _reply_to_metadata(message_id: Any, text: str | None) -> dict[str, Any] | None:
    if message_id is None and not text:
        return None
    return {
        "message_id": message_id,
        "text": text,
    }


def _safe_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _build_v2_relay_payload(event, source, platform: str, text: str) -> dict[str, Any]:
    message_type = _message_type(event)
    media_urls = list(getattr(event, "media_urls", []) or [])
    media_types = list(getattr(event, "media_types", []) or [])
    message_ref = _safe_ref(platform, "message", getattr(event, "message_id", None), fallback_parts=(text, message_type))
    conversation_ref = _safe_ref(
        platform,
        "chat",
        getattr(source, "chat_id", None),
        fallback_parts=(getattr(source, "chat_type", None), getattr(source, "thread_id", None)),
    )
    sender_ref = _safe_ref(
        platform,
        "user",
        getattr(source, "user_id", None) or getattr(source, "user_name", None),
        fallback_parts=(getattr(source, "chat_id", None),),
    )
    timestamp = _event_timestamp_iso(event)
    relay_id = _relay_id(platform, message_ref, timestamp)
    raw_url = _first_url(text)
    attachments = _v2_attachments(platform, message_ref, media_urls, media_types, message_type)
    return {
        "schema_version": 1,
        "relay_source": "hermes",
        "relay_id": relay_id,
        "gateway": platform if platform in V2_GATEWAYS else "other",
        "conversation_ref": conversation_ref,
        "message_ref": message_ref,
        "sender_ref": sender_ref,
        "kind": _v2_kind(message_type, raw_url, attachments),
        "text": text or _media_placeholder(message_type, media_urls),
        "attachments": attachments,
        "reply_to_message_ref": _safe_optional_ref(platform, "message", getattr(event, "reply_to_message_id", None)),
        "thread_ref": _safe_optional_ref(platform, "thread", getattr(source, "thread_id", None)),
        "topic_ref": _safe_optional_ref(platform, "topic", getattr(source, "chat_topic", None)),
        "received_at": timestamp,
        "raw_payload_ref": f"hermes://event/{relay_id}",
        "gateway_metadata": {
            "bridge": "hermes.workbench_bridge",
            "bridge_target": "v2",
            "message_type": message_type,
            "media_count": len(media_urls),
            "media_cache_status": "cached" if media_urls else "missing",
            "chat_type": _safe_text(getattr(source, "chat_type", None)),
            "platform": platform or "other",
            **({"url_detected": True} if raw_url else {}),
            **({"reply_to_text_present": True} if _safe_text(getattr(event, "reply_to_text", None)) else {}),
        },
    }


def _v2_attachments(
    platform: str,
    message_ref: str,
    media_urls: list[Any],
    media_types: list[Any],
    message_type: str,
) -> list[dict[str, Any]]:
    if message_type not in V2_ATTACHMENT_MESSAGE_TYPES and not media_urls:
        return []
    count = len(media_urls) or 1
    attachments = []
    for index in range(count):
        media_url = str(media_urls[index] if index < len(media_urls) else "").strip()
        mime_type = _safe_text(media_types[index] if index < len(media_types) else None)
        attachment_id = _safe_ref(platform, "attachment", f"{message_ref}-{index + 1}")
        filename = _safe_filename(media_url)
        attachments.append(
            {
                "id": attachment_id,
                "kind": "image" if message_type in {"photo", "image"} else "file",
                "raw_ref": f"hermes://attachment/{platform or 'other'}/{attachment_id}",
                "filename": filename,
                "mime_type": mime_type,
                "metadata": {
                    "message_type": message_type,
                    "media_index": index,
                    "cache_status": "cached" if media_url else "missing",
                    "has_local_cache": bool(media_url),
                },
            }
        )
    return attachments


def _post_target(target: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    if target == "v2":
        return _post_v2_relay(_v2_relay_url(), payload, timeout)
    return _post_ingress(_workbench_url(), payload, timeout)


def _post_ingress(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/api/adapters/hermes/ingress"
    return _post_json(endpoint, payload, timeout, headers={})


def _post_v2_relay(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    return _post_json(url, payload, timeout, headers=_v2_secret_headers())


def _post_json(
    endpoint: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:200]}") from exc
    return json.loads(raw)


async def _send_reply(gateway, event, message: str, *, extra_metadata: dict[str, Any] | None = None) -> None:
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
    if extra_metadata:
        metadata = {**(metadata or {}), **extra_metadata}
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


def _v2_reply_metadata(response: dict[str, Any], *, platform: str) -> dict[str, Any] | None:
    if platform != "telegram":
        return None
    buttons = []
    actions = response.get("reply_actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        button = _v2_copy_text_button(action)
        if button is not None:
            buttons.append(button)
    if not buttons:
        return None
    return {"telegram_copy_text_buttons": buttons[:1]}


def _v2_copy_text_button(action: Any) -> dict[str, str] | None:
    if not isinstance(action, dict) or action.get("kind") != "copy_text":
        return None
    ticket_ref = action.get("ticket_ref")
    text = action.get("text")
    label = action.get("label")
    if not isinstance(ticket_ref, str) or not SAFE_TICKET_REF_RE.fullmatch(ticket_ref):
        return None
    if text != f"{ticket_ref} 选":
        return None
    if not isinstance(label, str):
        return None
    label = label.strip()
    if not label or len(label) > 20 or SECRET_TEXT_RE.search(label):
        return None
    return {"label": label, "text": text, "ticket_ref": ticket_ref}


async def send_v2_delivery_request(
    gateway,
    payload: dict[str, Any],
    *,
    ledger: set[str] | None = None,
) -> dict[str, Any]:
    """Send a v2 normalized delivery request through Hermes gateway adapters.

    This is intentionally not wired into the agent loop. Callers must pass an
    already-running gateway object; Hermes owns platform credentials and v2 only
    receives the sanitized result.
    """

    parsed = _parse_v2_delivery_request(payload)
    if not parsed["ok"]:
        return parsed
    request = parsed["request"]
    ledger = _V2_DELIVERY_ATTEMPTS if ledger is None else ledger
    attempt_key = str(request["idempotency_key"])
    if attempt_key in ledger:
        return _delivery_result(
            "blocked",
            "send_already_used",
            request=request,
            limitations=("delivery request idempotency key was already consumed",),
        )
    adapter = getattr(gateway, "adapters", {}).get(_platform_key(request["gateway"]))
    if adapter is None:
        return _delivery_result(
            "blocked",
            "runtime_unavailable",
            request=request,
            limitations=("gateway adapter is not available in this Hermes runtime",),
        )
    ledger.add(attempt_key)
    try:
        result = await adapter.send(str(request["conversation_ref"]), str(request["message_text"]), metadata={})
    except Exception as exc:
        return _delivery_result(
            "failed",
            "adapter_failed",
            request=request,
            failure_reason=type(exc).__name__,
        )
    if getattr(result, "success", True) is False:
        return _delivery_result(
            "failed",
            "adapter_failed",
            request=request,
            failure_reason=_safe_failure_reason(getattr(result, "error", None)),
        )
    message_ref = _safe_delivery_ref(getattr(result, "message_id", None))
    return _delivery_result(
        "delivered",
        "delivered",
        request=request,
        gateway_message_ref=message_ref,
    )


def _parse_v2_delivery_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _delivery_rejected("invalid_schema", "payload must be an object")
    if _contains_forbidden_delivery_payload(payload):
        return _delivery_rejected("forbidden_payload", "payload contains token/secret/raw/control-plane fields")
    schema_version = payload.get("schema_version", HERMES_DELIVERY_SCHEMA_VERSION)
    if schema_version != HERMES_DELIVERY_SCHEMA_VERSION:
        return _delivery_rejected("unsupported_schema_version", "unsupported schema_version")
    missing = [field for field in V2_DELIVERY_REQUIRED_FIELDS if not _safe_text(payload.get(field))]
    if missing:
        return _delivery_rejected("missing_required_field", ",".join(missing))
    gateway_name = str(payload["gateway"]).strip().lower()
    if gateway_name not in V2_DELIVERY_GATEWAYS:
        return _delivery_rejected("unsupported_gateway", "gateway is not supported for v2 delivery relay")
    ticket_ref = str(payload["ticket_ref"]).strip()
    copy_block = str(payload["copy_block"])
    message_text = str(payload["message_text"])
    if not re.fullmatch(r"[a-z0-9]{12}", ticket_ref):
        return _delivery_rejected("invalid_ticket_ref", "ticket_ref must be 12 lowercase letters/digits")
    if f"收件编号\n{ticket_ref} 选" != copy_block or copy_block not in message_text:
        return _delivery_rejected("missing_copy_block", "message_text must contain the exact ticket copy block")
    if not message_text.startswith("AIWB 已收件"):
        return _delivery_rejected("not_human_first", "message_text must be human-readable first")
    request = {
        "delivery_request_id": _safe_text(payload["delivery_request_id"]),
        "delivery_id": _safe_text(payload["delivery_id"]),
        "ticket_ref": ticket_ref,
        "event_session_id": _safe_text(payload["event_session_id"]),
        "action_set_id": _safe_text(payload["action_set_id"]),
        "gateway": gateway_name,
        "conversation_ref": _safe_text(payload["conversation_ref"]),
        "message_text": message_text,
        "copy_block": copy_block,
        "idempotency_key": _safe_text(payload["idempotency_key"]),
    }
    return {"ok": True, "request": request}


def _delivery_rejected(reason: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "rejected",
        "reason": reason,
        "failure_reason": message,
        "gateway_message_ref": None,
        "limitations": ["Hermes rejected the v2 delivery request before gateway send."],
    }


def _delivery_result(
    status: str,
    reason: str,
    *,
    request: dict[str, Any],
    gateway_message_ref: str | None = None,
    failure_reason: str | None = None,
    limitations: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "ok": status == "delivered",
        "status": status,
        "reason": reason,
        "delivery_request_id": request.get("delivery_request_id"),
        "delivery_id": request.get("delivery_id"),
        "ticket_ref": request.get("ticket_ref"),
        "gateway": request.get("gateway"),
        "gateway_message_ref": gateway_message_ref,
        "failure_reason": _safe_failure_reason(failure_reason),
        "limitations": list(limitations),
    }


def _contains_forbidden_delivery_payload(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).strip().lower()
            if key_text in V2_DELIVERY_FORBIDDEN_KEYS or "token" in key_text or "secret" in key_text:
                return True
            if _contains_forbidden_delivery_payload(nested):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(_contains_forbidden_delivery_payload(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return bool(SECRET_TEXT_RE.search(value) or "raw platform payload" in lowered)
    return False


def _safe_delivery_ref(value: Any) -> str | None:
    text = _safe_text(value)
    if not text or _contains_forbidden_delivery_payload(text):
        return None
    if any(char in text for char in ("\x00", "\n", "\r", "\t", "?", "#", "=")):
        return None
    return text if SAFE_DELIVERY_REF_RE.fullmatch(text) else None


def _safe_failure_reason(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    if _contains_forbidden_delivery_payload(text):
        return "<redacted>"
    return text.replace("\r", " ").replace("\n", " ")[:240]


def _platform_key(value: Any) -> Any:
    text = str(value or "").strip().lower()
    try:
        from gateway.config import Platform

        return Platform(text)
    except Exception:
        return text


def _workbench_url() -> str:
    return os.getenv("HERMES_WORKBENCH_URL", DEFAULT_WORKBENCH_URL).strip() or DEFAULT_WORKBENCH_URL


def _workbench_target() -> str:
    raw = os.getenv("HERMES_WORKBENCH_TARGET", DEFAULT_WORKBENCH_TARGET).strip().lower()
    return raw if raw in {"v1", "v2"} else DEFAULT_WORKBENCH_TARGET


def _v2_relay_url() -> str:
    raw = os.getenv("HERMES_WORKBENCH_V2_RELAY_URL", "").strip()
    if raw:
        return raw
    return DEFAULT_V2_RELAY_URL


def _v2_secret_headers() -> dict[str, str]:
    env_name = os.getenv("HERMES_WORKBENCH_V2_RELAY_SECRET_ENV", DEFAULT_V2_SECRET_ENV).strip()
    if not env_name:
        return {}
    secret = os.getenv(env_name, "").strip()
    if not secret:
        return {}
    return {"x-aiwb-hermes-relay-secret": secret}


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


def _v2_kind(message_type: str, raw_url: str | None, attachments: list[dict[str, Any]]) -> str:
    if message_type == "command":
        return "command"
    if attachments:
        return "image" if any(attachment.get("kind") == "image" for attachment in attachments) else "file"
    if raw_url:
        return "url"
    return "text"


def _event_timestamp_iso(event) -> str:
    timestamp = getattr(event, "timestamp", None)
    if timestamp is None:
        from datetime import datetime

        timestamp = datetime.now(UTC)
    if getattr(timestamp, "tzinfo", None) is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def _relay_id(platform: str, message_ref: str, timestamp: str) -> str:
    digest = hashlib.sha256(f"{platform}:{message_ref}:{timestamp}".encode("utf-8")).hexdigest()[:16]
    return f"hermes-relay-{_safe_slug(platform or 'other')}-{digest}"


def _safe_ref(platform: str, label: str, value: Any, *, fallback_parts: tuple[Any, ...] = ()) -> str:
    text = _safe_text(value)
    if text:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    else:
        fallback = "|".join(str(part or "") for part in fallback_parts)
        digest = hashlib.sha256(f"{label}:{fallback}".encode("utf-8")).hexdigest()[:16]
    return f"{_safe_slug(platform or 'other')}-{label}-{digest}"


def _safe_optional_ref(platform: str, label: str, value: Any) -> str | None:
    if _safe_text(value) is None:
        return None
    return _safe_ref(platform, label, value)


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(value or "other").strip().lower()).strip("-._")
    return (slug or "other")[:40].strip("-._") or "other"


def _safe_filename(value: str) -> str | None:
    if not value:
        return None
    name = Path(value).name.strip()
    if not name or any(part in name.lower() for part in ("token", "secret", "cookie", "credential")):
        return None
    return name[:120]


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
