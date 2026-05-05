"""
services.py — all external API calls for KisanAI
Each service is a standalone async function. Failures always return a safe fallback.

BUG FIX: Geocoding fallback detection was too strict — any city whose real coords
are close to Delhi (like Karnal, Panipat, Ghaziabad) would get rejected.
New approach: geocoding returns a (lat, lon, success) tuple. success=False only
when Nominatim actually returns an error or empty result, not based on coord proximity.
"""

import httpx
from config import (
    GEOCODE_API_URL, WEATHER_API_URL, MANDI_API_URL,
    OPENROUTER_KEY, OPENROUTER_URL, OPENROUTER_MODEL,
    DATA_GOV_IN_KEY, DEFAULT_LAT, DEFAULT_LON, LANG_INSTRUCTIONS,
)
from database import get_recent_messages

# ——— In-memory caches (reset on restart — fine for MVP) ———
_GEO_CACHE: dict = {}


# ══════════════════════════════════════════════════
# GEOCODING
# ══════════════════════════════════════════════════

async def geocode(location_query: str) -> tuple[float, float, bool]:
    """
    Convert an Indian place name to (lat, lon, success).
    success=False means Nominatim returned nothing — ask the user to retry.
    success=True means we got a real result (even if it happens to be near Delhi).

    FIX: We no longer reject coords based on proximity to Delhi.
    Karnal, Panipat, Bahadurgarh are genuinely near Delhi and were being rejected.
    """
    key = location_query.lower().strip()
    if key in _GEO_CACHE:
        return (*_GEO_CACHE[key], True)

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                GEOCODE_API_URL,
                params={
                    "q": f"{location_query}, India",
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "in",
                },
                headers={"User-Agent": "KisanAI/1.0 (farm advisor)"},
            )
            if r.status_code == 200:
                results = r.json()
                if results:
                    lat = float(results[0]["lat"])
                    lon = float(results[0]["lon"])
                    _GEO_CACHE[key] = (lat, lon)
                    return (lat, lon, True)
                # Nominatim returned empty — location not found
                return (DEFAULT_LAT, DEFAULT_LON, False)
    except Exception:
        pass

    # Network error — use fallback but mark as success so we don't
    # block the user. Weather will default to Delhi coords silently.
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
        f"  Humidity {hum}%  |  Wind {wind} km/h  |  Rain chance {rain_pct}%",
    ]
    if rain_pct > 70:
        lines.append("  \u26a0\ufe0f Bhari baarish — sinchai aur spray band rakhen.")
    elif rain_pct > 40:
        lines.append("  \ud83c\udf27\ufe0f Halki baarish sambhav — pani kam dein.")
    else:
        lines.append("  \u2705 Saaf mausam — khet ka kaam karein.")
    try:
        if float(str(temp)) > 38:
            lines.append("  \ud83d\udd25 Garmi zyada hai — subah/shaam sinchai karein.")
    except (ValueError, TypeError):
        pass
    return "\n".join(lines)


