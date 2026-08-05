import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

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
    entity._async_stream_address_ready = AsyncMock(return_value=True)

    assert entity.supported_features & CameraEntityFeature.STREAM
    assert entity._srv_stream.name == "camera_stream_for_google_home"

    with patch("custom_components.xiaomi_miot.camera.asyncio.sleep", AsyncMock()):
        assert await entity.stream_source() == stream_url
    assert cloud_action.await_args_list == [
        call(
            {"did": device.did, "siid": 7, "aiid": 2, "in": []},
            sensitive=True,
        ),
        call(
            {"did": device.did, "siid": 7, "aiid": 1, "in": [1]},
            sensitive=True,
        ),
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
async def test_converter_camera_uses_configured_external_live_stream(
    make_device,
    load_miot_spec,
):
    stream_url = "rtsp://127.0.0.1:8554/living_camera"

    def custom_config(_entity, key=None, default=None):
        return {"live_stream_source": stream_url}.get(key, default)

    with patch.object(CameraEntity, "custom_config", custom_config):
        _, entity = camera_entity(
            make_device,
            load_miot_spec,
            "chuangmi.camera.p2p-only.json",
            "chuangmi.camera.p2p-only",
        )

    assert entity.supported_features & CameraEntityFeature.STREAM
    assert await entity.stream_source() == stream_url
    assert entity.is_streaming is True
    assert stream_url not in entity.extra_state_attributes.values()


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

    with patch("custom_components.xiaomi_miot.camera.asyncio.sleep", AsyncMock()):
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


@pytest.mark.asyncio
async def test_converter_camera_retries_hls_profiles_until_playlist_is_ready(
    make_device,
    load_miot_spec,
):
    device, entity = camera_entity(
        make_device,
        load_miot_spec,
        "chuangmi.camera.021a04.json",
        "chuangmi.camera.021a04",
    )
    first_url = "https://camera.example.test/live/profile-1.m3u8"
    second_url = "https://camera.example.test/live/profile-2.m3u8"
    device.cloud = object()
    device.async_call_action = AsyncMock(side_effect=[
        MiotResult({"code": 0}),
        MiotResult({"code": 0, "out": [first_url]}),
        MiotResult({"code": 0}),
        MiotResult({"code": 0, "out": [second_url]}),
    ])
    entity._async_stream_address_ready = AsyncMock(side_effect=[False, True])

    with patch("custom_components.xiaomi_miot.camera.asyncio.sleep", AsyncMock()):
        assert await entity.stream_source() == second_url

    assert entity._async_stream_address_ready.await_args_list == [
        call(first_url),
        call(second_url),
    ]
    assert device.async_call_action.await_args_list == [
        call(7, 2, None, cloud=True, sensitive=True),
        call(7, 1, [1], cloud=True, sensitive=True),
        call(7, 2, None, cloud=True, sensitive=True),
        call(7, 1, [2], cloud=True, sensitive=True),
    ]
