import os
import re
import json
import time
import hmac
import sqlite3
import secrets
import hashlib
import threading
from datetime import datetime, timezone, timedelta
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


            CREATE TABLE IF NOT EXISTS stock_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_id TEXT NOT NULL,
                acronym TEXT NOT NULL,
                name TEXT NOT NULL,
                current_price REAL NOT NULL,
                market_cap REAL,
                total_shares REAL,
                source TEXT NOT NULL DEFAULT 'torn',
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_stock_snapshots_acronym_time
                ON stock_snapshots(acronym, created_at);

            CREATE TABLE IF NOT EXISTS stock_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL DEFAULT 'global',
                chosen_by_torn_id INTEGER,
                stock_id TEXT NOT NULL,
                acronym TEXT NOT NULL,
                name TEXT NOT NULL,
                pick_price REAL NOT NULL,
                score REAL NOT NULL,
                confidence REAL NOT NULL,
                expected_24h_pct REAL NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                replaced_by_id INTEGER,
                created_at TEXT NOT NULL,
                replaced_at TEXT,
                result_checked_at TEXT,
                actual_24h_price REAL,
                actual_24h_pct REAL,
                was_profitable INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_stock_predictions_active
                ON stock_predictions(scope, status, created_at);

            CREATE TABLE IF NOT EXISTS learning_weights (
                scope TEXT NOT NULL,
                module TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                weight_value REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(scope, module, signal_key)
            );

            CREATE TABLE IF NOT EXISTS auto_scan_state (
                torn_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_scan_at TEXT,
                next_scan_at TEXT,
                last_ok INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                scans_completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                module TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_seen INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
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



def get_api_key(torn_id: int) -> str:
    with db() as conn:
        row = conn.execute("SELECT api_key FROM api_keys WHERE torn_id=?", (torn_id,)).fetchone()
    return row["api_key"] if row else ""


def normalize_stocks(payload):
    """Return a clean list from Torn's stocks response, supporting a few response shapes."""
    raw = payload.get("stocks", payload) if isinstance(payload, dict) else payload
    if isinstance(raw, dict):
        rows = raw.values()
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        sid = item.get("stock_id") or item.get("id") or item.get("stockID") or item.get("stock") or item.get("ticker") or item.get("acronym")
        acronym = str(item.get("acronym") or item.get("ticker") or item.get("symbol") or sid or "UNK").upper()
        name = str(item.get("name") or item.get("stock_name") or acronym)
        price = item.get("current_price") or item.get("price") or item.get("value") or item.get("current") or item.get("market_price")
        try:
            price = float(price)
        except Exception:
            continue
        if price <= 0:
            continue
        def f(key):
            try:
                return float(item.get(key)) if item.get(key) is not None else None
            except Exception:
                return None
        out.append({
            "stock_id": str(sid or acronym),
            "acronym": acronym[:20],
            "name": name[:120],
            "current_price": price,
            "market_cap": f("market_cap"),
            "total_shares": f("total_shares"),
        })
    return out


def fetch_torn_stocks(api_key: str):
    # Torn's classic read-only endpoint commonly returns stocks from /torn/?selections=stocks.
    # Keeping this wrapped lets us swap API v2 pathing later without touching the rest of the app.
    return normalize_stocks(torn_get("torn", "stocks", api_key))


def stock_stats(acronym: str, current_price: float):
    since_24 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    since_7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db() as conn:
        r24 = conn.execute(
            """
            SELECT MIN(current_price) mn, MAX(current_price) mx, AVG(current_price) av, COUNT(*) c
            FROM stock_snapshots WHERE acronym=? AND created_at>=?
            """,
            (acronym, since_24),
        ).fetchone()
        r7 = conn.execute(
            """
            SELECT MIN(current_price) mn, MAX(current_price) mx, AVG(current_price) av, COUNT(*) c
            FROM stock_snapshots WHERE acronym=? AND created_at>=?
            """,
            (acronym, since_7),
        ).fetchone()
        first = conn.execute(
            "SELECT current_price FROM stock_snapshots WHERE acronym=? ORDER BY id ASC LIMIT 1", (acronym,)
        ).fetchone()
        prev = conn.execute(
            "SELECT current_price FROM stock_snapshots WHERE acronym=? ORDER BY id DESC LIMIT 2", (acronym,)
        ).fetchall()
    def val(row, key, default=None):
        return row[key] if row and row[key] is not None else default
    mn24, mx24, avg24, c24 = val(r24, "mn", current_price), val(r24, "mx", current_price), val(r24, "av", current_price), int(val(r24, "c", 0) or 0)
    mn7, mx7, avg7, c7 = val(r7, "mn", current_price), val(r7, "mx", current_price), val(r7, "av", current_price), int(val(r7, "c", 0) or 0)
    range24 = max(mx24 - mn24, 0.0001)
    range7 = max(mx7 - mn7, 0.0001)
    position24 = (current_price - mn24) / range24 if mx24 > mn24 else 0.5
    position7 = (current_price - mn7) / range7 if mx7 > mn7 else 0.5
    prev_price = prev[1]["current_price"] if len(prev) > 1 else current_price
    tick_pct = ((current_price - prev_price) / prev_price * 100) if prev_price else 0
    first_price = first["current_price"] if first else current_price
    all_pct = ((current_price - first_price) / first_price * 100) if first_price else 0
    volatility24 = (range24 / avg24 * 100) if avg24 else 0
    return {
        "min24": mn24, "max24": mx24, "avg24": avg24, "count24": c24,
        "min7": mn7, "max7": mx7, "avg7": avg7, "count7": c7,
        "position24": position24, "position7": position7,
        "tick_pct": tick_pct, "all_pct": all_pct, "volatility24": volatility24,
    }


def score_stock(stock):
    price = float(stock["current_price"])
    st = stock_stats(stock["acronym"], price)
    cheap24 = (1 - st["position24"]) * 32
    cheap7 = (1 - st["position7"]) * 28
    bounce = max(0, st["tick_pct"]) * 6
    controlled_vol = max(0, min(st["volatility24"], 8)) * 3
    penalty = max(0, st["position24"] - 0.82) * 35
    score = max(0, cheap24 + cheap7 + bounce + controlled_vol - penalty)
    data_points = min(100, (st["count24"] * 5) + (st["count7"] * 2))
    confidence = max(12, min(95, 20 + data_points + min(20, st["volatility24"] * 2)))
    expected = max(-2.5, min(12.0, (st["volatility24"] * 0.65) + (1 - st["position24"]) * 3 + max(0, st["tick_pct"] * 0.35)))
    reasons = []
    if st["position24"] < 0.35: reasons.append("near its 24h low")
    if st["position7"] < 0.40: reasons.append("below its 7d range midpoint")
    if st["tick_pct"] > 0: reasons.append("recent tick is moving up")
    if st["volatility24"] > 0.5: reasons.append("has enough movement for a 24h trade")
    if not reasons: reasons.append("best score from available stock snapshots")
    return {**stock, **st, "score": round(score, 2), "confidence": round(confidence, 1), "expected_24h_pct": round(expected, 2), "reason": ", ".join(reasons)}


def latest_active_stock_pick():
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM stock_predictions WHERE scope='global' AND status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def choose_stock_pick(torn_id: int, stocks, force=False):
    scored = sorted([score_stock(s) for s in stocks], key=lambda x: x["score"], reverse=True)
    if not scored:
        return {"pick": None, "changed": False, "ranked": []}
    best = scored[0]
    current = latest_active_stock_pick()
    changed = False
    changed_reason = ""
    gap = 15.0
    try:
        with db() as conn:
            row = conn.execute("SELECT setting_value FROM settings WHERE torn_id=? AND setting_key='stock_pick_change_score_gap'", (torn_id,)).fetchone()
            if row: gap = float(row["setting_value"])
    except Exception:
        pass
    should_replace = force or current is None or (best["score"] >= float(current["score"]) + gap) or float(current["confidence"] or 0) < 20
    if should_replace:
        created = now_iso()
        with db() as conn:
            if current:
                conn.execute("UPDATE stock_predictions SET status='replaced', replaced_at=? WHERE id=?", (created, current["id"]))
                changed = True
                changed_reason = f"{best['acronym']} beat old pick {current['acronym']} by the drastic-change rule."
            conn.execute(
                """
                INSERT INTO stock_predictions(scope, chosen_by_torn_id, stock_id, acronym, name, pick_price, score, confidence, expected_24h_pct, reason, created_at)
                VALUES('global',?,?,?,?,?,?,?,?,?,?)
                """,
                (torn_id, best["stock_id"], best["acronym"], best["name"], best["current_price"], best["score"], best["confidence"], best["expected_24h_pct"], best["reason"], created),
            )
            new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            if current:
                conn.execute("UPDATE stock_predictions SET replaced_by_id=? WHERE id=?", (new_id, current["id"]))
                conn.execute(
                    "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                    (torn_id, "stock_changed", "Stock Brain changed today's pick", changed_reason, "https://www.torn.com/page.php?sid=stocks", created),
                )
        current = latest_active_stock_pick()
    return {"pick": current, "changed": changed, "changed_reason": changed_reason, "ranked": scored[:10]}


def save_stock_snapshots(torn_id: int, stocks):
    created = now_iso()
    with db() as conn:
        for s in stocks:
            conn.execute(
                """
                INSERT INTO stock_snapshots(stock_id, acronym, name, current_price, market_cap, total_shares, captured_by_torn_id, created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (s["stock_id"], s["acronym"], s["name"], s["current_price"], s.get("market_cap"), s.get("total_shares"), torn_id, created),
            )
    return created


SCANNER_STARTED = False
SCANNER_LOCK = threading.Lock()


def iso_to_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def get_setting_for_user(conn, torn_id: int, key: str, default: str) -> str:
    row = conn.execute(
        "SELECT setting_value FROM settings WHERE torn_id=? AND setting_key=?",
        (torn_id, key),
    ).fetchone()
    return row["setting_value"] if row else default


def scan_interval_for_user(conn, torn_id: int) -> int:
    try:
        minutes = int(float(get_setting_for_user(conn, torn_id, "scan_interval_minutes", "15")))
    except Exception:
        minutes = 15
    # Keep Render/Torn smooth: no aggressive spam scans.
    return max(5, min(240, minutes))


def enable_auto_scan(torn_id: int, immediate: bool = True):
    stamp = now_iso()
    next_at = stamp if immediate else (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(microsecond=0).isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(torn_id, setting_key, setting_value, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(torn_id, setting_key) DO UPDATE SET
                setting_value=excluded.setting_value,
                updated_at=excluded.updated_at
            """,
            (torn_id, "auto_scan_enabled", "true", stamp),
        )
        existing = conn.execute("SELECT torn_id FROM auto_scan_state WHERE torn_id=?", (torn_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE auto_scan_state SET enabled=1, next_scan_at=COALESCE(next_scan_at, ?), updated_at=? WHERE torn_id=?",
                (next_at, stamp, torn_id),
            )
        else:
            conn.execute(
                "INSERT INTO auto_scan_state(torn_id, enabled, next_scan_at, updated_at) VALUES(?,?,?,?)",
                (torn_id, 1, next_at, stamp),
            )


def record_scan_run(torn_id: int, status: str, rows_seen: int, message: str, started_at: str):
    finished = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO scan_runs(torn_id, module, status, rows_seen, message, started_at, finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (torn_id, "stock_brain", status, rows_seen, message[:500], started_at, finished),
        )


def perform_stock_scan_for_user(torn_id: int, reason: str = "auto"):
    started = now_iso()
    rows_seen = 0
    try:
        key = get_api_key(torn_id)
        if not key:
            raise ValueError("No Torn API key saved.")
        stocks = fetch_torn_stocks(key)
        rows_seen = len(stocks)
        if not stocks:
            raise ValueError("Torn returned no stock rows.")
        save_stock_snapshots(torn_id, stocks)
        result = choose_stock_pick(torn_id, stocks)
        message = "Auto scan complete."
        if result.get("changed"):
            message += " Stock pick changed."
        with db() as conn:
            interval = scan_interval_for_user(conn, torn_id)
            next_scan = (datetime.now(timezone.utc) + timedelta(minutes=interval)).replace(microsecond=0).isoformat()
            conn.execute(
                """
                INSERT INTO auto_scan_state(torn_id, enabled, last_scan_at, next_scan_at, last_ok, last_error, scans_completed, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(torn_id) DO UPDATE SET
                    enabled=1,
                    last_scan_at=excluded.last_scan_at,
                    next_scan_at=excluded.next_scan_at,
                    last_ok=1,
                    last_error=NULL,
                    scans_completed=auto_scan_state.scans_completed + 1,
                    updated_at=excluded.updated_at
                """,
                (torn_id, 1, now_iso(), next_scan, 1, None, 1, now_iso()),
            )
        record_scan_run(torn_id, "ok", rows_seen, message, started)
        return {"ok": True, "stocks_seen": rows_seen, **result}
    except Exception as e:
        err = str(e)
        with db() as conn:
            interval = scan_interval_for_user(conn, torn_id)
            # Retry sooner after an error, but not constantly.
            next_scan = (datetime.now(timezone.utc) + timedelta(minutes=max(5, min(interval, 15)))).replace(microsecond=0).isoformat()
            conn.execute(
                """
                INSERT INTO auto_scan_state(torn_id, enabled, last_scan_at, next_scan_at, last_ok, last_error, updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(torn_id) DO UPDATE SET
                    last_scan_at=excluded.last_scan_at,
                    next_scan_at=excluded.next_scan_at,
                    last_ok=0,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (torn_id, 1, now_iso(), next_scan, 0, err[:500], now_iso()),
            )
        record_scan_run(torn_id, "error", rows_seen, err, started)
        return {"ok": False, "error": err}


def due_auto_scan_users():
    now_dt = datetime.now(timezone.utc)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT u.torn_id, COALESCE(a.next_scan_at, '') AS next_scan_at
            FROM users u
            JOIN api_keys k ON k.torn_id = u.torn_id
            LEFT JOIN auto_scan_state a ON a.torn_id = u.torn_id
            LEFT JOIN settings s ON s.torn_id = u.torn_id AND s.setting_key='auto_scan_enabled'
            WHERE COALESCE(a.enabled, 1)=1
              AND COALESCE(s.setting_value, 'true')='true'
            LIMIT 25
            """
        ).fetchall()
    due = []
    for r in rows:
        dt = iso_to_dt(r["next_scan_at"])
        if dt is None or dt <= now_dt:
            due.append(int(r["torn_id"]))
    return due


def auto_scanner_loop():
    # Small delay lets Render finish booting before the worker starts scanning.
    time.sleep(8)
    while True:
        try:
            for torn_id in due_auto_scan_users():
                perform_stock_scan_for_user(torn_id, reason="auto")
                time.sleep(1.5)
        except Exception:
            pass
        time.sleep(35)


def start_background_scanner():
    global SCANNER_STARTED
    with SCANNER_LOCK:
        if SCANNER_STARTED:
            return
        SCANNER_STARTED = True
        t = threading.Thread(target=auto_scanner_loop, name="torn-brain-auto-scanner", daemon=True)
        t.start()

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
        "step": "2.1-auto",
        "message": "Backend online. Auto scanner runs server-side after login. Install /static/torn-brain.user.js in TornPDA/Tampermonkey."
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
            (torn_id, "system", "Torn Brain connected", "Auto Stock Brain is active. The backend now starts scanning after login and keeps the userscript smooth.", None, created),
        )

    enable_auto_scan(torn_id, immediate=True)

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
        auto = conn.execute(
            "SELECT enabled, last_scan_at, next_scan_at, last_ok, last_error, scans_completed FROM auto_scan_state WHERE torn_id=?",
            (request.user["torn_id"],),
        ).fetchone()
    return jsonify({
        "ok": True,
        "step": "2.1-auto",
        "user": request.user,
        "tabs": [
            "Overview", "Stock Brain", "Item Market", "Travel Profit", "Points Watcher",
            "Enemy Sleep", "Alerts", "Accuracy", "Settings"
        ],
        "modules": {
            "stock_brain": "active_step_2",
            "item_market": "coming_step_3",
            "points_watcher": "coming_step_4",
            "travel_profit": "coming_step_5",
            "enemy_sleep": "coming_step_6",
            "accuracy": "coming_step_7"
        },
        "unread_alerts": unread,
        "auto_scan": dict(auto) if auto else {"enabled": 1, "last_scan_at": None, "next_scan_at": None, "last_ok": 0, "last_error": None, "scans_completed": 0},
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
        "share_market_learning": "true",
        "auto_scan_enabled": "true",
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
        "share_market_learning",
        "auto_scan_enabled",
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



@app.post("/api/stocks/scan")
@require_auth
def scan_stocks():
    result = perform_stock_scan_for_user(request.user["torn_id"], reason="manual")
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.post("/api/auto/start")
@require_auth
def auto_start():
    enable_auto_scan(request.user["torn_id"], immediate=True)
    return jsonify({"ok": True, "message": "Backend auto scanner enabled. It will scan server-side so the userscript stays smooth."})


@app.post("/api/auto/stop")
@require_auth
def auto_stop():
    with db() as conn:
        conn.execute("UPDATE auto_scan_state SET enabled=0, updated_at=? WHERE torn_id=?", (now_iso(), request.user["torn_id"]))
        conn.execute(
            """
            INSERT INTO settings(torn_id, setting_key, setting_value, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(torn_id, setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at
            """,
            (request.user["torn_id"], "auto_scan_enabled", "false", now_iso()),
        )
    return jsonify({"ok": True})


@app.get("/api/auto/status")
@require_auth
def auto_status():
    with db() as conn:
        row = conn.execute(
            "SELECT enabled, last_scan_at, next_scan_at, last_ok, last_error, scans_completed, updated_at FROM auto_scan_state WHERE torn_id=?",
            (request.user["torn_id"],),
        ).fetchone()
        runs = conn.execute(
            "SELECT module, status, rows_seen, message, started_at, finished_at FROM scan_runs WHERE torn_id=? ORDER BY id DESC LIMIT 8",
            (request.user["torn_id"],),
        ).fetchall()
    return jsonify({"ok": True, "auto_scan": dict(row) if row else None, "recent_runs": [dict(r) for r in runs]})


@app.get("/api/stocks/brain")
@require_auth
def stocks_brain():
    pick = latest_active_stock_pick()
    with db() as conn:
        recent = conn.execute(
            """
            SELECT acronym, name, current_price, created_at
            FROM stock_snapshots
            WHERE id IN (SELECT MAX(id) FROM stock_snapshots GROUP BY acronym)
            ORDER BY acronym ASC
            """
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) AS c FROM stock_snapshots").fetchone()["c"]
    ranked = []
    for r in recent:
        ranked.append(score_stock(dict(r) | {"stock_id": r["acronym"], "market_cap": None, "total_shares": None}))
    ranked = sorted(ranked, key=lambda x: x["score"], reverse=True)[:10]
    return jsonify({"ok": True, "pick": pick, "ranked": ranked, "snapshot_count": count, "server_time": now_iso()})


@app.get("/api/stocks/predictions")
@require_auth
def stocks_predictions():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, scope, acronym, name, pick_price, score, confidence, expected_24h_pct, reason, status, created_at, replaced_at, actual_24h_pct
            FROM stock_predictions
            ORDER BY id DESC LIMIT 30
            """
        ).fetchall()
    return jsonify({"ok": True, "predictions": [dict(r) for r in rows]})

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


start_background_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
