"""
router.py — KisanAI message router and onboarding state machine

Onboarding steps:
  0  → farmer row just created → ask name
  1  → name saved              → ask language
  2  → language saved          → ask crop
  3  → crop saved              → ask district
  4  → fully onboarded         → normal command routing

BUG FIX: "hello" as first message caused Internal Server Error because:
  1. farmer was None → created with step=0
  2. On the SAME call, code fell through to onboarded routing
  3. Greeting handler matched "hello" but farmer["name"] was None → TypeError
  Fix: create farmer + return welcome in one shot, never fall through.

BUG FIX: Geocoding rejection of cities near Delhi (Karnal, Panipat etc.)
  Old code: rejected any coords within 0.05° of Delhi default
  New code: geocode() returns success=False only when Nominatim returns empty
"""

from database import (
    get_farmer, create_farmer, update_farmer,
    advance_step, touch_farmer, save_message,
)
from services import (
    geocode, fetch_weather, fetch_mandi, format_mandi,
    get_subsidy_info, get_ai_advice, CROP_ALIASES,
)
from config import LANGUAGES, LANG_NAMES


# ══════════════════════════════════════════════════
# Intent detection helpers
# ══════════════════════════════════════════════════

def _is_mandi(text: str) -> bool:
    keywords = {
        "mandi", "price", "rate", "bhav", "bhao", "bhaav", "daam",
        "gehu", "dhan", "chawal", "sarso", "pyaaz", "aloo", "makai",
        "chana", "arhar", "fasal ka daam", "kya bhav", "daam kya",
        "aaj ka bhav", "market rate",
    }
    return any(k in text for k in keywords)


def _is_weather(text: str) -> bool:
    keywords = {
        "weather", "mausam", "mosam", "barish", "baarish", "rain",
        "barsat", "tufan", "dhoop", "aandhi", "mausam kaisa",
        "kal mausam", "aaj mausam", "forecast",
    }
    return any(k in text for k in keywords)


def _is_subsidy(text: str) -> bool:
    keywords = {
        "subsidy", "scheme", "yojna", "yojana", "sarkari", "govt",
        "government", "paisa", "kisan samman", "beema", "bima",
        "loan", "kcc", "karz", "madad", "sahayata", "apply",
    }
    return any(k in text for k in keywords)


def _is_greeting(text: str) -> bool:
    keywords = {
        "namaste", "namaskar", "hello", "hi", "hey",
        "ram ram", "sat sri akal", "salaam",
        "bhai", "haan", "theek hai", "ok", "okay", "achha", "thik",
    }
    return any(k in text for k in keywords)


def _is_help(text: str) -> bool:
    keywords = {"help", "menu", "commands", "kya kar sakte", "features", "kya karta"}
    return any(k in text for k in keywords)


def _extract_crop(text: str, default: str) -> str:
    for alias, name in CROP_ALIASES.items():
        if alias in text:
            return name
    return default


# ══════════════════════════════════════════════════
# Main message processor
# ══════════════════════════════════════════════════

async def process_message(phone: str, message: str) -> str:
    """
    Entry point for every incoming message.
    Returns a string response to send back to the farmer.
    All exceptions are caught here so the webhook never returns 500.
    """
    try:
        return await _route(phone, message)
    except Exception as exc:
        # Log but never expose internals to the farmer
        print(f"[KisanAI ERROR] phone={phone} msg={message!r} err={exc!r}")
        return (
            "\u26a0\ufe0f Kuch technical problem aa gayi. "
            "Thodi der mein dobara try karein. \U0001F64F"
        )


