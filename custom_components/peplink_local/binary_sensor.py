"""Binary sensor platform for Peplink Local integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PeplinkDataUpdateCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class PeplinkBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class describing Peplink binary sensor entities."""

    value_fn: Callable[[dict], Any] | None = None
    icon: str | None = None


BINARY_SENSOR_TYPES: tuple[PeplinkBinarySensorEntityDescription, ...] = (
    PeplinkBinarySensorEntityDescription(
        key="connection_status",
        translation_key=None,
        name="Connection Status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda x: x.get("message", "").startswith("Connected") or x.get("message", "") == "Standby",
        icon="mdi:network",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Peplink binary sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    entities = []
    
    # Get the WAN status data
    wan_status = coordinator.data.get("wan_status", {})
    wan_connections = wan_status.get("connection", [])
    
    # Add binary sensors for each WAN connection
    for connection in wan_connections:
        wan_id = connection.get("id", "")
        wan_name = connection.get("name", f"WAN {wan_id}")
        
        # Skip disabled WANs
        if connection.get("enable") is False:
            continue
            
        # Use the device name from API if available, otherwise fallback to IP
        device_name = coordinator.device_name or f"Peplink {coordinator.host}"
            
        # Create device info for this WAN
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
            manufacturer="Peplink",
            model="WAN Connection",
            name=f"{device_name} WAN{wan_id}",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )
        
        # Add all binary sensors
        for description in BINARY_SENSOR_TYPES:
            entities.append(
                PeplinkWANBinarySensor(
                    coordinator=coordinator,
                    description=description,
                    sensor_data=connection,
                    device_info=device_info,
                    wan_id=wan_id,
                )
            )
    
    # Add SpeedFusion Connect / PepVPN connectivity binary sensors
    pepvpn_status = coordinator.data.get("pepvpn_status", {})
    pepvpn_peers = pepvpn_status.get("peers", [])
    device_name_prefix = coordinator.device_name or f"Peplink {coordinator.host}"

    # Build parent device per unique profile_id and add an aggregate connected sensor to each
    _pepvpn_parent_devices: dict[str, DeviceInfo] = {}
    for peer in pepvpn_peers:
        pid = peer.get("profile_id", "")
        if pid and pid not in _pepvpn_parent_devices:
            try:
                is_sfc = int(pid) >= 60000
            except ValueError:
                is_sfc = False
            parent_device = DeviceInfo(
                identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_pepvpn_profile_{pid}")},
                manufacturer="Peplink",
                model="SpeedFusion Connect" if is_sfc else "SpeedFusion VPN Profile",
                name=f"{device_name_prefix} SpeedFusion Connect" if is_sfc else f"{device_name_prefix} VPN Profile {pid}",
                via_device=(DOMAIN, coordinator.config_entry.entry_id),
            )
            _pepvpn_parent_devices[pid] = parent_device
            entities.append(
                PeplinkPepVPNProfileBinarySensor(
                    coordinator=coordinator,
                    profile_id=pid,
                    device_info=parent_device,
                )
            )

    for peer in pepvpn_peers:
        peer_id = peer["peer_id"]
        profile_id = peer["profile_id"]
        peer_name = peer.get("name") or peer.get("serial_number") or f"Peer {peer_id}"
        peer_device = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_pepvpn_peer_{peer_id}")},
            manufacturer="Peplink",
            model="SpeedFusion Connect Peer",
            name=f"{device_name_prefix} {peer_name}",
            via_device=(DOMAIN, f"{coordinator.config_entry.entry_id}_pepvpn_profile_{profile_id}"),
        )
        entities.append(
            PeplinkPepVPNPeerBinarySensor(
                coordinator=coordinator,
                peer_id=peer_id,
                device_info=peer_device,
            )
        )

    # Add health check binary sensors for WANs that appear in health check data
    wan_health_check = coordinator.data.get("wan_health_check", {})
    for wan_id, hc_data in wan_health_check.items():
        wan_name = hc_data.get("name", f"WAN {wan_id}")
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_wan{wan_id}")},
            manufacturer="Peplink",
            model="WAN Connection",
            name=f"{coordinator.device_name or 'Peplink'} WAN{wan_id}",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )
        entities.append(WanHealthCheckBinarySensor(coordinator, wan_id, wan_name, device_info))

    # Add AP-level binary sensor if the device supports an AP
    ap_status = coordinator.data.get("ap_status", {})
    if ap_status.get("support"):
        entities.append(ApEnabledBinarySensor(coordinator))

    # Add per-SSID active binary sensor for each VAP
    vap_summary = coordinator.data.get("vap_summary", {})
    for vap_id, vap_data in vap_summary.items():
        ssid = vap_data.get("ssid", f"VAP {vap_id}")
        vap_device = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_vap{vap_id}")},
            manufacturer="Peplink",
            model="Wi-Fi SSID",
            name=f"{device_name_prefix} {ssid}",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )
        entities.append(VapActiveBinarySensor(coordinator, vap_id, vap_device))

    # Add online binary sensor for local AP radio(s)
    ap_radio = coordinator.data.get("ap_radio", {})
    for ap_id, ap_data in ap_radio.items():
        if ap_data.get("is_local_ap"):
            entities.append(ApRadioOnlineBinarySensor(coordinator, ap_id))

    # Add port link binary sensors for LAN and WAN physical ports
    router_device = DeviceInfo(identifiers={(DOMAIN, coordinator.config_entry.entry_id)})
    for port_type, data_key in (("lan", "port_lan"), ("wan", "port_wan")):
        port_data = coordinator.data.get(data_key, {})
        order = port_data.get("order", [])
        for port_id in order:
            port_info = port_data.get(str(port_id))
            if isinstance(port_info, dict):
                entities.append(PortLinkBinarySensor(coordinator, port_type, str(port_id), router_device))

    async_add_entities(entities)


class PeplinkWANBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Implementation of a Peplink WAN binary sensor."""

    entity_description: PeplinkBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PeplinkDataUpdateCoordinator,
        description: PeplinkBinarySensorEntityDescription,
        sensor_data: dict[str, Any],
        device_info: DeviceInfo,
        wan_id: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)

        self.entity_description = description
        self._initial_sensor_data = sensor_data  # Keep reference to initial data as fallback
        self._wan_id = wan_id  # Store WAN ID to find the right data in updates
        self._attr_unique_id = f"{coordinator.host}_wan{wan_id}_{description.key}_binary_{coordinator.config_entry.entry_id}"
        self._attr_device_info = device_info
        
        # Set custom icon if provided
        if description.icon:
            self._attr_icon = description.icon

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        if self.entity_description.value_fn is None:
            return None

        # Try to get fresh data from coordinator
        if self.coordinator.data:
            try:
                # For WAN sensors, get data from wan_status
                wan_status = self.coordinator.data.get("wan_status", {})
                connections = wan_status.get("connection", [])
                
                for connection in connections:
                    if str(connection.get("id", "")) == self._wan_id:
                        return self.entity_description.value_fn(connection)
            except Exception:
                # If anything goes wrong, fall back to initial data
                pass
                
        # Fall back to initial sensor data
        return self.entity_description.value_fn(self._initial_sensor_data)


class PeplinkPepVPNProfileBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Connectivity binary sensor for a PepVPN profile."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connection Status"
    _attr_icon = "mdi:vpn"

    def __init__(
        self,
        coordinator,
        profile_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._profile_id = profile_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_pepvpn_profile_{profile_id}_connected"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        peers = self.coordinator.data.get("pepvpn_status", {}).get("peers", [])
        profile_peers = [p for p in peers if p.get("profile_id") == self._profile_id]
        if not profile_peers:
            return None
        return any(p.get("status") == "CONNECTED" for p in profile_peers)


class PeplinkPepVPNPeerBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Connectivity binary sensor for a PepVPN peer."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connection Status"
    _attr_icon = "mdi:vpn"

    def __init__(
        self,
        coordinator,
        peer_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._peer_id = peer_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_pepvpn_peer_{peer_id}_connected"
        self._attr_device_info = device_info

    def _current_peer(self) -> dict | None:
        for peer in self.coordinator.data.get("pepvpn_status", {}).get("peers", []):
            if peer["peer_id"] == self._peer_id:
                return peer
        return None

    @property
    def is_on(self) -> bool | None:
        peer = self._current_peer()
        if peer is None:
            return None
        return peer.get("status") == "CONNECTED"


class WanHealthCheckBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor for WAN logical health check result (PASS/FAIL)."""

    _attr_has_entity_name = True
    _attr_name = "Health Check"
    _attr_icon = "mdi:heart-pulse"

    def __init__(self, coordinator, wan_id: str, wan_name: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._wan_id = wan_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_wan{wan_id}_health_check"
        self._attr_device_info = device_info

    def _hc_data(self) -> dict:
        return self.coordinator.data.get("wan_health_check", {}).get(self._wan_id, {})

    @property
    def is_on(self) -> bool | None:
        data = self._hc_data()
        if not data:
            return None
        return data.get("result") == 1

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._hc_data())


class ApEnabledBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor showing whether the router's built-in AP is enabled."""

    _attr_has_entity_name = True
    _attr_name = "AP Enabled"
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_ap_enabled"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("ap_status", {}).get("enable")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(
            self.coordinator.data.get("ap_status")
        )


class VapActiveBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor showing whether a Wi-Fi SSID (VAP) is actively broadcasting."""

    _attr_has_entity_name = True
    _attr_name = "Active"
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator, vap_id: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._vap_id = vap_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_vap{vap_id}_active"
        self._attr_device_info = device_info

    def _vap_data(self) -> dict:
        return self.coordinator.data.get("vap_summary", {}).get(self._vap_id, {})

    @property
    def is_on(self) -> bool | None:
        data = self._vap_data()
        if not data:
            return None
        return data.get("active", False)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._vap_data())


class ApRadioOnlineBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor showing whether the router's built-in AP radio is online."""

    _attr_has_entity_name = True
    _attr_name = "AP Radio Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:access-point"

    def __init__(self, coordinator, ap_id: str) -> None:
        super().__init__(coordinator)
        self._ap_id = ap_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_ap{ap_id}_online"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        )

    def _ap_data(self) -> dict:
        return self.coordinator.data.get("ap_radio", {}).get(self._ap_id, {})

    @property
    def is_on(self) -> bool | None:
        data = self._ap_data()
        if not data:
            return None
        return data.get("is_online", False)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._ap_data())


class PortLinkBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor showing physical link status of a LAN or WAN port."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:ethernet"

    def __init__(self, coordinator, port_type: str, port_id: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._port_type = port_type
        self._port_id = port_id
        self._attr_device_info = device_info
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_port_{port_type}_{port_id}_link"

    def _port_data(self) -> dict:
        return self.coordinator.data.get(f"port_{self._port_type}", {}).get(self._port_id, {})

    @property
    def name(self) -> str:
        data = self._port_data()
        port_name = data.get("name") or f"{self._port_type.upper()} Port {self._port_id}"
        return f"{port_name} Link"

    @property
    def is_on(self) -> bool | None:
        data = self._port_data()
        if not data:
            return None
        return data.get("linkUp", False)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._port_data())
