"""Tests for media_player.py lazy micoapi bootstrap via HassEntry."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.media_player import MediaPlayerState

from custom_components.xiaomi_miot import DOMAIN, init_integration_data
from custom_components.xiaomi_miot.core.hass_entry import HassEntry
from custom_components.xiaomi_miot.core.xiaomi_cloud import CloudSid

# media_player imports one HomeKit event constant. The test does not exercise
# HomeKit, so avoid installing and initializing the whole optional integration.
homekit_const = ModuleType("homeassistant.components.homekit.const")
homekit_const.EVENT_HOMEKIT_TV_REMOTE_KEY_PRESSED = (
    "homekit_tv_remote_key_pressed"
)
sys.modules.setdefault("homeassistant.components.homekit.const", homekit_const)

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


class _FakeLogger:
    """No-op logger for unbound player-status tests."""

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class _FakePlayStatusEntity:
    """Small surface used by the real async_update_play_status method."""

    def __init__(self, cloud_result):
        self.xiaoai_id = "xiaoai-device"
        self.xiaoai_cloud = SimpleNamespace(
            async_request_api=AsyncMock(return_value=cloud_result)
        )
        self._attr_state = MediaPlayerState.PLAYING
        self._attr_media_duration = None
        self._attr_media_position = None
        self._vars = {}
        self.logger = _FakeLogger()
        self.name_model = "Fake Speaker"

    @property
    def state(self):
        return self._attr_state

    def update_attrs(self, *_args, **_kwargs):
        pass


async def test_empty_micoapi_status_clears_previous_player_state():
    """A failed or empty cloud refresh cannot preserve stale playback state."""
    entity = _FakePlayStatusEntity({})

    await MiotMediaPlayerEntity.async_update_play_status(entity)

    assert entity._attr_state is None


async def test_micoapi_status_sets_current_player_state():
    """A successful cloud refresh still maps the current XiaoAI status."""
    entity = _FakePlayStatusEntity({"data": {"info": {"status": 2}}})

    await MiotMediaPlayerEntity.async_update_play_status(entity)

    assert entity._attr_state is MediaPlayerState.PAUSED


def test_resolved_xiaoai_device_does_not_fall_back_to_stale_miot_state():
    """Resolved XiaoAI devices expose unknown when MICOAPI has no state."""
    entity = object.__new__(MiotMediaPlayerEntity)
    entity.xiaoai_device = {"deviceID": "xiaoai-device"}
    entity._attr_state = None

    assert entity.state is None
