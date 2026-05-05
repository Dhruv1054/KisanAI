"""
services.py — all external API calls for KisanAI

FIXES:
1. Weather URL: use params dict instead of f-string to avoid %2F encoding
   (this was causing "mausam unavailable" on Railway for all cities)
2. Conversation history restored in ai_advice()
3. ai_advice response: 80 words max, 3 points, WhatsApp-friendly formatting
4. Subsidy formatting uses *bold* for WhatsApp
"""

import httpx
from config import (
    GEOCODE_API_URL, WEATHER_API_URL, MANDI_API_URL,
    OPENROUTER_KEY, OPENROUTER_URL, OPENROUTER_MODEL,
    DATA_GOV_IN_KEY, DEFAULT_LAT, DEFAULT_LON, LANG_INSTRUCTIONS,
)
from database import get_recent_messages

_GEO_CACHE: dict = {}


# ══════════════════════════════════════════════════
# GEOCODING
# ══════════════════════════════════════════════════

async def geocode(location_query: str) -> tuple:
    key = location_query.lower().strip()
    if key in _GEO_CACHE:
        return (*_GEO_CACHE[key], True)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                GEOCODE_API_URL,
                params={"q": f"{location_query}, India", "format": "json", "limit": 1, "countrycodes": "in"},
                headers={"User-Agent": "KisanAI/1.0 (farm advisor)"},
            )
            if r.status_code == 200:
                results = r.json()
                if results:
                    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
                    _GEO_CACHE[key] = (lat, lon)
                    return (lat, lon, True)
                return (DEFAULT_LAT, DEFAULT_LON, False)
    except Exception:
        pass
    return (DEFAULT_LAT, DEFAULT_LON, True)


# ══════════════════════════════════════════════════
# WEATHER
# ══════════════════════════════════════════════════

_WMO_CODES: dict = {
    0: "Clear", 1: "Partly cloudy", 2: "Partly cloudy", 3: "Cloudy",
    45: "Foggy", 48: "Foggy", 51: "Light drizzle", 53: "Drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    80: "Rain showers", 81: "Showers", 82: "Heavy showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail",
}


def _fmt_weather(temp, hum, wind, wcode, max_t, min_t, rain_pct, district: str) -> str:
    condition = _WMO_CODES.get(wcode, "Clear")
    lines = [
        f"\u2600\ufe0f {district}",
        f"  {temp}\u00b0C  {condition}",
        f"  Min {min_t}\u00b0C / Max {max_t}\u00b0C",
        f"  Humidity {hum}%  |  Wind {wind} km/h  |  Rain {rain_pct}%",
    ]
    if rain_pct > 70:
        lines.append("  \u26a0\ufe0f Bhari baarish — sinchai aur spray band rakhen.")
    elif rain_pct > 40:
        lines.append("  Halki baarish sambhav — pani kam dein.")
    else:
        lines.append("  \u2705 Saaf mausam — khet ka kaam karein.")
    try:
        if float(str(temp)) > 38:
            lines.append("  Garmi zyada — subah/shaam sinchai karein.")
    except (ValueError, TypeError):
        pass
    return "\n".join(lines)


async def fetch_weather(lat: float, lon: float, district: str = "") -> str:
    """
    FIX: Use params dict — httpx handles encoding correctly.
    f-string was encoding / as %2F which open-meteo rejected.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                WEATHER_API_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
                    "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Kolkata",
                    "forecast_days": 3,
                },
            )
            if r.status_code == 200:
                d = r.json()
                cur = d.get("current", {})
                day = d.get("daily", {})
                return _fmt_weather(
                    temp=cur.get("temperature_2m", "--"),
                    hum=cur.get("relative_humidity_2m", "--"),
                    wind=cur.get("wind_speed_10m", "--"),
                    wcode=cur.get("weather_code", 0),
                    max_t=day.get("temperature_2m_max", ["--"])[0],
                    min_t=day.get("temperature_2m_min", ["--"])[0],
                    rain_pct=day.get("precipitation_probability_max", [0])[0],
                    district=district,
                )
    except Exception:
        pass
    return f"\u26a0\ufe0f {district or 'aapke ilaake'} ka mausam abhi unavailable hai. Thodi der mein try karein."


async def fetch_weather_raw(lat: float, lon: float) -> dict:
    """FIX: params dict used here too."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                WEATHER_API_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m",
                    "daily": "precipitation_probability_max",
                    "timezone": "Asia/Kolkata",
                    "forecast_days": 1,
                },
            )
            if r.status_code == 200:
                d = r.json()
                return {
                    "temp": d.get("current", {}).get("temperature_2m"),
                    "rain_prob": d.get("daily", {}).get("precipitation_probability_max", [0])[0],
                }
    except Exception:
        pass
    return {}


