"""
KisanAI - Autonomous WhatsApp-native Farm Advisor for Indian farmers
Day 1: District-based real-time weather via geocoding + live data APIs
"""

import os
import re
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# CONFIG
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "kisanai_verify_2024")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DATA_GOV_IN_KEY = os.getenv("DATA_GOV_IN_KEY", "")
DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "kisanai.db")

app = FastAPI(title="KisanAI", description="Autonomous farm advisor")

# ——— DATABASE ———
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS farmers (
            phone TEXT PRIMARY KEY,
            name TEXT,
            crop TEXT,
            district TEXT,
            state TEXT DEFAULT '',
            language TEXT DEFAULT 'hi',
            lat REAL DEFAULT 28.61,
            lon REAL DEFAULT 77.21,
            created_at TEXT,
            last_active TEXT,
            onboarded INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            direction TEXT,
            message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_farmer(phone: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM farmers WHERE phone = ?", (phone,)).fetchone()
        if not row:
            return None
        return {k: row[k] for k in row.keys()}


def save_convo(phone: str, direction: str, message: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO conversations (phone, direction, message) VALUES (?, ?, ?)",
            (phone, direction, message[:500]),
        )


# ——— GEOCODING ———
_GEO_CACHE = {}


async def geocode(district_query: str) -> tuple:
    """Convert Indian city/district to lat/lon via Nominatim (free)."""
    key = district_query.lower().strip()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": f"{district_query}, India",
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "in",
                },
                headers={"User-Agent": "KisanAI/1.0"},
            )
            if r.status_code == 200 and r.json():
                loc = r.json()[0]
                lat, lon = float(loc["lat"]), float(loc["lon"])
                _GEO_CACHE[key] = (lat, lon)
                return (lat, lon)
    except Exception:
        pass
    _GEO_CACHE[key] = (28.6139, 77.2090)
    return _GEO_CACHE[key]


# ——— WEATHER ———
_WMO = {
    0: "Clear", 1: "Partly cloudy", 2: "Partly cloudy", 3: "Cloudy",
    45: "Foggy", 48: "Foggy", 51: "Light drizzle", 53: "Drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 80: "Rain showers",
    81: "Shower", 82: "Heavy shower", 95: "Thunderstorm", 96: "Thunderstorm/hail",
}
def _weather_fmt(temp, hum, wind, wcode, max_t, min_t, rain, district):
    lines = []
    lines.append(f"\u2600 {district}")
    lines.append(f"  {temp}\u00b0C {_WMO.get(wcode, 'Clear')}  |  Min {min_t}\u00b0C / Max {max_t}\u00b0C")
    lines.append(f"  Humidity {hum}% | Wind {wind} km/h | Rain {rain}%")
    if rain > 70:
        lines.append("  Heavy rain expected - delay irrigation and spraying.")
    elif rain > 40:
        lines.append("  Light rain possible - skip heavy watering.")
    else:
        lines.append("  Clear weather - good time for field work.")
    try:
        if int(str(temp)) > 38:
            lines.append("  Heat warning (>38\u00b0C) - water only in morning/evening.")
    except ValueError:
        pass
    return "\n".join(lines)


async def fetch_weather(lat: float, lon: float, district: str = "") -> str:
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code"
        f"&daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min"
        f"&timezone=Asia/Kolkata&forecast_days=3"
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
            if r.status_code == 200:
                d = r.json()
                cur = d.get("current", {})
                day = d.get("daily", {})
                return _weather_fmt(
                    temp=cur.get("temperature_2m", "--"),
                    hum=cur.get("relative_humidity_2m", "--"),
                    wind=cur.get("wind_speed_10m", "--"),
                    wcode=cur.get("weather_code", 0),
                    max_t=day.get("temperature_2m_max", ["--"])[0],
                    min_t=day.get("temperature_2m_min", ["--"])[0],
                    rain=day.get("precipitation_probability_max", [0])[0],
                    district=district,
                )
    except Exception:
        pass
    return f"\u26A0 {district or 'your area'} - Weather unavailable. Try again later."


# ——— MANDI PRICES ———
_MANDI = [
    {"market": "Azadpur Mandi, Delhi", "commodity": "Wheat", "min": "1800", "max": "2200", "modal": "2000"},
    {"market": "Khanna Mandi, Punjab", "commodity": "Wheat", "min": "1700", "max": "2100", "modal": "1900"},
    {"market": "Agra Mandi, UP", "commodity": "Wheat", "min": "1650", "max": "1950", "modal": "1800"},
    {"market": "Indore Mandi, MP", "commodity": "Wheat", "min": "1750", "max": "2050", "modal": "1900"},
]


