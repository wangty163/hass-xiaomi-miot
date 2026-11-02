"""Account-owned Mina requests and recovery, shared by every speaker reader.

An HTTP caller never owns login. Cancellation only detaches that caller; the
config entry owns the one recovery task. Login works on a private candidate,
and only a verified, persisted candidate replaces the current session.
"""

import asyncio
import json
import logging
import time
import traceback
from urllib.parse import urlsplit

import aiohttp
import requests

from .xiaomi_cloud import (
    CloudSid, MiCloudAuthenticationError, MiCloudException, MiCloudNeedVerify,
    MiCloudStsUnauthorized, MiCloudVerificationError, MiotCloud,
)

_LOGGER = logging.getLogger(__name__)
API_VERSION = 1
BASE_URL = "https://api2.mina.mi.com"
ALLOWED_HOSTS = {"api2.mina.mi.com", "userprofile.mina.mi.com"}


class MicoApiError(Exception):
    """Only allowlisted diagnostics, never response bodies or credentials."""

    def __init__(self, reason, *, response_code=None):
        super().__init__(reason)
        self.reason = reason
        self.response_code = response_code if type(response_code) is int else None


def is_read_request(api, data, method):
    path = urlsplit(api).path
    return (
        method == "GET" and path in {
            "/admin/v2/device_list", "/device_profile/v2/conversation",
        }
    ) or (
        method == "POST" and path == "/remote/ubus"
        and isinstance(data, dict) and data.get("path") == "mediaplayer"
        and data.get("method") == "player_get_play_status"
    )


