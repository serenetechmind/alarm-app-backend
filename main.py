"""Alarm API (Firestore edition): lets other apps create alarms on a user's phone.

Flow:
  1. Android app calls POST /v1/device/register -> gets a device_token and a 6-digit pair_code.
  2. A developer registers (POST /v1/developers) and gets an api_key (shown once).
  3. The user shares the pair_code with the developer's app; the developer calls POST /v1/connections.
  4. Developer calls POST /v1/alarms -> stored in Firestore and pushed to the phone via FCM.
  5. Phone reports fired/snoozed/dismissed -> POST /v1/device/events -> signed webhooks to the developer.

Data lives in Firestore (collections: counters, developers, devices, connections, alarms, webhooks).
Ids stay small integers (kept in a counter document) so the Android app and the API contract don't change.
"""
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import time
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from google.cloud.firestore_v1 import Query
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

app = FastAPI(title="Alarm API", version="0.3.0")

# Authorization uses a header (no cookies), so allowing any origin is safe for browser testing.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

firebase_ready = False
db = None  # Firestore client, set at startup


# ---------- Firebase (Firestore + FCM) ----------

def init_firebase():
    """Credentials come from FIREBASE_CREDENTIALS_JSON (the whole service-account JSON, easiest on
    hosting sites) or GOOGLE_APPLICATION_CREDENTIALS (path to the JSON file)."""
    global firebase_ready, db
    raw = os.getenv("FIREBASE_CREDENTIALS_JSON")
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw and not (path and os.path.exists(path)):
        print("[WARN] No Firebase credentials found: the API cannot store data or send pushes.")
        return
    import firebase_admin
    from firebase_admin import credentials, firestore

    cred = credentials.Certificate(json.loads(raw)) if raw else credentials.Certificate(path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    firebase_ready = True


@app.on_event("startup")
def startup():
    init_firebase()


def fs():
    if db is None:
        raise HTTPException(503, "Database not configured: set FIREBASE_CREDENTIALS_JSON on the server")
    return db


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


# ---------- Firestore helpers ----------

def now() -> int:
    return int(time.time())


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def next_id(name: str) -> int:
    """Small incrementing integer ids, kept in counters/<name> and bumped inside a transaction."""
    from firebase_admin import firestore

    client = fs()
    ref = client.collection("counters").document(name)

    @firestore.transactional
    def bump(transaction):
        snap = ref.get(transaction=transaction)
        n = (snap.to_dict() or {}).get("value", 0) + 1
        transaction.set(ref, {"value": n})
        return n

    return bump(client.transaction())


def get_doc(collection: str, doc_id: int) -> Optional[dict]:
    snap = fs().collection(collection).document(str(doc_id)).get()
    return snap.to_dict() if snap.exists else None


def query(collection: str, **equals):
    ref = fs().collection(collection)
    for field, value in equals.items():
        ref = ref.where(filter=FieldFilter(field, "==", value))
    return ref


def first(collection: str, **equals):
    for snap in query(collection, **equals).limit(1).stream():
        return {"id": int(snap.id), **snap.to_dict()}
    return None


# ---------- auth ----------

def current_developer(authorization: str = Header(...)):
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise HTTPException(401, "Use header 'Authorization: Bearer <api_key>'")
    dev = first("developers", key_hash=sha(key.strip()))
    if not dev:
        raise HTTPException(401, "Invalid API key")
    return dev


def current_device(x_device_token: str = Header(...)):
    device = first("devices", token_hash=sha(x_device_token.strip()))
    if not device:
        raise HTTPException(401, "Invalid device token")
    return device


# ---------- models ----------

Day = Annotated[int, Field(ge=1, le=7)]  # 1 = Sunday ... 7 = Saturday
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


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
    time: str = Field(pattern=TIME_PATTERN, description="24h HH:MM, in the user's timezone")
    repeat_days: list[Day] = []
    label: str = Field("", max_length=80)
    snooze_minutes: int = Field(10, ge=1, le=60)


class AlarmPatch(BaseModel):
    time: Optional[str] = Field(None, pattern=TIME_PATTERN)
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

def alarm_out(alarm_id: int, a: dict, app_name: Optional[str] = None) -> dict:
    h, m = a["time"].split(":")
    out = {
        "id": alarm_id,
        "connection_id": a["connection_id"],
        "time": a["time"],
        "hour": int(h),
        "minute": int(m),
        "repeat_days": a.get("repeat_days", []),
        "label": a.get("label", ""),
        "snooze_minutes": a.get("snooze_minutes", 10),
        "enabled": bool(a.get("enabled", True)),
    }
    if app_name:
        out["source_app"] = app_name
    return out


def owned_connection(dev: dict, connection_id: int) -> dict:
    conn = get_doc("connections", connection_id)
    if not conn or conn["developer_id"] != dev["id"]:
        raise HTTPException(404, "Connection not found for this developer")
    device = get_doc("devices", conn["device_id"]) or {}
    return {**conn, "id": connection_id, "fcm_token": device.get("fcm_token", "")}


def owned_alarm(dev: dict, alarm_id: int):
    alarm = get_doc("alarms", alarm_id)
    if not alarm:
        raise HTTPException(404, "Alarm not found")
    try:
        conn = owned_connection(dev, alarm["connection_id"])
    except HTTPException:
        raise HTTPException(404, "Alarm not found")
    return alarm, conn


def log_event(developer_id: int, event: str, data: dict):
    """Keeps an activity feed per developer so the dashboard can show events without a webhook."""
    try:
        fs().collection("developers").document(str(developer_id)).collection("events").add(
            {"event": event, "data": data, "ts": int(time.time() * 1000)}
        )
    except Exception as e:
        print(f"[event log failed] {e}")


def deliver_webhooks(developer_id: int, event: str, data: dict):
    log_event(developer_id, event, data)
    hooks = [h.to_dict() for h in query("webhooks", developer_id=developer_id).stream()]
    body = json.dumps({"event": event, "data": data, "timestamp": now()})
    for h in hooks:
        if event not in h.get("events", []):
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
    dev_id = next_id("developers")
    fs().collection("developers").document(str(dev_id)).set(
        {"app_name": body.app_name, "key_hash": sha(key), "key_prefix": key[:8], "created_at": now()}
    )
    # Only a hash is stored, so this is the one and only time the key is shown.
    return {"id": dev_id, "app_name": body.app_name, "api_key": key}


@app.get("/v1/me")
def me(dev=Depends(current_developer)):
    return {"id": dev["id"], "app_name": dev["app_name"], "key_prefix": dev["key_prefix"]}


@app.post("/v1/connections", status_code=201)
def create_connection(body: ConnectionIn, dev=Depends(current_developer)):
    device = first("devices", pair_code=body.pair_code.strip())
    if not device:
        raise HTTPException(404, "Unknown pair code")
    existing = first("connections", developer_id=dev["id"], device_id=device["id"])
    if existing:
        return {"connection_id": existing["id"]}
    conn_id = next_id("connections")
    fs().collection("connections").document(str(conn_id)).set(
        {"developer_id": dev["id"], "device_id": device["id"], "created_at": now()}
    )
    return {"connection_id": conn_id}


@app.get("/v1/connections")
def list_connections(dev=Depends(current_developer)):
    return [
        {"connection_id": int(s.id), "created_at": s.to_dict().get("created_at")}
        for s in query("connections", developer_id=dev["id"]).stream()
    ]


@app.post("/v1/alarms", status_code=201)
def create_alarm(body: AlarmIn, dev=Depends(current_developer)):
    conn = owned_connection(dev, body.connection_id)
    alarm_id = next_id("alarms")
    alarm = {
        "connection_id": body.connection_id,
        "time": body.time,
        "repeat_days": sorted(set(body.repeat_days)),
        "label": body.label,
        "snooze_minutes": body.snooze_minutes,
        "enabled": True,
        "created_at": now(),
    }
    fs().collection("alarms").document(str(alarm_id)).set(alarm)
    out = alarm_out(alarm_id, alarm, dev["app_name"])
    push(conn["fcm_token"], "alarm.upsert", out)
    return out


@app.get("/v1/alarms")
def list_alarms(connection_id: int, dev=Depends(current_developer)):
    owned_connection(dev, connection_id)
    return [
        alarm_out(int(s.id), s.to_dict(), dev["app_name"])
        for s in query("alarms", connection_id=connection_id).stream()
    ]


@app.patch("/v1/alarms/{alarm_id}")
def update_alarm(alarm_id: int, body: AlarmPatch, dev=Depends(current_developer)):
    alarm, conn = owned_alarm(dev, alarm_id)
    changes = body.model_dump(exclude_none=True)
    if "repeat_days" in changes:
        changes["repeat_days"] = sorted(set(changes["repeat_days"]))
    if changes:
        fs().collection("alarms").document(str(alarm_id)).update(changes)
        alarm.update(changes)
    out = alarm_out(alarm_id, alarm, dev["app_name"])
    push(conn["fcm_token"], "alarm.upsert", out)
    return out


@app.delete("/v1/alarms/{alarm_id}", status_code=204)
def delete_alarm(alarm_id: int, dev=Depends(current_developer)):
    _, conn = owned_alarm(dev, alarm_id)
    fs().collection("alarms").document(str(alarm_id)).delete()
    push(conn["fcm_token"], "alarm.delete", {"id": alarm_id})


@app.post("/v1/webhooks", status_code=201)
def create_webhook(body: WebhookIn, dev=Depends(current_developer)):
    secret = "whsec_" + secrets.token_urlsafe(24)
    hook_id = next_id("webhooks")
    fs().collection("webhooks").document(str(hook_id)).set(
        {"developer_id": dev["id"], "url": body.url, "secret": secret, "events": body.events}
    )
    # The secret is shown once: use it to verify the X-Alarm-Signature header (HMAC-SHA256 of the body).
    return {"id": hook_id, "url": body.url, "events": body.events, "secret": secret}


@app.get("/v1/webhooks")
def list_webhooks(dev=Depends(current_developer)):
    return [
        {"id": int(s.id), "url": s.to_dict()["url"], "events": s.to_dict()["events"]}
        for s in query("webhooks", developer_id=dev["id"]).stream()
    ]


@app.delete("/v1/webhooks/{hook_id}", status_code=204)
def delete_webhook(hook_id: int, dev=Depends(current_developer)):
    hook = get_doc("webhooks", hook_id)
    if not hook or hook["developer_id"] != dev["id"]:
        raise HTTPException(404, "Webhook not found")
    fs().collection("webhooks").document(str(hook_id)).delete()


# ---------- device (Android app) endpoints ----------

@app.post("/v1/device/register", status_code=201)
def register_device(body: DeviceIn):
    token = "dt_" + secrets.token_urlsafe(24)
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not first("devices", pair_code=code):
            break
    device_id = next_id("devices")
    fs().collection("devices").document(str(device_id)).set(
        {
            "token_hash": sha(token),
            "fcm_token": body.fcm_token,
            "timezone": body.timezone,
            "pair_code": code,
            "created_at": now(),
        }
    )
    return {"device_token": token, "pair_code": code}


@app.put("/v1/device/fcm-token", status_code=204)
def update_fcm_token(body: FcmTokenIn, device=Depends(current_device)):
    fs().collection("devices").document(str(device["id"])).update({"fcm_token": body.fcm_token})


@app.get("/v1/device/alarms")
def device_alarms(device=Depends(current_device)):
    """Full sync: the app calls this on launch so it never misses a push."""
    out = []
    names: dict[int, str] = {}
    for c in query("connections", device_id=device["id"]).stream():
        dev_id = c.to_dict()["developer_id"]
        if dev_id not in names:
            names[dev_id] = (get_doc("developers", dev_id) or {}).get("app_name", "App")
        for a in query("alarms", connection_id=int(c.id)).stream():
            out.append(alarm_out(int(a.id), a.to_dict(), names[dev_id]))
    return out


@app.get("/v1/device/connections")
def device_connections(device=Depends(current_device)):
    out = []
    for c in query("connections", device_id=device["id"]).stream():
        d = c.to_dict()
        name = (get_doc("developers", d["developer_id"]) or {}).get("app_name", "App")
        out.append({"id": int(c.id), "app_name": name, "created_at": d.get("created_at")})
    return out


@app.delete("/v1/device/connections/{connection_id}", status_code=204)
def revoke_connection(connection_id: int, device=Depends(current_device)):
    conn = get_doc("connections", connection_id)
    if not conn or conn["device_id"] != device["id"]:
        raise HTTPException(404, "Connection not found")
    batch = fs().batch()
    for a in query("alarms", connection_id=connection_id).stream():
        batch.delete(a.reference)
    batch.delete(fs().collection("connections").document(str(connection_id)))
    batch.commit()


@app.post("/v1/device/events", status_code=202)
def device_event(body: DeviceEventIn, tasks: BackgroundTasks, device=Depends(current_device)):
    alarm = get_doc("alarms", body.alarm_id)
    conn = get_doc("connections", alarm["connection_id"]) if alarm else None
    if not alarm or not conn or conn["device_id"] != device["id"]:
        raise HTTPException(404, "Alarm not found")
    if body.type == "fired" and not alarm.get("repeat_days"):
        # one-time alarms switch themselves off after ringing
        fs().collection("alarms").document(str(body.alarm_id)).update({"enabled": False})
    tasks.add_task(
        deliver_webhooks, conn["developer_id"], f"alarm.{body.type}",
        {"alarm_id": body.alarm_id, "connection_id": alarm["connection_id"], "label": alarm.get("label", "")},
    )
    return {"status": "accepted"}


# ---------- "Connect with AlarmApp" (no codes to copy) ----------

CONNECT_TTL = 15 * 60  # seconds a connect link stays valid
ANDROID_SCHEME = os.getenv("ANDROID_SCHEME", "alarmapp")
ANDROID_PACKAGE = os.getenv("ANDROID_PACKAGE", "com.example.alarmapp")


class ConnectRequestIn(BaseModel):
    redirect_url: Optional[str] = Field(None, max_length=500)
    state: Optional[str] = Field(None, max_length=200)


def public_base(request: Request) -> str:
    env = os.getenv("PUBLIC_BASE_URL")
    if env:
        return env.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def add_query(url: str, params: dict) -> str:
    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True) + [(k, str(v)) for k, v in params.items()]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))


