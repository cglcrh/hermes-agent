import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins import workbench_bridge


def _event(text: str = "hello https://example.com/news") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=Platform.FEISHU,
            user_id="ou_user",
            chat_id="chat1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _reply_event() -> MessageEvent:
    event = _event("反馈在哪里？")
    event.source.platform = Platform.TELEGRAM
    event.reply_to_message_id = "3105"
    event.reply_to_text = "AIWB已收件，后台处理中...（1c84721eb0aa）"
    return event


def _media_event() -> MessageEvent:
    event = _event("")
    event.message_type = MessageType.DOCUMENT
    event.media_urls = ["/Users/mncstudio/.hermes/cache/documents/report.docx"]
    event.media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    return event


def _uncached_photo_event() -> MessageEvent:
    event = _event("")
    event.message_type = MessageType.PHOTO
    event.media_urls = []
    event.media_types = []
    return event


def _delivery_request(**overrides):
    payload = {
        "schema_version": 1,
        "delivery_request_id": "hermes-delivery-1",
        "delivery_id": "delivery-1",
        "ticket_ref": "8f3a91c2d740",
        "event_session_id": "event-session-1",
        "action_set_id": "action-set-1",
        "gateway": "telegram",
        "conversation_ref": "tg-chat-1",
        "message_text": "AIWB 已收件\n\n收件编号\n8f3a91c2d740 选\n\n下一步\n1. 保存",
        "copy_block": "收件编号\n8f3a91c2d740 选",
        "idempotency_key": "hgp-safe-key-1",
        "created_at": "2026-06-14T08:20:00+00:00",
        "policy": {"allow_send_once": True},
        "metadata": {"source": "aiwb-v2"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_bridge_forwards_and_skips(monkeypatch):
    sent_payload = {}

    def fake_post(base_url, payload, timeout):
        sent_payload.update({"base_url": base_url, "payload": payload, "timeout": timeout})
        return {"message": "已 capture-only 收录到 Workbench 收件箱：abc123"}

    monkeypatch.setenv("HERMES_WORKBENCH_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(workbench_bridge, "_post_ingress", fake_post)

    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(adapters={Platform.FEISHU: adapter})

    result = workbench_bridge._pre_gateway_dispatch(_event(), gateway)
    for _ in range(20):
        if adapter.send.await_count:
            break
        await asyncio.sleep(0.01)

    assert result == {"action": "skip", "reason": "workbench_bridge"}
    assert sent_payload["payload"]["source"] == "hermes.feishu"
    assert sent_payload["payload"]["url"] == "https://example.com/news"
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_forwards_reply_context(monkeypatch):
    sent_payload = {}

    def fake_post(base_url, payload, timeout):
        sent_payload.update({"base_url": base_url, "payload": payload, "timeout": timeout})
        return {"message": "已定位原收件编号"}

    monkeypatch.setattr(workbench_bridge, "_post_ingress", fake_post)

    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    result = workbench_bridge._pre_gateway_dispatch(_reply_event(), gateway)
    for _ in range(20):
        if adapter.send.await_count:
            break
        await asyncio.sleep(0.01)

    metadata = sent_payload["payload"]["adapter_metadata"]
    assert result == {"action": "skip", "reason": "workbench_bridge"}
    assert sent_payload["payload"]["source"] == "hermes.telegram"
    assert metadata["reply_to_message_id"] == "3105"
    assert metadata["reply_to_text"] == "AIWB已收件，后台处理中...（1c84721eb0aa）"
    assert metadata["reply_to"] == {
        "message_id": "3105",
        "text": "AIWB已收件，后台处理中...（1c84721eb0aa）",
    }


@pytest.mark.asyncio
async def test_bridge_retries_reply_send_failure(monkeypatch):
    def fake_post(base_url, payload, timeout):
        return {"message": "已 capture-only 收录到 Workbench 收件箱：abc123"}

    monkeypatch.setenv("HERMES_WORKBENCH_REPLY_ATTEMPTS", "2")
    monkeypatch.setattr(workbench_bridge, "_post_ingress", fake_post)
    monkeypatch.setattr(workbench_bridge, "_reply_retry_delay", lambda attempt: 0)

    adapter = SimpleNamespace(send=AsyncMock(side_effect=[RuntimeError("network"), None]))
    gateway = SimpleNamespace(adapters={Platform.FEISHU: adapter})

    result = workbench_bridge._pre_gateway_dispatch(_event(), gateway)
    for _ in range(20):
        if adapter.send.await_count >= 2:
            break
        await asyncio.sleep(0.01)

    assert result == {"action": "skip", "reason": "workbench_bridge"}
    assert adapter.send.await_count == 2


@pytest.mark.asyncio
async def test_bridge_forwards_media_only_message(monkeypatch):
    sent_payload = {}

    def fake_post(base_url, payload, timeout):
        sent_payload.update({"base_url": base_url, "payload": payload, "timeout": timeout})
        return {"message": "已 capture-only 收录到 Workbench 收件箱：media123"}

    monkeypatch.setattr(workbench_bridge, "_post_ingress", fake_post)

    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(adapters={Platform.FEISHU: adapter})

    result = workbench_bridge._pre_gateway_dispatch(_media_event(), gateway)
    for _ in range(20):
        if adapter.send.await_count:
            break
        await asyncio.sleep(0.01)

    assert result == {"action": "skip", "reason": "workbench_bridge"}
    assert sent_payload["payload"]["text"] == "[Hermes 文件消息：1 个附件]"
    assert sent_payload["payload"]["content_type"] == "document"
    assert sent_payload["payload"]["adapter_metadata"]["media_urls"] == [
        "/Users/mncstudio/.hermes/cache/documents/report.docx"
    ]
    assert sent_payload["payload"]["adapter_metadata"]["media_cache_status"] == "cached"


@pytest.mark.asyncio
async def test_bridge_v2_mode_forwards_normalized_relay_payload(monkeypatch):
    sent_payload = {}

    def fake_post(url, payload, timeout):
        sent_payload.update({"url": url, "payload": payload, "timeout": timeout})
        return {"message": "v2 accepted"}

    monkeypatch.setenv("HERMES_WORKBENCH_TARGET", "v2")
    monkeypatch.setenv("HERMES_WORKBENCH_V2_RELAY_URL", "http://127.0.0.1:8790/hermes-relay")
    monkeypatch.setattr(workbench_bridge, "_post_v2_relay", fake_post)

    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(adapters={Platform.FEISHU: adapter})

    result = workbench_bridge._pre_gateway_dispatch(_event(), gateway)
    for _ in range(20):
        if adapter.send.await_count:
            break
        await asyncio.sleep(0.01)

    payload = sent_payload["payload"]
    assert result == {"action": "skip", "reason": "workbench_bridge"}
    assert sent_payload["url"] == "http://127.0.0.1:8790/hermes-relay"
    assert payload["schema_version"] == 1
    assert payload["relay_source"] == "hermes"
    assert payload["relay_id"].startswith("hermes-relay-feishu-")
    assert payload["gateway"] == "feishu"
    assert payload["kind"] == "url"
    assert payload["text"] == "hello https://example.com/news"
    assert payload["attachments"] == []
    assert payload["raw_payload_ref"].startswith("hermes://event/hermes-relay-feishu-")
    assert payload["gateway_metadata"]["bridge"] == "hermes.workbench_bridge"
    assert payload["gateway_metadata"]["bridge_target"] == "v2"
    assert payload["gateway_metadata"]["url_detected"] is True
    adapter.send.assert_awaited_once()


def test_bridge_v2_media_payload_uses_opaque_attachment_ref():
    payload = workbench_bridge._build_v2_relay_payload(
        _media_event(),
        _media_event().source,
        "feishu",
        "",
    )

    assert payload["kind"] == "file"
    assert payload["attachments"] == [
        {
            "id": payload["attachments"][0]["id"],
            "kind": "file",
            "raw_ref": f"hermes://attachment/feishu/{payload['attachments'][0]['id']}",
            "filename": "report.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "metadata": {
                "message_type": "document",
                "media_index": 0,
                "cache_status": "cached",
                "has_local_cache": True,
            },
        }
    ]
    assert not payload["attachments"][0].get("gateway_file_ref")
    assert "/Users/mncstudio" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bridge_skips_uncached_media_download_failure(monkeypatch):
    sent_payload = {}

    def fake_post(base_url, payload, timeout):
        sent_payload.update({"base_url": base_url, "payload": payload, "timeout": timeout})
        return {"message": "已 capture-only 收录到 Workbench 收件箱：photo123"}

    monkeypatch.setattr(workbench_bridge, "_post_ingress", fake_post)

    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(adapters={Platform.FEISHU: adapter})

    result = workbench_bridge._pre_gateway_dispatch(_uncached_photo_event(), gateway)
    for _ in range(20):
        if adapter.send.await_count:
            break
        await asyncio.sleep(0.01)

    assert result == {"action": "skip", "reason": "workbench_bridge"}
    assert sent_payload["payload"]["text"] == "[Hermes 图片消息：1 个附件]"
    assert sent_payload["payload"]["content_type"] == "photo"
    assert sent_payload["payload"]["adapter_metadata"]["media_urls"] == []
    assert sent_payload["payload"]["adapter_metadata"]["media_cache_status"] == "missing"


def test_bridge_can_be_limited_to_platform(monkeypatch):
    monkeypatch.setenv("HERMES_WORKBENCH_PLATFORMS", "telegram")
    result = workbench_bridge._pre_gateway_dispatch(_event(), SimpleNamespace(adapters={}))
    assert result is None


def test_bridge_defaults_to_v1_and_v2_uses_local_relay_default(monkeypatch):
    monkeypatch.delenv("HERMES_WORKBENCH_TARGET", raising=False)
    monkeypatch.delenv("HERMES_WORKBENCH_V2_RELAY_URL", raising=False)

    assert workbench_bridge._workbench_target() == "v1"
    assert workbench_bridge._v2_relay_url() == "http://127.0.0.1:8790/hermes-relay"


def test_post_ingress_uses_json(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"message": "ok"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(workbench_bridge.urllib.request, "urlopen", fake_urlopen)

    response = workbench_bridge._post_ingress("http://wb", {"text": "你好"}, 3)

    assert response == {"message": "ok"}
    assert captured["url"] == "http://wb/api/adapters/hermes/ingress"
    assert json.loads(captured["body"]) == {"text": "你好"}
    assert captured["timeout"] == 3


def test_post_v2_relay_uses_configured_secret_env(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "accepted"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("HERMES_WORKBENCH_V2_RELAY_SECRET_ENV", "TEST_AIWB_V2_SECRET")
    monkeypatch.setenv("TEST_AIWB_V2_SECRET", "test-secret")
    monkeypatch.setattr(workbench_bridge.urllib.request, "urlopen", fake_urlopen)

    response = workbench_bridge._post_v2_relay(
        "http://127.0.0.1:8790/hermes-relay",
        {"schema_version": 1},
        7,
    )

    assert response == {"status": "accepted"}
    assert captured["url"] == "http://127.0.0.1:8790/hermes-relay"
    assert json.loads(captured["body"]) == {"schema_version": 1}
    assert captured["headers"]["X-aiwb-hermes-relay-secret"] == "test-secret"
    assert captured["timeout"] == 7


@pytest.mark.asyncio
async def test_v2_delivery_request_sends_through_existing_gateway_adapter():
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="tg-message-1")))
    gateway = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    ledger = set()

    result = await workbench_bridge.send_v2_delivery_request(gateway, _delivery_request(), ledger=ledger)

    assert result["ok"] is True
    assert result["status"] == "delivered"
    assert result["gateway_message_ref"] == "tg-message-1"
    assert result["ticket_ref"] == "8f3a91c2d740"
    adapter.send.assert_awaited_once_with("tg-chat-1", _delivery_request()["message_text"], metadata={})
    assert "hgp-safe-key-1" in ledger
    assert "raw_response" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_v2_delivery_request_blocks_duplicate_send_once():
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="tg-message-1")))
    gateway = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    ledger = {"hgp-safe-key-1"}

    result = await workbench_bridge.send_v2_delivery_request(gateway, _delivery_request(), ledger=ledger)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "send_already_used"
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_delivery_request_rejects_secret_raw_and_control_plane_payloads():
    gateway = SimpleNamespace(adapters={})

    missing = await workbench_bridge.send_v2_delivery_request(gateway, _delivery_request(ticket_ref=""), ledger=set())
    secret = await workbench_bridge.send_v2_delivery_request(
        gateway,
        _delivery_request(metadata={"authorization": "Bearer abc"}),
        ledger=set(),
    )
    raw = await workbench_bridge.send_v2_delivery_request(
        gateway,
        _delivery_request(raw_payload={"update_id": 123}),
        ledger=set(),
    )
    control = await workbench_bridge.send_v2_delivery_request(
        gateway,
        _delivery_request(agent_loop=True),
        ledger=set(),
    )

    assert missing["status"] == "rejected"
    assert missing["reason"] == "missing_required_field"
    assert secret["reason"] == "forbidden_payload"
    assert raw["reason"] == "forbidden_payload"
    assert control["reason"] == "forbidden_payload"


