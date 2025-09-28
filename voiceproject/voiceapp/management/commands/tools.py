# voiceapp/management/commands/tools.py
import logging
import aiohttp

async def get_weather(city: str) -> str:
    # Fix: Remove extra spaces in URL
    city = city.replace(' ', '+')
    url = f"https://wttr.in/{city}?format=3"  # Removed extra spaces after domain
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    weather = (await response.text()).strip()
                    logging.info(f"Weather for {city}: {weather}")
                    return weather
                else:
                    logging.error(f"Weather request failed for {city}: HTTP {response.status}")
                    return f"Could not retrieve weather for {city}."
    except Exception as e:
        logging.error(f"Weather request failed for {city}: {e}")
        return f"Could not retrieve weather for {city}."


# Async wrapper as requested
async def get_current_weather(city: str) -> str:
    return await get_weather(city)