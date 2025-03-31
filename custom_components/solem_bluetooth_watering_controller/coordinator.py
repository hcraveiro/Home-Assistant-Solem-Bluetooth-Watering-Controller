"""DataUpdateCoordinator for our integration."""

from datetime import datetime, timedelta
import logging
import asyncio
from asyncio import sleep

from typing import Any
from homeassistant.helpers.storage import Store

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_SENSORS,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import DOMAIN, HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.event import async_call_later

from .util import mac_to_uuid, ensure_datetime
from .models import IrrigationController, IrrigationStation
from .api import SolemAPI, OpenWeatherMapAPI, APIConnectionError
from .const import (
    DEFAULT_SCAN_INTERVAL,
    CONTROLLER_MAC_ADDRESS,
    NUM_STATIONS,
    OPEN_WEATHER_MAP_API_KEY,
    SPRINKLE_WITH_RAIN,
    BLUETOOTH_TIMEOUT,
    BLUETOOTH_MIN_TIMEOUT,
    BLUETOOTH_DEFAULT_TIMEOUT,
    OPEN_WEATHER_MAP_API_CACHE_TIMEOUT,
    OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT,
    SOLEM_API_MOCK
)

_LOGGER = logging.getLogger(__name__)


class SolemCoordinator(DataUpdateCoordinator):
    """Solem coordinator."""

    data: list[dict[str, Any]]

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""

        # Set variables from values entered in config flow setup
        self.controller_mac_address = config_entry.data[CONTROLLER_MAC_ADDRESS].rsplit(' - ', 1)[1]

        _LOGGER.info(f"{self.controller_mac_address} - Starting Coordinator initialization...")
        
        self.sprinkle_with_rain = config_entry.data[SPRINKLE_WITH_RAIN] == "true"
        self.openweathermap_api_key = config_entry.data[OPEN_WEATHER_MAP_API_KEY]
        zone_entity_id = config_entry.data[CONF_SENSORS]
        zone_state = hass.states.get(zone_entity_id)

        if zone_state:
            self.latitude = zone_state.attributes.get("latitude")
            self.longitude = zone_state.attributes.get("longitude")

        # set variables from options.  You need a default here in case options have not been set
        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.bluetooth_timeout = config_entry.options.get(
            BLUETOOTH_TIMEOUT, BLUETOOTH_DEFAULT_TIMEOUT
        )
        self.openweathermap_api_timeout = config_entry.options.get(
            OPEN_WEATHER_MAP_API_CACHE_TIMEOUT, OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT
        )
        self.solem_api_mock = config_entry.options.get(SOLEM_API_MOCK, "false") == "true"

        # Initialise DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            # Method to call on every update interval.
            update_method=self.async_update_data,
            # Polling interval. Will only be polled if you have made your
            # platform entities, CoordinatorEntities.
            # Using config option here but you can just use a fixed value.
            update_interval=timedelta(seconds=self.poll_interval),
        )

        self.num_stations = config_entry.data.get("num_stations", 2)
        self.station_areas = config_entry.data.get("station_areas", [0] * self.num_stations)
        if not isinstance(self.station_areas, list) or len(self.station_areas) != self.num_stations:
            _LOGGER.warning(f"{self.controller_mac_address} - station_areas missing or invalid, setting defaults.")
            self.station_areas = [0] * self.num_stations
            
        self.config_entry = config_entry

        # Create instances of devices
        self.controller = IrrigationController(
            device_id=f"{self.controller_mac_address}_irrigation_controller_status",
            device_name="Controller Status",
            device_uid="",
            software_version="1.0",
            icon = "mdi:state-machine"
        )

        self.stations = [
            IrrigationStation(
                device_id=f"{self.controller_mac_address}_irrigation_station_{station_id}_status",
                device_name=f"Station {station_id} Status",
                device_uid="",
                station_number=station_id,
                software_version="1.0",
                icon = "mdi:state-machine"
            )
            for station_id in range(1, self.num_stations + 1)
        ]
        
        self.api = SolemAPI(mac_address=self.controller_mac_address, bluetooth_timeout=self.bluetooth_timeout)
        self.weather_api = OpenWeatherMapAPI(self.openweathermap_api_key, self.latitude, self.longitude, self.openweathermap_api_timeout)
        self.storage = Store(hass, 1, f"irrigation_{config_entry.unique_id}")
        self.irrigation_stop_event = asyncio.Event()
        
        self.init_task = hass.async_create_task(self.async_init())
    
        _LOGGER.info(f"{self.controller_mac_address} - Coordinator initialization finished!")


    async def update_config(self, new_config: ConfigEntry):
        """Update the coordinator with new configuration."""
        
        _LOGGER.info(f"{self.controller_mac_address} - Updating Coordinator with new config...")
        self.config_entry = new_config  # Atualizar as configurações internas
    
        self.controller_mac_address = self.config_entry.data[CONTROLLER_MAC_ADDRESS].rsplit(' - ', 1)[1]
        self.sprinkle_with_rain = self.config_entry.data.get(SPRINKLE_WITH_RAIN, "False") == "true"
        self.openweathermap_api_key = self.config_entry.data[OPEN_WEATHER_MAP_API_KEY]
        zone_entity_id = self.config_entry.data[CONF_SENSORS]
        zone_state = self.hass.states.get(zone_entity_id)
        
        if zone_state:
            self.latitude = zone_state.attributes.get("latitude")
            self.longitude = zone_state.attributes.get("longitude")

        # set variables from options.  You need a default here in case options have not been set
        self.poll_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.bluetooth_timeout = config_entry.options.get(
            BLUETOOTH_TIMEOUT, BLUETOOTH_DEFAULT_TIMEOUT
        )
        self.openweathermap_api_timeout = config_entry.options.get(
            OPEN_WEATHER_MAP_API_CACHE_TIMEOUT, OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT
        )
        self.solem_api_mock = config_entry.options.get(SOLEM_API_MOCK, "false") == "true"

        self.api = SolemAPI(mac_address=self.controller_mac_address, bluetooth_timeout=self.bluetooth_timeout)
        self.weather_api = OpenWeatherMapAPI(self.openweathermap_api_key, self.latitude, self.longitude, self.openweathermap_api_timeout)

        self.num_stations = config_entry.data.get("num_stations", 2)
        self.station_areas = config_entry.data.get("station_areas", [0] * self.num_stations)
        if not isinstance(self.station_areas, list) or len(self.station_areas) != self.num_stations:
            _LOGGER.warning(f"{self.controller_mac_address} - station_areas missing or invalid on update, setting defaults.")
            self.station_areas = [0] * self.num_stations

        self.stations = [
            IrrigationStation(
                device_id=f"{self.controller_mac_address}_irrigation_station_{station_id}_status",
                device_name=f"Station {station_id} Status",
                device_uid="",
                station_number=station_id,
                software_version="1.0",
                icon = "mdi:state-machine"
            )
            for station_id in range(1, self.num_stations + 1)
        ]
        # Fazer um refresh imediato com os novos dados
        await self.initialize_schedule()
        await self.async_request_refresh()
        _LOGGER.info(f"{self.controller_mac_address} - Updated Coordinator with new config.")

    async def load_persistent_data(self):
        """Load persistent data from storage"""
        storage_data = await self.storage.async_load()

        if storage_data:
            # If data is available fill the variables.
            self.will_it_rain_today = storage_data.get("will_it_rain_today")
            self.will_it_rain_today_forecast = storage_data.get("will_it_rain_today_forecast")
            self.weather_api._cache_forecast = self.will_it_rain_today_forecast
            self.has_rained_today = storage_data.get("has_rained_today")
            self.is_raining_now = storage_data.get("is_raining_now")
            self.is_raining_now_json = storage_data.get("is_raining_now_json")
            self.weather_api._cache_current = self.is_raining_now_json
            self.irrigation_manual_duration = storage_data.get("irrigation_manual_duration")
            self.rain_time_today = storage_data.get("rain_time_today", 0)
            self.rain_total_amount_today = storage_data.get("rain_total_amount_today", 0)
            self.rain_total_amount_forecasted_today = storage_data.get("rain_total_amount_forecasted_today", 0)
            self.total_water_consumption = storage_data.get("total_water_consumption", 0)
            self.schedule = storage_data.get("schedule")
            
            self.water_flow_rate = storage_data.get("water_flow_rate")
            if not isinstance(self.water_flow_rate, list) or len(self.water_flow_rate) != self.num_stations:
                _LOGGER.debug(f"{self.controller_mac_address} - Initializing water_flow_rate with default values.")
                self.water_flow_rate = [12] * self.num_stations  # Inicializa com 20 para cada estação

            
            last_reset = storage_data.get("last_reset")
            if isinstance(last_reset, str):
                try:
                    self.last_reset = datetime.strptime(last_reset, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid date format for last_reset: {last_reset}")
                    self.last_reset = datetime.now()  # Fallback para a data e hora atuais
            else:
                self.last_reset = last_reset or datetime.now()

            last_rain = storage_data.get("last_rain")
            if isinstance(last_rain, str):
                try:
                    self.last_rain = datetime.strptime(last_rain, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid date format for last_rain: {last_rain}")
                    self.last_rain = datetime.now()  # Fallback para a data e hora atuais
            else:
                self.last_rain = last_rain or datetime.now()
            
            last_sprinkle = storage_data.get("last_sprinkle")
            if isinstance(last_sprinkle, str):
                try:
                    self.last_sprinkle = datetime.strptime(last_sprinkle, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid date format for last_sprinkle: {last_sprinkle}")
                    self.last_sprinkle = datetime.now()  # Fallback para a data e hora atuais
            else:
                self.last_sprinkle = last_sprinkle or datetime.now()

        else:
            # If data is not present, initialize with default data
            self.will_it_rain_today = False
            self.will_it_rain_today_forecast = []
            self.has_rained_today = False
            self.is_raining_now = False
            self.is_raining_now_json = []
            self.last_reset = None
            self.last_sprinkle = datetime.now()
            self.last_rain = datetime.now()
            self.rain_time_today = 0
            self.rain_total_amount_today = 0
            self.rain_total_amount_forecasted_today = 0
            self.total_water_consumption = 0
            self.irrigation_manual_duration = 10
            self.water_flow_rate = [12] * self.num_stations
            self.schedule = None

        _LOGGER.info(f"{self.controller_mac_address} - Persistent data loaded.")

    async def save_persistent_data(self):
        """Save persistent data on storage."""
        storage_data = {
            "will_it_rain_today": self.will_it_rain_today,
            "will_it_rain_today_forecast": self.will_it_rain_today_forecast,
            "has_rained_today": self.has_rained_today,
            "is_raining_now": self.is_raining_now,
            "is_raining_now_json": self.is_raining_now_json,
            "last_reset": ensure_datetime(self.last_reset).strftime("%Y-%m-%d %H:%M:%S"),
            "last_sprinkle": (self.last_sprinkle or datetime.min).strftime("%Y-%m-%d %H:%M:%S"),
            "last_rain": (self.last_rain or datetime.min).strftime("%Y-%m-%d %H:%M:%S"),
            "irrigation_manual_duration": self.irrigation_manual_duration,
            "water_flow_rate": self.water_flow_rate,
            "rain_time_today": self.rain_time_today,
            "rain_total_amount_today": self.rain_total_amount_today,
            "rain_total_amount_forecasted_today": self.rain_total_amount_forecasted_today,
            "total_water_consumption": self.total_water_consumption,
            "schedule": self.schedule,
        }

        await self.storage.async_save(storage_data)
        _LOGGER.debug(f"{self.controller_mac_address} - Persistent data saved.")

    async def setup_scheduled_tasks(self):
        """Create scheduled tasks."""
        
        _LOGGER.info(f"{self.controller_mac_address} - Scheduling tasks for midnight...")
        async_track_time_change(
            self.hass,
            lambda *_: self.hass.create_task(self.reset_rain_flag()),
            hour=0, minute=0, second=0
        )
        async_track_time_change(
            self.hass,
            lambda *_: self.hass.create_task(self.check_and_schedule_watering()),
            hour=0, minute=0, second=0
        )
        _LOGGER.info(f"{self.controller_mac_address} - Scheduled tasks.")

    async def async_init(self):
        await self.load_persistent_data()
        
        """Init APIs and schedule tasks."""

        _LOGGER.info(f"{self.controller_mac_address} - Connecting to Solem API...")
        try:
            self.api.mock = self.solem_api_mock
            await self.api.connect()
            _LOGGER.info(f"{self.controller_mac_address} - Connected to Solem API")
        except Exception as ex:
            _LOGGER.warning(f"{self.controller_mac_address} - Failed connecting to Solem device ({self.controller_mac_address})!, ex={ex}")

        await self.initialize_schedule()

        # Executa imediatamente após inicialização
        await self.check_and_schedule_watering()
        await self.setup_scheduled_tasks()
        self.data = await self.async_update_all_sensors()

    async def reset_rain_flag(self, *_):
        """Reset raind indicators."""
        self.has_rained_today = False
        self.will_it_rain_today = False
        self.rain_time_today = 0
        self.rain_total_amount_today = 0
        self.rain_total_amount_forecasted_today = await self.weather_api.get_total_rain_forecast_for_today()
        self.last_reset = datetime.now().isoformat()
        
        _LOGGER.info(f"{self.controller_mac_address} - Resetted rain indicators.")


    async def check_and_schedule_watering(self, *_):
        """Check if there should be watering today and schedule the tasks."""
        _LOGGER.info(f"{self.controller_mac_address} - Checking and scheduling watering times...")
    
        today = datetime.now().date()
        current_month_index = today.month - 1
    
        # Procurar um mês com configuração válida
        for i in range(12):
            month_config = self.schedule[(current_month_index + i) % 12]
            watering_hours = month_config.get("hours", [])
    
            if month_config and watering_hours:  # Apenas meses com horários definidos
                break
        else:
            _LOGGER.info(f"{self.controller_mac_address} - No valid configuration found for any month.")
            return
    
        interval_days = month_config.get("interval_days", 2)
        stations = month_config.get("stations", {})
    
        # Se não for para regar com chuva e choveu ou vai chover, cancelar
        if not self.sprinkle_with_rain and (self.has_rained_today or self.will_it_rain_today or self.is_raining_now):
            _LOGGER.info(f"{self.controller_mac_address} - Canceling watering: rain detected.")
            return
    
        # Se houve rega ou chuva recente, respeitar o intervalo
        if self.last_rain or self.last_sprinkle:
            last_event_date = max(filter(None, [self.last_rain, self.last_sprinkle]))
            days_since_last_event = (today - last_event_date.date()).days
            if days_since_last_event < interval_days:
                _LOGGER.info(f"{self.controller_mac_address} - Last event was {days_since_last_event} days ago. Interval of {interval_days} days not yet passed.")
                return
    
        # Agendar rega para os horários configurados
        for hour in watering_hours:
            if hour:
                try:
                    watering_time = datetime.combine(today, datetime.strptime(hour, "%H:%M:%S").time())
                    delay = (watering_time - datetime.now()).total_seconds()
                    if delay > 0:
                        async_call_later(self.hass, delay, self.run_watering_cycle)
                        _LOGGER.info(f"{self.controller_mac_address} - Watering scheduled for {watering_time}")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid hour format: {hour}")
                    
        _LOGGER.debug(f"{self.controller_mac_address} - Scheduled watering.")


    async def get_next_watering_date(self) -> datetime:
        """
        Get next watering time considering configurations.
        """
        _LOGGER.debug(f"{self.controller_mac_address} - Determining next watering schedule...")
        today = datetime.now().date()
        current_month_index = today.month - 1
    
        # Procurar um mês com configuração e horários definidos
        for i in range(12):
            month_config = self.schedule[(current_month_index + i) % 12]
            watering_hours = month_config.get("hours", [])
    
            if month_config and watering_hours:  # Só considera meses com horários definidos
                break
        else:
            _LOGGER.debug(f"{self.controller_mac_address} - No configuration with valid hours found for any month.")
            return None
    
        interval_days = month_config.get("interval_days", 2)
    
        # Se choveu ou vai chover, adia a rega
        if self.has_rained_today or self.will_it_rain_today or self.is_raining_now:
            _LOGGER.debug(f"{self.controller_mac_address} - No watering today due to rain.")
            next_watering_day = today + timedelta(days=interval_days)
        else:
            next_watering_day = today
    
        # Se já houve chuva ou rega recente, respeita o intervalo
        if self.last_rain or self.last_sprinkle:
            last_event_date = max(filter(None, [self.last_rain, self.last_sprinkle]))
            days_since_last_event = (today - last_event_date.date()).days
            if days_since_last_event < interval_days:
                next_watering_day = last_event_date.date() + timedelta(days=interval_days)
    
        # Garantir que estamos num mês com horários configurados
        while not self.schedule[next_watering_day.month - 1].get("hours", []):
            next_watering_day += timedelta(days=1)
    
        # Determinar a próxima hora válida
        for hour in watering_hours:
            try:
                next_watering_time = datetime.strptime(hour, "%H:%M:%S").time()
                next_watering_datetime = datetime.combine(next_watering_day, next_watering_time)
                if next_watering_datetime > datetime.now():
                    return next_watering_datetime
            except ValueError:
                _LOGGER.error(f"{self.controller_mac_address} - Invalid hour format: {hour}")
    
        _LOGGER.debug(f"{self.controller_mac_address} - Determined next watering schedule.")
    
        # Se não houver horas válidas, evitar erro de índice e retornar None
        if not watering_hours:
            return None
    
        return datetime.combine(next_watering_day + timedelta(days=1), datetime.strptime(watering_hours[0], "%H:%M:%S").time())


    
    async def run_watering_cycle(self, *_):
        """Sprinkle if all conditions are valid."""
        if not self.sprinkle_with_rain and (self.has_rained_today or self.will_it_rain_today or self.is_raining_now):
            _LOGGER.info(f"{self.controller_mac_address} - Canceling watering as the weather forecast changed and it is now supposed to rain.")
            return

        current_month_index = datetime.now().month - 1
        month_config = self.schedule[current_month_index]

        if not month_config:
            _LOGGER.info(f"{self.controller_mac_address} - No configuration active for this month.")
            return

        stations = month_config.get("stations", {})

        for station_key, minutes in stations.items():
            if isinstance(minutes, int) and minutes > 0:
                station_name = station_key.replace("_minutes", "").replace("station_", "")
                await self.start_irrigation(station_name, minutes)

    
    async def async_update_all_sensors(self):
        _LOGGER.debug(f"{self.controller_mac_address} - Updating all sensors...")

        if not hasattr(self, 'rain_time_today') or self.rain_time_today is None:
            self.rain_time_today = 0
        if not hasattr(self, 'rain_total_amount_today') or self.rain_total_amount_today is None:
            self.rain_total_amount_today = 0
        if not hasattr(self, 'rain_total_amount_forecasted_today') or self.rain_total_amount_forecasted_today is None:
            self.rain_total_amount_forecasted_today = 0
        if not hasattr(self, 'last_reset'):
            self.last_reset = None
        
        # Verifies if it's after 00:05:00
        now = datetime.now()
        if now.time() > datetime.strptime("00:05:00", "%H:%M:%S").time():
            # If last_reset was not today
            if self.last_reset is None or datetime.fromisoformat(self.last_reset).date() != now.date():
                await self.reset_rain_flag()
                
        data = []
        
        counter = 1
        water_flow_counter = 701
        stations_counter = 801
        buttons_counter = 901
        
        will_it_rain_result = await self.weather_api.will_it_rain()
        self.will_it_rain_today = will_it_rain_result.get("will_rain", False)
        self.will_it_rain_today_forecast = will_it_rain_result.get("forecast", [])
        is_raining_result = await self.weather_api.is_raining()
        self.is_raining_now = is_raining_result["is_raining"]
        self.is_raining_now_json = is_raining_result["current"]
        if self.is_raining_now:
            self.has_rained_today = True
            self.last_rain = datetime.now()
            self.rain_time_today += self.poll_interval / 60
            self.rain_total_amount_today += await self.calculate_rain_amount()

            if not self.sprinkle_with_rain:
                for station_id in range(1, self.num_stations + 1):
                    if self.stations[station_id - 1].state == "Sprinkling":
                        self.stop_irrigation()
                        break
        
        self.rain_total_amount_forecasted_today = (await self.weather_api.get_total_rain_forecast_for_today()) + self.rain_total_amount_today

        self.next_schedule = await self.get_next_watering_date()

        # Controller
        data.append({
            "device_id": self.controller.device_id,
            "device_type": "STATE_SENSOR",
            "device_name": self.controller.device_name,
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": self.controller.software_version,
            "state": self.controller.state,
            "icon": self.controller.icon,
            "last_reboot": self.controller.last_reboot,
        })
        counter += 1
    
        # Stations
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": self.stations[station_id - 1].device_id,
                "device_type": "STATE_SENSOR",
                "device_name": self.stations[station_id - 1].device_name,
                "device_uid": mac_to_uuid(self.controller_mac_address, stations_counter),
                "software_version": self.stations[station_id - 1].software_version,
                "state": self.stations[station_id - 1].state,
                "icon": self.stations[station_id - 1].icon,
                "last_reboot": self.stations[station_id - 1].last_reboot,
            })
            stations_counter += 1

        # Configurations
        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_manual_duration",
            "device_type": "IRRIGATION_DURATION_NUMBER",
            "device_name": "Irrigation Manual Duration",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "value": self.irrigation_manual_duration,
            "icon": "mdi:clock-time-five-outline",
            "last_reboot": None,
        })
        counter += 1
        
        # Stations
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": f"{self.controller_mac_address}_water_flow_rate_{station_id}",
                "device_type": "WATER_FLOW_NUMBER",
                "device_name": f"Water Flow Rate {station_id}",
                "device_uid": mac_to_uuid(self.controller_mac_address, water_flow_counter),
                "software_version": "1.0",
                "value": self.water_flow_rate[station_id - 1],
                "icon": "mdi:water-pump",
                "last_reboot": None,
            })
            water_flow_counter += 1
        

        # Buttons
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": f"{self.controller_mac_address}_irrigation_manual_start_station_{station_id}",
                "device_type": "SPRINKLE_BUTTON",
                "device_name": f"Sprinkle station {station_id}",
                "device_uid": mac_to_uuid(self.controller_mac_address, buttons_counter),
                "software_version": "1.0",
                "icon": "mdi:sprinkler",
                "last_reboot": None,
            })
            buttons_counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_stop",
            "device_type": "STOP_BUTTON",
            "device_name": f"Stop sprinkle",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "icon": "mdi:water-off",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_controller_on",
            "device_type": "ON_BUTTON",
            "device_name": f"Turn on controller",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "icon": "mdi:power-on",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_controller_off",
            "device_type": "OFF_BUTTON",
            "device_name": f"Turn off controller",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "icon": "mdi:power-off",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_will_rain_today",
            "device_type": "WILL_RAIN_SENSOR",
            "device_name": f"Will it rain today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.will_it_rain_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_has_rained_today",
            "device_type": "HAS_RAINED_SENSOR",
            "device_name": f"Has rained today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.has_rained_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_is_raining_now",
            "device_type": "IS_RAINING_SENSOR",
            "device_name": f"Is it raining now",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.is_raining_now,
            "icon": "mdi:weather-pouring",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_next_schedule",
            "device_type": "NEXT_SCHEDULE_SENSOR",
            "device_name": f"Next schedule",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.next_schedule,
            "icon": "mdi:home-clock",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_last_sprinkle",
            "device_type": "LAST_SPRINKLE_SENSOR",
            "device_name": f"Last sprinkle",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.last_sprinkle,
            "icon": "mdi:sprinkler",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_last_rain",
            "device_type": "LAST_RAIN_SENSOR",
            "device_name": f"Last rain",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.last_rain,
            "icon": "mdi:weather-pouring",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_rain_time_today",
            "device_type": "RAIN_TIME_TODAY_SENSOR",
            "device_name": f"Rain time today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.rain_time_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_total_water_consumption",
            "device_type": "TOTAL_WATER_CONSUMPTION_SENSOR",
            "device_name": f"Total water consumption",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.total_water_consumption,
            "icon": "mdi:water-pump",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_total_amount_rain_today",
            "device_type": "TOTAL_AMOUNT_RAIN_TODAY",
            "device_name": f"Total amount of rain today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.rain_total_amount_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_total_forecasted_rain_today",
            "device_type": "TOTAL_FORECASTED_RAIN_TODAY",
            "device_name": f"Total forecasted rain today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.rain_total_amount_forecasted_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1

        # Save persistent data
        await self.save_persistent_data()
        _LOGGER.debug(f"{self.controller_mac_address} - Updated sensors.")
        return data

    async def async_update_data(self):
        data = []

        try:
            data = await self.async_update_all_sensors()
        except Exception as err:
            # This will show entities as unavailable by raising UpdateFailed exception
            _LOGGER.error(f"{self.controller_mac_address} - Error: {err}", exc_info=True)

        # What is returned here is stored in self.data by the DataUpdateCoordinator
        return data


    async def calculate_rain_amount(self) -> float:
        if "rain" not in self.is_raining_now_json:
            return 0.0  # No rain
    
        rain_data = self.is_raining_now_json["rain"]
        
        for key in rain_data:
            if key.endswith("h") and key[:-1].isdigit():
                hours = int(key[:-1])  # Extract the number of hours
                rain_amount = rain_data[key]  # Amount of water during this period
                minutes = hours * 60  # Convert from hours to minutes
                return (rain_amount / minutes) * (self.poll_interval / 60)  # Returns the amount of water for the polling frequency

        return 0.0  # In case there are no recognizable keys


    async def start_irrigation(self, station: int, minutes: int | None = None):
        duration = int(minutes if minutes is not None else self.irrigation_manual_duration)
        _LOGGER.info(f"{self.controller_mac_address} - Going to start watering on station {station} for {duration} minutes...")
        
        try:
            await self.api.sprinkle_station_x_for_y_minutes(station, duration)
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            return
        
        self.stations[station - 1].state = "Sprinkling"
        data = await self.async_update_all_sensors()
        if data is not None:  # Update only if data is valid
            self.async_set_updated_data(data)
        else:
            _LOGGER.warning(f"{self.controller_mac_address} - async_update_all_sensors() returned None, skipping update.")

        for _ in range(duration * 60):
            # Verify if exit condition is met
            if self.irrigation_stop_event.is_set():
                _LOGGER.info(f"{self.controller_mac_address} - Irrigation cancelation triggered.")
                break
            await sleep(1)  # Validate every second
            self.total_water_consumption += (self.water_flow_rate[station - 1] / 60)

        else:  # Só entra aqui se o loop terminar normalmente (sem interrupção)
            self.stations[station - 1].state = "Stopped"
            _LOGGER.info(f"{self.controller_mac_address} - Finished watering on station {station}.")
        
        now = datetime.now()
        self.last_sprinkle = now
    
        data = await self.async_update_all_sensors()
        if data is not None:  # Update only if data is valid
            self.async_set_updated_data(data)
        else:
            _LOGGER.warning(f"{self.controller_mac_address} - async_update_all_sensors() returned None, skipping update.")

    async def stop_irrigation(self):
        _LOGGER.info(f"{self.controller_mac_address} - Stopping watering...")
        try:
            await self.api.stop_manual_sprinkle()
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            return

        # Trigger event to stop sprinkling task
        self.irrigation_stop_event.set()

        for station_id in range(1, self.num_stations + 1):
            self.stations[station_id - 1].state = "Stopped"

        _LOGGER.info(f"{self.controller_mac_address} - Stopped watering.")
        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)
    
    async def turn_controller_on(self):
        _LOGGER.info(f"{self.controller_mac_address} - Turning irrigation controller on...")

        try:
            await self.api.turn_on()
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            return
        
        self.controller.state = "On"
        
        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)
        _LOGGER.info(f"{self.controller_mac_address} - Irrigation controller turned on.")
    
    async def turn_controller_off(self):
        _LOGGER.info(f"{self.controller_mac_address} - Turning irrigation controller off..")
        try:
            await self.api.turn_off_permanent()
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            return

        self.controller.state = "Off"

        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)
        _LOGGER.info(f"{self.controller_mac_address} - Irrigation controller turned off.")


    async def async_set_schedule(self, new_schedule):
        """Replaces irrigation schedule from frontend card"""
        
        # Atualiza a variável interna para refletir a nova configuração
        self.schedule = new_schedule
        
        await self.save_persistent_data()

        # Atualiza os sensores
        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)

        _LOGGER.info(f"{self.controller_mac_address} - Updated schedule.")

    
    async def initialize_schedule(self):
        """Initialize the schedule if not already set"""
        _LOGGER.info(f"{self.controller_mac_address} - Initializing schedule...")
    
        # Is there a storaged schedule
        if not self.schedule:
            _LOGGER.debug(f"{self.controller_mac_address} - No schedule found, creating a new one...")
    
            # Creates new schedule based on the number of stations
            new_schedule = [
                {
                    "interval_days": 0,
                    "stations": {
                        f"station_{i+1}_minutes": 0
                        for i in range(self.num_stations)
                    },
                    "hours": []
                }
                for _ in range(12)  # Lista com 12 meses
            ]
    
            self.schedule = new_schedule
    
            # Saves new schedule on storage
            await self.save_persistent_data()

            return
    
        # Verifies if the number of stations have changed and adapts schedule accordingly
        current_num_stations = len(next(iter(self.schedule))["stations"])

        if current_num_stations != self.num_stations:
            _LOGGER.debug(f"{self.controller_mac_address} - Updating schedule due to station count change.")
            for month_config in self.schedule:
                current_stations = month_config.get("stations", {})
                new_station_keys = {f"station_{i+1}_minutes" for i in range(self.num_stations)}
    
                # Adds new stations
                for new_station in new_station_keys - set(current_stations.keys()):
                    month_config["stations"][new_station] = 0
    
                # Remove obsolet stations
                for old_station in set(current_stations.keys()) - new_station_keys:
                    del month_config["stations"][old_station]
    
            # Saves the schedule on storage
            await self.save_persistent_data()

        _LOGGER.info(f"{self.controller_mac_address} - Schedule initialized.")

    # ----------------------------------------------------------------------------
    # Here we add some custom functions on our data coordinator to be called
    # from entity platforms to get access to the specific data they want.
    #
    # These will be specific to your api or yo may not need them at all
    # ----------------------------------------------------------------------------
    def get_device(self, device_id: int) -> dict[str, Any]:
        """Get a device entity from our api data."""
        try:
            return [
                devices for devices in self.data if devices["device_id"] == device_id
            ][0]
        except (TypeError, IndexError):
            # In this case if the device id does not exist you will get an IndexError.
            # If api did not return any data, you will get TypeError.
            return None

    def get_device_parameter(self, device_id: int, parameter: str) -> Any:
        """Get the parameter value of one of our devices from our api data."""
        if device := self.get_device(device_id):
            return device.get(parameter)
