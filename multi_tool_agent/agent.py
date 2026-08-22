# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from google.adk.agents import Agent

def _get_city_data(city: str) -> dict:
    """Helper function to fetch city coordinates and timezone using Open-Meteo."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=en&format=json"
    try:
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())
            if data.get("results"):
                return data["results"][0]
    except Exception:
        pass
    return None

def get_weather(city: str) -> dict:
    """Retrieves the current weather report for a specified city.

    Args:
        city (str): The name of the city for which to retrieve the weather report.

    Returns:
        dict: status and result or error msg.
    """
    city_data = _get_city_data(city)
    if not city_data:
        return {
            "status": "error",
            "error_message": f"Weather information for '{city}' is not available (city not found).",
        }
    
    lat = city_data["latitude"]
    lon = city_data["longitude"]
    name = city_data["name"]

    weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    try:
        with urllib.request.urlopen(weather_url) as response:
            weather_data = json.loads(response.read().decode())
            current = weather_data.get("current_weather", {})
            temp = current.get("temperature")
            if temp is not None:
                return {
                    "status": "success",
                    "report": f"The weather in {name} is currently {temp} degrees Celsius.",
                }
            else:
                return {
                    "status": "error",
                    "error_message": f"Weather data for '{name}' could not be parsed.",
                }
    except Exception as e:
        return {
            "status": "error",
            "error_message": f"Failed to retrieve weather for '{city}': {str(e)}",
        }


def get_current_time(city: str) -> dict:
    """Returns the current time in a specified city.

    Args:
        city (str): The name of the city for which to retrieve the current time.

    Returns:
        dict: status and result or error msg.
    """
    city_data = _get_city_data(city)
    if not city_data or "timezone" not in city_data:
        return {
            "status": "error",
            "error_message": f"Sorry, I don't have timezone information for '{city}'.",
        }
    
    tz_identifier = city_data["timezone"]
    name = city_data["name"]

    try:
        tz = ZoneInfo(tz_identifier)
        now = datetime.datetime.now(tz)
        report = (
            f'The current time in {name} is {now.strftime("%Y-%m-%d %H:%M:%S %Z%z")}'
        )
        return {"status": "success", "report": report}
    except Exception as e:
        return {
            "status": "error",
            "error_message": f"Sorry, could not process timezone information for '{city}': {str(e)}",
        }


root_agent = Agent(
    name="weather_time_agent",
    model="gemini-3.7-pro",
    description=(
        "Agent to answer questions about the time and weather in a city."
    ),
    instruction=(
        "You are a helpful agent who can answer user questions about the time and weather in a city."
    ),
    tools=[get_weather, get_current_time],
)