# ══════════════════════════════════════════════════
# MANDI PRICES
# ══════════════════════════════════════════════════

_MANDI_FALLBACK: list = [
    {"market": "Azadpur Mandi, Delhi", "commodity": "Wheat", "min": "1800", "max": "2200", "modal": "2000"},
    {"market": "Khanna Mandi, Punjab", "commodity": "Wheat", "min": "1700", "max": "2100", "modal": "1900"},
    {"market": "Agra Mandi, UP",       "commodity": "Wheat", "min": "1650", "max": "1950", "modal": "1800"},
    {"market": "Indore Mandi, MP",     "commodity": "Wheat", "min": "1750", "max": "2050", "modal": "1900"},
]

CROP_ALIASES: dict = {
    "gehu": "Wheat",    "wheat": "Wheat",
    "dhan": "Paddy",    "paddy": "Paddy",
    "chawal": "Rice",   "rice": "Rice",
    "sarso": "Mustard", "mustard": "Mustard",
    "pyaaz": "Onion",   "onion": "Onion",
    "aloo": "Potato",   "potato": "Potato",
    "makai": "Maize",   "maize": "Maize",
    "chana": "Gram",    "gram": "Gram",
    "arhar": "Tur",     "tur": "Tur",
}


async def fetch_mandi(crop: str = "", state: str = "") -> list:
    if not DATA_GOV_IN_KEY:
        return [{**e, "commodity": crop or e["commodity"]} for e in _MANDI_FALLBACK]
    params: dict = {"api-key": DATA_GOV_IN_KEY, "format": "json", "limit": 5}
    if crop:
        params["filters[Commodity.keyword]"] = crop
    if state:
        params["filters[State.keyword]"] = state
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(MANDI_API_URL, params=params)
            if r.status_code == 200:
                records = r.json().get("records", [])[:5]
                if records:
                    return [
                        {
                            "market":    rec.get("Market", "?"),
                            "commodity": rec.get("Commodity", crop),
                            "min":       rec.get("Min Price", "N/A"),
                            "max":       rec.get("Max Price", "N/A"),
                            "modal":     rec.get("Modal Price", "N/A"),
                        }
                        for rec in records
                    ]
    except Exception:
        pass
    return [{**e, "commodity": crop or e["commodity"]} for e in _MANDI_FALLBACK]


