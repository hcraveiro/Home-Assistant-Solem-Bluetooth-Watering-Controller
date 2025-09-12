import logging
import sys
import struct
import asyncio
from typing import Any
from datetime import datetime, timedelta, timezone

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakOutOfConnectionSlotsError
from bleak.exc import BleakDBusError

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util.dt import as_local
from homeassistant.util import dt as dt_util
from homeassistant.components.bluetooth import async_ble_device_from_address

from .const import OPEN_WEATHER_MAP_FORECAST_URL, OPEN_WEATHER_MAP_CURRENT_URL

_LOGGER = logging.getLogger(__name__)


class SolemAPI:
    """Class for Solem API."""

    def __init__(self, hass: HomeAssistant, mac_address: str, bluetooth_timeout: int) -> None:
        """Initialise."""
        self.hass = hass
        self.mac_address = mac_address
        self.characteristic_uuid: str | None = None
        self._write_response_required: bool = False  # write com resposta?
        self.bluetooth_timeout = bluetooth_timeout
        self.mock = False
        self._conn_lock = asyncio.Lock()  # serialize BLE connections

    async def scan_bluetooth(self):
        devices = await BleakScanner.discover()
        return devices

    async def _resolve_ble_device(self):
        """Resolve the best BLE device via HA (proxies/adapter) or fallback to direct scan."""
        # Preferir o resolver do HA (usa proxies automaticamente)
        ble_device = async_ble_device_from_address(self.hass, self.mac_address, connectable=True)
        if ble_device:
            # Alguns ambientes devolvem details como string → converter para dict
            try:
                details = getattr(ble_device, "details", None)
                if isinstance(details, str):
                    ble_device = BLEDevice(
                        ble_device.address,
                        ble_device.name,
                        {"path": details},
                        getattr(ble_device, "rssi", 0),
                    )
            except Exception:
                # Se algo correr mal, seguimos com o objeto original
                pass
            return ble_device

        # Fallback: scan direto no adaptador local
        ble_device = await BleakScanner.find_device_by_address(self.mac_address, timeout=self.bluetooth_timeout)
        if ble_device:
            details = getattr(ble_device, "details", None)
            if isinstance(details, str):
                ble_device = BLEDevice(
                    ble_device.address,
                    ble_device.name,
                    {"path": details},
                    getattr(ble_device, "rssi", 0),
                )
            return ble_device

        _LOGGER.debug("Device not found! Failed connecting!")
        raise APIConnectionError("Device not found! Failed connecting!")

    async def _connect_client(self) -> BleakClient:
        """Establish a robust connection using bleak-retry-connector, with a lock."""
        async with self._conn_lock:
            ble_device = await self._resolve_ble_device()
            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    name=f"Solem - {self.mac_address}",
                    timeout=self.bluetooth_timeout,
                    max_attempts=3,
                )
                return client
            except BleakOutOfConnectionSlotsError as exc:
                raise APIConnectionError(
                    "Bluetooth adapter/proxy out of connection slots or device busy/unreachable"
                ) from exc
            except AttributeError as exc:
                _LOGGER.debug("establish_connection AttributeError: %r (ble_device=%r)", exc, ble_device, exc_info=True)
                raise APIConnectionError(f"BLE internal attribute error during connection: {exc}") from exc
            except Exception as exc:
                _LOGGER.debug("establish_connection Exception: %s: %r", type(exc).__name__, exc, exc_info=True)
                raise APIConnectionError(f"Unexpected BLE error: {exc}") from exc

    async def connect(self) -> str:
        """Verify if it's possible to connect to the bluetooth device and cache the write characteristic UUID."""
        try:
            return await self.connect_with_retries()
        except Exception as ex:
            _LOGGER.info(f"Timeout connecting to device after retries!, ex:{ex}")
            raise APIConnectionError("Timeout connecting to device after retries!")

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def connect_with_retries(self) -> str:
        """Estabelece ligação e deteta a characteristic de escrita (com retries)."""
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return ""

        client = await self._connect_client()
        try:
            if not client.is_connected:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")

            _LOGGER.debug("Connected: True")

            # Alguns backends precisam de um pequeno delay antes de enumerar serviços
            await asyncio.sleep(0.3)

            # Tenta get_services(); se falhar, tenta o atributo 'services'
            try:
                services = await client.get_services()
            except Exception as e:
                _LOGGER.debug(
                    "get_services() falhou (%s: %r), fallback para client.services",
                    type(e).__name__, e, exc_info=True
                )
                services = getattr(client, "services", None)

            if not services:
                raise APIConnectionError("Could not enumerate GATT services")

            # Recolher *todas* as candidates com write/write-without-response
            candidates: list[tuple[Any, set[str]]] = []
            for service in services:
                chars = getattr(service, "characteristics", []) or []
                for char in chars:
                    uuid = getattr(char, "uuid", "<no-uuid>")
                    props_raw = getattr(char, "properties", []) or []
                    try:
                        props = {(p.lower() if isinstance(p, str) else str(p).lower()) for p in props_raw}
                    except Exception:
                        props = {str(p).lower() for p in props_raw}

                    _LOGGER.debug("Candidate char %s props=%s", uuid, sorted(list(props)))

                    if "write" in props or "write-without-response" in props:
                        candidates.append((char, props))

            if not candidates:
                raise APIConnectionError("No writable characteristic found on device!")

            # Preferir write-without-response; fallback para write normal
            chosen, chosen_props = None, set()
            for char, props in candidates:
                if "write-without-response" in props:
                    chosen, chosen_props = char, props
                    break
            if chosen is None:
                chosen, chosen_props = candidates[0]

            self.characteristic_uuid = getattr(chosen, "uuid", None)
            if not self.characteristic_uuid:
                raise APIConnectionError("Writable characteristic has no UUID")

            # Se tiver 'write' e não tiver apenas 'write-without-response', pedimos response
            self._write_response_required = (
                "write" in chosen_props and "write-without-response" not in chosen_props
            )

            _LOGGER.debug(
                "Selected write characteristic: %s (props=%s, response_required=%s)",
                self.characteristic_uuid, sorted(list(chosen_props)), self._write_response_required
            )
            return ""
        except NameError as e:
            _LOGGER.error("NameError durante a descoberta GATT: %r", e, exc_info=True)
            raise
        except Exception as e:
            _LOGGER.debug("Erro em connect_with_retries: %s: %r", type(e).__name__, e, exc_info=True)
            raise
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _write_with_auth_retry(self, client: BleakClient, payload: bytes) -> None:
        """Escreve na characteristic; se BlueZ exigir autorização, tenta emparelhar e re-tenta uma vez."""
        try:
            await client.write_gatt_char(self.characteristic_uuid, payload, response=self._write_response_required)
        except BleakDBusError as e:
            msg = f"{e}"
            if "NotAuthorized" in msg or "org.bluez.Error.NotAuthorized" in msg:
                _LOGGER.warning("Write devolveu NotAuthorized → a tentar emparelhar e repetir...")
                try:
                    if hasattr(client, "pair"):
                        await client.pair()
                        await asyncio.sleep(0.5)
                    await client.write_gatt_char(self.characteristic_uuid, payload, response=self._write_response_required)
                    _LOGGER.debug("Write após pairing OK")
                    return
                except Exception as e2:
                    _LOGGER.error("Falha no re-write após pairing: %r", e2, exc_info=True)
                    raise
            raise

    async def sprinkle_station_x_for_y_minutes(self, station: int, minutes: int):
        """Sprinkle a specific station for a specified number of minutes """
        try:
            await self.sprinkle_station_x_for_y_minutes_with_retry(station, minutes)
        except Exception as ex:
            _LOGGER.debug(f"Error connecting to Solem device after retries!, ex: {ex}", exc_info=True)
            raise APIConnectionError("Error connecting to Solem device after retries!")

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def sprinkle_station_x_for_y_minutes_with_retry(self, station: int, minutes: int):
        """Function with retries"""
        if self.mock:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug(f"writing command: Sprinkle station {station} for {minutes} minutes")

                command = struct.pack(">HBBBH", 0x3105, 0x12, station & 0xFF, 0x00, (minutes * 60) & 0xFFFF)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("Committing")
                commit_command = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit_command)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to API")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def stop_manual_sprinkle(self):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug("writing command: Stop manual sprinkle")
                command = struct.pack(">HBBBH", 0x3105, 0x15, 0x00, 0xFF, 0x0000)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("committing")
                commit = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def list_characteristics(self):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug("Listing services")
                services = await client.get_services()
                for service in services:
                    _LOGGER.info(f"Service: {service.uuid}")
                    for char in service.characteristics:
                        _LOGGER.info(f"  Characteristic: {char.uuid} props={getattr(char, 'properties', [])}")
                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def turn_off_permanent(self):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug("writing command: Turn off permanent")
                command = struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, 0x00, 0x0000)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("committing")
                commit = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def turn_off_x_days(self, days: int):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug("writing command: Turn off for X days")
                command = struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, days & 0xFF, 0x0000)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("committing")
                commit = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def turn_on(self):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug("writing command: Turn on")
                command = struct.pack(">HBBBH", 0x3105, 0xA0, 0x00, 0x01, 0x0000)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("committing")
                commit = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug(f"writing command: Sprinkle all stations for {minutes} minutes")
                command = struct.pack(">HBBBH", 0x3105, 0x11, 0x00, 0x00, (minutes * 60) & 0xFFFF)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("committing")
                commit = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def run_program_x(self, program: int):
        if self.mock is True:
            _LOGGER.debug("Mock=True, Returning from function...")
            return

        if self.characteristic_uuid is None:
            await self.connect()

        client = await self._connect_client()
        try:
            if client.is_connected:
                _LOGGER.debug("Connected: True")
                _LOGGER.debug(f"writing command: Run program {program}")
                command = struct.pack(">HBBBH", 0x3105, 0x14, 0x00, program & 0xFF, 0x0000)
                await self._write_with_auth_retry(client, command)

                _LOGGER.debug("committing")
                commit = struct.pack(">BB", 0x3B, 0x00)
                await self._write_with_auth_retry(client, commit)

                _LOGGER.debug("Success")
            else:
                _LOGGER.debug("Failed connecting!")
                raise APIConnectionError("Timeout connecting to api")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


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