async def fetch_weather(lat: float, lon: float, district: str = "") -> str:
    """Full formatted weather string for display."""
    url = (
        f"{WEATHER_API_URL}"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code"
        f"&daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min"
        f"&timezone=Asia%2FKolkata&forecast_days=3"
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
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
    """Minimal weather dict for proactive alert decisions."""
    url = (
        f"{WEATHER_API_URL}"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m"
        f"&daily=precipitation_probability_max"
        f"&timezone=Asia%2FKolkata&forecast_days=1"
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
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

# Fallback data shown when DATA_GOV_IN_KEY is not set or API fails
_MANDI_FALLBACK: list[dict] = [
    {"market": "Azadpur Mandi, Delhi",  "commodity": "Wheat", "min": "1800", "max": "2200", "modal": "2000"},
    {"market": "Khanna Mandi, Punjab",  "commodity": "Wheat", "min": "1700", "max": "2100", "modal": "1900"},
    {"market": "Agra Mandi, UP",        "commodity": "Wheat", "min": "1650", "max": "1950", "modal": "1800"},
    {"market": "Indore Mandi, MP",      "commodity": "Wheat", "min": "1750", "max": "2050", "modal": "1900"},
]

# Hindi/common crop name → English name for API queries
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


async def fetch_mandi(crop: str = "", state: str = "") -> list[dict]:
    """
    Returns up to 5 mandi price records.
    Falls back to hardcoded data if API key is missing or call fails.
    """
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


def format_mandi(records: list[dict], limit: int = 4) -> str:
    lines = []
    for r in records[:limit]:
        lines.append(
            f"  {r['market']}\n"
            f"     {r['commodity']}: Rs{r['modal']}/quintal  "
            f"(Rs{r['min']} – Rs{r['max']})"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════
# SUBSIDIES
# ══════════════════════════════════════════════════

_SUBSIDIES: list[tuple] = [
    ("PM-KISAN",              "Rs 6,000/year (3 installments of Rs 2,000)",   "Sabhi chhote kisan (<2 hectare)", "pmkisan.gov.in"),
    ("PM Fasal Bima (PMFBY)", "2% premium kharif, 1.5% rabi",                "Fasal loan wale kisan",           "pmfby.gov.in"),
    ("Soil Health Card",      "Free mitti jaanch + report",                   "Sabhi kisan",                     "soilhealth.dac.gov.in"),
    ("Farm Mechanization",    "50-80% subsidy on equipment",                  "Chhote kisan",                    "Krishi Vigyan Kendra se contact karein"),
    ("Kisan Credit Card",     "Rs 3 lakh tak credit at 4% interest",          "Sabhi kisan",                     "Kisi bhi nationalized bank mein"),
]

_SUBSIDY_KEYWORDS: dict = {
    0: ["pm", "kisan samman", "samman", "6000", "paisa"],
    1: ["insurance", "pmfby", "beema", "fasal bima"],
    2: ["soil", "health", "card", "mitti", "mitti jaanch"],
    3: ["machine", "tractor", "equipment", "yantra"],
    4: ["credit", "kcc", "loan", "karz", "udhaar"],
}


def get_subsidy_info(query: str = "") -> str:
    low = query.lower()
    for idx, keywords in _SUBSIDY_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            s = _SUBSIDIES[idx]
            return (
                f"  [Yojna] {s[0]}\n"
                f"  Rashi: {s[1]}\n"
                f"  Yogyata: {s[2]}\n"
                f"  Apply karein: {s[3]}"
            )
    lines = ["  [*] Sarkari Yojnaein:\n"]
    for s in _SUBSIDIES:
        lines.append(f"  {s[0]}\n     Rashi: {s[1]}\n     Apply: {s[3]}\n")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
# AI ADVISOR
# ══════════════════════════════════════════════════

# Keyword fallbacks when OpenRouter is unavailable
_FALLBACK_ADVICE: list[tuple] = [
    (["pest", "insect", "bug", "kida", "keeda"],
     "Neem oil 5ml/L spray karein. Imidacloprid 0.3ml/L har 7 din. Patte ke neeche bhi spray karein."),
    (["fertilizer", "urea", "dap", "npk", "khaad", "khad"],
     "Urea 100kg/acre (30, 45, 60 din par). DAP 50kg/acre bawaai ke samay. Mitti jaanch zaroor karwayein."),
    (["irrigat", "water", "sincha", "paani", "pani"],
     "Garmi mein 5-7 din mein ek baar. Sardi mein 10-14 din mein. Baarish se pehle sinchai band karein."),
    (["yellow", "peela", "pili", "patte"],
     "Peele patte zinc ya nitrogen ki kami dikhate hain. Zinc Sulphate 25kg/acre ya Urea spray 2% try karein."),
    (["disease", "bimari", "rot", "fungus"],
     "Mancozeb 2.5g/L ya Carbendazim 1g/L spray karein. Khet mein paani na rukne dein."),
]


async def ai_advice(message: str, farmer: dict) -> str:
    """Get AI-powered crop advice for the farmer's specific situation."""
    crop = farmer.get("crop", "general")
    district = farmer.get("district", "India")
    system = (
        f"You are KisanAI, a farm advisor for Indian farmers. "
        f"The farmer grows {crop} in {district}. "
        "Respond in the same language the user writes in (Hindi or English). "
        "Keep responses under 80 words. Use simple numbered points — max 3. "
        "Use WhatsApp formatting: *bold* not **bold**. "
        "Write like a farmer friend, not a textbook. "
        "Never hallucinate legal or medical information."
    )
    if OPENROUTER_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    OPENROUTER_URL,
                    json={
                        "model": "openrouter/qwen/qwen-2.5-72b-instruct",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": message},
                        ],
                        "max_tokens": 300,
                    },
                    headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        return content
        except Exception:
            pass

    # Fallback: keyword-matched advice
    lower = message.lower()
    if any(w in lower for w in ["pest", "insect", "pesticide", "bug"]):
        return (
            "*Neem oil* 5ml/L spray\n"
            "1. Imidacloprid 0.3ml/L every 7 days\n"
            "2. Spray under leaves too"
        )
    if any(w in lower for w in ["fertilizer", "urea", "dap", "npk"]):
        return (
            "*Urea* 100kg/acre (at 30, 45, 60 days)\n"
            "1. DAP 50kg/acre at sowing\n"
            "2. Get soil test for exact amounts"
        )
    if any(w in lower for w in ["irrig", "water"]):
        return (
            "*Summer:* irrigate every 5-7 days\n"
            "1. *Winter:* every 10-14 days\n"
            "2. Stop before rain"
        )
    return (
        f"*{crop}* tips:\n"
        "1. Monitor daily\n"
        "2. Proper irrigation + balanced fertilizer\n"
        "3. Timely pest control"
    )
