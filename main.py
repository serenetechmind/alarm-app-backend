"""Alarm API: lets other apps create alarms on a user's phone.

Flow:
  1. Android app calls POST /v1/device/register -> gets a device_token and a 6-digit pair_code.
  2. A developer registers (POST /v1/developers) and gets an api_key.
  3. User types the pair_code into the developer's app; the developer calls POST /v1/connections.
  4. Developer calls POST /v1/alarms -> server stores it and pushes it to the phone via FCM.
  5. Phone reports fired/snoozed/dismissed -> POST /v1/device/events -> signed webhooks to the developer.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Annotated, Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "alarmapi.db")
app = FastAPI(title="Alarm API", version="0.1.0")

# ---------- database ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS developers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_name TEXT NOT NULL,
  api_key TEXT UNIQUE NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_token TEXT UNIQUE NOT NULL,
  fcm_token TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'UTC',
  pair_code TEXT UNIQUE NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, developer_id INTEGER NOT NULL,
  device_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
  UNIQUE(developer_id, device_id));
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id INTEGER NOT NULL,
  time TEXT NOT NULL, repeat_days TEXT NOT NULL DEFAULT '[]', label TEXT NOT NULL DEFAULT '',
  snooze_minutes INTEGER NOT NULL DEFAULT 10, enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, developer_id INTEGER NOT NULL,
  url TEXT NOT NULL, secret TEXT NOT NULL, events TEXT NOT NULL);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def startup():
    with db() as c:
        c.executescript(SCHEMA)
    init_firebase()


# ---------- push (FCM) ----------

firebase_ready = False


def init_firebase():
    """Real pushes need Firebase credentials, given either way:
      FIREBASE_CREDENTIALS_JSON = the whole service-account JSON (easiest on hosting sites), or
      GOOGLE_APPLICATION_CREDENTIALS = path to the JSON file.
    Without either, pushes are only printed (dry run), so the API still works for testing."""
    global firebase_ready
    raw = os.getenv("FIREBASE_CREDENTIALS_JSON")
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw and not (path and os.path.exists(path)):
        return
    import firebase_admin
    from firebase_admin import credentials

    cred = credentials.Certificate(json.loads(raw)) if raw else credentials.Certificate(path)
    firebase_admin.initialize_app(cred)
    firebase_ready = True


def push(fcm_token: str, msg_type: str, payload: dict):
    data = {"type": msg_type, "payload": json.dumps(payload)}
    if not firebase_ready:
        print(f"[DRY RUN] FCM -> {fcm_token[:12]}...: {data}")
        return
    from firebase_admin import messaging

    try:
        messaging.send(
            messaging.Message(
                token=fcm_token,
                data=data,
                android=messaging.AndroidConfig(priority="high"),  # wakes the phone from Doze
            )
        )
    except Exception as e:  # a bad or expired token must not break the API call
        print(f"[FCM failed] {e}")


# ---------- auth ----------

def current_developer(authorization: str = Header(...)):
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise HTTPException(401, "Use header 'Authorization: Bearer <api_key>'")
    with db() as c:
        dev = c.execute("SELECT * FROM developers WHERE api_key=?", (key,)).fetchone()
    if not dev:
        raise HTTPException(401, "Invalid API key")
    return dict(dev)


def current_device(x_device_token: str = Header(...)):
    with db() as c:
        dev = c.execute("SELECT * FROM devices WHERE device_token=?", (x_device_token,)).fetchone()
    if not dev:
        raise HTTPException(401, "Invalid device token")
    return dict(dev)


# ---------- models ----------

Day = Annotated[int, Field(ge=1, le=7)]  # 1 = Sunday ... 7 = Saturday


class DeveloperIn(BaseModel):
    app_name: str = Field(min_length=2, max_length=80)


class DeviceIn(BaseModel):
    fcm_token: str
    timezone: str = "UTC"


class FcmTokenIn(BaseModel):
    fcm_token: str


class ConnectionIn(BaseModel):
    pair_code: str


class AlarmIn(BaseModel):
    connection_id: int
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="24h HH:MM, in the user's timezone")
    repeat_days: list[Day] = []
    label: str = Field("", max_length=80)
    snooze_minutes: int = Field(10, ge=1, le=60)


class AlarmPatch(BaseModel):
    time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    repeat_days: Optional[list[Day]] = None
    label: Optional[str] = Field(None, max_length=80)
    snooze_minutes: Optional[int] = Field(None, ge=1, le=60)
    enabled: Optional[bool] = None


class WebhookIn(BaseModel):
    url: str
    events: list[str] = ["alarm.fired", "alarm.snoozed", "alarm.dismissed"]


class DeviceEventIn(BaseModel):
    alarm_id: int
    type: str = Field(pattern=r"^(fired|snoozed|dismissed)$")


# ---------- helpers ----------

def alarm_out(row, app_name: Optional[str] = None) -> dict:
    h, m = row["time"].split(":")
    out = {
        "id": row["id"],
        "connection_id": row["connection_id"],
        "time": row["time"],
        "hour": int(h),
        "minute": int(m),
        "repeat_days": json.loads(row["repeat_days"]),
        "label": row["label"],
        "snooze_minutes": row["snooze_minutes"],
        "enabled": bool(row["enabled"]),
    }
    if app_name:
        out["source_app"] = app_name
    return out


def owned_connection(c, developer_id: int, connection_id: int):
    row = c.execute(
        """SELECT cn.*, d.fcm_token, dv.app_name FROM connections cn
           JOIN devices d ON d.id = cn.device_id
           JOIN developers dv ON dv.id = cn.developer_id
           WHERE cn.id=? AND cn.developer_id=?""",
        (connection_id, developer_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Connection not found for this developer")
    return row


def owned_alarm(c, developer_id: int, alarm_id: int):
    row = c.execute(
        """SELECT a.*, d.fcm_token, dv.app_name FROM alarms a
           JOIN connections cn ON cn.id = a.connection_id
           JOIN devices d ON d.id = cn.device_id
           JOIN developers dv ON dv.id = cn.developer_id
           WHERE a.id=? AND cn.developer_id=?""",
        (alarm_id, developer_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Alarm not found")
    return row


def deliver_webhooks(developer_id: int, event: str, data: dict):
    with db() as c:
        hooks = c.execute("SELECT * FROM webhooks WHERE developer_id=?", (developer_id,)).fetchall()
    body = json.dumps({"event": event, "data": data, "timestamp": int(time.time())})
    for h in hooks:
        if event not in json.loads(h["events"]):
            continue
        sig = hmac.new(h["secret"].encode(), body.encode(), hashlib.sha256).hexdigest()
        try:
            httpx.post(
                h["url"], content=body, timeout=5,
                headers={"Content-Type": "application/json", "X-Alarm-Signature": sig},
            )
        except Exception as e:  # a dead webhook must never break the API
            print(f"[webhook failed] {h['url']}: {e}")


# ---------- developer endpoints ----------

@app.post("/v1/developers", status_code=201)
def create_developer(body: DeveloperIn):
    key = "ak_" + secrets.token_urlsafe(24)
    with db() as c:
        cur = c.execute(
            "INSERT INTO developers (app_name, api_key, created_at) VALUES (?,?,?)",
            (body.app_name, key, int(time.time())),
        )
    return {"id": cur.lastrowid, "app_name": body.app_name, "api_key": key}


@app.post("/v1/connections", status_code=201)
def create_connection(body: ConnectionIn, dev=Depends(current_developer)):
    with db() as c:
        device = c.execute("SELECT * FROM devices WHERE pair_code=?", (body.pair_code,)).fetchone()
        if not device:
            raise HTTPException(404, "Unknown pair code")
        c.execute(
            "INSERT OR IGNORE INTO connections (developer_id, device_id, created_at) VALUES (?,?,?)",
            (dev["id"], device["id"], int(time.time())),
        )
        conn = c.execute(
            "SELECT id FROM connections WHERE developer_id=? AND device_id=?", (dev["id"], device["id"])
        ).fetchone()
    return {"connection_id": conn["id"]}


@app.post("/v1/alarms", status_code=201)
def create_alarm(body: AlarmIn, dev=Depends(current_developer)):
    with db() as c:
        conn = owned_connection(c, dev["id"], body.connection_id)
        cur = c.execute(
            """INSERT INTO alarms (connection_id, time, repeat_days, label, snooze_minutes, created_at)
               VALUES (?,?,?,?,?,?)""",
            (body.connection_id, body.time, json.dumps(sorted(set(body.repeat_days))),
             body.label, body.snooze_minutes, int(time.time())),
        )
        row = c.execute("SELECT * FROM alarms WHERE id=?", (cur.lastrowid,)).fetchone()
    out = alarm_out(row, conn["app_name"])
    push(conn["fcm_token"], "alarm.upsert", out)
    return out


@app.get("/v1/alarms")
def list_alarms(connection_id: int, dev=Depends(current_developer)):
    with db() as c:
        conn = owned_connection(c, dev["id"], connection_id)
        rows = c.execute("SELECT * FROM alarms WHERE connection_id=?", (connection_id,)).fetchall()
    return [alarm_out(r, conn["app_name"]) for r in rows]


@app.patch("/v1/alarms/{alarm_id}")
def update_alarm(alarm_id: int, body: AlarmPatch, dev=Depends(current_developer)):
    with db() as c:
        existing = owned_alarm(c, dev["id"], alarm_id)
        changes = body.model_dump(exclude_none=True)
        if "repeat_days" in changes:
            changes["repeat_days"] = json.dumps(sorted(set(changes["repeat_days"])))
        if "enabled" in changes:
            changes["enabled"] = int(changes["enabled"])
        if changes:
            sets = ", ".join(f"{k}=?" for k in changes)
            c.execute(f"UPDATE alarms SET {sets} WHERE id=?", (*changes.values(), alarm_id))
        row = c.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    out = alarm_out(row, existing["app_name"])
    push(existing["fcm_token"], "alarm.upsert", out)
    return out


@app.delete("/v1/alarms/{alarm_id}", status_code=204)
def delete_alarm(alarm_id: int, dev=Depends(current_developer)):
    with db() as c:
        existing = owned_alarm(c, dev["id"], alarm_id)
        c.execute("DELETE FROM alarms WHERE id=?", (alarm_id,))
    push(existing["fcm_token"], "alarm.delete", {"id": alarm_id})


@app.post("/v1/webhooks", status_code=201)
def create_webhook(body: WebhookIn, dev=Depends(current_developer)):
    secret = "whsec_" + secrets.token_urlsafe(24)
    with db() as c:
        cur = c.execute(
            "INSERT INTO webhooks (developer_id, url, secret, events) VALUES (?,?,?,?)",
            (dev["id"], body.url, secret, json.dumps(body.events)),
        )
    # The secret is shown once: use it to verify the X-Alarm-Signature header (HMAC-SHA256 of the body).
    return {"id": cur.lastrowid, "url": body.url, "events": body.events, "secret": secret}


# ---------- device (Android app) endpoints ----------

@app.post("/v1/device/register", status_code=201)
def register_device(body: DeviceIn):
    token = "dt_" + secrets.token_urlsafe(24)
    with db() as c:
        while True:
            code = f"{secrets.randbelow(1_000_000):06d}"
            if not c.execute("SELECT 1 FROM devices WHERE pair_code=?", (code,)).fetchone():
                break
        c.execute(
            "INSERT INTO devices (device_token, fcm_token, timezone, pair_code, created_at) VALUES (?,?,?,?,?)",
            (token, body.fcm_token, body.timezone, code, int(time.time())),
        )
    return {"device_token": token, "pair_code": code}


@app.put("/v1/device/fcm-token", status_code=204)
def update_fcm_token(body: FcmTokenIn, device=Depends(current_device)):
    with db() as c:
        c.execute("UPDATE devices SET fcm_token=? WHERE id=?", (body.fcm_token, device["id"]))


@app.get("/v1/device/alarms")
def device_alarms(device=Depends(current_device)):
    """Full sync: the app calls this on launch so it never misses a push."""
    with db() as c:
        rows = c.execute(
            """SELECT a.*, dv.app_name FROM alarms a
               JOIN connections cn ON cn.id = a.connection_id
               JOIN developers dv ON dv.id = cn.developer_id
               WHERE cn.device_id=?""",
            (device["id"],),
        ).fetchall()
    return [alarm_out(r, r["app_name"]) for r in rows]


@app.get("/v1/device/connections")
def device_connections(device=Depends(current_device)):
    with db() as c:
        rows = c.execute(
            """SELECT cn.id, dv.app_name, cn.created_at FROM connections cn
               JOIN developers dv ON dv.id = cn.developer_id WHERE cn.device_id=?""",
            (device["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/v1/device/connections/{connection_id}", status_code=204)
def revoke_connection(connection_id: int, device=Depends(current_device)):
    with db() as c:
        row = c.execute(
            "SELECT id FROM connections WHERE id=? AND device_id=?", (connection_id, device["id"])
        ).fetchone()
        if not row:
            raise HTTPException(404, "Connection not found")
        c.execute("DELETE FROM alarms WHERE connection_id=?", (connection_id,))
        c.execute("DELETE FROM connections WHERE id=?", (connection_id,))


@app.post("/v1/device/events", status_code=202)
def device_event(body: DeviceEventIn, tasks: BackgroundTasks, device=Depends(current_device)):
    with db() as c:
        row = c.execute(
            """SELECT a.*, cn.developer_id FROM alarms a
               JOIN connections cn ON cn.id = a.connection_id
               WHERE a.id=? AND cn.device_id=?""",
            (body.alarm_id, device["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Alarm not found")
    if body.type == "fired" and json.loads(row["repeat_days"]) == []:
        with db() as c:  # one-time alarms switch themselves off after ringing
            c.execute("UPDATE alarms SET enabled=0 WHERE id=?", (body.alarm_id,))
    tasks.add_task(
        deliver_webhooks, row["developer_id"], f"alarm.{body.type}",
        {"alarm_id": body.alarm_id, "connection_id": row["connection_id"], "label": row["label"]},
    )
    return {"status": "accepted"}


@app.get("/")
def health():
    return {"status": "ok", "fcm": "live" if firebase_ready else "dry-run"}
