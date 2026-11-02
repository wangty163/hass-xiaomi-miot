"""Account recovery contracts, including concurrent readers and real HTTP."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState

from custom_components.xiaomi_miot.core.hass_entry import HassEntry
from custom_components.xiaomi_miot.core.micoapi import MicoApiClient, MicoApiError
from custom_components.xiaomi_miot.core.xiaomi_cloud import (
    CloudSid, MiCloudAuthenticationError, MiotCloud,
)


class Cloud:
    def __init__(self, token):
        self.service_token = token
        self.user_id = "fixture-user"
        self.async_session = None
        self.async_stored_auth = AsyncMock()
        self._has_complete_captcha = lambda: False
        self.async_login_attempt = AsyncMock(side_effect=self.login)

    async def login(self, *, persist):
        assert persist is False
        self.service_token = "fresh"
        return True


@pytest.fixture
async def account(hass):
    config = {"username": "fixture", "user_id": "fixture-user"}
    owner = SimpleNamespace(
        hass=hass, clouds={}, async_auth_failed=AsyncMock(),
        get_config=lambda key=None: config.get(key) if key else config,
        entry=SimpleNamespace(async_create_background_task=lambda _hass, coro, name, **kw: asyncio.create_task(coro, name=name)),
    )
    client = owner.micoapi = MicoApiClient(owner)
    made = []

    async def candidate(*, cached):
        cloud = Cloud("cached" if cached else None)
        made.append(cloud)
        return cloud

    async def exchange(cloud, api, data, method, **kwargs):
        assert cloud.service_token
        return {"code": 0, "data": [] if api.endswith("device_list") else {"ok": True}}

    client._candidate = AsyncMock(side_effect=candidate)
    client._exchange = AsyncMock(side_effect=exchange)
    yield owner, client, made
    await client.close()


async def test_true_ha_sync_reauth_contract(hass):
    # Use the actual HA method, including its None return when a flow exists.
    fake = SimpleNamespace(state=ConfigEntryState.LOADED)
    fake.async_get_active_flows = Mock(return_value=iter(["existing-flow"]))
    fake.async_start_reauth = lambda *a, **kw: ConfigEntry.async_start_reauth(fake, *a, **kw)
    owner = HassEntry(hass, SimpleNamespace(entry_id="fixture", **vars(fake)))
    await owner.async_auth_failed(CloudSid.MICOAPI)
    fake.async_get_active_flows.assert_called_once()


async def test_private_candidate_preserves_login_device_identity(account):
    owner, client, _ = account
    owner.hass.data["xiaomi_miot"] = {"config": {}, "sessions": {}}
    cached = Cloud("cached")
    cached.client_id, cached.useragent = "same-client", "same-agent"
    with patch.object(MiotCloud, "from_token", AsyncMock(return_value=cached)):
        await MicoApiClient._candidate(client, cached=True)
    fresh = await MicoApiClient._candidate(client, cached=False)
    assert fresh.client_id == cached.client_id
    assert fresh.useragent == cached.useragent
    assert fresh.service_token is None
    assert cached.service_token == "cached"


async def test_concurrent_readers_share_one_bootstrap(account):
    owner, client, made = account
    gate = asyncio.Event()
    original = client._exchange.side_effect

    async def delayed(*args, **kwargs):
        await gate.wait()
        return await original(*args, **kwargs)

    client._exchange.side_effect = delayed
    tasks = [asyncio.create_task(client.ready()) for _ in range(3)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert client._candidate.await_count == 1
    gate.set()
    clouds = await asyncio.gather(*tasks)
    assert clouds[0] is clouds[1] is clouds[2]
    assert client.generation == 1
    owner.async_auth_failed.assert_not_awaited()


async def test_temporary_bootstrap_failure_recovers_after_backoff(account):
    owner, client, _ = account
    client.clock = lambda: 10
    original = client._exchange.side_effect
    client._exchange.side_effect = MicoApiError("connection_error")
    with pytest.raises(MicoApiError, match="account_retry"):
        await client.ready()
    assert CloudSid.MICOAPI not in owner.clouds
    with pytest.raises(MicoApiError):
        await client.ready()
    assert client._candidate.await_count == 1
    client.clock = lambda: 12
    client._exchange.side_effect = original
    assert await client.ready() is client.cloud
    owner.async_auth_failed.assert_not_awaited()


async def test_expired_session_recovery_is_shared_and_published_atomically(account):
    owner, client, made = account
    old = await client.ready()
    login_started, release = asyncio.Event(), asyncio.Event()
    original_candidate = client._candidate.side_effect
    original_exchange = client._exchange.side_effect

    async def candidate(**kwargs):
        cloud = await original_candidate(**kwargs)
        async def login(**kw):
            login_started.set()
            await release.wait()
            cloud.service_token = "fresh"
            return True
        cloud.async_login_attempt = AsyncMock(side_effect=login)
        return cloud

    async def exchange(cloud, *args, **kwargs):
        if cloud is old:
            raise MicoApiError("auth_required", response_code=401)
        return await original_exchange(cloud, *args, **kwargs)

    client._candidate.side_effect = candidate
    client._exchange.side_effect = exchange
    tasks = [asyncio.create_task(client.request("/remote/ubus", {}, "POST", read_only=True)) for _ in range(3)]
    await login_started.wait()
    assert client.state == "refreshing"
    assert owner.clouds[CloudSid.MICOAPI] is old
    assert old.service_token == "cached"
    assert sum(c.async_login_attempt.await_count for c in made) == 1
    release.set()
    assert all(r["code"] == 0 for r in await asyncio.gather(*tasks))
    assert client.generation == 2
    assert client.cloud is not old
    assert client.cloud.service_token == "fresh"
    owner.async_auth_failed.assert_not_awaited()


async def test_late_old_unauthorized_cannot_invalidate_replacement(account):
    _, client, _ = account
    await client.ready()
    old_generation = client.generation
    client._reject(old_generation)
    await client.ready()
    task = client.task
    client._reject(old_generation)
    assert client.state == "ready"
    assert client.task is task


async def test_control_is_never_replayed(account):
    _, client, _ = account
    old = await client.ready()
    original = client._exchange.side_effect
    controls = []
    async def exchange(cloud, api, data, method, **kwargs):
        if api == "/remote/ubus":
            controls.append(data)
            raise MicoApiError("auth_required", response_code=401)
        return await original(cloud, api, data, method, **kwargs)
    client._exchange.side_effect = exchange
    with pytest.raises(MicoApiError, match="account_refreshing"):
        await client.request("/remote/ubus", {"method": "player_set_volume"}, "POST")
    await client.ready()
    assert client.cloud is not old
    assert len(controls) == 1


async def test_reader_timeout_does_not_cancel_shared_recovery(account):
    _, client, _ = account
    release = asyncio.Event()
    original = client._exchange.side_effect
    async def delayed(*args, **kwargs):
        await release.wait()
        return await original(*args, **kwargs)
    client._exchange.side_effect = delayed
    with pytest.raises(MicoApiError, match="account_refreshing"):
        await client.request("/admin/v2/device_list", timeout=0.01, read_only=True)
    assert not client.task.done()
    release.set()
    await client.ready()
    assert client._candidate.await_count == 1


async def test_invalid_credentials_request_one_reauth_without_login_storm(account):
    owner, client, made = account
    original = client._candidate.side_effect
    async def candidate(**kwargs):
        c = await original(**kwargs)
        c.service_token = None
        c.async_login_attempt.side_effect = MiCloudAuthenticationError("fixture private text")
        return c
    client._candidate.side_effect = candidate
    for _ in range(3):
        with pytest.raises(MicoApiError, match="auth_required"):
            await client.ready()
    assert client.state == "reauth_required"
    owner.async_auth_failed.assert_awaited_once_with(CloudSid.MICOAPI)
    assert sum(c.async_login_attempt.await_count for c in made) == 1
    assert "private" not in json.dumps(client.diagnostics)


async def test_unexpected_failure_logs_location_without_private_exception_text(account, caplog):
    _, client, _ = account
    client._exchange.side_effect = TypeError("PRIVATE-FIXTURE-BODY")
    with pytest.raises(MicoApiError, match="account_retry"):
        await client.ready()
    assert "TypeError" in caplog.text and "_recover:" in caplog.text
    assert "PRIVATE-FIXTURE-BODY" not in caplog.text


async def test_unload_cancels_recovery_without_publishing_or_persisting(account):
    _, client, made = account
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    client._exchange.side_effect = blocked
    waiter = asyncio.create_task(client.ready())
    await entered.wait()
    task = client.task
    await client.close()
    await asyncio.gather(waiter, return_exceptions=True)
    assert task.done() and client.cloud is None
    assert client.state == "closed"
    made[0].async_stored_auth.assert_not_awaited()


async def test_late_executor_login_cannot_persist_after_unload(account):
    owner, client, _ = account
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    candidate = Cloud(None)
    candidate.hass, candidate.login_times = owner.hass, 0

    def login(_data):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        candidate.service_token = "late-result"
        loop.call_soon_threadsafe(finished.set)
        return True

    candidate._login_request = login
    candidate.async_login_attempt = lambda **kw: MiotCloud.async_login_attempt(candidate, **kw)
    client._candidate = AsyncMock(return_value=candidate)
    waiter = asyncio.create_task(client.ready())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await client.close()
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.gather(waiter, return_exceptions=True)
    candidate.async_stored_auth.assert_not_awaited()
    assert client.cloud is None and CloudSid.MICOAPI not in owner.clouds


async def test_legacy_conversation_routes_through_same_account_client(account):
    owner, client, _ = account
    cloud = SimpleNamespace(sid=CloudSid.MICOAPI, hass_entry=owner, http_timeout=10)
    client.request = AsyncMock(return_value={"code": 0, "data": {"records": []}})
    await MiotCloud.async_request_api(cloud,
        "https://userprofile.mina.mi.com/device_profile/v2/conversation",
        {"limit": 3}, method="GET", cookies={"deviceId": "fixture"})
    assert client.request.await_args.kwargs["read_only"] is True
    assert client.request.await_args.kwargs["cookies"] == {"deviceId": "fixture"}


async def test_real_http_status_business_errors_and_timeout_cleanup(account, aiohttp_server, socket_enabled):
    _, client, _ = account
    # Restore the real exchange; the adapter changes only the destination of
    # requests after production endpoint validation, never production policy.
    del client._exchange
    body, status, delay = {"code": 0, "data": []}, 200, 0
    seen = []
    async def handler(request):
        seen.append(dict(request.query))
        if delay:
            await asyncio.sleep(delay)
        return web.json_response(body, status=status)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        class Proxy:
            def request(self, method, url, **kwargs):
                return session.request(method, str(server.make_url("/test")), **kwargs)
            async def close(self):
                await session.close()
        proxy = Proxy()
        cloud = Cloud("fixture")
        cloud.async_session = proxy
        cloud.api_session = lambda **kw: proxy
        assert (await client._exchange(cloud, "/admin/v2/device_list", {"presence": False}, "GET", timeout=1))["code"] == 0
        assert seen[-1]["presence"] == "false"
        for status, body, expected in [
            (200, {"code": 3}, "auth_required"),
            (401, {}, "auth_required"), (429, {}, "rate_limited"),
            (503, {}, "http_error"), (200, {}, "invalid_envelope"),
        ]:
            with pytest.raises(MicoApiError, match=expected):
                await client._exchange(cloud, "/admin/v2/device_list", {}, "GET", timeout=1)
        status, body, delay = 200, {"code": 0}, 0.1
        with pytest.raises(TimeoutError):
            await client._exchange(cloud, "/admin/v2/device_list", {}, "GET", timeout=0.01)
        assert not session.connector._acquired
