"""Solem API wrapper that delegates device actions to the Solem Toolkit integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.dt import as_local
from homeassistant.util import dt as dt_util

from .const import OPEN_WEATHER_MAP_CURRENT_URL, OPEN_WEATHER_MAP_FORECAST_URL

_LOGGER = logging.getLogger(__name__)


class SolemAPI:
    """Adapter that forwards Solem device commands to the `solem_toolkit` integration services."""

    _TOOLKIT_DOMAIN = "solem_toolkit"

    def __init__(self, hass: HomeAssistant, mac_address: str | None, bluetooth_timeout: int) -> None:
        """Initialize the adapter.

        Args:
            hass: Home Assistant instance.
            mac_address: Device MAC address.
            bluetooth_timeout: Timeout used by the underlying toolkit services.
        """
        self.hass = hass
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout
        self.mock = False

    async def _async_call_toolkit_service(self, service: str, data: dict[str, Any] | None = None) -> None:
        """Call a Solem Toolkit service and translate errors to APIConnectionError."""
        if self.mock:
            _LOGGER.debug("Mock=True, skipping toolkit service call: %s", service)
            return

        if not self.mac_address:
            raise APIConnectionError("Device MAC address is not set")

        if data is None:
            data = {}

        # Solem Toolkit supports an optional bluetooth_timeout field on services.
        payload = {"device_mac": self.mac_address, "bluetooth_timeout": self.bluetooth_timeout, **data}

        try:
            await self.hass.services.async_call(
                self._TOOLKIT_DOMAIN,
                service,
                payload,
                blocking=True,
            )
        except HomeAssistantError as exc:
            raise APIConnectionError(str(exc)) from exc
        except Exception as exc:
            # This also catches ServiceNotFound and other runtime errors.
            raise APIConnectionError(f"Error calling Solem Toolkit service '{service}': {exc}") from exc

    async def scan_bluetooth(self):
        """Scan for BLE devices.

        Used by the config flow.

        Discovery is implemented by `solem_toolkit` so it can be reused by other
        integrations. This method is kept for backward compatibility and simply
        delegates to the toolkit helper.
        """
        try:
            from custom_components.solem_toolkit.bluetooth import async_scan_devices

            return await async_scan_devices(self.hass, timeout=self.bluetooth_timeout)
        except Exception as exc:
            raise APIConnectionError(f"Bluetooth scan failed: {exc}") from exc

    async def connect(self) -> str:
        """Validate that the device is reachable.

        The Solem Toolkit does not expose a dedicated 'connect' action. We call the read-only
        `list_characteristics` service to validate connectivity without changing device state.

        Note: Solem Toolkit logs characteristics to HA logs.
        """
        await self._async_call_toolkit_service("list_characteristics")
        return ""

    async def sprinkle_station_x_for_y_minutes(self, station: int, minutes: int) -> None:
        """Sprinkle a specific station for a specified number of minutes."""
        await self._async_call_toolkit_service(
            "sprinkle_station_x_for_y_minutes",
            {"station": int(station), "minutes": int(minutes)},
        )

    async def stop_manual_sprinkle(self) -> None:
        """Stop a running manual sprinkle."""
        await self._async_call_toolkit_service("stop_manual_sprinkle")

    async def list_characteristics(self) -> None:
        """List GATT characteristics (logs are written by Solem Toolkit)."""
        await self._async_call_toolkit_service("list_characteristics")

    async def turn_off_permanent(self) -> None:
        """Turn off the sprinkler permanently."""
        await self._async_call_toolkit_service("turn_off_permanent")

    async def turn_off_x_days(self, days: int) -> None:
        """Turn off the sprinkler for a number of days."""
        await self._async_call_toolkit_service("turn_off_x_days", {"days": int(days)})

    async def turn_on(self) -> None:
        """Turn on the sprinkler."""
        await self._async_call_toolkit_service("turn_on")

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int) -> None:
        """Sprinkle all stations for a specified number of minutes."""
        await self._async_call_toolkit_service(
            "sprinkle_all_stations_for_y_minutes",
            {"minutes": int(minutes)},
        )

    async def run_program_x(self, program: int) -> None:
        """Run a stored program."""
        await self._async_call_toolkit_service("run_program_x", {"program": int(program)})

class OpenWeatherMapAPI:
    """Class for OpenWeatherMap API."""

    def __init__(self, api_key: str, latitude: str, longitude: str, timeout: int) -> None:
        """Initialise."""
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.timeout = timeout
        self._cache_forecast = None
        self._cache_current = None
        self._last_forecast_fetch_time = None
        self.last_forecast_date = datetime.now().date()
        self._last_current_fetch_time = None

    async def get_current_weather(self) -> Any:
        now = dt_util.now()  # timezone-aware now

        if self._cache_current and self._last_current_fetch_time and now - self._last_current_fetch_time < timedelta(minutes=self.timeout):
            _LOGGER.debug("Returning cached data.")
            return self._cache_current

        weather_url = f"{OPEN_WEATHER_MAP_CURRENT_URL}appid={self.api_key}&lat={self.latitude}&lon={self.longitude}"
        _LOGGER.debug("Getting current weather at : %s", weather_url)

        async with aiohttp.ClientSession() as session:
            async with session.get(weather_url) as response:
                try:
                    data = await response.json()
                    _LOGGER.debug("Current Weather Data: %s", data)

                    if "dt" in data:
                        utc_dt = datetime.fromtimestamp(data["dt"], tz=timezone.utc)
                        local_dt = as_local(utc_dt)
                        data["dt_txt"] = local_dt.strftime('%Y-%m-%d %H:%M:%S')

                        _LOGGER.debug(
                            f"UTC time from API: {utc_dt.strftime('%Y-%m-%d %H:%M:%S')}, "
                            f"Local time after as_local: {local_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                        )

                    self._cache_current = data
                    self._last_current_fetch_time = now
                except Exception:
                    _LOGGER.error("Error processing Current Weather data: JSON format invalid!")
                    raise APIConnectionError("Error processing Current Weather data: JSON format invalid!")

        return self._cache_current

    async def is_raining(self) -> dict:
        current_weather = await self.get_current_weather()
        return {
            "is_raining": "rain" in current_weather,
            "current": current_weather
        }

    async def get_forecast(self) -> list:
        """Obtains and preserves data from 00h till 00h of the next day."""
        now = datetime.now()

        # If data is recent returns what is on the cache
        if self._cache_forecast and self._last_forecast_fetch_time and now - self._last_forecast_fetch_time < timedelta(minutes=self.timeout):
            _LOGGER.debug("Returning cached data.")
            return self._cache_forecast

        temp_cache = self._cache_forecast.copy() if self._cache_forecast else []

        # If it is a new day, resets and preserves the block from 0h to 3h obtained yesterday
        if self.last_forecast_date != now.date():
            _LOGGER.debug("Day changed, will get 00h forecast to new day...")
            last_00_03_forecast = None

            if self._cache_forecast:
                for forecast in self._cache_forecast:
                    forecast_time_str = forecast.get("dt_txt")
                    if not forecast_time_str:
                        continue
                    forecast_dt = datetime.strptime(forecast_time_str, "%Y-%m-%d %H:%M:%S")
                    if forecast_dt.hour == 0:
                        _LOGGER.debug(f"Found 00h block: {forecast_time_str}")
                        last_00_03_forecast = forecast
                        break

            self._cache_forecast = []
            self.last_forecast_date = now.date()

            if last_00_03_forecast:
                self._cache_forecast.append(last_00_03_forecast)
                _LOGGER.debug(f"Inserting 00h block in new cache: {last_00_03_forecast}")

        current_hour = now.hour
        forecast_hours = [h for h in range(0, 21, 3) if h >= current_hour]
        forecast_hours.append(0)
        items = len(forecast_hours)

        weather_url = f"{OPEN_WEATHER_MAP_FORECAST_URL}&appid={self.api_key}&lat={self.latitude}&lon={self.longitude}&cnt={items}"
        _LOGGER.debug("Getting forecast at: %s", weather_url)

        async with aiohttp.ClientSession() as session:
            async with session.get(weather_url) as response:
                try:
                    data = await response.json()
                    _LOGGER.debug("Forecast Weather Data: %s", data)

                    for item in data["list"]:
                        # Keep dt_txt as-is (already local time per API)
                        forecast_time_str = item["dt_txt"]

                        _LOGGER.debug(
                            f"Forecast timestamp from API (dt_txt): {forecast_time_str}"
                        )

                        existing_index = next(
                            (index for index, forecast in enumerate(self._cache_forecast)
                             if forecast["dt_txt"] == forecast_time_str),
                            None
                        )

                        if existing_index is not None:
                            _LOGGER.debug(f"Replacing block for {forecast_time_str}")
                            self._cache_forecast[existing_index] = item
                        else:
                            _LOGGER.debug(f"Appending item {forecast_time_str} to _cache_forecast")
                            self._cache_forecast.append(item)

                    self._last_forecast_fetch_time = now

                except Exception:
                    _LOGGER.error("Error processing Forecast Weather data: JSON format invalid!", exc_info=True)

                    if not self._cache_forecast:
                        self._cache_forecast = temp_cache

                    raise APIConnectionError("Error processing Forecast Weather data: JSON format invalid!")

        _LOGGER.debug(f"self._cache_forecast={self._cache_forecast}")
        return self._cache_forecast

    async def will_it_rain(self) -> dict:
        """Verifies if it will rain for the rest of the day."""
        forecast = await self.get_forecast()

        now = dt_util.now()  # Local time with tz
        today_str = now.strftime("%Y-%m-%d")
        current_hour = now.hour

        block_hours = [h for h in range(0, 21, 3)]
        current_block = max([h for h in block_hours if h <= current_hour])

        relevant_forecasts = []
        for item in forecast:
            forecast_time_str = item["dt_txt"]  # already local time
            forecast_date, forecast_hour_minute = forecast_time_str.split(" ")
            forecast_hour, _, _ = forecast_hour_minute.split(":")
            forecast_hour = int(forecast_hour)

            if forecast_date == today_str and forecast_hour >= current_block:
                relevant_forecasts.append(item)

        will_rain = any(item.get("pop", 0) > 0.50 for item in relevant_forecasts)

        return {
            "will_rain": will_rain,
            "forecast": forecast
        }

    async def get_total_rain_forecast_for_today(self) -> float:
        """Calculates total amount of rain predicted (mm) for the rest of the day."""
        will_it_rain_result = await self.will_it_rain()
        forecasts = will_it_rain_result.get("forecast", [])

        now = dt_util.now()
        current_time = now.hour * 60 + now.minute
        today_str = now.strftime("%Y-%m-%d")
        total_rain_mm = 0.0

        for item in forecasts:
            forecast_time_str = item["dt_txt"]  # already local
            forecast_date, forecast_hour_minute = forecast_time_str.split(" ")
            forecast_hour, _, _ = forecast_hour_minute.split(":")
            forecast_hour = int(forecast_hour)

            rain_data = item.get("rain", {})
            rain_mm = rain_data.get("3h", 0.0)

            # Only consider today
            if forecast_date != today_str:
                continue

            forecast_start_minute = forecast_hour * 60
            forecast_end_minute = forecast_start_minute + 180

            # Skip past blocks
            if forecast_end_minute <= current_time:
                continue

            # If inside current block, prorate remaining time
            if forecast_start_minute <= current_time < forecast_end_minute:
                remaining_minutes = forecast_end_minute - current_time
                rain_mm = (remaining_minutes / 180) * rain_mm

            total_rain_mm += rain_mm

        return total_rain_mm


class APIConnectionError(Exception):
    """Exception class for connection error."""
