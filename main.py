import os
import json
import hmac
import hashlib
import base64
import uuid
import threading
from typing import Optional

import requests
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="ESP32 LINE Watering Cloud")

# =========================================================
# ENVIRONMENT VARIABLES (ตั้งค่าใน Render Environment)
# =========================================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
DEVICE_KEY = os.getenv("DEVICE_KEY", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()  # optional fallback recipient

LINE_API_REPLY = "https://api.line.me/v2/bot/message/reply"
LINE_API_PUSH = "https://api.line.me/v2/bot/message/push"

# คำสั่งที่รอ ESP32 มารับ มีหนึ่งคำสั่งต่อครั้ง
_lock = threading.Lock()
_pending_command = None
_command_users = {}
_last_report_user_id = None


# =========================================================
# LINE API HELPERS
# =========================================================
def line_headers():
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN")
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def line_reply(reply_token: str, text: str):
    if not reply_token:
        return
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    response = requests.post(
        LINE_API_REPLY, headers=line_headers(), json=payload, timeout=15
    )
    if not response.ok:
        print("LINE reply error:", response.status_code, response.text)


def line_push(user_id: Optional[str], text: str):
    target = user_id or LINE_USER_ID
    if not target:
        print("LINE push skipped: no user ID (set LINE_USER_ID or message the bot first)")
        return False
    payload = {
        "to": target,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    response = requests.post(
        LINE_API_PUSH, headers=line_headers(), json=payload, timeout=15
    )
    if not response.ok:
        print("LINE push error:", response.status_code, response.text)
        return False
    return True


def verify_line_signature(body: bytes, signature: Optional[str]) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# =========================================================
# COMMAND QUEUE
# =========================================================
def queue_device_command(command: str, user_id: Optional[str]):
    global _pending_command, _last_report_user_id

    with _lock:
        if _pending_command is not None:
            return None

        command_id = str(uuid.uuid4())
        _pending_command = {
            "command": command,
            "command_id": command_id,
        }
        if user_id:
            _command_users[command_id] = user_id
            _last_report_user_id = user_id
        return command_id


def finish_device_command(command_id: str):
    global _pending_command
    with _lock:
        if (
            _pending_command is not None
            and _pending_command.get("command_id") == command_id
        ):
            _pending_command = None


# =========================================================
# HEALTH CHECK
# =========================================================
@app.get("/")
def root():
    return {
        "status": "online",
        "service": "ESP32 LINE Watering Cloud",
        "webhook": "/line/webhook",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# =========================================================
# LINE WEBHOOK
# =========================================================
@app.post("/line/webhook")
async def line_webhook(request: Request,
                       x_line_signature: Optional[str] = Header(default=None)):
    body = await request.body()

    if not verify_line_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        text = message.get("text", "").strip()
        reply_token = event.get("replyToken", "")
        source = event.get("source", {})
        user_id = source.get("userId")

        # คำสั่งเปิดระบบ
        if text == "เปิด":
            command_id = queue_device_command("OPEN", user_id)
            if command_id:
                line_reply(
                    reply_token,
                    "รับคำสั่งเปิดระบบแล้ว กำลังตรวจสอบความชื้นดินและสั่งงานรีเลย์"
                )
            else:
                line_reply(
                    reply_token,
                    "ขณะนี้มีคำสั่งค้างอยู่ กรุณารอสักครู่แล้วลองใหม่"
                )

        # คำสั่งตรวจสอบความชื้น
        elif text in ("เช็คความชื้น", "ตรวจสอบความชื้น", "ความชื้น"):
            command_id = queue_device_command("CHECK_HUMIDITY", user_id)
            if command_id:
                line_reply(reply_token, "กำลังอ่านค่าความชื้นดินจาก ESP32...")
            else:
                line_reply(
                    reply_token,
                    "ขณะนี้มีคำสั่งค้างอยู่ กรุณารอสักครู่แล้วลองใหม่"
                )

        else:
            line_reply(
                reply_token,
                "คำสั่งที่รองรับ:\nเปิด\nเช็คความชื้น"
            )

    return {"status": "ok"}


# =========================================================
# ESP32: GET PENDING COMMAND
# Header: X-Device-Key
# =========================================================
@app.get("/device/command")
def get_device_command(x_device_key: Optional[str] = Header(default=None)):
    if not DEVICE_KEY or x_device_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized device")

    with _lock:
        if _pending_command is None:
            return {"command": None}
        # ส่งคำสั่งเดิมซ้ำจนกว่า ESP32 จะส่งผลกลับมาพร้อม command_id
        return dict(_pending_command)


# =========================================================
# ESP32: POST COMMAND RESULT
# =========================================================
@app.post("/device/result")
def device_result(payload: dict,
                  x_device_key: Optional[str] = Header(default=None)):
    if not DEVICE_KEY or x_device_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized device")

    command_id = str(payload.get("command_id", "")).strip()
    if not command_id:
        raise HTTPException(status_code=400, detail="Missing command_id")

    with _lock:
        user_id = _command_users.get(command_id)

    status = str(payload.get("status", "OK")).upper()
    command = None
    with _lock:
        if _pending_command and _pending_command.get("command_id") == command_id:
            command = _pending_command.get("command")

    if command == "CHECK_HUMIDITY":
        humidity = int(payload.get("humidity", 0))
        raw = payload.get("raw", "-")
        relay1 = str(payload.get("relay1", "OFF")).upper()
        relay2 = str(payload.get("relay2", "OFF")).upper()
        mode = str(payload.get("mode", "AUTO"))

        if humidity <= 30:
            soil_status = "ดินแห้ง"
        elif humidity <= 50:
            soil_status = "ความชื้นปานกลาง"
        else:
            soil_status = "ดินชื้นเพียงพอแล้ว"

        message = (
            "ผลการตรวจสอบความชื้นดิน\n"
            f"ความชื้นดิน: {humidity}%\n"
            f"สถานะ: {soil_status}\n"
            f"ค่าเซนเซอร์: {raw}\n"
            f"วาล์ว 1: {'เปิด' if relay1 == 'ON' else 'ปิด'}\n"
            f"วาล์ว 2: {'เปิด' if relay2 == 'ON' else 'ปิด'}\n"
            f"โหมด: {mode}"
        )
        line_push(user_id, message)

    elif command == "OPEN":
        if status == "REJECTED":
            reason = str(payload.get("reason", ""))
            humidity = int(payload.get("humidity", 0))

            if reason == "SOIL_WET" or humidity > 50:
                message = (
                    "ไม่สามารถเปิดระบบรดน้ำได้\n"
                    "สถานะดินตอนนี้ชื้นเพียงพอแล้ว\n"
                    f"ความชื้นดิน: {humidity}%"
                )
            elif reason == "FLOW_LOCKOUT":
                message = (
                    "ไม่สามารถเปิดระบบรดน้ำได้\n"
                    "ระบบตรวจพบข้อผิดพลาดจาก Flow Sensor กรุณาตรวจสอบก่อน"
                )
            else:
                message = "ไม่สามารถเปิดระบบรดน้ำได้ กรุณาตรวจสอบอุปกรณ์"
            line_push(user_id, message)

        elif status == "OPENED":
            # ไม่ส่งรายงานซ้ำตรงนี้ เพราะ /device/report จะส่งรายงานรูปแบบเต็ม
            print("OPEN command accepted by ESP32")

    finish_device_command(command_id)
    return {"status": "received", "command_id": command_id}


# =========================================================
# ESP32: POST SYSTEM REPORT
# Header: X-Device-Key
# =========================================================
@app.post("/device/report")
def device_report(payload: dict,
                  x_device_key: Optional[str] = Header(default=None)):
    if not DEVICE_KEY or x_device_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized device")

    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="Missing message")

    with _lock:
        user_id = _last_report_user_id

    sent = line_push(user_id, message)
    if not sent:
        # ถ้าไม่มีผู้ใช้ที่ส่งข้อความถึงบอท ให้ลองส่งไปยัง LINE_USER_ID
        if LINE_USER_ID and LINE_USER_ID != user_id:
            sent = line_push(LINE_USER_ID, message)

    return {"status": "sent" if sent else "not_sent"}