async def _route(phone: str, message: str) -> str:
    farmer = get_farmer(phone)
    low = message.lower().strip()

    # ── STEP 0: Brand new user ──────────────────────────────────────────
    if farmer is None:
        create_farmer(phone)
        return (
            "\U0001F33E Namaste! Main KisanAI hoon — aapka apna kheti sahayak.\n\n"
            "Main aapki madad kar sakta hoon:\n"
            "  \U0001F331 Fasal ki samasya\n"
            "  \U0001F4B0 Mandi ka sahi bhav\n"
            "  \U0001F327\ufe0f Mausam ki jaankari\n"
            "  \U0001F4CB Sarkari yojnaein\n\n"
            "Shuru karein — aapka naam kya hai?"
        )

    step = farmer.get("onboarding_step", 0)

    # ── STEP 0 → 1: Save name ───────────────────────────────────────────
    if step == 0:
        name = message.strip()[:50] or "Kisan bhai"
        update_farmer(phone, name=name)
        advance_step(phone, 1)
        return (
            f"Namaste {name} ji! \U0001F64F\n\n"
            "Aap kis bhasha mein baat karna chahte hain?\n\n"
            "  1 \u2014 Hindi\n"
            "  2 \u2014 English\n"
            "  3 \u2014 Punjabi\n\n"
            "1, 2, ya 3 reply karein."
        )

    # ── STEP 1 → 2: Save language ───────────────────────────────────────
    if step == 1:
        lang = LANGUAGES.get(low, "hi")
        lang_name = LANG_NAMES.get(lang, "Hindi")
        update_farmer(phone, language=lang)
        advance_step(phone, 2)
        return f"  {lang_name} set! \u2705\n\nAap mainly kaun si fasal ugaate hain?"

    # ── STEP 2 → 3: Save crop ───────────────────────────────────────────
    if step == 2:
        crop = message.strip()[:50] or "fasal"
        update_farmer(phone, crop=crop)
        advance_step(phone, 3)
        return (
            f"  {crop} note kar liya! \U0001F33E\n\n"
            "Ab apne gaon ya nearest town/district ka naam bataiye.\n"
            "Main aapke liye local mausam aur mandi bhav laaunga."
        )

    # ── STEP 3 → 4: Save location, show weather + mandi ────────────────
    if step == 3:
        district = message.strip()[:60] or ""
        if not district:
            return "Kripya apne gaon ya district ka naam bataiye."

        crop = farmer.get("crop", "fasal")
        lat, lon, found = await geocode(district)

        if not found:
            return (
                f"  '{district}' nahi mila. \n\n"
                "Kripya nearest bade shahar ya district ka naam bataiye.\n"
                "Jaise: 'Karnal', 'Nashik', 'Ludhiana', 'Jaipur'"
            )

        # Persist location and mark onboarded
        update_farmer(
            phone,
            district=district,
            lat=lat,
            lon=lon,
            onboarding_step=4,
            onboarded=1,
        )

        # Fetch weather and mandi (outside DB context — correct)
        weather = await fetch_weather(lat, lon, district)
        mandi_records = await fetch_mandi(crop)
        mandi_text = format_mandi(mandi_records, limit=3)

        return (
            f"  {district} set! \u2705\n\n"
            f"{weather}\n\n"
            f"  {crop} mandi prices:\n{mandi_text}\n\n"
            "Sab kuch tayar hai! Ab kuch bhi poochhen. \U0001F33E"
        )

    # ── FULLY ONBOARDED (step == 4) ──────────────────────────────────────

    name = farmer.get("name") or "bhai"
    crop = farmer.get("crop") or "fasal"

    if _is_help(low):
        resp = (
            "  \U0001F33E KisanAI kya kar sakta hai:\n\n"
            "  Poochhen: keede, khaad, sinchai, bimari\n"
            "  'mandi'   \u2014 aaj ka bhav\n"
            "  'mausam'  \u2014 mausam ka haal\n"
            "  'yojna'   \u2014 sarkari schemes\n"
        )
        touch_farmer(phone)
        return resp

    if _is_greeting(low):
        resp = f"\U0001F33E Namaste {name} ji!\nAaj {crop} ke baare mein kya jaanna hai?"
        touch_farmer(phone)
        return resp

    if _is_mandi(low):
        crop_q = _extract_crop(low, crop)
        mandi_records = await fetch_mandi(crop_q, farmer.get("state", ""))
        resp = f"  {crop_q} mandi prices:\n\n{format_mandi(mandi_records)}"
        save_message(phone, "bot", resp)
        touch_farmer(phone)
        return resp

    if _is_weather(low):
        resp = await fetch_weather(
            farmer.get("lat", 28.6139),
            farmer.get("lon", 77.2090),
            farmer.get("district", ""),
        )
        save_message(phone, "bot", resp)
        touch_farmer(phone)
        return resp

    if _is_subsidy(low):
        resp = get_subsidy_info(message)
        save_message(phone, "bot", resp)
        touch_farmer(phone)
        return resp

    # Default — AI advisory
    save_message(phone, "user", message)
    advice = await get_ai_advice(message, farmer, phone)
    resp = f"  \U0001F331 KisanAI Salah:\n\n{advice}"
    save_message(phone, "bot", resp)
    touch_farmer(phone)
    return resp