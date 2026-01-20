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
    _LOGGER.debug("=" * 60)
    _LOGGER.debug("SWITCH SETUP STARTING")
    _LOGGER.debug("=" * 60)
    
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    _LOGGER.debug("Got coordinator: %s", coordinator)

    entities = []

    # Get WAN connection data
    wan_status = coordinator.data.get("wan_status", {})
    _LOGGER.debug("wan_status keys: %s", list(wan_status.keys()) if wan_status else "None")
    
    connections = wan_status.get("connection", [])
    _LOGGER.debug("Found %s WAN connections", len(connections) if connections else 0)

    if connections:
        for connection in connections:
            wan_id = str(connection.get("id", ""))
            wan_name = connection.get("name", f"WAN {wan_id}")
            wan_enable = connection.get("enable", False)
            
            _LOGGER.debug(
                "Processing WAN connection: id=%s, name=%s, enable=%s",
                wan_id,
                wan_name,
                wan_enable
            )
            
            # Create device info for this WAN
            device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
                manufacturer="Peplink",
                model="WAN Connection",
                name=f"{coordinator.device_name or 'Peplink'} WAN{wan_id}",
                via_device=(DOMAIN, coordinator.config_entry.entry_id),
            )
            
            # Create WAN enable/disable switch
            _LOGGER.debug(
                "Creating WAN enable switch for WAN %s (%s), initial state=%s",
                wan_id,
                wan_name,
                wan_enable
            )
            
            switch = PeplinkWANSwitch(
                coordinator=coordinator,
                wan_id=wan_id,
                wan_name=wan_name,
                device_info=device_info,
                initial_state=wan_enable,
            )
            entities.append(switch)
            _LOGGER.debug("Created switch entity: %s", switch.unique_id)
    else:
        _LOGGER.warning("No WAN connections found to create switches!")

    _LOGGER.debug("Adding %s switch entities to Home Assistant", len(entities))
    async_add_entities(entities)
    _LOGGER.debug("=" * 60)
    _LOGGER.debug("SWITCH SETUP COMPLETE - Added %s entities", len(entities))
    _LOGGER.debug("=" * 60)


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

        _LOGGER.debug("Initializing PeplinkWANSwitch for WAN %s", wan_id)
        
        self._wan_id = wan_id
        self._wan_name = wan_name
        self._attr_device_info = device_info
        
        # Set unique ID
        self._attr_unique_id = f"{coordinator.device_name or coordinator.host}_wan{wan_id}_enable"
        _LOGGER.debug("Switch unique_id: %s", self._attr_unique_id)
        
        # Set name and icon
        self._attr_name = "Enabled"
        self._attr_icon = "mdi:wan"
        
        # Track state
        self._is_on = initial_state
        
        _LOGGER.debug(
            "Initialized WAN switch for WAN %s (%s), initial state: %s, unique_id: %s",
            wan_id,
            wan_name,
            initial_state,
            self._attr_unique_id
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Update our state from coordinator data
        if self.coordinator.data:
            try:
                wan_status = self.coordinator.data.get("wan_status", {})
                connections = wan_status.get("connection", [])
                for connection in connections:
                    if str(connection.get("id", "")) == self._wan_id:
                        coordinator_state = connection.get("enable", False)
                        if coordinator_state != self._is_on:
                            _LOGGER.debug(
                                "WAN %s state updated from coordinator: %s -> %s",
                                self._wan_id,
                                self._is_on,
                                coordinator_state
                            )
                            self._is_on = coordinator_state
                        break
            except Exception as e:
                _LOGGER.debug(
                    "Error updating WAN %s state from coordinator: %s",
                    self._wan_id,
                    e
                )
        
        # Call parent to update HA state
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool:
        """Return true if switch is on."""
        # Always return the local state - we trust our API calls
        # The coordinator will update in the background after a delay
        _LOGGER.debug(
            "is_on called for WAN %s, returning local state: %s",
            self._wan_id,
            self._is_on
        )
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the WAN on (enable it)."""
        _LOGGER.info("=" * 60)
        _LOGGER.info("TURN ON called for WAN %s (%s)", self._wan_id, self._wan_name)
        _LOGGER.info("Current state before turn_on: %s", self._is_on)
        _LOGGER.info("kwargs: %s", kwargs)
        _LOGGER.info("=" * 60)
        
        success = await self._set_wan_state(True)
        
        if success:
            _LOGGER.info("Turn ON successful for WAN %s, updating state", self._wan_id)
            self._is_on = True
            self.async_write_ha_state()
            
            # Schedule a delayed refresh to allow router to apply changes
            # Don't await it - let it happen in background
            async def delayed_refresh():
                await asyncio.sleep(3)  # Wait 3 seconds for router to apply
                _LOGGER.debug("Running delayed coordinator refresh for WAN %s", self._wan_id)
                await self.coordinator.async_request_refresh()
            
            self.hass.async_create_task(delayed_refresh())
        else:
            _LOGGER.error("Failed to enable WAN %s", self._wan_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the WAN off (disable it)."""
        _LOGGER.info("=" * 60)
        _LOGGER.info("TURN OFF called for WAN %s (%s)", self._wan_id, self._wan_name)
        _LOGGER.info("Current state before turn_off: %s", self._is_on)
        _LOGGER.info("kwargs: %s", kwargs)
        _LOGGER.info("=" * 60)
        
        success = await self._set_wan_state(False)
        
        if success:
            _LOGGER.info("Turn OFF successful for WAN %s, updating state", self._wan_id)
            self._is_on = False
            self.async_write_ha_state()
            
            # Schedule a delayed refresh to allow router to apply changes
            # Don't await it - let it happen in background
            async def delayed_refresh():
                await asyncio.sleep(3)  # Wait 3 seconds for router to apply
                _LOGGER.debug("Running delayed coordinator refresh for WAN %s", self._wan_id)
                await self.coordinator.async_request_refresh()
            
            self.hass.async_create_task(delayed_refresh())
        else:
            _LOGGER.error("Failed to disable WAN %s", self._wan_id)

    async def _set_wan_state(self, enable: bool) -> bool:
        """Set WAN enable/disable state via API."""
        _LOGGER.debug("-" * 60)
        _LOGGER.debug("_set_wan_state called: WAN %s, enable=%s", self._wan_id, enable)
        
        try:
            # Get the API instance from coordinator
            api = self.coordinator.api
            _LOGGER.debug("Got API instance: %s", api)
            
            # Prepare the request payload
            payload = {
                "action": "update",
                "list": [
                    {
                        "id": int(self._wan_id),
                        "enable": enable
                    }
                ]
            }
            
            _LOGGER.debug(
                "Prepared payload for WAN %s: %s",
                self._wan_id,
                payload
            )
            
            # Make API request to update WAN config
            _LOGGER.debug("Making API request to config.wan.connection...")
            response = await api._make_api_request(
                "config.wan.connection",
                method="POST",
                data=payload,
                public_api=True
            )
            _LOGGER.debug("API response: %s", response)
            
            # Check response
            if response.get("stat") != "ok":
                _LOGGER.error(
                    "API returned error for WAN %s: %s (code: %s)",
                    self._wan_id,
                    response.get("message", "Unknown error"),
                    response.get("code", "Unknown")
                )
                return False
            
            _LOGGER.debug("WAN %s config updated successfully, now applying changes...", self._wan_id)
            
            # Apply the configuration changes
            apply_response = await api._make_api_request(
                "cmd.config.apply",
                method="POST",
                data={},
                public_api=True
            )
            _LOGGER.debug("Config apply response: %s", apply_response)
            
            if apply_response.get("stat") != "ok":
                _LOGGER.error(
                    "Failed to apply config for WAN %s: %s",
                    self._wan_id,
                    apply_response.get("message", "Unknown error")
                )
                return False
            
            _LOGGER.info(
                "Successfully set WAN %s enable=%s and applied config",
                self._wan_id,
                enable
            )
            _LOGGER.debug("-" * 60)
            return True
                
        except Exception as e:
            _LOGGER.error(
                "Exception setting WAN %s state: %s",
                self._wan_id,
                e,
                exc_info=True
            )
            _LOGGER.debug("-" * 60)
            return False

    @property
    def available(self) -> bool:
        """Return if entity is available."""
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
