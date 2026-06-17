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

    entity_reg = er.async_get(hass)
    prefix = f"{coordinator.host}_client_"
    wifi_prefix = f"{coordinator.host}_wifi_client_"
    gps_uid = f"{coordinator.host}_gps"
    gps_enabled = bool(coordinator.data.get("location_info", {}).get("gps", False))

    pre_existing = list(er.async_entries_for_config_entry(entity_reg, entry.entry_id))

    # ── Wi-Fi client trackers (extap.client → SSID devices) ──────────────────
    # These are keyed by client_name (for MAC rotation) and link to the VAP
    # device the client is currently associated with.

    wifi_known_names: set[str] = set()
    wifi_known_uids: set[str] = set()

    def _wifi_client_uid(name: str) -> str:
        return f"{coordinator.host}_wifi_client_{name}"

    def _add_wifi_clients(clients_data: dict) -> None:
        new_entities = []
        for mac, client in clients_data.items():
            name = client.get("client_name") or mac
            if not name or name in wifi_known_names:
                continue
            wifi_known_names.add(name)
            wifi_known_uids.add(_wifi_client_uid(name))
            _LOGGER.debug("Creating Wi-Fi tracker for client: %s (%s)", name, mac)
            new_entities.append(
                PeplinkWifiClientTracker(coordinator, entry.entry_id, name, mac)
            )
        if new_entities:
            async_add_entities(new_entities)

    current_wifi_clients = coordinator.data.get("wifi_clients", {})
    wifi_current_names = {
        (v.get("client_name") or k) for k, v in current_wifi_clients.items()
    }

    # Restore long-term offline Wi-Fi clients from the entity registry.
    wifi_offline_names = {
        e.original_name
        for e in pre_existing
        if e.domain == "device_tracker"
        and e.unique_id.startswith(wifi_prefix)
        and e.original_name
        and e.original_name not in wifi_current_names
    }

    _add_wifi_clients(current_wifi_clients)
    if wifi_offline_names:
        _LOGGER.debug("Restoring %d offline Wi-Fi client trackers", len(wifi_offline_names))
        _add_wifi_clients({n: {"client_name": n} for n in wifi_offline_names})

    # Remove stale Wi-Fi tracker entries no longer seen.
    wifi_to_remove = [
        e.entity_id
        for e in pre_existing
        if e.domain == "device_tracker"
        and e.unique_id.startswith(wifi_prefix)
        and e.unique_id not in wifi_known_uids
    ]
    for entity_id in wifi_to_remove:
        _LOGGER.debug("Removing stale Wi-Fi tracker: %s", entity_id)
        entity_reg.async_remove(entity_id)

    # ── Ethernet client trackers (status.client → main router device) ─────────
    # Wi-Fi clients are excluded here to avoid creating duplicate trackers.

    known_names: set[str] = set()
    known_uids: set[str] = set()

    def _client_uid(name: str) -> str:
        return f"{coordinator.host}_client_{name}"

    def _add_clients(clients: list[dict]) -> None:
        new_entities = []
        for client in clients:
            mac = client.get("mac")
            name = client.get("name") or mac
            if not name:
                continue
            if name in known_names:
                _LOGGER.debug("Skipping duplicate client name %s", name)
                continue
            if name in wifi_known_names:
                continue  # Already tracked as a Wi-Fi client
            known_names.add(name)
            known_uids.add(_client_uid(name))
            _LOGGER.debug(
                "Creating device tracker for client: %s (%s)",
                name, mac or "offline",
            )
            new_entities.append(
                PeplinkClientTracker(coordinator, entry.entry_id, name, mac)
            )
        if new_entities:
            async_add_entities(new_entities)

    current_clients = coordinator.data.get("clients", {}).get("client", [])
    current_names = {
        (c.get("name") or c.get("mac"))
        for c in current_clients
        if c.get("name") or c.get("mac")
    }

    # Collect offline ethernet-only names from the registry (exclude Wi-Fi names).
    offline_names = {
        e.original_name
        for e in pre_existing
        if e.domain == "device_tracker"
        and e.unique_id.startswith(prefix)
        and e.original_name
        and e.original_name not in current_names
        and e.original_name not in wifi_known_names
        and ":" not in e.unique_id[len(prefix):]
    }

    # Remove legacy MAC-format entries before registering name-format ones.
    for e in pre_existing:
        if (e.domain == "device_tracker"
                and e.unique_id.startswith(prefix)
                and ":" in e.unique_id[len(prefix):]):
            _LOGGER.debug("Removing legacy MAC-format tracker: %s", e.entity_id)
            entity_reg.async_remove(e.entity_id)

    _add_clients(current_clients)

    if offline_names:
        _LOGGER.debug("Restoring %d offline client trackers", len(offline_names))
        _add_clients([{"name": n, "mac": None} for n in offline_names])

    # Remove stale ethernet trackers (scoped to the ethernet prefix only so
    # Wi-Fi trackers are not accidentally pruned).
    to_remove = [
        e.entity_id
        for e in pre_existing
        if e.domain == "device_tracker"
        and not (gps_enabled and e.unique_id == gps_uid)
        and e.unique_id.startswith(prefix)
        and e.unique_id not in known_uids
        and ":" not in e.unique_id[len(prefix):]
    ]
    for entity_id in to_remove:
        _LOGGER.debug("Removing stale device tracker from registry: %s", entity_id)
        entity_reg.async_remove(entity_id)

    # Dynamically add new clients discovered on subsequent coordinator updates.
    @callback
    def _handle_coordinator_update() -> None:
        _add_clients(coordinator.data.get("clients", {}).get("client", []))
        _add_wifi_clients(coordinator.data.get("wifi_clients", {}))

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
    """Representation of a Peplink ethernet client device tracker.

    Keyed by device NAME rather than MAC address so that MAC rotation
    (Apple Watch, iPhone privacy MACs) is handled without creating duplicate
    entities or requiring a restart.  _update_device_data finds the current
    MAC by searching coordinator data by name on every coordinator update.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        client_name: str,
        initial_mac: Optional[str],
    ):
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self._client_name = client_name
        self._attr_unique_id = f"{coordinator.host}_client_{client_name}"
        self._attr_name = client_name
        self._current_mac: Optional[str] = initial_mac
        self._is_connected = False
        self._ip_address = None
        self._attributes: Dict[str, Any] = {}
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
        return self._current_mac

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
            ATTR_CLIENT_MAC: self._current_mac or "",
        }

        if not self.coordinator.data:
            return

        # Find this device by name.  When the same name appears more than once
        # (device has multiple DHCP leases from MAC rotation), prefer the active
        # entry so the entity correctly shows "home" even after a MAC change.
        matched = None
        for client in self.coordinator.data.get("clients", {}).get("client", []):
            if (client.get("name") or client.get("mac")) != self._client_name:
                continue
            if matched is None or client.get("active", False):
                matched = client
            if matched.get("active", False):
                break

        if matched:
            new_mac = matched.get("mac")
            if new_mac:
                self._current_mac = new_mac
            self._is_connected = matched.get("active", False)
            self._ip_address = matched.get("ip")
            for key, value in matched.items():
                if key not in ["mac", "name", "ip"]:
                    self._attributes[key] = value
            self._attributes[ATTR_CLIENT_MAC] = self._current_mac or ""
            _LOGGER.debug(
                "Client %s (%s) active=%s",
                self._client_name, self._current_mac, self._is_connected,
            )


class PeplinkWifiClientTracker(CoordinatorEntity, ScannerEntity):
    """Device tracker for a Wi-Fi client on the router's built-in AP.

    Keyed by client_name for MAC rotation handling. Appears under the SSID
    (VAP) device the client is currently associated with, falling back to the
    main router device when the client is offline.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        client_name: str,
        initial_mac: Optional[str],
    ):
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self._client_name = client_name
        self._attr_unique_id = f"{coordinator.host}_wifi_client_{client_name}"
        self._attr_name = client_name
        self._current_mac: Optional[str] = initial_mac
        self._last_vap_id: Optional[str] = None
        self._is_connected = False
        self._ip_address: Optional[str] = None
        self._attributes: Dict[str, Any] = {}
        self._update_wifi_data()

    def _matched_client(self) -> Optional[dict]:
        if not self.coordinator.data:
            return None
        matched = None
        for mac, client in self.coordinator.data.get("wifi_clients", {}).items():
            if (client.get("client_name") or mac) != self._client_name:
                continue
            if matched is None or client.get("is_assoc", False):
                matched = client
            if matched.get("is_assoc", False):
                break
        return matched

    @property
    def device_info(self) -> DeviceInfo:
        vap_id = self._last_vap_id
        if vap_id and self.coordinator.data:
            if vap_id in self.coordinator.data.get("vap_summary", {}):
                return DeviceInfo(
                    identifiers={(DOMAIN, f"{self._config_entry_id}_vap{vap_id}")},
                )
        return DeviceInfo(identifiers={(DOMAIN, self._config_entry_id)})

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
        return self._current_mac

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return self._attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_wifi_data()
        self.async_write_ha_state()

    def _update_wifi_data(self) -> None:
        self._is_connected = False
        self._ip_address = None
        self._attributes = {
            ATTR_CLIENT_NAME: self._client_name,
            ATTR_CLIENT_MAC: self._current_mac or "",
        }

        matched = self._matched_client()
        if not matched:
            return

        new_mac = matched.get("mac")
        if new_mac:
            self._current_mac = new_mac

        self._is_connected = matched.get("is_assoc", False)

        ip = matched.get("ip_addr")
        if ip and ip != "0.0.0.0":
            self._ip_address = ip

        vap_id = matched.get("vap_id")
        if vap_id is not None:
            self._last_vap_id = str(vap_id)

        self._attributes[ATTR_CLIENT_MAC] = self._current_mac or ""

        rssi = matched.get("rssi")
        if rssi:
            self._attributes["rssi"] = rssi
        freq = matched.get("freq")
        if freq:
            self._attributes["frequency"] = freq
        mode = matched.get("mode")
        if mode:
            self._attributes["wifi_mode"] = mode
        wifigen = matched.get("wifigen")
        if wifigen:
            self._attributes["wifi_generation"] = wifigen
        ssid = matched.get("ssid")
        if ssid:
            self._attributes["ssid"] = ssid
        duration = matched.get("duration")
        if duration is not None:
            self._attributes["duration"] = duration

        _LOGGER.debug(
            "Wi-Fi client %s (%s) assoc=%s vap=%s rssi=%s",
            self._client_name, self._current_mac, self._is_connected,
            self._last_vap_id, rssi,
        )
