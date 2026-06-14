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
