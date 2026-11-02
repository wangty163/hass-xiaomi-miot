"""Versioned account API for dependent integrations; no credential access."""

from .core.hass_entry import HassEntry
from .core.micoapi import API_VERSION, MicoApiError


def get_micoapi_client(hass, entry_id):
    owner = HassEntry.ALL.get(entry_id)
    if owner is None or owner.hass is not hass:
        raise MicoApiError("account_unavailable")
    return owner.micoapi
