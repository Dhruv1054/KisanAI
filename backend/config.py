"""
config.py — centralised settings for KisanAI
All env vars and constants live here. Import from this module everywhere.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# WhatsApp
WHATSAPP_TOKEN: str = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID: str = os.getenv("WHATSAPP_PHONE_ID", "")
WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "kisanai_verify_2024")

# OpenRouter
OPENROUTER_KEY: str = os.getenv("OPENROUTER_KEY", "")
OPENROUTER_URL: str = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL: str = "qwen/qwen-2.5-72b-instruct"

# External APIs
DATA_GOV_IN_KEY: str = os.getenv("DATA_GOV_IN_KEY", "")
MANDI_API_URL: str = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
WEATHER_API_URL: str = "https://api.open-meteo.com/v1/forecast"
GEOCODE_API_URL: str = "https://nominatim.openstreetmap.org/search"

# Database
import pathlib
BASE_DIR = pathlib.Path(__file__).parent
DB_PATH: str = str(BASE_DIR / "kisanai.db")

# Defaults (Delhi fallback coords)
DEFAULT_LAT: float = 28.6139
DEFAULT_LON: float = 77.2090

# Geocode fallback threshold (degrees ~5km)
GEOCODE_FALLBACK_THRESHOLD: float = 0.05

# Supported languages
LANGUAGES: dict = {
    "1": "hi", "2": "en", "3": "pa",
    "hindi": "hi", "english": "en", "punjabi": "pa",
}
LANG_NAMES: dict = {"hi": "Hindi", "en": "English", "pa": "Punjabi"}
LANG_INSTRUCTIONS: dict = {
    "hi": "Respond in simple Hindi (Devanagari script).",
    "en": "Respond in simple English.",
    "pa": "Respond in Punjabi (Gurmukhi script).",
}