async def fetch_mandi(crop: str = "", state: str = "") -> list:
    """Live mandi via data.gov.in API or fallback."""
    if not DATA_GOV_IN_KEY:
        return [{**e, "commodity": crop or e["commodity"]} for e in _MANDI]
    url = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
    params = {"api-key": DATA_GOV_IN_KEY, "format": "json", "limit": 5}
    if crop:
        params["filters[Commodity.keyword]"] = crop
    if state:
        params["filters[State.keyword]"] = state
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, params=params)
            if r.status_code == 200:
                recs = r.json().get("records", [])[:5]
                return [
                    {
                        "market": rec.get("Market", "?"),
                        "commodity": rec.get("Commodity", crop),
                        "min": rec.get("Min Price", "N/A"),
                        "max": rec.get("Max Price", "N/A"),
                        "modal": rec.get("Modal Price", "N/A"),
                    }
                    for rec in recs
                ]
    except Exception:
        pass
    return [{**e, "commodity": crop or e["commodity"]} for e in _MANDI]


# ——— AI ADVISOR ———
async def ai_advice(message: str, farmer: dict) -> str:
    crop = farmer.get("crop", "general")
    district = farmer.get("district", "India")
    system = (
        f"You are KisanAI, a farm advisor for Indian farmers. "
        f"Farmer grows {crop} in {district}. "
        "Respond in the same language the user writes in. "
        "Keep responses under 200 characters. Practical advice only."
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
                    c = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                    if c:
                        return c
        except Exception:
            pass
    low = message.lower()
    if any(w in low for w in ["pest", "insect", "pesticide", "bug", "kida"]):
        return "Neem oil 5ml/L spray. Imidacloprid 0.3ml/L every 7 days. Also spray under leaves."
    if any(w in low for w in ["fertilizer", "urea", "dap", "npk", "khaad"]):
        return "Urea 100kg/acre (at 30, 45, 60 days). DAP 50kg/acre at sowing. Get soil test done."
    if any(w in low for w in ["irrigat", "water", "sincha", "paani"]):
        return "Summer: water every 5-7 days. Winter: every 10-14 days. Stop watering if rain expected."
    return f"Monitor {crop} daily. Use proper irrigation + balanced fertilizer + pest control on time."


# ——— SUBSIDIES ———
SUBSIDIES = [
    ("PM-KISAN", "Rs 6,000/year (3 installments)", "Small farmers (<2 hectares)", "pmkisan.gov.in"),
    ("PM Crop Insurance (PMFBY)", "2% premium (kharif), 1.5% (rabi)", "Farmers with crop loans", "pmfby.gov.in"),
    ("Soil Health Card", "Free soil testing + report", "All farmers", "soilhealth.dac.gov.in"),
    ("Farm Mechanization", "50-80% subsidy on equipment", "Small farmers", "Contact Krishi Vigyan Kendra"),
    ("Kisan Credit Card (KCC)", "Credit up to Rs 3 lakh at 4%", "All farmers", "Any nationalized bank"),
]


def subsidy_info(query: str = "") -> str:
    low = query.lower()
    keywords = {
        0: ["pm", "kisan", "samman"],
        1: ["insurance", "crop", "pmfby", "beema"],
        2: ["soil", "health", "card", "mitti"],
        3: ["machine", "tractor", "equipment"],
        4: ["credit", "kcc", "loan"],
    }
    for idx, words in keywords.items():
        if any(w in low for w in words):
            s = SUBSIDIES[idx]
            return f"  {s[0]}\n  Amount: {s[1]}\n  Eligibility: {s[2]}\n  Apply: {s[3]}"
    lines = ["\n  Government Schemes:\n"]
    for s in SUBSIDIES:
        lines.append(f"  {s[0]} - {s[1]}\n     Apply: {s[3]}")
    return "\n".join(lines)


# ——— MESSAGE ROUTER ———
async def process_message(phone: str, message: str) -> str:
    farmer = get_farmer(phone)
    low = message.lower().strip()

    # Step 1: New user → collect name
    if not farmer:
        name = message.strip()[:50]
        with get_db() as conn:
            conn.execute(
                "INSERT INTO farmers (phone, name, created_at, last_active) VALUES (?, ?, ?, ?)",
                (phone, name, datetime.now().isoformat(), datetime.now().isoformat()),
            )
        return f"Namaste {name}! I am KisanAI - your personal farm advisor.\n\nWhat crop do you mainly grow?"

    # Step 2: Collect crop
    if not farmer.get("crop"):
        crop = message.strip()[:50]
        with get_db() as conn:
            conn.execute(
                "UPDATE farmers SET crop = ?, last_active = ? WHERE phone = ?",
                (crop, datetime.now().isoformat(), phone),
            )
            return f"  {crop} noted!\n\nNow tell me your city or district so I can give local weather and mandi prices."

    # Step 3: Collect district → geocode → weather + mandi
    if not farmer.get("district"):
        district = message.strip()[:50]
        lat, lon = await geocode(district)
        with get_db() as conn:
            conn.execute(
                "UPDATE farmers SET district = ?, lat = ?, lon = ?, last_active = ?, onboarded = 1 WHERE phone = ?",
                (district, lat, lon, datetime.now().isoformat(), phone),
            )
        weather = await fetch_weather(lat, lon, district)
        mandi = await fetch_mandi(farmer["crop"])
        mtxt = "\n".join(
            [f"  {r['market']}\n     {r['commodity']}: Rs{r['modal']}/quintal (Rs{r['min']}-Rs{r['max']})" for r in mandi[:3]]
        )
        return f"  {district} set!\n\n{weather}\n\n  {farmer['crop']} mandi prices:\n{mtxt}\n\nYou are all set. Ask me anything."

    # Onboarded → command routing
    if re.match(r"^(mandi|price|rate|bhav|bhao)", low):
        crop = re.sub(r"^(mandi|price|rate|bhav|bhao)\s*", "", low, flags=re.IGNORECASE).strip() or farmer["crop"]
        mandi = await fetch_mandi(crop, farmer.get("state", ""))
        mtxt = "\n".join(
            [f"  {r['market']}\n     {r['commodity']}: Rs{r['modal']}/quintal (Rs{r['min']}-Rs{r['max']})" for r in mandi[:4]]
        )
        return f"  {crop} mandi prices:\n\n{mtxt}"

    if re.match(r"^(weather|mausam|mosam|barish|rain|mosum)", low):
        return await fetch_weather(farmer.get("lat", 28.61), farmer.get("lon", 77.21), farmer.get("district", ""))

    if re.match(r"^(subsidy|scheme|yojna|sarkari|govt|plan)", low):
        return subsidy_info(message)

    if low in ("help", "menu", "commands"):
        return ("  KisanAI Features:\n\n"
                "  Ask about: pests, fertilizer, irrigation, diseases\n"
                "  'mandi' - crop prices\n"
                "  'weather' - forecast\n"
                "  'schemes' - govt programs\n")

    if low in ("hi", "namaste", "hello", "hey", "ram ram"):
        return f"Namaste {farmer['name']}!\nWhat would you like to know about {farmer['crop']} today?"

    # Default → AI advisory
    save_convo(phone, "user", message)
    advice = await ai_advice(message, farmer)
    resp = f"  KisanAI Advice:\n\n{advice}"
    save_convo(phone, "bot", resp)
    with get_db() as conn:
        conn.execute("UPDATE farmers SET last_active = ? WHERE phone = ?", (datetime.now().isoformat(), phone))
    return resp


# ——— WHATSAPP SEND ———
async def send_whatsapp(to: str, text: str):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[KisanAI WA -> {to}]: {text[:120]}...")
        return
    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                print(f"WA fail ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"WA error: {e}")


# ——— WEBHOOKS ———
@app.get("/webhook")
async def webhook_verify(request: Request):
    p = {k: v for k, v in request.query_params.items()}
    if p.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
        return int(p.get("hub.challenge", 0))
    return JSONResponse({"error": "no"}, status_code=403)


@app.post("/webhook")
async def webhook_receive(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "bad"}, status_code=400)
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue
                phone = msg.get("from", "")
                text = msg.get("text", {}).get("body", "").strip()
                if phone and text:
                    resp = await process_message(phone, text)
                    await send_whatsapp(phone, resp)
    return JSONResponse({"status": "ok"})


# ——— API ENDPOINTS ———
class TestMsg(BaseModel):
    phone: str
    message: str


@app.post("/api/message")
async def api_test(msg: TestMsg):
    return {"phone": msg.phone, "response": await process_message(msg.phone, msg.message)}


@app.get("/api/farmers")
async def api_farmers():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT phone, name, crop, district, lat, lon, created_at, last_active, onboarded "
            "FROM farmers ORDER BY created_at DESC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


@app.get("/api/stats")
async def api_stats():
    with get_db() as conn:
        return {
            "farmers": conn.execute("SELECT COUNT(*) FROM farmers").fetchone()[0],
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        }


# ——— STARTUP ———
@app.on_event("startup")
async def startup():
    init_db()
    print("KisanAI started")


if __name__ == "__main__":
    init_db()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
