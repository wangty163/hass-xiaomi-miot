"""Tests for media_player.py lazy micoapi bootstrap via HassEntry."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from homeassistant.components.media_player import MediaPlayerState

homekit_module = ModuleType("homeassistant.components.homekit")
homekit_module.__path__ = []
homekit_const_module = ModuleType("homeassistant.components.homekit.const")
homekit_const_module.EVENT_HOMEKIT_TV_REMOTE_KEY_PRESSED = (
    "homekit_tv_remote_key_pressed"
)
sys.modules.setdefault("homeassistant.components.homekit", homekit_module)
sys.modules.setdefault(
    "homeassistant.components.homekit.const",
    homekit_const_module,
)

from custom_components.xiaomi_miot import DOMAIN, init_integration_data
from custom_components.xiaomi_miot.core.hass_entry import HassEntry
from custom_components.xiaomi_miot.core.xiaomi_cloud import CloudSid
from custom_components.xiaomi_miot.media_player import MiotMediaPlayerEntity


class _FakeEntity:
    """Replicates the new async_added_to_hass shape under test."""

    def __init__(self, hass, intelligent_speaker=True, entry_id="eid"):
        self.hass = hass
        self._intelligent_speaker = intelligent_speaker
        self._config = {"entry_id": entry_id} if entry_id else {}
        self.xiaoai_cloud = None
        self.unique_id = "u-uniq"
        self.name_model = "Fake Speaker"
        self.logger = SimpleNamespace(warning=lambda *a, **k: None)

    async def async_added_to_hass(self):
        self.xiaoai_cloud = None
        if not self._intelligent_speaker:
            return
        entry_id = (self._config or {}).get("entry_id")
        owner = (entry_id and self.hass.data.get(DOMAIN, {}).get(entry_id)) or None
        if owner is None:
            return
        try:
            self.xiaoai_cloud = await owner.async_get_cloud(CloudSid.MICOAPI)
        except Exception as exc:
            self.logger.warning("%s: micoapi bootstrap failed: %s", self.name_model, exc)
            self.xiaoai_cloud = None


def _make_owner(fake_cloud):
    return SimpleNamespace(
        async_get_cloud=AsyncMock(return_value=fake_cloud),
        clouds={},
        _cloud_lock=asyncio.Lock(),
        cloud=None,
        get_config=lambda k=None, d=None: d,
        filter_models=False,
        new_device=AsyncMock(),
        get_cloud_devices=AsyncMock(return_value={}),
        async_unload=AsyncMock(return_value=True),
    )


async def test_lazy_micoapi_probe_uses_owner(hass):
    init_integration_data(hass)
    fake_cloud = SimpleNamespace(sid="micoapi", async_check_micoapi_auth=AsyncMock(return_value=True))
    he = _make_owner(fake_cloud)
    HassEntry.ALL["eid"] = he
    hass.data[DOMAIN]["eid"] = he
    ent = _FakeEntity(hass)
    await ent.async_added_to_hass()
    he.async_get_cloud.assert_awaited_once_with(CloudSid.MICOAPI)
    assert ent.xiaoai_cloud is fake_cloud


async def test_no_owner_skips_micoapi_probe(hass):
    init_integration_data(hass)
    ent = _FakeEntity(hass, entry_id=None)
    await ent.async_added_to_hass()
    assert ent.xiaoai_cloud is None


async def test_non_speaker_skips_micoapi_probe(hass):
    init_integration_data(hass)
    ent = _FakeEntity(hass, intelligent_speaker=False)
    await ent.async_added_to_hass()
    assert ent.xiaoai_cloud is None


async def test_owner_failure_leaves_xiaoai_cloud_none(hass):
    init_integration_data(hass)

    class _OwnerBoom:
        async def async_get_cloud(self, sid):
            raise RuntimeError("nope")

    hass.data[DOMAIN]["eid"] = _OwnerBoom()
    ent = _FakeEntity(hass)
    await ent.async_added_to_hass()
    assert ent.xiaoai_cloud is None


def test_resolved_xiaoai_uses_micoapi_state_without_miot_fallback():
    """A resolved speaker never exposes its known-stale MIoT state."""
    ent = object.__new__(MiotMediaPlayerEntity)
    ent.xiaoai_device = {"deviceID": "fake-speaker"}
    ent._attr_state = MediaPlayerState.PAUSED

    assert MiotMediaPlayerEntity.state.fget(ent) == MediaPlayerState.PAUSED

    ent._attr_state = None
    assert MiotMediaPlayerEntity.state.fget(ent) is None


async def test_failed_micoapi_refresh_clears_previous_player_state():
    """A failed refresh cannot preserve a previous successful cloud result."""
    ent = object.__new__(MiotMediaPlayerEntity)
    ent.xiaoai_device = {"deviceID": "fake-speaker"}
    ent._attr_state = MediaPlayerState.PLAYING
    ent._vars = {}
    ent.update_attrs = Mock()
    ent.logger = Mock()
    ent.xiaoai_cloud = SimpleNamespace(
        async_request_api=AsyncMock(return_value={})
    )

    with patch.object(
        MiotMediaPlayerEntity,
        "name_model",
        new_callable=PropertyMock,
        return_value="Fake Speaker",
    ):
        await ent.async_update_play_status()

    assert ent._attr_state is None
    assert MiotMediaPlayerEntity.state.fget(ent) is None
