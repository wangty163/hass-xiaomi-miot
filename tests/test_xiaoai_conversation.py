"""Tests for resilient XiaoAI conversation polling."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import requests

from custom_components.xiaomi_miot.sensor import (
    XIAOAI_CONVERSATION_ATTEMPTS,
    _decode_xiaoai_conversation_data,
    _request_xiaoai_conversation,
)


def test_none_conversation_data_is_an_empty_result():
    assert _decode_xiaoai_conversation_data(None) == {}
    assert _decode_xiaoai_conversation_data("null") == {}


def test_conversation_data_accepts_objects_and_json_objects():
    payload = {"records": [{"query": "快速降温"}]}

    assert _decode_xiaoai_conversation_data(payload) is payload
    assert _decode_xiaoai_conversation_data(
        '{"records": [{"query": "快速降温"}]}'
    ) == payload


@pytest.mark.parametrize("payload", [[], 1, '"not-an-object"', "[1, 2]"])
def test_conversation_data_rejects_non_object_payloads(payload):
    with pytest.raises(TypeError):
        _decode_xiaoai_conversation_data(payload)


async def test_conversation_request_retries_one_timeout_then_succeeds():
    cloud = AsyncMock()
    response = {"data": {"records": []}}
    cloud.async_request_api.side_effect = [
        requests.exceptions.ReadTimeout("slow"),
        response,
    ]

    with patch(
        "custom_components.xiaomi_miot.sensor.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep:
        result = await _request_xiaoai_conversation(
            cloud,
            "https://userprofile.mina.mi.com/device_profile/v2/conversation",
            {"limit": 3},
            {"deviceId": "speaker"},
        )

    assert result == response
    assert cloud.async_request_api.await_count == 2
    sleep.assert_awaited_once()
    assert all(
        call.kwargs["raise_timeout"] is True
        for call in cloud.async_request_api.await_args_list
    )


@pytest.mark.parametrize(
    "timeout_error",
    [asyncio.TimeoutError(), requests.exceptions.ReadTimeout("slow")],
)
async def test_conversation_request_raises_after_retry_exhaustion(timeout_error):
    cloud = AsyncMock()
    cloud.async_request_api.side_effect = [
        timeout_error
        for _ in range(XIAOAI_CONVERSATION_ATTEMPTS)
    ]

    with patch(
        "custom_components.xiaomi_miot.sensor.asyncio.sleep",
        new=AsyncMock(),
    ) as sleep:
        with pytest.raises(type(timeout_error)):
            await _request_xiaoai_conversation(
                cloud,
                "https://userprofile.mina.mi.com/device_profile/v2/conversation",
                {"limit": 3},
                {"deviceId": "speaker"},
            )

    assert cloud.async_request_api.await_count == XIAOAI_CONVERSATION_ATTEMPTS
    assert sleep.await_count == XIAOAI_CONVERSATION_ATTEMPTS - 1
