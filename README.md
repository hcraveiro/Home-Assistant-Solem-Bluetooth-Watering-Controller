# Home Assistant Solem Bluetooth Watering Controller Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/hcraveiro/Home-Assistant-Solem-Bluetooth-Watering-Controller.svg)](https://github.com/hcraveiro/Home-Assistant-Solem-Bluetooth-Watering-Controller/releases/)

Integrate Solem Watering Bluetooth Controllers (only tested in BL-IP) into your Home Assistant. This Integration allows you to manually control the irrigation or to createa a schedule.

- [Home Assistant Solem Bluetooth Watering Controller Integration](#home-assistant-solem-bluetooth-watering-controller-integration)
    - [Installation](#installation)
    - [Configuration](#configuration)
    - [Sensors](#sensors)
    - [FAQ](#faq)

## Installation

This integration can be added as a custom repository in HACS and from there you can install it. It has a dependency on [Solem Toolkit](https://github.com/hcraveiro/Home-Assistant-Solem-Toolkit) for executing Solem operations, namely sprinkling X minutes in a specific station, stopping sprinkly, etc.

When the integration is installed in HACS, you need to add it in Home Assistant: Settings → Devices & Services → Add Integration → Search for Solem Bluetooth Watering Controller.

The configuration happens in the configuration flow when you add the integration.
If you want to configure the schedule you should install the card [Solem Schedule Card](https://github.com/hcraveiro/solem-schedule-card).

## Configuration

For each controller that you want to use, you should add a config entry. You will have a config flow where it is asked:
* which is the bluetooth device
* the number of stations your controller have
* the controller location (it loads the zones you have in HA)
* the OpenWeatherMap API key (optional - you will need to create an API key. first you need to create an [account](https://home.openweathermap.org/users/sign_up))
* sprinkle even when raining (a true/false dropdown - true if you still want to sprinke even if it's raining, false otherwise)

Afterwards an empty irrigation schedule is created. If you want to control it you will need the [Solem Schedule Card](https://github.com/hcraveiro/solem-schedule-card) installed. Previously I had it on the config flow but it is so not user friendly that I decided that a card would be better.

## Sensors

There is a number of sensors that are mande available for each controller/config entry:
* Controller status - on or off and also has an attribute that stores the schedule in json
* Station(n) status - stopped or sprinkling
* Has rained today - true if it has, false otherwise
* Is it raining now - true if it is raining, false otherwise
* Will it rain today - true if it will rain from this moment, false otherwise
* Last rain - datetime of last time it rained
* Last sprinkle - last time there was a sprinkle either manual or scheduled
* Next schedule - next time that it is scheduled to sprinkle
* Rain time today - amount of minutes that rained today
* Total amount of rain today - amount of mm of rain until now
* Total forecasted rain today - amount of mm of forecasted rain, taking into account what already rained and what will rain from now 
* Water flow rate (n) - water flow rate for station n (Liter/minute)
* Total water consumption - total water consumption for the whole system, taking into account how much time it sprinkles and the water flow rate
* Irrigation manual duration - number of minutes for sprinkle (manual)
* Sprinkle station (n) - trigger sprinkling on station n
* Stop sprinkle - stop any ongoing sprinkling
* Turn on controller - turn on controller
* Turn off controller - turn off controller

## FAQ

### Can I configure other controller models?

Not, yet, I haven't reverse engineered yet other controllers other than BLIP.I'm planning to do it on BLNR soon, though.
