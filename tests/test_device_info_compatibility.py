"""Complete releases retain the installed HA device-registry compatibility."""

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.xiaomi_miot.core.device import Device


def test_device_info_resolves_registered_proxy_id():
    device = SimpleNamespace(
        identifiers={("xiaomi_miot", "speaker")}, name="Speaker",
        model="xiaomi.speaker", sw_version="1", hass=object(),
        info=SimpleNamespace(room_name="Room"), _proxy_device=None,
    )
    registry = SimpleNamespace(async_get_device=lambda ids: SimpleNamespace(id="parent"))
    with patch("custom_components.xiaomi_miot.core.device.dr.async_get", return_value=registry):
        assert "via_device" not in Device.hass_device_info.fget(device)
        assert "via_device_id" not in Device.hass_device_info.fget(device)
        device._proxy_device = SimpleNamespace(identifiers={("xiaomi_miot", "proxy")})
        assert Device.hass_device_info.fget(device)["via_device_id"] == "parent"
        registry.async_get_device = lambda ids: None
        assert "via_device_id" not in Device.hass_device_info.fget(device)
