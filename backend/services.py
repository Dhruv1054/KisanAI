"""
services.py — all external API calls for KisanAI
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
        f"☀️ {district}",
        f"  {temp}°C  {condition}",
        f"  Min {min_t}°C / Max {max_t}°C",
        f"  Humidity {hum}%  |  Wind {wind} km/h  |  Rain {rain_pct}%",
    ]
    if rain_pct > 70:
        lines.append("  ⚠️ Bhari baarish — sinchai aur spray band rakhen.")
    elif rain_pct > 40:
        lines.append("  Halki baarish sambhav — pani kam dein.")
    else:
        lines.append("  ✅ Saaf mausam — khet ka kaam karein.")
    try:
        if float(str(temp)) > 38:
            lines.append("  Garmi zyada — subah/shaam sinchai karein.")
    except (ValueError, TypeError):
        pass
    return "\n".join(lines)


async def fetch_weather(lat: float, lon: float, district: str = "") -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                WEATHER_API_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,apparent_temperature,"
                        "relative_humidity_2m,precipitation,"
                        "wind_speed_10m,weather_code"
                    ),
                    "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Kolkata",
                    "forecast_days": 1,
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
            print(f"[weather] HTTP {r.status_code} lat={lat} lon={lon}: {r.text[:200]}")
    except Exception as exc:
        print(f"[weather] exception lat={lat} lon={lon}: {exc!r}")
    return f"⚠️ {district or 'aapke ilaake'} ka mausam abhi unavailable hai. Thodi der mein try karein."


async def fetch_weather_raw(lat: float, lon: float) -> dict:
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

# Fallback data based on 2024-25 MSP / typical market ranges, organised by crop
_MANDI_FALLBACK_BY_CROP: dict = {
    "Wheat": [
        {"market": "Azadpur Mandi, Delhi",    "min": "2100", "max": "2450", "modal": "2275"},
        {"market": "Khanna Mandi, Punjab",    "min": "2050", "max": "2400", "modal": "2250"},
        {"market": "Agra Mandi, UP",          "min": "2000", "max": "2350", "modal": "2200"},
        {"market": "Hapur Mandi, UP",         "min": "2050", "max": "2380", "modal": "2220"},
    ],
    "Paddy": [
        {"market": "Patna Mandi, Bihar",      "min": "2100", "max": "2500", "modal": "2300"},
        {"market": "Burdwan Mandi, WB",       "min": "2050", "max": "2450", "modal": "2280"},
        {"market": "Karimnagar Mandi, TS",    "min": "2000", "max": "2400", "modal": "2250"},
        {"market": "Nizamabad Mandi, TS",     "min": "2100", "max": "2500", "modal": "2300"},
    ],
    "Rice": [
        {"market": "Kolkata Mandi, WB",       "min": "3000", "max": "4000", "modal": "3500"},
        {"market": "Chennai Mandi, TN",       "min": "2800", "max": "3800", "modal": "3300"},
        {"market": "Hyderabad Mandi, TS",     "min": "2900", "max": "3900", "modal": "3400"},
        {"market": "Lucknow Mandi, UP",       "min": "2700", "max": "3700", "modal": "3200"},
    ],
    "Mustard": [
        {"market": "Jaipur Mandi, Rajasthan", "min": "5600", "max": "6300", "modal": "5950"},
        {"market": "Alwar Mandi, Rajasthan",  "min": "5500", "max": "6200", "modal": "5850"},
        {"market": "Bharatpur Mandi, Raj.",   "min": "5550", "max": "6250", "modal": "5900"},
        {"market": "Agra Mandi, UP",          "min": "5600", "max": "6300", "modal": "5950"},
    ],
    "Onion": [
        {"market": "Lasalgaon Mandi, MH",     "min": "1200", "max": "2800", "modal": "2000"},
        {"market": "Nashik Mandi, MH",        "min": "1100", "max": "2700", "modal": "1900"},
        {"market": "Azadpur Mandi, Delhi",    "min": "1500", "max": "3000", "modal": "2200"},
        {"market": "Hubli Mandi, KA",         "min": "1000", "max": "2500", "modal": "1800"},
    ],
    "Potato": [
        {"market": "Agra Mandi, UP",          "min": "800",  "max": "1600", "modal": "1200"},
        {"market": "Kolkata Mandi, WB",       "min": "700",  "max": "1500", "modal": "1100"},
        {"market": "Jalandhar Mandi, PB",     "min": "750",  "max": "1550", "modal": "1150"},
        {"market": "Indore Mandi, MP",        "min": "780",  "max": "1580", "modal": "1180"},
    ],
    "Maize": [
        {"market": "Gulbarga Mandi, KA",      "min": "2000", "max": "2450", "modal": "2225"},
        {"market": "Davangere Mandi, KA",     "min": "1950", "max": "2400", "modal": "2200"},
        {"market": "Nizamabad Mandi, TS",     "min": "2000", "max": "2450", "modal": "2225"},
        {"market": "Warangal Mandi, TS",      "min": "1980", "max": "2430", "modal": "2210"},
    ],
    "Gram": [
        {"market": "Indore Mandi, MP",        "min": "5000", "max": "5800", "modal": "5400"},
        {"market": "Kota Mandi, Rajasthan",   "min": "4900", "max": "5700", "modal": "5300"},
        {"market": "Nagpur Mandi, MH",        "min": "5100", "max": "5900", "modal": "5500"},
        {"market": "Gulbarga Mandi, KA",      "min": "5000", "max": "5800", "modal": "5400"},
    ],
    "Tur": [
        {"market": "Latur Mandi, MH",         "min": "6500", "max": "7500", "modal": "7000"},
        {"market": "Gulbarga Mandi, KA",      "min": "6400", "max": "7400", "modal": "6900"},
        {"market": "Akola Mandi, MH",         "min": "6600", "max": "7600", "modal": "7100"},
        {"market": "Nizamabad Mandi, TS",     "min": "6500", "max": "7500", "modal": "7000"},
    ],
}


def _get_fallback(crop: str) -> list:
    """Return crop-appropriate fallback mandi data with correct commodity label."""
    canon = CROP_ALIASES.get(crop.lower(), crop.title())
    rows = _MANDI_FALLBACK_BY_CROP.get(canon) or _MANDI_FALLBACK_BY_CROP.get(crop.title(), _MANDI_FALLBACK_BY_CROP["Wheat"])
    return [{"market": r["market"], "commodity": canon, "min": r["min"], "max": r["max"], "modal": r["modal"]} for r in rows]


async def fetch_mandi(crop: str = "", state: str = "") -> list:
    crop_canon = CROP_ALIASES.get(crop.lower(), crop) if crop else ""

    if not DATA_GOV_IN_KEY:
        return _get_fallback(crop_canon or "Wheat")

    # Correct filter syntax for data.gov.in — no .keyword suffix (was a bug)
    params: dict = {"api-key": DATA_GOV_IN_KEY, "format": "json", "limit": 10}
    if crop_canon:
        params["filters[Commodity]"] = crop_canon
    if state:
        params["filters[State]"] = state

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(MANDI_API_URL, params=params)
            if r.status_code == 200:
                records = r.json().get("records", [])[:5]
                if records:
                    parsed = []
                    for rec in records:
                        # API may return snake_case or TitleCase field names
                        parsed.append({
                            "market":    rec.get("market") or rec.get("Market", "?"),
                            "commodity": rec.get("commodity") or rec.get("Commodity", crop_canon),
                            "min":       rec.get("min_price") or rec.get("Min Price", "N/A"),
                            "max":       rec.get("max_price") or rec.get("Max Price", "N/A"),
                            "modal":     rec.get("modal_price") or rec.get("Modal Price", "N/A"),
                        })
                    if any(p["market"] != "?" for p in parsed):
                        return parsed
            else:
                print(f"[mandi] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"[mandi] exception: {exc!r}")

    return _get_fallback(crop_canon or "Wheat")


def format_mandi(records: list, limit: int = 4) -> str:
    lines = []
    for r in records[:limit]:
        market    = r.get("market",    r.get("Market",      "?"))
        commodity = r.get("commodity", r.get("Commodity",   "?"))
        modal     = r.get("modal",     r.get("modal_price", r.get("Modal Price", "N/A")))
        lo        = r.get("min",       r.get("min_price",   r.get("Min Price",   "N/A")))
        hi        = r.get("max",       r.get("max_price",   r.get("Max Price",   "N/A")))
        lines.append(
            f"  {market}\n"
            f"     {commodity}: Rs{modal}/quintal (Rs{lo}-Rs{hi})"
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

    # Keyword fallbacks when OpenRouter is unavailable
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
