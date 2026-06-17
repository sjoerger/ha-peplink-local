"""Select platform for Peplink Local integration — WAN priority."""
import asyncio
import logging
from typing import Any, Optional

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PeplinkDataUpdateCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Peplink priority levels: 1–3 are active priorities, 0 means the WAN is
# excluded from routing (shown as "Disabled" in the router UI).
PRIORITY_OPTIONS = ["Priority 1 (Highest)", "Priority 2", "Priority 3", "Disabled"]

_OPTION_TO_API: dict[str, int] = {
    "Priority 1 (Highest)": 1,
    "Priority 2": 2,
    "Priority 3": 3,
    "Disabled": 0,
}
_API_TO_OPTION: dict[int, str] = {v: k for k, v in _OPTION_TO_API.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WAN priority select entities."""
    coordinator: PeplinkDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []
    for connection in coordinator.data.get("wan_status", {}).get("connection", []):
        wan_id = str(connection.get("id", ""))
        if not wan_id:
            continue
        entities.append(WanPrioritySelect(coordinator, config_entry.entry_id, wan_id, connection.get("name", f"WAN {wan_id}")))

    async_add_entities(entities)


class WanPrioritySelect(CoordinatorEntity, SelectEntity):
    """Select entity to get/set the routing priority of a WAN interface."""

    _attr_has_entity_name = True
    _attr_options = PRIORITY_OPTIONS
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:priority-high"

    def __init__(
        self,
        coordinator: PeplinkDataUpdateCoordinator,
        config_entry_id: str,
        wan_id: str,
        wan_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._wan_id = wan_id
        self._wan_name = wan_name
        self._attr_unique_id = f"{coordinator.device_name or coordinator.host}_wan{wan_id}_priority"
        self._attr_name = "Priority"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
            name=f"{coordinator.device_name or 'Peplink'} WAN{wan_id}",
            manufacturer="Peplink",
            model="WAN Connection",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _wan_data(self) -> dict[str, Any]:
        for conn in self.coordinator.data.get("wan_status", {}).get("connection", []):
            if str(conn.get("id", "")) == self._wan_id:
                return conn
        return {}

    @property
    def current_option(self) -> Optional[str]:
        priority = self._wan_data().get("priority")
        if priority is None:
            return "Disabled"
        option = _API_TO_OPTION.get(priority)
        if option is None:
            _LOGGER.warning("WAN %s has unrecognised priority value %r; defaulting to Priority 1", self._wan_id, priority)
            return "Priority 1 (Highest)"
        return option

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        """Set WAN routing priority."""
        api_value = _OPTION_TO_API.get(option)
        if api_value is None:
            _LOGGER.error("Unknown priority option: %s", option)
            return

        api = self.coordinator.api
        payload = {
            "action": "update",
            "list": [{"id": int(self._wan_id), "priority": api_value}],
        }

        response = await api._make_api_request(
            "config.wan.connection",
            method="POST",
            data=payload,
            public_api=True,
        )
        if response.get("stat") != "ok":
            _LOGGER.error(
                "Failed to set WAN %s priority to %s: %s",
                self._wan_id, option, response.get("message", response),
            )
            return

        apply_response = await api._make_api_request(
            "cmd.config.apply",
            method="POST",
            data={},
            public_api=True,
        )
        if apply_response.get("stat") != "ok":
            _LOGGER.error(
                "Failed to apply config after WAN %s priority change: %s",
                self._wan_id, apply_response.get("message", apply_response),
            )
            return

        _LOGGER.info("WAN %s (%s) priority set to %s", self._wan_id, self._wan_name, option)

        async def _delayed_refresh() -> None:
            await asyncio.sleep(3)
            await self.coordinator.async_request_refresh()

        self.hass.async_create_task(_delayed_refresh())
