"""
main.py — KisanAI FastAPI application
Webhooks, REST endpoints, and startup.

Run locally:  uvicorn main:app --reload --port 8000
Swagger UI:   http://localhost:8000/docs
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_VERIFY_TOKEN
from database import init_db, get_all_farmers, get_stats, delete_farmer, get_all_onboarded
from router import process_message
from services import fetch_weather_raw

import httpx

app = FastAPI(
    title="KisanAI",
    description="Autonomous WhatsApp-native farm advisor for Indian farmers",
    version="2.1.0",
)


# ══════════════════════════════════════════════════
# WhatsApp messenger
# ══════════════════════════════════════════════════

async def send_whatsapp(to: str, text: str) -> None:
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[KisanAI -> {to}]: {text[:100]}...")
        return
    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                print(f"[WA ERROR {r.status_code}]: {r.text[:200]}")
    except Exception as e:
        print(f"[WA EXCEPTION]: {e}")


# ══════════════════════════════════════════════════
# WhatsApp Webhooks
# ══════════════════════════════════════════════════

@app.get("/webhook", tags=["WhatsApp"])
async def webhook_verify(request: Request):
    """Meta webhook verification challenge."""
    params = dict(request.query_params)
    if params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
        return int(params.get("hub.challenge", 0))
    return JSONResponse({"error": "invalid verify token"}, status_code=403)


@app.post("/webhook", tags=["WhatsApp"])
async def webhook_receive(request: Request):
    """Receive and process incoming WhatsApp messages."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue
                phone = msg.get("from", "").strip()
                text = msg.get("text", {}).get("body", "").strip()
                if phone and text:
                    resp = await process_message(phone, text)
                    await send_whatsapp(phone, resp)

    return JSONResponse({"status": "ok"})


# ══════════════════════════════════════════════════
# Test & Dev Endpoints
# ══════════════════════════════════════════════════

class MessageRequest(BaseModel):
    phone: str
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "phone": "911234567890",
                "message": "hello",
            }
        }


@app.post(
    "/api/message",
    tags=["Testing"],
    summary="Simulate a farmer message",
    description=(
        "Use this in Swagger to test the full conversation flow.\n\n"
        "**Test sequence:**\n"
        "1. `hello` → welcome + name ask\n"
        "2. `Ramesh` → language choice\n"
        "3. `1` → crop question\n"
        "4. `Gehun` → district question\n"
        "5. `Karnal` → weather + mandi\n"
        "6. `aaj mausam kaisa` → weather\n"
        "7. `gehu ka bhav` → mandi prices\n"
        "8. `PM Kisan yojna` → subsidy info\n"
    ),
)
async def api_message(msg: MessageRequest):
    response = await process_message(msg.phone, msg.message)
    return {"phone": msg.phone, "message": msg.message, "response": response}


# ══════════════════════════════════════════════════
# Admin & Analytics Endpoints
# ══════════════════════════════════════════════════

@app.get("/api/stats", tags=["Admin"], summary="Platform stats")
async def api_stats():
    """Total farmers, onboarded count, total messages."""
    return get_stats()


@app.get("/api/farmers", tags=["Admin"], summary="All farmer profiles")
async def api_farmers():
    """Full list of all farmers with their profiles."""
    return get_all_farmers()


@app.delete(
    "/api/farmer/{phone}",
    tags=["Admin"],
    summary="Delete a farmer (dev only)",
    description="Removes farmer + all their conversations. Use during Swagger testing to reset a test phone.",
)
async def api_delete_farmer(phone: str):
    delete_farmer(phone)
    return {"deleted": phone}


# ══════════════════════════════════════════════════
# Proactive Alerts
# ══════════════════════════════════════════════════

@app.get(
    "/api/daily-alerts",
    tags=["Alerts"],
    summary="Send proactive weather alerts",
    description=(
        "Loops through all onboarded farmers, checks weather, and sends:\n"
        "- Rain alert if precipitation probability > 60%\n"
        "- Heat alert if temperature > 38°C\n\n"
        "Call this via cron-job.org every morning at 7am IST."
    ),
)
async def daily_alerts():
    sent = []
    skipped = []

    farmers = get_all_onboarded()

    for f in farmers:
        phone = f["phone"]
        name = f.get("name") or "bhai"
        district = f.get("district") or "aapke ilaake"
        lat = f.get("lat") or 28.6139
        lon = f.get("lon") or 77.2090

        weather = await fetch_weather_raw(lat, lon)
        if not weather:
            skipped.append({"phone": phone, "name": name, "reason": "weather unavailable"})
            continue

        rain = weather.get("rain_prob", 0)
        temp = weather.get("temp")

        alerts = []
        if rain > 60:
            alerts.append(
                f"\u26a0\ufe0f {name} bhai \u2014 agle 24 ghante mein {district} mein "
                "baarish ho sakti hai. Sinchai mat karo aur fasal dhako."
            )
        try:
            if temp is not None and float(temp) > 38:
                alerts.append(
                    f"\U0001F321\ufe0f {name} bhai \u2014 aaj {district} mein bahut garmi hai. "
                    "Sinchai sirf subah ya shaam ko karein."
                )
        except (ValueError, TypeError):
            pass

        if not alerts:
            skipped.append({"phone": phone, "name": name, "reason": "no alert needed"})
            continue

        await send_whatsapp(phone, "\n\n".join(alerts))
        sent.append({"phone": phone, "name": name, "alert_count": len(alerts)})

    return {
        "total_farmers": len(farmers),
        "alerts_sent": len(sent),
        "skipped": len(skipped),
        "details": {"sent": sent, "skipped": skipped},
    }


# ══════════════════════════════════════════════════
# Startup
# ══════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    init_db()
    print("KisanAI v2.1 started \U0001F33E")


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000)