def format_mandi(records: list, limit: int = 4) -> str:
    lines = []
    for r in records[:limit]:
        lines.append(
            f"  {r['market']}\n"
            f"     {r['commodity']}: Rs{r['modal']}/quintal (Rs{r['min']}-Rs{r['max']})"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════
# SUBSIDIES
# ══════════════════════════════════════════════════

_SUBSIDIES: list = [
    ("PM-KISAN",              "Rs 6,000/year (3 installments)", "Chhote kisan <2 hectare",  "pmkisan.gov.in"),
    ("PM Fasal Bima (PMFBY)", "2% kharif, 1.5% rabi premium",  "Fasal loan wale kisan",    "pmfby.gov.in"),
    ("Soil Health Card",      "Free mitti jaanch",              "Sabhi kisan",              "soilhealth.dac.gov.in"),
    ("Farm Mechanization",    "50-80% subsidy on equipment",    "Chhote kisan",             "Krishi Vigyan Kendra"),
    ("Kisan Credit Card",     "Rs 3 lakh tak at 4% interest",   "Sabhi kisan",              "Kisi bhi nationalized bank"),
]

_SUBSIDY_KEYWORDS: dict = {
    0: ["pm", "kisan samman", "samman", "6000", "paisa", "pmkisan"],
    1: ["insurance", "pmfby", "beema", "fasal bima"],
    2: ["soil", "health", "card", "mitti"],
    3: ["machine", "tractor", "equipment", "yantra"],
    4: ["credit", "kcc", "loan", "karz", "udhaar"],
}


def get_subsidy_info(query: str = "") -> str:
    low = query.lower()
    for idx, keywords in _SUBSIDY_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            s = _SUBSIDIES[idx]
            return f"*{s[0]}*\nRashi: {s[1]}\nYogyata: {s[2]}\nApply: {s[3]}"
    lines = ["*Sarkari Yojnaein:*\n"]
    for s in _SUBSIDIES:
        lines.append(f"*{s[0]}* — {s[1]}\nApply: {s[3]}\n")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
# AI ADVISOR
# ══════════════════════════════════════════════════

async def ai_advice(message: str, farmer: dict) -> str:
    """
    AI-powered farm advice.
    FIX: Conversation history restored using farmer["phone"].
    FIX: Response capped at 80 words, max 3 points.
    """
    crop = farmer.get("crop", "fasal")
    district = farmer.get("district", "India")
    lang = farmer.get("language", "hi")
    phone = farmer.get("phone", "")

    lang_instruction = LANG_INSTRUCTIONS.get(lang, LANG_INSTRUCTIONS["hi"])

    system = (
        f"You are KisanAI, an expert farm advisor for Indian farmers. "
        f"This farmer grows {crop} in {district}. "
        f"{lang_instruction} "
        "Keep responses under 80 words. Max 3 numbered points. "
        "Use *bold* for key terms (WhatsApp format). Plain text otherwise. "
        "Be direct and practical like a farmer friend. "
        "If question is vague, ask ONE specific follow-up question. "
        "If unsure, say: nishchit nahi, kisi agronomist se poochhen."
    )

    # Conversation history for context
    messages = [{"role": "system", "content": system}]
    if phone:
        try:
            history = get_recent_messages(phone, limit=4)
            for h in history:
                role = "user" if h["direction"] == "user" else "assistant"
                messages.append({"role": role, "content": h["message"]})
        except Exception:
            pass
    messages.append({"role": "user", "content": message})

    if OPENROUTER_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    OPENROUTER_URL,
                    json={"model": OPENROUTER_MODEL, "messages": messages, "max_tokens": 300},
                    headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                )
                if r.status_code == 200:
                    content = (
                        r.json().get("choices", [{}])[0]
                        .get("message", {}).get("content", "").strip()
                    )
                    if content:
                        return content
        except Exception:
            pass

    # Keyword fallbacks
    low = message.lower()
    if any(w in low for w in ["pest", "insect", "bug", "kida", "keeda"]):
        return "*Keeda control:*\n1. Neem oil 5ml/L spray\n2. Imidacloprid 0.3ml/L har 7 din\n3. Patte ke neeche bhi spray karein"
    if any(w in low for w in ["fertilizer", "urea", "dap", "npk", "khaad", "khad"]):
        return "*Khaad:*\n1. Urea 100kg/acre (30, 45, 60 din)\n2. DAP 50kg/acre bawaai pe\n3. Mitti jaanch zaroor karwayein"
    if any(w in low for w in ["irrigat", "water", "sincha", "paani", "pani"]):
        return "*Sinchai:*\n1. Garmi: har 5-7 din\n2. Sardi: har 10-14 din\n3. Baarish se pehle band karein"
    if any(w in low for w in ["yellow", "peela", "pili", "patte"]):
        return "*Peele patte:*\n1. Zinc ya nitrogen ki kami\n2. Zinc Sulphate 25kg/acre\n3. Ya Urea spray 2% try karein"
    if any(w in low for w in ["disease", "bimari", "rot", "fungus"]):
        return "*Bimari:*\n1. Mancozeb 2.5g/L spray\n2. Khet mein paani na rukne dein\n3. Prabhavit patte hatayein"
    return f"*{crop}* ke baare mein thodi aur detail dein — kya problem hai, kab se, aur kaun sa hissa prabhavit hai?"