@pytest.mark.asyncio
async def test_v2_delivery_request_rejects_bad_copy_block_and_unsupported_gateway():
    gateway = SimpleNamespace(adapters={})

    bad_copy = await workbench_bridge.send_v2_delivery_request(
        gateway,
        _delivery_request(copy_block="收件编号\nwrong 选"),
        ledger=set(),
    )
    unsupported = await workbench_bridge.send_v2_delivery_request(
        gateway,
        _delivery_request(gateway="cli"),
        ledger=set(),
    )

    assert bad_copy["status"] == "rejected"
    assert bad_copy["reason"] == "missing_copy_block"
    assert unsupported["reason"] == "unsupported_gateway"


@pytest.mark.asyncio
async def test_v2_delivery_result_sanitizes_adapter_failure_and_message_ref():
    failed_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=False, error="Authorization: Bearer abc"))
    )
    unsafe_ref_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="msg?token=abc", raw_response={"token": "x"}))
    )

    failed = await workbench_bridge.send_v2_delivery_request(
        SimpleNamespace(adapters={Platform.TELEGRAM: failed_adapter}),
        _delivery_request(idempotency_key="hgp-safe-key-2"),
        ledger=set(),
    )
    unsafe_ref = await workbench_bridge.send_v2_delivery_request(
        SimpleNamespace(adapters={Platform.TELEGRAM: unsafe_ref_adapter}),
        _delivery_request(idempotency_key="hgp-safe-key-3"),
        ledger=set(),
    )

    rendered = json.dumps({"failed": failed, "unsafe_ref": unsafe_ref}, ensure_ascii=False)
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "<redacted>"
    assert unsafe_ref["status"] == "delivered"
    assert unsafe_ref["gateway_message_ref"] is None
    assert "Bearer abc" not in rendered
    assert "token" not in rendered.lower()
