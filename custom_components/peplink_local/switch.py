"""Support for Peplink switches."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Callable

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)
from homeassistant.config_entries import ConfigEntry

from . import PeplinkDataUpdateCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class PeplinkSwitchEntityDescription(SwitchEntityDescription):
    """Class describing Peplink switch entities."""

    icon_off: str | None = None
    entity_category: EntityCategory | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Peplink switches."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []
    for connection in coordinator.data.get("wan_status", {}).get("connection", []):
        wan_id = str(connection.get("id", ""))
        if not wan_id:
            continue
        wan_name = connection.get("name", f"WAN {wan_id}")
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
            manufacturer="Peplink",
            model="WAN Connection",
            name=f"{coordinator.device_name or 'Peplink'} WAN{wan_id}",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )
        entities.append(PeplinkWANSwitch(
            coordinator=coordinator,
            wan_id=wan_id,
            wan_name=wan_name,
            device_info=device_info,
            initial_state=connection.get("enable", False),
        ))

    router_device = DeviceInfo(identifiers={(DOMAIN, coordinator.config_entry.entry_id)})

    # Add watchdog switch if supported
    watchdog = coordinator.data.get("watchdog", {})
    if watchdog.get("support"):
        entities.append(WatchdogSwitch(coordinator, router_device))

    # Add experimental feature switches
    experimental = coordinator.data.get("experimental", {})
    if experimental.get("dpi", {}).get("support"):
        entities.append(DpiSwitch(coordinator, router_device))
    if "bssidSteering" in experimental:
        entities.append(BssidSteeringSwitch(coordinator, router_device))
    if "starlinkApiProxy" in experimental:
        entities.append(StarlinkProxySwitch(coordinator, router_device))

    # Add Bluetooth switch if the API responded
    if coordinator.data.get("bluetooth"):
        entities.append(BluetoothSwitch(coordinator, router_device))

    # Add health check failure simulation switches for WANs with active health checks
    for wan_id, hc_data in coordinator.data.get("wan_health_check", {}).items():
        wan_name = hc_data.get("name", f"WAN {wan_id}")
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
            manufacturer="Peplink",
            model="WAN Connection",
            name=f"{coordinator.device_name or 'Peplink'} WAN{wan_id}",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )
        entities.append(WanHCFailureSimSwitch(coordinator, wan_id, wan_name, device_info))

    async_add_entities(entities)


class PeplinkWANSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a Peplink WAN enable/disable switch."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PeplinkDataUpdateCoordinator,
        wan_id: str,
        wan_name: str,
        device_info: DeviceInfo,
        initial_state: bool,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)

        self._wan_id = wan_id
        self._wan_name = wan_name
        self._attr_device_info = device_info
        self._attr_unique_id = f"{coordinator.device_name or coordinator.host}_wan{wan_id}_enable"
        self._attr_name = "WAN Enabled"
        self._attr_icon = "mdi:wan"
        self._is_on = initial_state

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data:
            for conn in self.coordinator.data.get("wan_status", {}).get("connection", []):
                if str(conn.get("id", "")) == self._wan_id:
                    self._is_on = conn.get("enable", False)
                    break
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool:
        """Return true if switch is on."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the WAN on (enable it)."""
        if await self._set_wan_state(True):
            self._is_on = True
            self.async_write_ha_state()
            self.hass.async_create_task(self._delayed_refresh())
        else:
            _LOGGER.error("Failed to enable WAN %s (%s)", self._wan_id, self._wan_name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the WAN off (disable it)."""
        if await self._set_wan_state(False):
            self._is_on = False
            self.async_write_ha_state()
            self.hass.async_create_task(self._delayed_refresh())
        else:
            _LOGGER.error("Failed to disable WAN %s (%s)", self._wan_id, self._wan_name)

    async def _delayed_refresh(self) -> None:
        await asyncio.sleep(3)
        await self.coordinator.async_request_refresh()

    async def _set_wan_state(self, enable: bool) -> bool:
        """Set WAN enable/disable state via API."""
        try:
            api = self.coordinator.api
            payload = {
                "action": "update",
                "list": [{"id": int(self._wan_id), "enable": enable}],
            }
            response = await api._make_api_request(
                "config.wan.connection",
                method="POST",
                data=payload,
                public_api=True,
            )
            if response.get("stat") != "ok":
                _LOGGER.error(
                    "API error setting WAN %s enable=%s: %s (code: %s)",
                    self._wan_id, enable,
                    response.get("message", "Unknown error"),
                    response.get("code", "Unknown"),
                )
                return False

            apply_response = await api._make_api_request(
                "cmd.config.apply",
                method="POST",
                data={},
                public_api=True,
            )
            if apply_response.get("stat") != "ok":
                _LOGGER.error(
                    "Failed to apply config after WAN %s enable=%s: %s",
                    self._wan_id, enable,
                    apply_response.get("message", "Unknown error"),
                )
                return False

            _LOGGER.info("WAN %s (%s) %s", self._wan_id, self._wan_name, "enabled" if enable else "disabled")
            return True

        except Exception as e:
            _LOGGER.error("Exception setting WAN %s state: %s", self._wan_id, e, exc_info=True)
            return False

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class WanHCFailureSimSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable health check failure simulation on a WAN interface."""

    _attr_has_entity_name = True
    _attr_name = "Health Check Simulation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:heart-broken"

    def __init__(self, coordinator, wan_id: str, wan_name: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._wan_id = wan_id
        self._wan_name = wan_name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_wan{wan_id}_hc_sim"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool:
        return self._wan_id in self.coordinator.data.get("hc_failure_simulation", set())

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_hc_failure_simulation(int(self._wan_id), True):
            _LOGGER.info("WAN %s (%s) health check simulation enabled", self._wan_id, self._wan_name)
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable health check simulation for WAN %s (%s)", self._wan_id, self._wan_name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_hc_failure_simulation(int(self._wan_id), False):
            _LOGGER.info("WAN %s (%s) health check simulation disabled", self._wan_id, self._wan_name)
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable health check simulation for WAN %s (%s)", self._wan_id, self._wan_name)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attrs = {
            "wan_id": self._wan_id,
            "wan_name": self._wan_name,
        }
        
        # Add current connection info if available
        if self.coordinator.data:
            try:
                wan_status = self.coordinator.data.get("wan_status", {})
                connections = wan_status.get("connection", [])
                for connection in connections:
                    if str(connection.get("id", "")) == self._wan_id:
                        attrs["status_led"] = connection.get("statusLed")
                        attrs["message"] = connection.get("message")
                        attrs["type"] = connection.get("type")
                        attrs["method"] = connection.get("method")
                        break
            except Exception:
                pass

        return attrs


class WatchdogSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable the router watchdog."""

    _attr_has_entity_name = True
    _attr_name = "Watchdog"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:dog"

    def __init__(self, coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_watchdog"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("watchdog", {}).get("enable")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(
            self.coordinator.data.get("watchdog")
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_watchdog_enabled(True):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable watchdog")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_watchdog_enabled(False):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable watchdog")


class DpiSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable the DPI engine."""

    _attr_has_entity_name = True
    _attr_name = "DPI Support"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:shield-search"

    def __init__(self, coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_dpi"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("experimental", {}).get("dpi", {}).get("enable")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(
            self.coordinator.data.get("experimental", {}).get("dpi")
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_dpi_enabled(True):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable DPI support")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_dpi_enabled(False):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable DPI support")


class BssidSteeringSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable Wi-Fi BSSID steering."""

    _attr_has_entity_name = True
    _attr_name = "Wi-Fi BSSID Steering"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi-arrow-left-right"

    def __init__(self, coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_bssid_steering"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("experimental", {}).get("bssidSteering", {}).get("enable")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and (
            "bssidSteering" in self.coordinator.data.get("experimental", {})
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_bssid_steering_enabled(True):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable BSSID steering")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_bssid_steering_enabled(False):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable BSSID steering")


class StarlinkProxySwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable the Starlink gRPC API proxy."""

    _attr_has_entity_name = True
    _attr_name = "Starlink gRPC Proxy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:satellite-variant"

    def __init__(self, coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_starlink_proxy"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("experimental", {}).get("starlinkApiProxy", {}).get("enable")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and (
            "starlinkApiProxy" in self.coordinator.data.get("experimental", {})
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_starlink_proxy_enabled(True):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable Starlink gRPC proxy")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_starlink_proxy_enabled(False):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable Starlink gRPC proxy")


class BluetoothSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable Bluetooth."""

    _attr_has_entity_name = True
    _attr_name = "Bluetooth"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth"

    def __init__(self, coordinator, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_bluetooth"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("bluetooth", {}).get("enable")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(
            self.coordinator.data.get("bluetooth")
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_bluetooth_enabled(True):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to enable Bluetooth")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.coordinator.api.set_bluetooth_enabled(False):
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Failed to disable Bluetooth")