def load_request(rid: str) -> dict:
    snap = fs().collection("connect_requests").document(rid).get()
    if not snap.exists:
        raise HTTPException(404, "Unknown connect link")
    r = snap.to_dict()
    if r["status"] == "pending" and r["expires_at"] < now():
        r["status"] = "expired"
    return r


@app.post("/v1/connect/requests", status_code=201)
def create_connect_request(body: ConnectRequestIn, request: Request, dev=Depends(current_developer)):
    """Step 1 for a developer: get a link/QR to show the user. The user approves inside AlarmApp."""
    if body.redirect_url and not body.redirect_url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "redirect_url must start with http:// or https://")
    rid = secrets.token_urlsafe(16)
    t = now()
    fs().collection("connect_requests").document(rid).set(
        {
            "developer_id": dev["id"],
            "status": "pending",
            "redirect_url": body.redirect_url,
            "state": body.state,
            "connection_id": None,
            "created_at": t,
            "expires_at": t + CONNECT_TTL,
        }
    )
    base = public_base(request)
    return {
        "request_id": rid,
        "connect_url": f"{base}/c/{rid}",
        "qr_url": f"{base}/c/{rid}/qr.svg",
        "expires_in": CONNECT_TTL,
    }


@app.get("/v1/connect/requests/{rid}")
def get_connect_request(rid: str, dev=Depends(current_developer)):
    """Poll this (or use the connection.approved webhook) to learn when the user tapped Allow."""
    r = load_request(rid)
    if r["developer_id"] != dev["id"]:
        raise HTTPException(404, "Unknown connect link")
    return {"request_id": rid, "status": r["status"], "connection_id": r.get("connection_id"), "state": r.get("state")}


