import os
import re
import json
import time
import hmac
import sqlite3
import secrets
import hashlib
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

APP_SECRET = os.environ.get("APP_SECRET", "dev-change-me")
DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "torn_brain.sqlite3"))
TORN_API_BASE = "https://api.torn.com"
KEY_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")

app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/api/*": {"origins": "*"}})


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                torn_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                level INTEGER,
                faction_id INTEGER,
                faction_name TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                torn_id INTEGER PRIMARY KEY,
                api_key TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                masked_key TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                torn_id INTEGER NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(torn_id, setting_key)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            """
        )


init_db()


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "*" * max(0, len(key) - 4) + key[-2:]
    return key[:4] + "•" * (len(key) - 8) + key[-4:]


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hmac.new(APP_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def torn_get(section: str, selections: str, key: str, torn_id: str = ""):
    url = f"{TORN_API_BASE}/{section}/{torn_id}"
    params = {"selections": selections, "key": key}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise ValueError(f"Torn API error {err.get('code')}: {err.get('error')}")
    return data


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "", 1).strip()
        if not token:
            return jsonify({"ok": False, "error": "Missing session token."}), 401
        th = token_hash(token)
        with db() as conn:
            row = conn.execute(
                """
                SELECT s.torn_id, u.name, u.level, u.faction_id, u.faction_name, k.masked_key
                FROM sessions s
                JOIN users u ON u.torn_id = s.torn_id
                LEFT JOIN api_keys k ON k.torn_id = u.torn_id
                WHERE s.token_hash = ?
                """,
                (th,),
            ).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Invalid or expired session."}), 401
            conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now_iso(), th))
        request.user = dict(row)
        return fn(*args, **kwargs)
    return wrapper


@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "app": "Fries91 Torn Brain",
        "step": 1,
        "message": "Backend online. Install /static/torn-brain.user.js in TornPDA/Tampermonkey."
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    key = (payload.get("api_key") or "").strip()
    if not KEY_RE.match(key):
        return jsonify({"ok": False, "error": "Enter a valid Torn API key."}), 400

    try:
        profile = torn_get("user", "profile", key)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not validate API key: {e}"}), 400

    torn_id = int(profile.get("player_id") or profile.get("id") or 0)
    name = str(profile.get("name") or "Unknown")
    level = profile.get("level")
    faction = profile.get("faction") or {}
    faction_id = faction.get("faction_id") or faction.get("id")
    faction_name = faction.get("faction_name") or faction.get("name")

    if not torn_id:
        return jsonify({"ok": False, "error": "Torn profile response did not include a player ID."}), 400

    token = secrets.token_urlsafe(32)
    created = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users(torn_id, name, level, faction_id, faction_name, created_at, last_seen)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(torn_id) DO UPDATE SET
                name=excluded.name,
                level=excluded.level,
                faction_id=excluded.faction_id,
                faction_name=excluded.faction_name,
                last_seen=excluded.last_seen
            """,
            (torn_id, name, level, faction_id, faction_name, created, created),
        )
        conn.execute(
            """
            INSERT INTO api_keys(torn_id, api_key, key_hash, masked_key, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(torn_id) DO UPDATE SET
                api_key=excluded.api_key,
                key_hash=excluded.key_hash,
                masked_key=excluded.masked_key,
                updated_at=excluded.updated_at
            """,
            (torn_id, key, sha256(key), mask_key(key), created),
        )
        conn.execute(
            "INSERT INTO sessions(token_hash, torn_id, created_at, last_seen) VALUES(?,?,?,?)",
            (token_hash(token), torn_id, created, created),
        )
        conn.execute(
            """
            INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (torn_id, "system", "Torn Brain connected", "Step 1 shell is active. Stock, item, travel, points, and enemy tracking tabs are ready for the next steps.", None, created),
        )

    return jsonify({
        "ok": True,
        "token": token,
        "user": {
            "torn_id": torn_id,
            "name": name,
            "level": level,
            "faction_id": faction_id,
            "faction_name": faction_name,
            "masked_key": mask_key(key),
        }
    })


@app.post("/api/logout")
@require_auth
def logout():
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "", 1).strip()
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
    return jsonify({"ok": True})


@app.get("/api/state")
@require_auth
def state():
    with db() as conn:
        unread = conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE torn_id=? AND is_read=0",
            (request.user["torn_id"],),
        ).fetchone()["c"]
    return jsonify({
        "ok": True,
        "step": 1,
        "user": request.user,
        "tabs": [
            "Overview", "Stock Brain", "Item Market", "Travel Profit", "Points Watcher",
            "Enemy Sleep", "Alerts", "Accuracy", "Settings"
        ],
        "modules": {
            "stock_brain": "coming_step_2",
            "item_market": "coming_step_3",
            "points_watcher": "coming_step_4",
            "travel_profit": "coming_step_5",
            "enemy_sleep": "coming_step_6",
            "accuracy": "coming_step_7"
        },
        "unread_alerts": unread,
        "server_time": now_iso()
    })


@app.get("/api/settings")
@require_auth
def get_settings():
    defaults = {
        "scan_interval_minutes": "15",
        "stock_pick_change_score_gap": "15",
        "enemy_tracking_window_hours": "72",
        "alerts_enabled": "true",
    }
    with db() as conn:
        rows = conn.execute("SELECT setting_key, setting_value FROM settings WHERE torn_id=?", (request.user["torn_id"],)).fetchall()
    settings = defaults | {r["setting_key"]: r["setting_value"] for r in rows}
    return jsonify({"ok": True, "settings": settings, "user": request.user})


@app.post("/api/settings")
@require_auth
def save_settings():
    payload = request.get_json(silent=True) or {}
    allowed = {
        "scan_interval_minutes",
        "stock_pick_change_score_gap",
        "enemy_tracking_window_hours",
        "alerts_enabled",
    }
    changed = {}
    with db() as conn:
        for k, v in payload.items():
            if k not in allowed:
                continue
            val = str(v).strip()[:200]
            conn.execute(
                """
                INSERT INTO settings(torn_id, setting_key, setting_value, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(torn_id, setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value,
                    updated_at=excluded.updated_at
                """,
                (request.user["torn_id"], k, val, now_iso()),
            )
            changed[k] = val
    return jsonify({"ok": True, "changed": changed})


@app.get("/api/alerts")
@require_auth
def alerts():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, alert_type, title, body, link, is_read, created_at
            FROM alerts
            WHERE torn_id=?
            ORDER BY id DESC
            LIMIT 50
            """,
            (request.user["torn_id"],),
        ).fetchall()
    return jsonify({"ok": True, "alerts": [dict(r) for r in rows]})


@app.post("/api/alerts/read")
@require_auth
def mark_alerts_read():
    with db() as conn:
        conn.execute("UPDATE alerts SET is_read=1 WHERE torn_id=?", (request.user["torn_id"],))
    return jsonify({"ok": True})


@app.post("/api/dev/test-alert")
@require_auth
def dev_test_alert():
    created = now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
            (request.user["torn_id"], "test", "Test alert", "Alerts are working. Future steps will use this for buy zones, points, travel, and stock changes.", "https://www.torn.com/", created),
        )
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
