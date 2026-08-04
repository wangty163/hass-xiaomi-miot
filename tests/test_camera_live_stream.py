import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from homeassistant.components.camera import CameraEntityFeature

from custom_components.xiaomi_miot.camera import CameraEntity
from custom_components.xiaomi_miot.core.converters import MiotCameraConv
from custom_components.xiaomi_miot.core.miot_spec import MiotResult


def camera_entity(make_device, load_miot_spec, fixture, model):
    device = make_device(load_miot_spec(fixture), model=model)
    converter = next(
        converter
        for converter in device.converters
        if isinstance(converter, MiotCameraConv)
    )
    return device, CameraEntity(device, converter)


@pytest.mark.asyncio
async def test_converter_camera_uses_direct_hls_stream(
    caplog,
    make_device,
    load_miot_spec,
):
    device, entity = camera_entity(
        make_device,
        load_miot_spec,
        "chuangmi.camera.021a04.json",
        "chuangmi.camera.021a04",
    )
    stream_url = "https://camera.example.test/live/playlist.m3u8?token=secret"
    cloud_action = AsyncMock(side_effect=[
        {"code": 0},
        {"code": 0, "out": [stream_url]},
    ])
    device.cloud = SimpleNamespace(async_do_action=cloud_action)
    caplog.set_level(logging.DEBUG)

    assert entity.supported_features & CameraEntityFeature.STREAM
    assert entity._srv_stream.name == "camera_stream_for_google_home"

    assert await entity.stream_source() == stream_url
    assert cloud_action.await_args_list == [
        call({"did": device.did, "siid": 7, "aiid": 2, "in": []}),
        call({"did": device.did, "siid": 7, "aiid": 1, "in": [1]}),
    ]

    assert await entity.stream_source() == stream_url
    assert cloud_action.await_count == 2
    assert stream_url not in caplog.text
    assert "stream_address" not in entity.extra_state_attributes


@pytest.mark.asyncio
async def test_converter_camera_does_not_claim_p2p_as_ha_stream(
    make_device,
    load_miot_spec,
):
    _, entity = camera_entity(
        make_device,
        load_miot_spec,
        "chuangmi.camera.p2p-only.json",
        "chuangmi.camera.p2p-only",
    )
    playback_url = "https://camera.example.test/alarm/playlist.m3u8"
    entity._attr_stream_source = playback_url

    assert not entity.supported_features & CameraEntityFeature.STREAM
    assert await entity.stream_source() == playback_url


@pytest.mark.asyncio
async def test_converter_camera_handles_direct_stream_action_failure(
    make_device,
    load_miot_spec,
):
    device, entity = camera_entity(
        make_device,
        load_miot_spec,
        "chuangmi.camera.021a04.json",
        "chuangmi.camera.021a04",
    )
    device.async_call_action = AsyncMock(side_effect=[
        MiotResult({"code": 0}),
        MiotResult({"code": -1, "error": "cloud unavailable"}),
    ])
    device.cloud = object()

    assert await entity.stream_source() is None
    assert entity.is_streaming is False
    assert "stream_address" not in entity.extra_state_attributes


@pytest.mark.asyncio
async def test_converter_camera_never_falls_back_to_local_live_action(
    make_device,
    load_miot_spec,
):
    device, entity = camera_entity(
        make_device,
        load_miot_spec,
        "chuangmi.camera.021a04.json",
        "chuangmi.camera.021a04",
    )
    device.async_call_action = AsyncMock()

    assert await entity.stream_source() is None
    device.async_call_action.assert_not_awaited()