CONNECT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to AlarmApp</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0B0F1A;color:#F2F4FA;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:20px;box-sizing:border-box}
main{max-width:380px;text-align:center}
h1{font-size:24px;font-weight:400;margin:18px 0 8px}
p{color:#9BA6C4;margin:6px 0}
b{color:#FFB547;font-weight:500}
.btn{display:inline-block;margin-top:18px;background:#FFB547;color:#1C1200;font-weight:600;text-decoration:none;padding:14px 26px;border-radius:999px}
.qr{background:#fff;border-radius:16px;padding:8px;width:210px;height:210px;margin-top:14px}
.ok{color:#5AD19A}.err{color:#FF6B6B}
</style></head><body><main>
<svg width="72" height="72" viewBox="0 0 108 108" aria-hidden="true"><rect width="108" height="108" rx="30" fill="#141A2B"/><circle cx="34" cy="36" r="8" fill="#FFB547"/><circle cx="74" cy="36" r="8" fill="#FFB547"/><circle cx="54" cy="56" r="22" fill="none" stroke="#FFB547" stroke-width="5"/><path d="M54,43 L54,56 L63,62" fill="none" stroke="#FFB547" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg>
<h1><b>__APP__</b> wants to set alarms on your phone</h1>
<p>Approve it in the AlarmApp on your Android phone. You can disconnect it any time.</p>
<div id="phone"><a class="btn" href="__INTENT__">Open AlarmApp to approve</a><p style="font-size:13px">Nothing opened? Install AlarmApp first, then tap the button again.</p></div>
<div id="desktop" hidden><img class="qr" src="/c/__RID__/qr.svg" alt="QR code"><p>Scan this with your phone's camera to continue.</p></div>
<p id="status"></p>
</main>
<script>
const RID = __RID_JSON__, INTENT = __INTENT_JSON__;
const android = /Android/i.test(navigator.userAgent);
if (android) { setTimeout(function () { location.href = INTENT; }, 400); }
else { document.getElementById('phone').hidden = true; document.getElementById('desktop').hidden = false; }
const statusEl = document.getElementById('status');
async function poll() {
  try {
    const j = await (await fetch('/c/' + RID + '/status')).json();
    if (j.status === 'approved') {
      statusEl.innerHTML = '<span class="ok">Connected \u2713</span> You can go back to the app.';
      if (j.redirect_to) setTimeout(function () { location.href = j.redirect_to; }, 1200);
      return;
    }
    if (j.status === 'denied') { statusEl.innerHTML = '<span class="err">Request declined.</span>'; return; }
    if (j.status === 'expired' || j.detail) { statusEl.innerHTML = '<span class="err">This link expired.</span> Ask the app for a new one.'; return; }
  } catch (e) {}
  setTimeout(poll, 2000);
}
poll();
</script></body></html>"""


@app.get("/c/{rid}", response_class=HTMLResponse, include_in_schema=False)
def connect_page(rid: str):
    try:
        r = load_request(rid)
    except HTTPException:
        return HTMLResponse("<p style='font:16px system-ui;padding:24px'>This connect link is not valid. Ask the app for a new one.</p>", status_code=404)
    dev = get_doc("developers", r["developer_id"]) or {}
    intent = f"intent://connect/{rid}#Intent;scheme={ANDROID_SCHEME};package={ANDROID_PACKAGE};end"
    page = (
        CONNECT_PAGE.replace("__APP__", html.escape(dev.get("app_name", "An app")))
        .replace("__INTENT_JSON__", json.dumps(intent))
        .replace("__INTENT__", html.escape(intent, quote=True))
        .replace("__RID_JSON__", json.dumps(rid))
        .replace("__RID__", rid)
    )
    return HTMLResponse(page)


@app.get("/c/{rid}/status", include_in_schema=False)
def connect_status(rid: str):
    r = load_request(rid)
    out = {"status": r["status"]}
    if r["status"] == "approved" and r.get("redirect_url"):
        params = {"connection_id": r["connection_id"]}
        if r.get("state"):
            params["state"] = r["state"]
        out["redirect_to"] = add_query(r["redirect_url"], params)
    return out


@app.get("/c/{rid}/qr.svg", include_in_schema=False)
def connect_qr(rid: str, request: Request):
    load_request(rid)
    import segno

    buf = io.BytesIO()
    segno.make(f"{public_base(request)}/c/{rid}", error="m").save(
        buf, kind="svg", scale=6, border=2, dark="#0B0F1A", light="#FFFFFF"
    )
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.get("/v1/device/connect/{rid}")
def device_connect_info(rid: str, device=Depends(current_device)):
    r = load_request(rid)
    dev = get_doc("developers", r["developer_id"]) or {}
    return {"request_id": rid, "app_name": dev.get("app_name", "App"), "status": r["status"]}


@app.post("/v1/device/connect/{rid}/approve")
def device_connect_approve(rid: str, tasks: BackgroundTasks, device=Depends(current_device)):
    r = load_request(rid)
    if r["status"] != "pending":
        raise HTTPException(409, f"This request is {r['status']}")
    developer_id = r["developer_id"]
    existing = first("connections", developer_id=developer_id, device_id=device["id"])
    if existing:
        conn_id = existing["id"]
    else:
        conn_id = next_id("connections")
        fs().collection("connections").document(str(conn_id)).set(
            {"developer_id": developer_id, "device_id": device["id"], "created_at": now()}
        )
    fs().collection("connect_requests").document(rid).update(
        {"status": "approved", "connection_id": conn_id, "device_id": device["id"]}
    )
    tasks.add_task(deliver_webhooks, developer_id, "connection.approved", {"connection_id": conn_id, "state": r.get("state")})
    name = (get_doc("developers", developer_id) or {}).get("app_name", "App")
    return {"connection_id": conn_id, "app_name": name}


@app.post("/v1/device/connect/{rid}/deny")
def device_connect_deny(rid: str, tasks: BackgroundTasks, device=Depends(current_device)):
    r = load_request(rid)
    if r["status"] != "pending":
        raise HTTPException(409, f"This request is {r['status']}")
    fs().collection("connect_requests").document(rid).update({"status": "denied", "device_id": device["id"]})
    tasks.add_task(deliver_webhooks, r["developer_id"], "connection.denied", {"state": r.get("state")})
    return {"status": "denied"}


# ---------- dashboard ----------

@app.get("/v1/events")
def list_events(limit: int = 30, dev=Depends(current_developer)):
    snaps = (
        fs().collection("developers").document(str(dev["id"])).collection("events")
        .order_by("ts", direction=Query.DESCENDING).limit(max(1, min(limit, 100))).stream()
    )
    return [s.to_dict() for s in snaps]


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


@app.get("/")
def health():
    return {
        "status": "ok",
        "fcm": "live" if firebase_ready else "not-configured",
        "database": "firestore" if db is not None else "not-configured",
    }
