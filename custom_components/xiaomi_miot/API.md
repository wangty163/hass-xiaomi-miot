# Mina account API v1

`custom_components.xiaomi_miot.api` exports `API_VERSION = 1`,
`get_micoapi_client(hass, account_entry_id)` and `MicoApiError`.
Consumers keep the config entry reference, never a second copy of credentials.
Resolve the client again after an account reload; the old client closes with its
config entry. The supported provider is Xiaomi Miot `1.1.5-wty.7` or newer with
the same API version. XiaoAI Playback `1.1.0` is the companion consumer.

`await client.request(path, data, method, read_only=False, timeout=10, cookies=None)`
accepts HTTPS requests only to the Mina API and conversation hosts. It checks
HTTP status and the JSON business code, limits response size, and bounds the
whole caller operation including authentication. Only explicitly read-only
requests may replay once after a confirmed authentication rejection. Controls
are never replayed. A timeout does not establish whether a control took effect.

The account owns one recovery task for every playback and conversation reader.
An expired session triggers login on a private candidate using the same login
device identity. The candidate is probed and saved before atomic publication.
Late rejection of an older generation cannot invalidate its replacement.
Cancelling a reader detaches it without cancelling account recovery. Unloading
the account cancels recovery; a late executor result cannot publish or save it.

Lifecycle states are `initializing`, `ready`, `refreshing`, `retry_wait`,
`reauth_required` and `closed`. Temporary recovery failures back off for
2, 4, 8, 16, 32 and then 60 seconds; a later reader retries after the deadline.
A failed initial probe never permanently caches an absent account. Only a
confirmed login rejection or verification requirement starts HA reauthentication.
The HA `async_start_reauth` callback is synchronous despite its name.

`MicoApiError.reason` and optional integer `response_code` are safe diagnostics.
Expected reasons include `account_refreshing`, `account_retry`, `auth_required`,
`account_unavailable`, `connection_error`, `rate_limited`, `http_error`,
`invalid_json`, `invalid_envelope` and `cloud_rejected`. Do not interpret network
failure or account initialization as invalid credentials. `client.diagnostics`
adds state, generation, failure count and remaining backoff, without tokens,
account identity, URLs, response bodies, device IDs or conversation content.

Legacy `MiotCloud.async_request_api` calls with an owning MICOAPI account route
through this same client. Existing media and conversation consumers therefore
participate in the same recovery instead of clearing shared credentials.
