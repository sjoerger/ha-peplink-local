"""The Peplink Local integration."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_POLL_FREQUENCY,
    DEFAULT_POLL_FREQUENCY,
    SCAN_INTERVAL,
)
from .peplink_api import PeplinkAPI, PeplinkAuthFailed

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.SELECT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Peplink Local component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Peplink Local from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    
    # Get poll frequency from config, fall back to default if not specified
    poll_frequency = entry.data.get(CONF_POLL_FREQUENCY, DEFAULT_POLL_FREQUENCY)

    session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    api = PeplinkAPI(
        host=host,
        username=username,
        password=password,
        session=session,
        verify_ssl=verify_ssl,
    )

    try:
        if not await api.connect():
            raise ConfigEntryAuthFailed("Failed to connect to Peplink router")

        coordinator = PeplinkDataUpdateCoordinator(
            hass=hass,
            logger=_LOGGER,
            name=f"Peplink {host}",
            update_interval=timedelta(seconds=poll_frequency),
            api=api,
            config_entry=entry,
        )

        await coordinator.async_config_entry_first_refresh()

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "coordinator": coordinator,
            "api": api,
        }

        # Get device name from device info if available, otherwise use a generic name
        device_name = coordinator.device_name or f"Peplink Router ({host})"
        
        # Create model string with available info
        model_string = coordinator.model or "Router"
        if coordinator.product_code and coordinator.hardware_revision:
            model_string = f"{model_string} ({coordinator.product_code} HW {coordinator.hardware_revision})"
        elif coordinator.product_code:
            model_string = f"{model_string} ({coordinator.product_code})"
        elif coordinator.hardware_revision:
            model_string = f"{model_string} (HW {coordinator.hardware_revision})"
        
        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name,
            manufacturer="Peplink",
            model=model_string,
            serial_number=coordinator.serial_number,
            sw_version=coordinator.firmware,
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        return True

    except PeplinkAuthFailed as err:
        raise ConfigEntryAuthFailed from err
    except Exception as err:
        _LOGGER.exception("Error setting up Peplink integration")
        raise ConfigEntryNotReady(f"Failed to connect: {err}") from err


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        api = data["api"]
        await api.close()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


class PeplinkDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta,
        api: PeplinkAPI,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
        )
        self.api = api
        self.config_entry = config_entry
        self.host = config_entry.data[CONF_HOST]
        self.model = "Router"  # Can be updated later if the API provides model info
        self.firmware = "Unknown"  # Can be updated later if the API provides firmware info
        self.device_name = None  # Can be updated later if the API provides device name
        self.serial_number = None  # Can be updated later if the API provides serial number
        self.product_code = None  # Can be updated later if the API provides product code
        self.hardware_revision = None  # Can be updated later if the API provides hardware revision
        self._pepvpn_prev_bytes: dict = {}  # {peer_id: {conn_id: {rx, tx}}}
        self._pepvpn_prev_time: float | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via API."""
        try:
            # Ensure we're connected
            if not await self.api.ensure_connected():
                raise UpdateFailed("Failed to connect to Peplink router")

            # Parallelize API calls using asyncio.gather
            # Use the new combined system info API for device info, thermal sensors, and fan speeds
            results = await asyncio.gather(
                self.api.get_wan_status(),
                self.api.get_clients(),
                self.api.get_system_info(),  # Combined system info call
                self.api.get_traffic_stats(),
                self.api.get_location(),      # Get location data from GPS
                self.api.get_pepvpn_status(), # PepVPN/SpeedFusion VPN status
                self.api.get_wan_health_check(),
                self.api.get_hc_failure_simulation(),
                self.api.get_ap_status(),
                self.api.get_vap_summary(),
                self.api.get_wifi_clients(),
                return_exceptions=True,
            )

            # Check results for exceptions — PepVPN and health check failures are non-fatal
            required_calls = ["WAN status", "client information", "system information", "traffic statistics", "location information"]
            for i, result in enumerate(results[:5]):
                if isinstance(result, Exception):
                    raise UpdateFailed(f"Failed to get {required_calls[i]}: {result}")

            pepvpn_status = results[5]
            if isinstance(pepvpn_status, Exception):
                _LOGGER.debug("PepVPN status unavailable (not supported on this device): %s", pepvpn_status)
                pepvpn_status = {"profiles": [], "peers": [], "tunnels": {}}

            wan_health_check = results[6]
            if isinstance(wan_health_check, Exception):
                _LOGGER.debug("WAN health check status unavailable: %s", wan_health_check)
                wan_health_check = {}

            hc_failure_simulation = results[7]
            if isinstance(hc_failure_simulation, Exception):
                _LOGGER.debug("HC failure simulation status unavailable: %s", hc_failure_simulation)
                hc_failure_simulation = set()

            ap_status = results[8]
            if isinstance(ap_status, Exception):
                _LOGGER.debug("AP status unavailable (not supported on this device): %s", ap_status)
                ap_status = {}

            vap_summary = results[9]
            if isinstance(vap_summary, Exception):
                _LOGGER.debug("VAP summary unavailable (not supported on this device): %s", vap_summary)
                vap_summary = {}

            wifi_clients = results[10]
            if isinstance(wifi_clients, Exception):
                _LOGGER.debug("Wi-Fi client details unavailable: %s", wifi_clients)
                wifi_clients = {}

            # Unpack results
            wan_status, clients, system_info, traffic_stats, location_info = results[:5]
            
            # Extract components from the combined system_info call
            thermal_sensors = system_info.get("thermal_sensors", {"sensors": []})
            fan_speeds = system_info.get("fan_speeds", {"fans": []})
            device_info = {"device_info": system_info.get("device_info", {})} 
            system_time = system_info.get("system_time", {})
            
            # Validate results
            if not wan_status:
                raise UpdateFailed("Failed to get WAN status")
            if not clients:
                raise UpdateFailed("Failed to get client information")
            if not system_info:
                raise UpdateFailed("Failed to get system information")
            if not traffic_stats:
                raise UpdateFailed("Failed to get traffic statistics")
                
            # Update model and firmware information if available
            device_info_data = device_info.get("device_info", {})
            if device_info_data:
                if device_info_data.get("model"):
                    self.model = device_info_data.get("model")
                if device_info_data.get("firmware_version"):
                    self.firmware = device_info_data.get("firmware_version")
                if device_info_data.get("name"):
                    self.device_name = device_info_data.get("name")
                if device_info_data.get("serial_number"):
                    self.serial_number = device_info_data.get("serial_number") 
                if device_info_data.get("product_code"):
                    self.product_code = device_info_data.get("product_code")
                if device_info_data.get("hardware_revision"):
                    self.hardware_revision = device_info_data.get("hardware_revision")
                
            # Compute instantaneous kbps rates for SFC/PepVPN tunnel WAN links
            now = time.monotonic()
            elapsed = (now - self._pepvpn_prev_time) if self._pepvpn_prev_time else None
            new_prev_bytes: dict = {}
            for peer_id, tunnel in pepvpn_status.get("tunnels", {}).items():
                prev_peer = self._pepvpn_prev_bytes.get(peer_id, {})
                new_prev_bytes[peer_id] = {}
                for wan_link in tunnel.get("wan_links", []):
                    conn_id = wan_link["conn_id"]
                    rx = wan_link.get("rx") or 0
                    tx = wan_link.get("tx") or 0
                    prev = prev_peer.get(conn_id, {})
                    if elapsed and elapsed > 0 and prev:
                        rx_delta = max(0, rx - (prev.get("rx") or 0))
                        tx_delta = max(0, tx - (prev.get("tx") or 0))
                        wan_link["rx_rate"] = round(rx_delta * 8 / elapsed / 1000, 1)
                        wan_link["tx_rate"] = round(tx_delta * 8 / elapsed / 1000, 1)
                    else:
                        wan_link["rx_rate"] = None
                        wan_link["tx_rate"] = None
                    new_prev_bytes[peer_id][conn_id] = {"rx": rx, "tx": tx}
            self._pepvpn_prev_time = now
            self._pepvpn_prev_bytes = new_prev_bytes

            # Combine all data into a single data structure
            return {
                "wan_status": wan_status,
                "clients": clients,
                "thermal_sensors": thermal_sensors,
                "fan_speeds": fan_speeds,
                "traffic_stats": traffic_stats,
                "device_info": device_info_data,
                "system_time": system_time,
                "location_info": location_info,
                "pepvpn_status": pepvpn_status,
                "wan_health_check": wan_health_check,
                "hc_failure_simulation": hc_failure_simulation,
                "ap_status": ap_status,
                "vap_summary": vap_summary,
                "wifi_clients": wifi_clients,
            }
                
        except Exception as e:
            _LOGGER.exception("Failed to update data: %s", e)
            raise UpdateFailed(f"Failed to update data: {e}") from e
