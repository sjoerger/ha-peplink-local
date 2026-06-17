"""Device tracker platform for Peplink Local integration."""
import logging
from typing import Any, Dict, Optional

from homeassistant.components.device_tracker.const import SourceType
from homeassistant.components.device_tracker import ScannerEntity, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    ATTR_CLIENT_NAME,
    ATTR_CLIENT_MAC,
    ATTR_CLIENT_IP,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    """Set up Peplink device tracker based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    _LOGGER.debug("Setting up Peplink device trackers for entry: %s", entry.entry_id)

    await coordinator.async_config_entry_first_refresh()

    entities = []

    # GPS tracker for the router itself
    location_info = coordinator.data.get("location_info", {})
    if location_info.get("gps", False):
        location_data = location_info.get("location", {})
        if location_data and "latitude" in location_data and "longitude" in location_data:
            _LOGGER.debug("Creating GPS device tracker for Peplink router")
            entities.append(PeplinkGPSTracker(coordinator=coordinator, config_entry_id=entry.entry_id))

    async_add_entities(entities, True)

    # --- Client trackers ---
    # Track which MACs have entity objects so we never create duplicates.
    known_macs: set[str] = set()

    def _add_clients(clients: list[dict]) -> None:
        """Create entity objects for any MACs not yet tracked."""
        new_entities = []
        for client in clients:
            mac = client.get("mac")
            if not mac or mac in known_macs:
                continue
            known_macs.add(mac)
            name = client.get("name") or mac
            _LOGGER.debug("Creating device tracker for client: %s (%s)", name, mac)
            new_entities.append(
                PeplinkClientTracker(coordinator, entry.entry_id, name, mac)
            )
        if new_entities:
            async_add_entities(new_entities, True)

    # Restore previously registered client entities from the entity registry so
    # they show as "not_home" rather than "unavailable" after a restart.
    entity_reg = er.async_get(hass)
    prefix = f"{coordinator.host}_client_"
    restored = [
        {"mac": e.unique_id[len(prefix):], "name": e.original_name or e.unique_id[len(prefix):]}
        for e in er.async_entries_for_config_entry(entity_reg, entry.entry_id)
        if e.domain == "device_tracker" and e.unique_id.startswith(prefix)
    ]
    if restored:
        _LOGGER.debug("Restoring %d previously registered client trackers", len(restored))
        _add_clients(restored)

    # Add currently visible clients (may include ones not in registry yet)
    current_clients = coordinator.data.get("clients", {}).get("client", [])
    _add_clients(current_clients)

    # Dynamically add new clients discovered on subsequent coordinator updates
    @callback
    def _handle_coordinator_update() -> None:
        new_clients = coordinator.data.get("clients", {}).get("client", [])
        _add_clients(new_clients)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


class PeplinkGPSTracker(CoordinatorEntity, TrackerEntity):
    """Representation of the Peplink router GPS location."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, config_entry_id: str):
        """Initialize the GPS tracker."""
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self._attr_unique_id = f"{coordinator.host}_gps"
        self._attr_name = "GPS Location"
        self._latitude = None
        self._longitude = None
        self._attributes = {}
        self._update_gps_data()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Peplink router."""
        coordinator = self.coordinator
        device_name = (coordinator.device_name if coordinator.device_name else f"Peplink {coordinator.host}")
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry_id)},
            manufacturer="Peplink",
            model=coordinator.model,
            name=device_name,
            sw_version=coordinator.firmware,
        )

    @property
    def source_type(self) -> str:
        return SourceType.GPS

    @property
    def latitude(self) -> Optional[float]:
        return self._latitude

    @property
    def longitude(self) -> Optional[float]:
        return self._longitude

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return self._attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_gps_data()
        self.async_write_ha_state()

    def _update_gps_data(self) -> None:
        self._latitude = None
        self._longitude = None
        self._attributes = {}
        location_info = self.coordinator.data.get("location_info", {}) if self.coordinator.data else {}
        location_data = location_info.get("location", {})
        if location_data:
            self._latitude = location_data.get("latitude")
            self._longitude = location_data.get("longitude")
            for key, value in location_data.items():
                if key not in ["latitude", "longitude"]:
                    self._attributes[key] = value


class PeplinkClientTracker(CoordinatorEntity, ScannerEntity):
    """Representation of a Peplink client device tracker."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        client_name: str,
        client_mac: str,
    ):
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self._client_name = client_name
        self._client_mac = client_mac
        self._attr_unique_id = f"{coordinator.host}_client_{client_mac}"
        self._attr_name = client_name
        self._is_connected = False
        self._ip_address = None
        self._attributes = {}
        self._update_device_data()

    @property
    def device_info(self) -> DeviceInfo:
        coordinator = self.coordinator
        device_name = coordinator.device_name or f"Peplink {coordinator.host}"
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry_id)},
            manufacturer="Peplink",
            model=coordinator.model,
            name=device_name,
            sw_version=coordinator.firmware,
        )

    @property
    def source_type(self) -> str:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def ip_address(self) -> Optional[str]:
        return self._ip_address

    @property
    def mac_address(self) -> Optional[str]:
        return self._client_mac

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return self._attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_device_data()
        self.async_write_ha_state()

    def _update_device_data(self) -> None:
        self._is_connected = False
        self._ip_address = None
        self._attributes = {
            ATTR_CLIENT_NAME: self._client_name,
            ATTR_CLIENT_MAC: self._client_mac,
        }

        if not self.coordinator.data:
            return

        for client in self.coordinator.data.get("clients", {}).get("client", []):
            if client.get("mac") == self._client_mac:
                self._is_connected = client.get("active", False)
                self._ip_address = client.get("ip")
                for key, value in client.items():
                    if key not in ["mac", "name", "ip"]:
                        self._attributes[key] = value
                _LOGGER.debug(
                    "Client %s (%s) active=%s",
                    self._client_name, self._client_mac, self._is_connected,
                )
                break