class MicoApiClient:
    """One client per config entry, with bounded and deduplicated recovery."""

    api_version = API_VERSION

    def __init__(self, owner, *, clock=time.monotonic):
        self.owner = owner
        self.clock = clock
        self.cloud = None
        self.task = None
        self.closed = False
        self.state = "initializing"
        self.generation = 0
        self.failures = 0
        self.retry_at = 0.0
        self.last_error = None
        self.response_code = None
        self._active = {}
        self._retired = set()
        self._sessions = set()
        self._login_identity = None

    @property
    def diagnostics(self):
        return {
            "api_version": API_VERSION, "state": self.state,
            "generation": self.generation, "failures": self.failures,
            "retry_seconds": max(0, round(self.retry_at - self.clock(), 1)),
            "error": self.last_error, "response_code": self.response_code,
        }

    def _start_recovery(self):
        if self.closed or self.state == "reauth_required":
            return
        if self.task is not None and not self.task.done():
            return
        if self.clock() < self.retry_at:
            return
        self.state = "refreshing" if self.generation else "initializing"
        self.task = self.owner.entry.async_create_background_task(
            self.owner.hass, self._recover(), "xiaomi-micoapi-recovery",
            eager_start=False,
        )

    async def ready(self):
        if self.closed:
            raise MicoApiError("account_unavailable")
        if self.state != "ready":
            self._start_recovery()
            if self.task is not None and not self.task.done():
                await asyncio.shield(self.task)
        if self.state != "ready" or self.closed:
            raise MicoApiError(
                "auth_required" if self.state == "reauth_required" else
                "account_retry" if self.state == "retry_wait" else "account_unavailable",
                response_code=self.response_code,
            )
        return self.cloud

    async def _candidate(self, *, cached):
        config = {**self.owner.get_config(), "sid": CloudSid.MICOAPI.value}
        if cached:
            candidate = await MiotCloud.from_token(
                self.owner.hass, config, login=False, hass_entry=self.owner,
            )
            candidate.async_session = None
            self._login_identity = (candidate.client_id, candidate.useragent)
            return candidate
        # Never clear credentials on the object being used by another reader.
        candidate = await self.owner.hass.async_add_executor_job(
            MiotCloud, self.owner.hass, config.get("username"),
            config.get("password"), config.get("server_country"),
            CloudSid.MICOAPI.value, self.owner,
        )
        candidate.user_id = str(config.get("user_id") or "")
        if self._login_identity is not None:
            candidate.client_id, candidate.useragent = self._login_identity
        return candidate

    async def _probe(self, cloud):
        result = await self._exchange(cloud, "/admin/v2/device_list", {
            "presence": "false", "master": "false",
        }, "GET", timeout=5)
        if not isinstance(result.get("data"), list):
            raise MicoApiError("invalid_device_list")

    async def _recover(self):
        candidate = None
        try:
            candidate = await self._candidate(cached=self.generation == 0)
            needs_login = not candidate.service_token
            if not needs_login:
                try:
                    await self._probe(candidate)
                except MicoApiError as exc:
                    if exc.reason != "auth_required":
                        raise
                    needs_login = True
            if needs_login:
                if candidate.service_token:
                    await self._retire(candidate)
                    candidate = await self._candidate(cached=False)
                # The worker mutates only this private candidate. In particular,
                # a cancelled/unloaded owner cannot persist a late worker result.
                if not await candidate.async_login_attempt(persist=False):
                    if candidate._has_complete_captcha():
                        raise MicoApiError("auth_required")
                    raise MicoApiError("login_incomplete")
                expected = str(self.owner.get_config("user_id") or "")
                if expected and str(candidate.user_id) != expected:
                    raise MicoApiError("auth_required")
                await self._probe(candidate)
            if self.closed:
                return
            await candidate.async_stored_auth(save=True)
            if self.closed:
                return
            # No await between publication of the credentials/session and state.
            old_cloud = self.cloud
            self.cloud = candidate
            self.owner.clouds[CloudSid.MICOAPI] = candidate
            self.generation += 1
            self.failures = 0
            self.retry_at = 0
            self.last_error = self.response_code = None
            self.state = "ready"
            if old_cloud is not None and old_cloud is not candidate:
                await self._retire(old_cloud)
        except asyncio.CancelledError:
            raise
        except (MiCloudAuthenticationError, MiCloudNeedVerify,
                MiCloudStsUnauthorized, MiCloudVerificationError):
            await self._failed(MicoApiError("auth_required"))
        except MicoApiError as exc:
            await self._failed(exc)
        except (TimeoutError, aiohttp.ClientError, requests.exceptions.RequestException):
            await self._failed(MicoApiError("connection_error"))
        except MiCloudException:
            # Some library captcha paths use an untyped exception.
            reason = "auth_required" if candidate is not None and candidate._has_complete_captcha() else "login_incomplete"
            await self._failed(MicoApiError(reason))
        except Exception as exc:
            # Frame names and exception type are sufficient; no exception text,
            # locals, request bodies or credential-bearing URLs are logged.
            frames = [f"{frame.name}:{frame.lineno}" for frame in traceback.extract_tb(exc.__traceback__)]
            _LOGGER.error("Mina recovery failed (%s; frames=%s)", type(exc).__name__, frames)
            await self._failed(MicoApiError("internal_error"))
        finally:
            if candidate is not None and candidate is not self.cloud:
                await self._retire(candidate)

    async def _failed(self, error):
        if self.closed:
            return
        self.last_error, self.response_code = error.reason, error.response_code
        self.failures += 1
        if error.reason == "auth_required":
            self.state = "reauth_required"
            await self.owner.async_auth_failed(CloudSid.MICOAPI)
        else:
            self.state = "retry_wait"
            self.retry_at = self.clock() + min(60, 2 ** min(self.failures, 6))

    def _reject(self, generation):
        # A slow response from the old session cannot discard the new one.
        if generation == self.generation and self.state == "ready":
            self.state = "refreshing"
            self._start_recovery()

    async def request(self, api, data=None, method="GET", *, read_only=False,
                      timeout=10, cookies=None):
        try:
            async with asyncio.timeout(timeout):
                for attempt in range(2 if read_only else 1):
                    cloud = await self.ready()
                    generation = self.generation
                    try:
                        return await self._exchange(
                            cloud, api, data, method, timeout=timeout, cookies=cookies,
                        )
                    except MicoApiError as exc:
                        if exc.reason != "auth_required":
                            raise
                        self._reject(generation)
                        if not read_only or attempt:
                            raise MicoApiError("account_refreshing", response_code=exc.response_code) from None
        except TimeoutError:
            if self.state in {"initializing", "refreshing"}:
                raise MicoApiError("account_refreshing") from None
            raise

    async def _exchange(self, cloud, api, data, method, *, timeout, cookies=None):
        url = api if api.startswith("https://") else BASE_URL + "/" + api.lstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise MicoApiError("invalid_endpoint")
        if isinstance(data, dict):
            data = {k: str(v).lower() if type(v) is bool else v for k, v in data.items()}
        kwargs = {"params" if method == "GET" else "data": data}
        if cookies:
            kwargs["cookies"] = cookies
        session = cloud.api_session(**{"async": True})
        self._sessions.add(session)
        key = id(cloud)
        self._active[key] = self._active.get(key, 0) + 1
        try:
            async with session.request(method, url, **kwargs,
                    timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise MicoApiError("auth_required", response_code=response.status)
                if response.status != 200:
                    raise MicoApiError("rate_limited" if response.status == 429 else "http_error", response_code=response.status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise MicoApiError("response_too_large")
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError):
                    raise MicoApiError("invalid_json") from None
                if not isinstance(result, dict) or type(result.get("code")) is not int:
                    raise MicoApiError("invalid_envelope")
                if result["code"] in (2, 3):
                    raise MicoApiError("auth_required", response_code=result["code"])
                if result["code"]:
                    raise MicoApiError("cloud_rejected", response_code=result["code"])
                return result
        except aiohttp.ClientError:
            raise MicoApiError("connection_error") from None
        finally:
            self._active[key] -= 1
            if not self._active[key]:
                self._active.pop(key)
                if key in self._retired:
                    await self._retire(cloud)

    async def _retire(self, cloud):
        key = id(cloud)
        self._retired.add(key)
        if not self._active.get(key):
            session = getattr(cloud, "async_session", None)
            if session in self._sessions:
                await session.close()
                self._sessions.discard(session)
            self._retired.discard(key)

    async def close(self):
        self.closed = True
        self.state = "closed"
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = self.cloud = None
        await asyncio.gather(*(session.close() for session in self._sessions))
        self._sessions.clear()
