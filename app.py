import os
import re
import json
import time
import hmac
import sqlite3
PG_DRIVER = None
PG_IMPORT_ERROR = None
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PG_DRIVER = "psycopg2"
except Exception as e2:
    try:
        import psycopg
        from psycopg.rows import dict_row
        PG_DRIVER = "psycopg3"
    except Exception as e3:
        psycopg2 = None
        RealDictCursor = None
        psycopg = None
        dict_row = None
        PG_IMPORT_ERROR = f"psycopg2: {e2}; psycopg3: {e3}"
import secrets
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from functools import wraps

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from flask_cors import CORS

APP_SECRET = os.environ.get("APP_SECRET", "dev-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "torn_brain.sqlite3"))
USE_POSTGRES = bool(DATABASE_URL)
TORN_API_BASE = "https://api.torn.com"
KEY_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")

app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/api/*": {"origins": "*"}})


@app.errorhandler(HTTPException)
def _tb_http_error(e):
    # Always return JSON so TornPDA/userscript never sees an HTML error page.
    return jsonify({"ok": False, "error": getattr(e, "description", str(e)), "status": getattr(e, "code", 500), "step": "10.12-server-json-fix"}), getattr(e, "code", 500)


@app.errorhandler(Exception)
def _tb_unhandled_error(e):
    # Keep the overlay readable if Render hits a backend exception.
    return jsonify({"ok": False, "error": "Backend error: " + str(e), "step": "10.12-server-json-fix"}), 500


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _pg_sql(sql: str) -> str:
    # Convert sqlite-style placeholders used by this app to psycopg2 placeholders.
    # The app does not use literal question marks in SQL, so this is safe for our queries.
    return sql.replace("?", "%s")


class PgCursorWrap:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class PgConnWrap:
    def __init__(self):
        if PG_DRIVER is None:
            raise RuntimeError("DATABASE_URL is set but no PostgreSQL driver could import. " + str(PG_IMPORT_ERROR))
        if PG_DRIVER == "psycopg2":
            self.conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        else:
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        self._last_insert_id = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def execute(self, sql, params=()):
        sql_clean = sql.strip()
        if sql_clean.upper().startswith("SELECT LAST_INSERT_ROWID()"):
            class LastId:
                def __init__(self, value): self.value = value
                def fetchone(self): return {"id": self.value}
                def fetchall(self): return [{"id": self.value}]
            return LastId(self._last_insert_id)
        cur = self.conn.cursor()
        cur.execute(_pg_sql(sql), params or ())
        # Do not call PostgreSQL LASTVAL() automatically. Some inserts target tables
        # without sequences (users/api_keys/sessions), which can crash login or scans.
        # Code that needs a new id should query the row it just inserted.
        return PgCursorWrap(cur)

    def executescript(self, script):
        cur = self.conn.cursor()
        for part in script.split(";"):
            stmt = part.strip()
            if stmt:
                cur.execute(stmt)


def db():
    if USE_POSTGRES:
        return PgConnWrap()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_schema():
    return r"""

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


            CREATE TABLE IF NOT EXISTS user_stock_holding_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                stock_id TEXT,
                acronym TEXT NOT NULL,
                name TEXT,
                shares REAL,
                average_buy_price REAL,
                current_price REAL,
                estimated_value REAL,
                estimated_profit REAL,
                estimated_profit_pct REAL,
                source TEXT NOT NULL DEFAULT 'user_stocks',
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_user_stock_holdings_scope_time
                ON user_stock_holding_snapshots(torn_id, acronym, created_at);

            CREATE TABLE IF NOT EXISTS learning_weights (
                scope TEXT NOT NULL,
                module TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                weight_value REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(scope, module, signal_key)
            );



            CREATE TABLE IF NOT EXISTS item_catalog (
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                item_type TEXT,
                buy_price REAL,
                sell_value REAL,
                market_value REAL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS item_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                buy_zone REAL,
                sell_zone REAL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(torn_id, item_id),
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS item_market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                lowest_price REAL,
                avg_price REAL,
                listing_count INTEGER NOT NULL DEFAULT 0,
                total_quantity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'market',
                last_error TEXT,
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_item_market_snapshots_item_time
                ON item_market_snapshots(item_id, created_at);

            CREATE TABLE IF NOT EXISTS item_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                signal TEXT NOT NULL,
                current_price REAL,
                buy_zone REAL,
                sell_zone REAL,
                reason TEXT NOT NULL,
                link TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_item_signals_user_time
                ON item_signals(torn_id, created_at);

            CREATE TABLE IF NOT EXISTS points_market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lowest_price REAL,
                avg_price REAL,
                listing_count INTEGER NOT NULL DEFAULT 0,
                total_quantity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'market',
                last_error TEXT,
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_points_market_snapshots_time
                ON points_market_snapshots(created_at);

            CREATE TABLE IF NOT EXISTS points_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                signal TEXT NOT NULL,
                current_price REAL,
                buy_zone REAL,
                sell_zone REAL,
                reason TEXT NOT NULL,
                link TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_points_signals_user_time
                ON points_signals(torn_id, created_at);

            CREATE TABLE IF NOT EXISTS travel_route_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country TEXT NOT NULL,
                item_id INTEGER,
                item_name TEXT NOT NULL,
                abroad_cost REAL,
                home_price REAL,
                estimated_profit REAL,
                profit_per_minute REAL,
                arrival_chance REAL,
                score REAL,
                signal TEXT NOT NULL,
                reason TEXT,
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_travel_route_snapshots_time
                ON travel_route_snapshots(country, item_name, created_at);

            CREATE TABLE IF NOT EXISTS travel_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                country TEXT NOT NULL,
                item_id INTEGER,
                item_name TEXT NOT NULL,
                abroad_cost REAL,
                home_price REAL,
                estimated_profit REAL,
                profit_per_minute REAL,
                arrival_chance REAL,
                score REAL,
                signal TEXT NOT NULL,
                reason TEXT NOT NULL,
                link TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_travel_recommendations_user_time
                ON travel_recommendations(torn_id, created_at);

            CREATE TABLE IF NOT EXISTS enemy_tracking_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                faction_id INTEGER,
                faction_name TEXT,
                enemy_faction_id INTEGER,
                enemy_faction_name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                last_scan_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(torn_id, faction_id, enemy_faction_id),
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS enemy_activity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                faction_id INTEGER,
                enemy_faction_id INTEGER NOT NULL,
                enemy_torn_id INTEGER NOT NULL,
                enemy_name TEXT NOT NULL,
                online_status TEXT,
                status_state TEXT,
                status_description TEXT,
                status_until INTEGER,
                last_action_status TEXT,
                last_action_timestamp INTEGER,
                activity_bucket TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_enemy_activity_scope_time
                ON enemy_activity_snapshots(torn_id, enemy_faction_id, captured_at);

            CREATE TABLE IF NOT EXISTS enemy_activity_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                faction_id INTEGER,
                enemy_faction_id INTEGER NOT NULL,
                enemy_faction_name TEXT,
                window_hours INTEGER NOT NULL DEFAULT 72,
                best_attack_window TEXT,
                best_turtle_window TEXT,
                confidence TEXT NOT NULL,
                active_ratio REAL,
                inactive_ratio REAL,
                member_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_enemy_reports_user_time
                ON enemy_activity_reports(torn_id, enemy_faction_id, created_at);

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


            CREATE TABLE IF NOT EXISTS accuracy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER,
                scope TEXT NOT NULL DEFAULT 'global',
                module TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                target_name TEXT NOT NULL,
                signal TEXT NOT NULL,
                predicted_value REAL,
                actual_value REAL,
                result_pct REAL,
                score_before REAL,
                confidence_before REAL,
                was_correct INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_table, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_accuracy_events_module_time
                ON accuracy_events(module, created_at);

            CREATE TABLE IF NOT EXISTS learning_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL DEFAULT 'global',
                module TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                old_weight REAL,
                new_weight REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            
"""


def _postgres_schema():
    return r"""

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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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
                id SERIAL PRIMARY KEY,
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


            CREATE TABLE IF NOT EXISTS user_stock_holding_snapshots (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                stock_id TEXT,
                acronym TEXT NOT NULL,
                name TEXT,
                shares REAL,
                average_buy_price REAL,
                current_price REAL,
                estimated_value REAL,
                estimated_profit REAL,
                estimated_profit_pct REAL,
                source TEXT NOT NULL DEFAULT 'user_stocks',
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_user_stock_holdings_scope_time
                ON user_stock_holding_snapshots(torn_id, acronym, created_at);

            CREATE TABLE IF NOT EXISTS learning_weights (
                scope TEXT NOT NULL,
                module TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                weight_value REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(scope, module, signal_key)
            );



            CREATE TABLE IF NOT EXISTS item_catalog (
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                item_type TEXT,
                buy_price REAL,
                sell_value REAL,
                market_value REAL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS item_watchlist (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                buy_zone REAL,
                sell_zone REAL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(torn_id, item_id),
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS item_market_snapshots (
                id SERIAL PRIMARY KEY,
                item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                lowest_price REAL,
                avg_price REAL,
                listing_count INTEGER NOT NULL DEFAULT 0,
                total_quantity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'market',
                last_error TEXT,
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_item_market_snapshots_item_time
                ON item_market_snapshots(item_id, created_at);

            CREATE TABLE IF NOT EXISTS item_signals (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                signal TEXT NOT NULL,
                current_price REAL,
                buy_zone REAL,
                sell_zone REAL,
                reason TEXT NOT NULL,
                link TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_item_signals_user_time
                ON item_signals(torn_id, created_at);

            CREATE TABLE IF NOT EXISTS points_market_snapshots (
                id SERIAL PRIMARY KEY,
                lowest_price REAL,
                avg_price REAL,
                listing_count INTEGER NOT NULL DEFAULT 0,
                total_quantity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'market',
                last_error TEXT,
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_points_market_snapshots_time
                ON points_market_snapshots(created_at);

            CREATE TABLE IF NOT EXISTS points_signals (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                signal TEXT NOT NULL,
                current_price REAL,
                buy_zone REAL,
                sell_zone REAL,
                reason TEXT NOT NULL,
                link TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_points_signals_user_time
                ON points_signals(torn_id, created_at);

            CREATE TABLE IF NOT EXISTS travel_route_snapshots (
                id SERIAL PRIMARY KEY,
                country TEXT NOT NULL,
                item_id INTEGER,
                item_name TEXT NOT NULL,
                abroad_cost REAL,
                home_price REAL,
                estimated_profit REAL,
                profit_per_minute REAL,
                arrival_chance REAL,
                score REAL,
                signal TEXT NOT NULL,
                reason TEXT,
                captured_by_torn_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_travel_route_snapshots_time
                ON travel_route_snapshots(country, item_name, created_at);

            CREATE TABLE IF NOT EXISTS travel_recommendations (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                country TEXT NOT NULL,
                item_id INTEGER,
                item_name TEXT NOT NULL,
                abroad_cost REAL,
                home_price REAL,
                estimated_profit REAL,
                profit_per_minute REAL,
                arrival_chance REAL,
                score REAL,
                signal TEXT NOT NULL,
                reason TEXT NOT NULL,
                link TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_travel_recommendations_user_time
                ON travel_recommendations(torn_id, created_at);

            CREATE TABLE IF NOT EXISTS enemy_tracking_sessions (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                faction_id INTEGER,
                faction_name TEXT,
                enemy_faction_id INTEGER,
                enemy_faction_name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                last_scan_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(torn_id, faction_id, enemy_faction_id),
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE TABLE IF NOT EXISTS enemy_activity_snapshots (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                faction_id INTEGER,
                enemy_faction_id INTEGER NOT NULL,
                enemy_torn_id INTEGER NOT NULL,
                enemy_name TEXT NOT NULL,
                online_status TEXT,
                status_state TEXT,
                status_description TEXT,
                status_until INTEGER,
                last_action_status TEXT,
                last_action_timestamp INTEGER,
                activity_bucket TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_enemy_activity_scope_time
                ON enemy_activity_snapshots(torn_id, enemy_faction_id, captured_at);

            CREATE TABLE IF NOT EXISTS enemy_activity_reports (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                faction_id INTEGER,
                enemy_faction_id INTEGER NOT NULL,
                enemy_faction_name TEXT,
                window_hours INTEGER NOT NULL DEFAULT 72,
                best_attack_window TEXT,
                best_turtle_window TEXT,
                confidence TEXT NOT NULL,
                active_ratio REAL,
                inactive_ratio REAL,
                member_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );

            CREATE INDEX IF NOT EXISTS idx_enemy_reports_user_time
                ON enemy_activity_reports(torn_id, enemy_faction_id, created_at);

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
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                module TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_seen INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );


            CREATE TABLE IF NOT EXISTS accuracy_events (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER,
                scope TEXT NOT NULL DEFAULT 'global',
                module TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                target_name TEXT NOT NULL,
                signal TEXT NOT NULL,
                predicted_value REAL,
                actual_value REAL,
                result_pct REAL,
                score_before REAL,
                confidence_before REAL,
                was_correct INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_table, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_accuracy_events_module_time
                ON accuracy_events(module, created_at);

            CREATE TABLE IF NOT EXISTS learning_adjustments (
                id SERIAL PRIMARY KEY,
                scope TEXT NOT NULL DEFAULT 'global',
                module TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                old_weight REAL,
                new_weight REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            
"""


def init_db():
    if USE_POSTGRES:
        with db() as conn:
            conn.executescript(_postgres_schema())
            try:
                conn.execute("ALTER TABLE item_market_snapshots ADD COLUMN source TEXT DEFAULT 'market'")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE item_market_snapshots ADD COLUMN last_error TEXT")
            except Exception:
                pass
        return
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.executescript(_sqlite_schema())
        try:
            conn.execute("ALTER TABLE item_market_snapshots ADD COLUMN source TEXT DEFAULT 'market'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE item_market_snapshots ADD COLUMN last_error TEXT")
        except Exception:
            pass


init_db()

# Step 10.2: lightweight user feedback for prediction quality.
def init_step_10_2_db():
    if USE_POSTGRES:
        script = """
        CREATE TABLE IF NOT EXISTS prediction_feedback (
            id SERIAL PRIMARY KEY,
            torn_id INTEGER NOT NULL,
            module TEXT NOT NULL,
            target_name TEXT NOT NULL,
            feedback TEXT NOT NULL,
            context_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(torn_id) REFERENCES users(torn_id)
        );
        CREATE INDEX IF NOT EXISTS idx_prediction_feedback_module_time
            ON prediction_feedback(module, created_at);


            CREATE TABLE IF NOT EXISTS stock_move_watches (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                from_acronym TEXT NOT NULL,
                from_name TEXT,
                from_price REAL,
                to_acronym TEXT,
                to_name TEXT,
                to_price REAL,
                status TEXT NOT NULL DEFAULT 'active',
                reason TEXT,
                created_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                notified_at TEXT,
                reviewed_at TEXT,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_stock_move_watches_user_status
                ON stock_move_watches(torn_id, status, due_at);
        """
    else:
        script = """
        CREATE TABLE IF NOT EXISTS prediction_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torn_id INTEGER NOT NULL,
            module TEXT NOT NULL,
            target_name TEXT NOT NULL,
            feedback TEXT NOT NULL,
            context_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(torn_id) REFERENCES users(torn_id)
        );
        CREATE INDEX IF NOT EXISTS idx_prediction_feedback_module_time
            ON prediction_feedback(module, created_at);


            CREATE TABLE IF NOT EXISTS stock_move_watches (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                from_acronym TEXT NOT NULL,
                from_name TEXT,
                from_price REAL,
                to_acronym TEXT,
                to_name TEXT,
                to_price REAL,
                status TEXT NOT NULL DEFAULT 'active',
                reason TEXT,
                created_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                notified_at TEXT,
                reviewed_at TEXT,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_stock_move_watches_user_status
                ON stock_move_watches(torn_id, status, due_at);
        """
    with db() as conn:
        conn.executescript(script)

init_step_10_2_db()

# Step 10.3: defensive runtime migrations.
# Existing PostgreSQL databases created before Step 10 may be missing newer tables.
# This runs once on startup/request and safely creates only what is missing.
_RUNTIME_MIGRATIONS_DONE = False
_RUNTIME_MIGRATION_LOCK = threading.Lock()

def ensure_runtime_migrations():
    global _RUNTIME_MIGRATIONS_DONE
    if _RUNTIME_MIGRATIONS_DONE:
        return
    with _RUNTIME_MIGRATION_LOCK:
        if _RUNTIME_MIGRATIONS_DONE:
            return
        if USE_POSTGRES:
            script = """
            CREATE TABLE IF NOT EXISTS user_stock_holding_snapshots (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                stock_id TEXT,
                acronym TEXT NOT NULL,
                name TEXT,
                shares REAL,
                average_buy_price REAL,
                current_price REAL,
                estimated_value REAL,
                estimated_profit REAL,
                estimated_profit_pct REAL,
                source TEXT NOT NULL DEFAULT 'user_stocks',
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_stock_holdings_scope_time
                ON user_stock_holding_snapshots(torn_id, acronym, created_at);

            CREATE TABLE IF NOT EXISTS prediction_feedback (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER NOT NULL,
                module TEXT NOT NULL,
                target_name TEXT NOT NULL,
                feedback TEXT NOT NULL,
                context_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_prediction_feedback_module_time
                ON prediction_feedback(module, created_at);

            CREATE TABLE IF NOT EXISTS stock_pattern_results (
                id SERIAL PRIMARY KEY,
                torn_id INTEGER,
                stock_id TEXT,
                acronym TEXT NOT NULL,
                name TEXT,
                current_price REAL,
                pattern_label TEXT,
                pattern_confidence REAL,
                pattern_score REAL,
                support_touches INTEGER DEFAULT 0,
                trend_1h_pct REAL,
                trend_6h_pct REAL,
                trend_24h_pct REAL,
                trend_7d_pct REAL,
                position_24h REAL,
                position_7d REAL,
                volatility_24h REAL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stock_pattern_results_acr_time
                ON stock_pattern_results(acronym, created_at);
            """
        else:
            script = """
            CREATE TABLE IF NOT EXISTS user_stock_holding_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                stock_id TEXT,
                acronym TEXT NOT NULL,
                name TEXT,
                shares REAL,
                average_buy_price REAL,
                current_price REAL,
                estimated_value REAL,
                estimated_profit REAL,
                estimated_profit_pct REAL,
                source TEXT NOT NULL DEFAULT 'user_stocks',
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_stock_holdings_scope_time
                ON user_stock_holding_snapshots(torn_id, acronym, created_at);

            CREATE TABLE IF NOT EXISTS prediction_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER NOT NULL,
                module TEXT NOT NULL,
                target_name TEXT NOT NULL,
                feedback TEXT NOT NULL,
                context_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(torn_id) REFERENCES users(torn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_prediction_feedback_module_time
                ON prediction_feedback(module, created_at);

            CREATE TABLE IF NOT EXISTS stock_pattern_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torn_id INTEGER,
                stock_id TEXT,
                acronym TEXT NOT NULL,
                name TEXT,
                current_price REAL,
                pattern_label TEXT,
                pattern_confidence REAL,
                pattern_score REAL,
                support_touches INTEGER DEFAULT 0,
                trend_1h_pct REAL,
                trend_6h_pct REAL,
                trend_24h_pct REAL,
                trend_7d_pct REAL,
                position_24h REAL,
                position_7d REAL,
                volatility_24h REAL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stock_pattern_results_acr_time
                ON stock_pattern_results(acronym, created_at);
            """
        with db() as conn:
            conn.executescript(script)
        _RUNTIME_MIGRATIONS_DONE = True

# Run once at import, then again defensively before API requests if startup was interrupted.
try:
    ensure_runtime_migrations()
except Exception:
    _RUNTIME_MIGRATIONS_DONE = False

@app.before_request
def _tb_runtime_migration_guard():
    if request.path.startswith('/api/') or request.path in ('/', '/health'):
        ensure_runtime_migrations()


def data_strength_label(samples):
    try:
        n = int(samples or 0)
    except Exception:
        n = 0
    if n >= 500:
        return "High"
    if n >= 80:
        return "Medium"
    if n >= 15:
        return "Low+"
    return "Learning"


def risk_label(signal=None, confidence=None, data_strength=None):
    sig = str(signal or "").upper()
    try:
        conf = float(confidence or 0)
    except Exception:
        conf = 0
    if sig in ("WAIT", "HOLD", "WATCH") and conf < 65:
        return "WAIT"
    if conf >= 78 and data_strength in ("High", "Medium"):
        return "SAFE"
    if conf >= 60:
        return "GOOD"
    if sig in ("BUY", "GO", "PICK", "SELL"):
        return "RISKY"
    return "LEARNING"


TOS_TEXT = """AI🫰 Fries91 Torn Brain is a Torn companion tool in active development. It provides predictions and suggestions only. It does not guarantee profit, does not auto-buy, does not auto-sell, and does not perform actions on your Torn account. Use all predictions at your own risk.

Privacy: your Torn API key is stored on the backend so the app can read Torn data for analysis. Your API key, personal holdings, and personal stock history are private. Shared learning uses anonymous market outcomes, prediction accuracy, item/travel price history, and aggregate signals so the tool can improve for everyone. Personal holdings are not shown to other users.

By using the tool, you agree that it is experimental, may be wrong, and should be treated as decision support rather than financial certainty inside Torn."""


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


def validate_torn_key_profile(key: str):
    """Validate a user key with graceful fallbacks for limited keys."""
    errors = []
    for selection in ("profile", "basic", "personalstats"):
        try:
            data = torn_get("user", selection, key)
            torn_id = int(data.get("player_id") or data.get("id") or data.get("user_id") or data.get("ID") or 0)
            if torn_id:
                faction = data.get("faction") or {}
                return {
                    "player_id": torn_id,
                    "name": str(data.get("name") or f"User {torn_id}"),
                    "level": data.get("level"),
                    "faction": faction if isinstance(faction, dict) else {},
                    "validation_selection": selection,
                }
            errors.append(f"{selection}: no player_id in response")
        except Exception as e:
            errors.append(f"{selection}: {e}")
    raise ValueError("Could not validate API key with profile/basic/personalstats. Make sure the limited key allows basic user/profile reads. " + " | ".join(errors[-3:]))



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


def normalize_user_stock_holdings(payload, market_lookup=None):
    """Normalize a user's current stock holdings.

    Torn response shapes can vary by API version/key access, so this accepts
    dict/list styles and records whatever usable values are present. It does
    not need perfect holdings to help learning: even a current owned stock plus
    market price is useful for personal bias and portfolio history.
    """
    market_lookup = market_lookup or {}
    if not isinstance(payload, dict):
        return []
    raw = payload.get("stocks") or payload.get("stock") or payload.get("portfolio") or payload.get("holdings") or []
    if isinstance(raw, dict):
        rows = raw.values()
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("stock_id") or row.get("id") or row.get("stockID") or row.get("stock") or row.get("ticker") or row.get("acronym")
        acronym = str(row.get("acronym") or row.get("ticker") or row.get("symbol") or sid or "").upper()[:20]
        if not acronym:
            continue
        market = market_lookup.get(acronym, {})
        name = str(row.get("name") or row.get("stock_name") or market.get("name") or acronym)[:120]
        def f(*keys):
            for k in keys:
                try:
                    v = row.get(k)
                    if v is not None and v != "":
                        return float(v)
                except Exception:
                    pass
            return None
        shares = f("shares", "quantity", "owned", "amount", "holdings")
        avg_buy = f("average_buy_price", "avg_buy_price", "bought_price", "buy_price", "purchase_price", "price_bought")
        cur = f("current_price", "price", "market_price", "value")
        if cur is None:
            cur = market.get("current_price")
        try:
            cur = float(cur) if cur is not None else None
        except Exception:
            cur = None
        est_value = (shares * cur) if shares is not None and cur is not None else None
        est_profit = (shares * (cur - avg_buy)) if shares is not None and cur is not None and avg_buy else None
        est_pct = ((cur - avg_buy) / avg_buy * 100.0) if cur is not None and avg_buy else None
        out.append({
            "stock_id": str(sid or acronym),
            "acronym": acronym,
            "name": name,
            "shares": shares,
            "average_buy_price": avg_buy,
            "current_price": cur,
            "estimated_value": est_value,
            "estimated_profit": est_profit,
            "estimated_profit_pct": est_pct,
        })
    return out


def fetch_user_stock_holdings(api_key: str, market_lookup=None):
    """Best-effort current holdings read. If a user's key does not allow this,
    the scan continues using shared market prediction data only.
    """
    try:
        return normalize_user_stock_holdings(torn_get("user", "stocks", api_key), market_lookup)
    except Exception:
        return []


def save_user_stock_holding_snapshots(torn_id: int, holdings):
    if not holdings:
        return 0
    stamp = now_iso()
    with db() as conn:
        for h in holdings:
            conn.execute(
                """
                INSERT INTO user_stock_holding_snapshots(torn_id, stock_id, acronym, name, shares, average_buy_price,
                    current_price, estimated_value, estimated_profit, estimated_profit_pct, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (torn_id, h.get("stock_id"), h.get("acronym"), h.get("name"), h.get("shares"), h.get("average_buy_price"),
                 h.get("current_price"), h.get("estimated_value"), h.get("estimated_profit"), h.get("estimated_profit_pct"), stamp),
            )
    return len(holdings)


def stock_history_learning(acronym: str, torn_id: int = None):
    """Return a small score/confidence adjustment from past prediction outcomes
    and the user's own recorded holdings performance.
    """
    acronym = str(acronym or "").upper()
    global_count = 0
    global_avg = None
    global_win = None
    user_count = 0
    user_avg = None
    with db() as conn:
        gr = conn.execute(
            """
            SELECT COUNT(*) AS c, AVG(actual_24h_pct) AS avg_pct, AVG(was_profitable) AS win_rate
            FROM stock_predictions
            WHERE acronym=? AND actual_24h_pct IS NOT NULL
            """,
            (acronym,),
        ).fetchone()
        if gr:
            global_count = int(gr["c"] or 0)
            global_avg = gr["avg_pct"]
            global_win = gr["win_rate"]
        if torn_id:
            ur = conn.execute(
                """
                SELECT COUNT(*) AS c, AVG(estimated_profit_pct) AS avg_pct
                FROM user_stock_holding_snapshots
                WHERE torn_id=? AND acronym=? AND estimated_profit_pct IS NOT NULL
                """,
                (torn_id, acronym),
            ).fetchone()
            if ur:
                user_count = int(ur["c"] or 0)
                user_avg = ur["avg_pct"]
    bonus = 0.0
    conf_bonus = 0.0
    parts = []
    if global_count >= 3 and global_avg is not None:
        bonus += max(-10.0, min(10.0, float(global_avg) * 1.8))
        conf_bonus += min(8.0, global_count * 0.5)
        parts.append(f"past global picks avg {float(global_avg):+.2f}% over {global_count}")
    if global_count >= 3 and global_win is not None:
        bonus += max(-5.0, min(5.0, (float(global_win) - 0.5) * 10.0))
    if user_count >= 2 and user_avg is not None:
        bonus += max(-8.0, min(8.0, float(user_avg) * 0.9))
        conf_bonus += min(6.0, user_count * 0.7)
        parts.append(f"your recorded holdings avg {float(user_avg):+.2f}% over {user_count}")
    return {
        "bonus": round(max(-18.0, min(18.0, bonus)), 2),
        "confidence_bonus": round(max(0.0, min(14.0, conf_bonus)), 1),
        "global_count": global_count,
        "global_avg_pct": round(float(global_avg), 3) if global_avg is not None else None,
        "global_win_rate": round(float(global_win) * 100.0, 1) if global_win is not None else None,
        "user_count": user_count,
        "user_avg_pct": round(float(user_avg), 3) if user_avg is not None else None,
        "reason": "; ".join(parts),
    }


def stock_learning_summary(torn_id: int):
    """Compact visibility summary so users can see what is shared vs private.
    Shared global learning uses prediction outcomes only; personal holdings stay private
    but can still adjust that user's own stock scoring.
    """
    with db() as conn:
        global_results = conn.execute(
            """
            SELECT COUNT(*) AS checked,
                   AVG(actual_24h_pct) AS avg_result_pct,
                   AVG(was_profitable) AS win_rate
            FROM stock_predictions
            WHERE actual_24h_pct IS NOT NULL
            """
        ).fetchone()
        global_stocks = conn.execute(
            """
            SELECT COUNT(DISTINCT acronym) AS c
            FROM stock_predictions
            WHERE actual_24h_pct IS NOT NULL
            """
        ).fetchone()
        user_snapshots = conn.execute(
            """
            SELECT COUNT(*) AS snapshots,
                   COUNT(DISTINCT acronym) AS stocks,
                   AVG(estimated_profit_pct) AS avg_profit_pct,
                   MAX(created_at) AS last_seen
            FROM user_stock_holding_snapshots
            WHERE torn_id=?
            """,
            (torn_id,),
        ).fetchone()
    checked = int(global_results["checked"] or 0) if global_results else 0
    win_rate = global_results["win_rate"] if global_results else None
    avg_result = global_results["avg_result_pct"] if global_results else None
    snapshots = int(user_snapshots["snapshots"] or 0) if user_snapshots else 0
    user_stocks = int(user_snapshots["stocks"] or 0) if user_snapshots else 0
    last_seen = user_snapshots["last_seen"] if user_snapshots else None
    global_stock_count = int(global_stocks["c"] or 0) if global_stocks else 0
    return {
        "global_results_checked": checked,
        "global_stocks_learned": global_stock_count,
        "global_avg_result_pct": round(float(avg_result), 3) if avg_result is not None else None,
        "global_win_rate": round(float(win_rate) * 100.0, 1) if win_rate is not None else None,
        "user_stock_snapshots": snapshots,
        "user_stocks_tracked": user_stocks,
        "user_avg_profit_pct": round(float(user_snapshots["avg_profit_pct"]), 3) if user_snapshots and user_snapshots["avg_profit_pct"] is not None else None,
        "user_last_seen": last_seen,
        "shared_note": "Global prediction outcomes help everyone. Personal holdings stay private and only adjust that user's scoring unless converted into anonymous aggregated results.",
    }



def brain_strength_summary(torn_id: int, stock_learning: dict, item_samples: int, travel_samples: int, auto=None):
    """Overall visible brain strength for the Overview page.
    Step 10.8: this percentage is intentionally data-maturity based.
    It should not hit 100% just because the backend is healthy or one early signal looks good.
    More stored snapshots, checked results, and contributing users raise the cap over time.
    """
    try:
        stock_checked = int(stock_learning.get("global_results_checked") or 0)
        user_stock_snaps = int(stock_learning.get("user_stock_snapshots") or 0)
        stock_samples = stock_checked + user_stock_snaps
    except Exception:
        stock_checked = 0
        user_stock_snaps = 0
        stock_samples = 0
    try:
        item_samples = int(item_samples or 0)
    except Exception:
        item_samples = 0
    try:
        travel_samples = int(travel_samples or 0)
    except Exception:
        travel_samples = 0

    total_samples = stock_samples + item_samples + travel_samples

    # Data maturity score: 100% requires a lot of stored information, not just a few scans.
    # These caps keep the brain honest while it is still learning from users.
    if total_samples <= 0:
        info_score = 3.0
    elif total_samples < 100:
        info_score = 8.0 + (total_samples / 100.0) * 12.0       # up to 20
    elif total_samples < 1000:
        info_score = 20.0 + ((total_samples - 100) / 900.0) * 20.0 # up to 40
    elif total_samples < 5000:
        info_score = 40.0 + ((total_samples - 1000) / 4000.0) * 18.0 # up to 58
    elif total_samples < 20000:
        info_score = 58.0 + ((total_samples - 5000) / 15000.0) * 17.0 # up to 75
    elif total_samples < 75000:
        info_score = 75.0 + ((total_samples - 20000) / 55000.0) * 15.0 # up to 90
    else:
        info_score = min(100.0, 90.0 + ((total_samples - 75000) / 25000.0) * 10.0)

    win_rate = stock_learning.get("global_win_rate")
    if win_rate is None or stock_checked < 25:
        accuracy_score = 18.0 if total_samples < 100 else 30.0
    else:
        try:
            wr = float(win_rate)
        except Exception:
            wr = 50.0
        # Early checked results are useful, but do not let accuracy dominate until there are many checked outcomes.
        raw_accuracy = max(15.0, min(95.0, 50.0 + ((wr - 50.0) * 1.25)))
        confidence_cap = min(95.0, 35.0 + (stock_checked / 500.0) * 60.0)
        accuracy_score = min(raw_accuracy, confidence_cap)

    if auto and int(auto["last_ok"] or 0) == 1:
        health_score = 80.0
    elif auto:
        health_score = 45.0
    else:
        health_score = 20.0

    module_count = 0
    if stock_samples >= 25:
        module_count += 1
    if item_samples >= 25:
        module_count += 1
    if travel_samples >= 25:
        module_count += 1
    balance_score = (module_count / 3.0) * 100.0

    # Users contribution is calculated below, but give it a placeholder here.
    users_contributing = 0

    score = (info_score * 0.62) + (accuracy_score * 0.18) + (health_score * 0.08) + (balance_score * 0.12)

    # Hard maturity caps: prevents showing 100% while it is still learning.
    maturity_cap = 15.0
    if total_samples >= 100: maturity_cap = 28.0
    if total_samples >= 1000: maturity_cap = 45.0
    if total_samples >= 5000: maturity_cap = 62.0
    if total_samples >= 20000: maturity_cap = 80.0
    if total_samples >= 75000 and stock_checked >= 500: maturity_cap = 92.0
    if total_samples >= 100000 and stock_checked >= 1000: maturity_cap = 100.0

    score = round(max(0.0, min(score, maturity_cap, 100.0)), 1)
    if score >= 85:
        label = "Sharp"
    elif score >= 65:
        label = "Strong"
    elif score >= 35:
        label = "Growing"
    else:
        label = "Learning"

    try:
        with db() as conn:
            user_rows = conn.execute(
                """
                SELECT COUNT(DISTINCT uid) AS c FROM (
                    SELECT captured_by_torn_id AS uid FROM stock_snapshots WHERE captured_by_torn_id IS NOT NULL
                    UNION SELECT captured_by_torn_id AS uid FROM item_market_snapshots WHERE captured_by_torn_id IS NOT NULL
                    UNION SELECT captured_by_torn_id AS uid FROM travel_route_snapshots WHERE captured_by_torn_id IS NOT NULL
                ) x
                """
            ).fetchone()
        users_contributing = int(user_rows["c"] or 0) if user_rows else 0
    except Exception:
        users_contributing = 0

    reasons = []
    if total_samples < 100:
        reasons.append("Very early learning: needs more stock, item, and travel snapshots")
    elif total_samples < 1000:
        reasons.append("Learning from early data, but not enough history for high confidence")
    elif total_samples < 5000:
        reasons.append("Growing data bank: predictions should start improving")
    elif total_samples < 20000:
        reasons.append("Solid history building, but still not fully matured")
    else:
        reasons.append("Large stored history is improving the prediction brain")
    if win_rate is not None:
        reasons.append(f"Global stock win rate is {float(win_rate):.1f}%")
    if auto and int(auto["last_ok"] or 0) == 1:
        reasons.append("Backend watcher is online")
    else:
        reasons.append("Backend watcher needs a clean scan")

    return {
        "score": score,
        "label": label,
        "info_score": round(info_score, 1),
        "accuracy_score": round(accuracy_score, 1),
        "health_score": round(health_score, 1),
        "balance_score": round(balance_score, 1),
        "total_samples": total_samples,
        "stock_samples": stock_samples,
        "item_samples": item_samples,
        "travel_samples": travel_samples,
        "users_contributing": users_contributing,
        "global_win_rate": win_rate,
        "reason": " · ".join(reasons),
        "maturity_cap": round(maturity_cap, 1),
        "stock_checked": stock_checked,
        "user_stock_snapshots": user_stock_snaps,
    }

def _row_price(row):
    try:
        return float(row["current_price"])
    except Exception:
        return None


def _pct_change(current, old):
    try:
        current = float(current)
        old = float(old)
        if old == 0:
            return 0.0
        return ((current - old) / old) * 100.0
    except Exception:
        return 0.0


def _oldest_price_since(acronym: str, since_iso: str, fallback_price: float):
    with db() as conn:
        row = conn.execute(
            """
            SELECT current_price FROM stock_snapshots
            WHERE acronym=? AND created_at>=?
            ORDER BY created_at ASC, id ASC LIMIT 1
            """,
            (acronym, since_iso),
        ).fetchone()
    return _row_price(row) if row else float(fallback_price)


def stock_stats(acronym: str, current_price: float):
    """Pattern-ready stock stats.

    Uses only stored Torn API stock snapshots. No Torn page scraping and no account action.
    The goal is to detect simple repeatable behavior: dips, turns, bounces, breakouts,
    over-extension, and falling-knife risk.
    """
    current_price = float(current_price)
    now_dt = datetime.now(timezone.utc)
    since_1 = (now_dt - timedelta(hours=1)).replace(microsecond=0).isoformat()
    since_6 = (now_dt - timedelta(hours=6)).replace(microsecond=0).isoformat()
    since_24 = (now_dt - timedelta(hours=24)).replace(microsecond=0).isoformat()
    since_7 = (now_dt - timedelta(days=7)).replace(microsecond=0).isoformat()
    with db() as conn:
        def stat_since(since):
            return conn.execute(
                """
                SELECT MIN(current_price) mn, MAX(current_price) mx, AVG(current_price) av, COUNT(*) c
                FROM stock_snapshots WHERE acronym=? AND created_at>=?
                """,
                (acronym, since),
            ).fetchone()
        r1 = stat_since(since_1)
        r6 = stat_since(since_6)
        r24 = stat_since(since_24)
        r7 = stat_since(since_7)
        first = conn.execute(
            "SELECT current_price FROM stock_snapshots WHERE acronym=? ORDER BY id ASC LIMIT 1", (acronym,)
        ).fetchone()
        prev = conn.execute(
            "SELECT current_price FROM stock_snapshots WHERE acronym=? ORDER BY id DESC LIMIT 2", (acronym,)
        ).fetchall()
        support_row = conn.execute(
            """
            SELECT MIN(current_price) mn FROM stock_snapshots
            WHERE acronym=? AND created_at>=?
            """,
            (acronym, since_7),
        ).fetchone()

    def val(row, key, default=None):
        return row[key] if row and row[key] is not None else default

    def pack(row):
        mn = float(val(row, "mn", current_price) or current_price)
        mx = float(val(row, "mx", current_price) or current_price)
        av = float(val(row, "av", current_price) or current_price)
        c = int(val(row, "c", 0) or 0)
        return mn, mx, av, c

    mn1, mx1, avg1, c1 = pack(r1)
    mn6, mx6, avg6, c6 = pack(r6)
    mn24, mx24, avg24, c24 = pack(r24)
    mn7, mx7, avg7, c7 = pack(r7)
    range24 = max(mx24 - mn24, 0.0001)
    range7 = max(mx7 - mn7, 0.0001)
    position24 = (current_price - mn24) / range24 if mx24 > mn24 else 0.5
    position7 = (current_price - mn7) / range7 if mx7 > mn7 else 0.5
    prev_price = _row_price(prev[1]) if len(prev) > 1 else current_price
    tick_pct = _pct_change(current_price, prev_price)
    first_price = _row_price(first) if first else current_price
    all_pct = _pct_change(current_price, first_price)
    change_1h_pct = _pct_change(current_price, _oldest_price_since(acronym, since_1, current_price))
    change_6h_pct = _pct_change(current_price, _oldest_price_since(acronym, since_6, current_price))
    change_24h_pct = _pct_change(current_price, _oldest_price_since(acronym, since_24, current_price))
    change_7d_pct = _pct_change(current_price, _oldest_price_since(acronym, since_7, current_price))
    volatility1 = (max(mx1 - mn1, 0.0) / avg1 * 100.0) if avg1 else 0.0
    volatility6 = (max(mx6 - mn6, 0.0) / avg6 * 100.0) if avg6 else 0.0
    volatility24 = (range24 / avg24 * 100.0) if avg24 else 0.0
    volatility7 = (range7 / avg7 * 100.0) if avg7 else 0.0

    # Support touches: how often recent prices were close to the 7-day low.
    support_low = float(support_row["mn"] or mn7) if support_row else mn7
    support_ceiling = support_low * 1.03 if support_low else current_price
    with db() as conn:
        touch = conn.execute(
            """
            SELECT COUNT(*) AS c FROM stock_snapshots
            WHERE acronym=? AND created_at>=? AND current_price<=?
            """,
            (acronym, since_7, support_ceiling),
        ).fetchone()
    support_touches = int((touch or {}).get("c", 0) or 0) if hasattr(touch, 'get') else int(touch["c"] or 0)

    return {
        "min1": mn1, "max1": mx1, "avg1": avg1, "count1": c1,
        "min6": mn6, "max6": mx6, "avg6": avg6, "count6": c6,
        "min24": mn24, "max24": mx24, "avg24": avg24, "count24": c24,
        "min7": mn7, "max7": mx7, "avg7": avg7, "count7": c7,
        "position24": max(0.0, min(1.0, position24)),
        "position7": max(0.0, min(1.0, position7)),
        "tick_pct": tick_pct, "all_pct": all_pct,
        "change_1h_pct": change_1h_pct,
        "change_6h_pct": change_6h_pct,
        "change_24h_pct": change_24h_pct,
        "change_7d_pct": change_7d_pct,
        "volatility1": volatility1,
        "volatility6": volatility6,
        "volatility24": volatility24,
        "volatility7": volatility7,
        "support_touches": support_touches,
    }


def detect_stock_pattern(st: dict):
    pos7 = float(st.get("position7") or 0.5)
    pos24 = float(st.get("position24") or 0.5)
    ch1 = float(st.get("change_1h_pct") or 0.0)
    ch6 = float(st.get("change_6h_pct") or 0.0)
    ch24 = float(st.get("change_24h_pct") or 0.0)
    vol24 = float(st.get("volatility24") or 0.0)
    c7 = int(st.get("count7") or 0)
    touches = int(st.get("support_touches") or 0)

    label = "Learning Pattern"
    reason = "Needs more stored stock checks before the pattern is strong."
    confidence = min(38.0, 12.0 + c7 * 1.3)
    pattern_bonus = 0.0

    if pos7 <= 0.30 and touches >= 3 and (ch1 >= 0 or ch6 >= -0.20):
        label = "Bounce Setup"
        reason = "Price is near a repeated low/support area and is no longer dropping hard."
        confidence = 42.0 + min(30.0, touches * 4.0) + min(12.0, max(0.0, ch1) * 5.0)
        pattern_bonus = 14.0
    elif ch6 < -0.15 and ch1 > 0.05 and pos7 < 0.65:
        label = "Early Turn"
        reason = "Short-term price action is turning upward after a recent dip."
        confidence = 48.0 + min(22.0, abs(ch6) * 4.0 + ch1 * 6.0)
        pattern_bonus = 10.0
    elif ch1 < -0.10 and ch6 < -0.25 and pos7 < 0.35:
        label = "Risky Dip"
        reason = "It is cheap, but the drop is still active. Watch instead of chasing."
        confidence = 45.0 + min(25.0, abs(ch6) * 5.0)
        pattern_bonus = -10.0
    elif pos24 >= 0.86 and ch1 > 0 and ch6 > 0:
        label = "Breakout Watch"
        reason = "Price is near a 24h high and still moving up; stronger momentum but less cheap."
        confidence = 45.0 + min(24.0, ch6 * 4.0)
        pattern_bonus = 4.0
    elif pos7 >= 0.82 and ch24 > 0.35:
        label = "Overextended"
        reason = "Price is close to recent highs after rising; risk of buying late."
        confidence = 48.0 + min(20.0, ch24 * 3.0)
        pattern_bonus = -12.0
    elif ch6 > 0.05 and ch24 > 0.05 and pos7 < 0.80:
        label = "Slow Climber"
        reason = "The price is moving up steadily without being too close to its high."
        confidence = 42.0 + min(25.0, (ch6 + ch24) * 3.5)
        pattern_bonus = 7.0
    elif vol24 < 0.20 and c7 >= 8:
        label = "Flat / Waiting"
        reason = "Price has not moved enough recently for a strong 24h opportunity."
        confidence = 45.0
        pattern_bonus = -4.0

    # Data cap: do not overtrust patterns with tiny history.
    data_cap = 30.0 + min(65.0, c7 * 2.0)
    confidence = max(10.0, min(95.0, confidence, data_cap))
    return {
        "pattern_label": label,
        "pattern_reason": reason,
        "pattern_confidence": round(confidence, 1),
        "pattern_bonus": round(pattern_bonus, 2),
        "support_touches": touches,
    }


def score_stock(stock, torn_id: int = None, include_learning: bool = True):
    price = float(stock["current_price"])
    st = stock_stats(stock["acronym"], price)
    pat = detect_stock_pattern(st)

    price_position_score = ((1 - st["position24"]) * 16) + ((1 - st["position7"]) * 14)
    momentum_score = max(-8.0, min(18.0, (st["change_1h_pct"] * 5.0) + (st["change_6h_pct"] * 2.2)))
    bounce_score = min(20.0, (pat["support_touches"] * 2.2) + (14.0 if pat["pattern_label"] == "Bounce Setup" else 0.0))
    volatility_score = max(0.0, min(15.0, st["volatility24"] * 3.2))
    overheat_penalty = max(0.0, st["position7"] - 0.78) * 30.0
    falling_penalty = 10.0 if pat["pattern_label"] == "Risky Dip" else 0.0
    base_score = max(0.0, price_position_score + momentum_score + bounce_score + volatility_score + pat["pattern_bonus"] - overheat_penalty - falling_penalty)

    data_points = min(100.0, (st["count1"] * 8) + (st["count6"] * 5) + (st["count24"] * 3) + (st["count7"] * 1.5))
    confidence = max(10.0, min(95.0, 16.0 + data_points + min(18.0, st["volatility24"] * 2.0) + (pat["pattern_confidence"] * 0.20)))
    expected = max(-3.5, min(13.0, (st["volatility24"] * 0.45) + (1 - st["position24"]) * 2.2 + max(0, st["change_1h_pct"] * 0.45) + max(0, st["change_6h_pct"] * 0.12)))

    learning = stock_history_learning(stock["acronym"], torn_id) if include_learning else {"bonus": 0, "confidence_bonus": 0, "reason": ""}
    score = max(0.0, base_score + float(learning.get("bonus") or 0))
    confidence = max(10.0, min(97.0, confidence + float(learning.get("confidence_bonus") or 0)))
    if learning.get("global_avg_pct") is not None:
        expected += max(-1.5, min(1.5, float(learning["global_avg_pct"]) * 0.25))
    if learning.get("user_avg_pct") is not None:
        expected += max(-1.0, min(1.0, float(learning["user_avg_pct"]) * 0.15))
    expected = max(-3.5, min(13.0, expected))

    reasons = [pat["pattern_reason"]]
    if st["position24"] < 0.35: reasons.append("near its 24h low")
    if st["position7"] < 0.40: reasons.append("below its 7d range midpoint")
    if st["change_1h_pct"] > 0: reasons.append("1h movement is green")
    if st["change_6h_pct"] > 0: reasons.append("6h movement is improving")
    if st["volatility24"] > 0.5: reasons.append("has enough movement for a 24h trade")
    if learning.get("reason"):
        reasons.append("history learning: " + learning["reason"])

    return {
        **stock, **st, **pat,
        "base_score": round(base_score, 2),
        "price_position_score": round(price_position_score, 2),
        "momentum_score": round(momentum_score, 2),
        "bounce_score": round(bounce_score, 2),
        "volatility_score": round(volatility_score, 2),
        "history_bonus": round(float(learning.get("bonus") or 0), 2),
        "history_global_count": learning.get("global_count", 0),
        "history_global_avg_pct": learning.get("global_avg_pct"),
        "history_global_win_rate": learning.get("global_win_rate"),
        "history_user_count": learning.get("user_count", 0),
        "history_user_avg_pct": learning.get("user_avg_pct"),
        "score": round(score, 2),
        "confidence": round(confidence, 1),
        "expected_24h_pct": round(expected, 2),
        "reason": ", ".join(reasons),
    }


def save_stock_pattern_results(torn_id: int, scored_rows):
    """Store pattern score snapshots so the brain can review which patterns worked later."""
    stamp = now_iso()
    rows = list(scored_rows or [])[:25]
    if not rows:
        return 0
    with db() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO stock_pattern_results(torn_id, stock_id, acronym, name, current_price,
                    pattern_label, pattern_confidence, pattern_score, support_touches,
                    trend_1h_pct, trend_6h_pct, trend_24h_pct, trend_7d_pct,
                    position_24h, position_7d, volatility_24h, reason, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (torn_id, r.get("stock_id"), r.get("acronym"), r.get("name"), r.get("current_price"),
                 r.get("pattern_label"), r.get("pattern_confidence"), r.get("score"), r.get("support_touches"),
                 r.get("change_1h_pct"), r.get("change_6h_pct"), r.get("change_24h_pct"), r.get("change_7d_pct"),
                 r.get("position24"), r.get("position7"), r.get("volatility24"), r.get("reason"), stamp),
            )
    return len(rows)


def latest_active_stock_pick():
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM stock_predictions WHERE scope='global' AND status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _parse_iso_age_hours(value):
    """Return age in hours for an ISO timestamp, or a large value if unreadable."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 9999.0


def choose_stock_pick(torn_id: int, stocks, force=False):
    """
    Pick one 24h stock, but do not freeze forever.

    Earlier versions compared the new best score against the saved score from when
    the old pick was first created. That made a pick feel stuck because the old
    saved score never decayed. This version rescans and re-scores the current
    pick every time, then uses the live score comparison.
    """
    scored = sorted([score_stock(s, torn_id) for s in stocks], key=lambda x: x["score"], reverse=True)
    if not scored:
        return {"pick": None, "changed": False, "ranked": [], "decision": "No scored stocks available."}

    try:
        save_stock_pattern_results(torn_id, scored)
    except Exception:
        pass

    best = scored[0]
    current = latest_active_stock_pick()
    current_live = None
    if current:
        for row in scored:
            if str(row.get("acronym")) == str(current.get("acronym")):
                current_live = row
                break

    changed = False
    changed_reason = ""
    decision = "Holding current pick."

    # Lower default gap. We still avoid flipping every scan, but the pick can move.
    gap = 6.0
    try:
        with db() as conn:
            row = conn.execute("SELECT setting_value FROM settings WHERE torn_id=? AND setting_key='stock_pick_change_score_gap'", (torn_id,)).fetchone()
            if row:
                # Clamp old saved settings like 15 down to a practical range so older users do not get stuck forever.
                gap = max(3.0, min(float(row["setting_value"]), 8.0))
    except Exception:
        pass

    age_hours = _parse_iso_age_hours(current.get("created_at")) if current else 9999.0
    current_score_live = float(current_live["score"]) if current_live else -999.0
    current_conf_live = float(current_live["confidence"]) if current_live else 0.0
    best_score = float(best["score"])
    best_conf = float(best["confidence"])

    should_replace = False
    replace_kind = ""

    if force or current is None:
        should_replace = True
        replace_kind = "new pick" if current is None else "forced refresh"
    elif age_hours >= 24:
        should_replace = True
        replace_kind = "24h refresh"
    elif current_live is None:
        should_replace = True
        replace_kind = "old pick missing from live stock scan"
    elif best["acronym"] != current["acronym"] and best_score >= current_score_live + gap:
        should_replace = True
        replace_kind = "better live score"
    elif best["acronym"] != current["acronym"] and current_score_live < 28 and best_score >= 35:
        should_replace = True
        replace_kind = "current pick weakened"
    elif best["acronym"] != current["acronym"] and best_conf >= current_conf_live + 20 and best_score >= current_score_live + 3:
        should_replace = True
        replace_kind = "higher confidence replacement"

    if should_replace:
        created = now_iso()
        with db() as conn:
            if current:
                conn.execute("UPDATE stock_predictions SET status='replaced', replaced_at=? WHERE id=?", (created, current["id"]))
                changed = True
                changed_reason = (
                    f"{best['acronym']} replaced {current['acronym']} ({replace_kind}). "
                    f"Live score {best_score:.2f} vs {current_score_live:.2f}; pick age {age_hours:.1f}h."
                )
            else:
                changed_reason = f"{best['acronym']} selected as first active 24h pick."

            conn.execute(
                """
                INSERT INTO stock_predictions(scope, chosen_by_torn_id, stock_id, acronym, name, pick_price, score, confidence, expected_24h_pct, reason, created_at)
                VALUES('global',?,?,?,?,?,?,?,?,?,?)
                """,
                (torn_id, best["stock_id"], best["acronym"], best["name"], best["current_price"], best["score"], best["confidence"], best["expected_24h_pct"], best["reason"] + f"; {replace_kind}", created),
            )
            new_row = conn.execute(
                "SELECT id FROM stock_predictions WHERE scope='global' AND acronym=? AND created_at=? ORDER BY id DESC LIMIT 1",
                (best["acronym"], created),
            ).fetchone()
            new_id = new_row["id"] if new_row else None
            if current and new_id:
                conn.execute("UPDATE stock_predictions SET replaced_by_id=? WHERE id=?", (new_id, current["id"]))
                conn.execute(
                    "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                    (torn_id, "stock_changed", "Stock Brain changed today's pick", changed_reason, "https://www.torn.com/page.php?sid=stocks", created),
                )
        current = latest_active_stock_pick()
        decision = changed_reason
    else:
        decision = (
            f"Holding {current['acronym']}. Best live score is {best['acronym']} {best_score:.2f}; "
            f"current live score {current_score_live:.2f}; change gap {gap:.1f}; age {age_hours:.1f}h."
        )

    return {
        "pick": current,
        "changed": changed,
        "changed_reason": changed_reason,
        "ranked": scored[:10],
        "decision": decision,
        "current_live_score": round(current_score_live, 2) if current else None,
        "pick_age_hours": round(age_hours, 2) if current else None,
        "change_gap_used": gap,
    }

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


# ---------- Step 3: Item Market Scanner ----------

def normalize_items(payload):
    raw = payload.get("items", payload) if isinstance(payload, dict) else payload
    rows = raw.items() if isinstance(raw, dict) else enumerate(raw or [])
    out = []
    for key, item in rows:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id") or item.get("item_id") or key)
        except Exception:
            continue
        name = str(item.get("name") or item.get("item_name") or f"Item {item_id}")[:120]
        def f(*keys):
            for k in keys:
                try:
                    if item.get(k) is not None:
                        return float(item.get(k))
                except Exception:
                    pass
            return None
        out.append({
            "item_id": item_id,
            "name": name,
            "item_type": str(item.get("type") or item.get("category") or "")[:80],
            "buy_price": f("buy_price", "buyPrice"),
            "sell_value": f("sell_value", "sellPrice", "value"),
            "market_value": f("market_value", "marketValue", "circulation_value"),
        })
    return out


def refresh_item_catalog(api_key: str):
    data = torn_get("torn", "items", api_key)
    items = normalize_items(data)
    stamp = now_iso()
    with db() as conn:
        for it in items:
            conn.execute(
                """
                INSERT INTO item_catalog(item_id, name, item_type, buy_price, sell_value, market_value, updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    name=excluded.name,
                    item_type=excluded.item_type,
                    buy_price=excluded.buy_price,
                    sell_value=excluded.sell_value,
                    market_value=excluded.market_value,
                    updated_at=excluded.updated_at
                """,
                (it["item_id"], it["name"], it.get("item_type"), it.get("buy_price"), it.get("sell_value"), it.get("market_value"), stamp),
            )
    return items


def find_catalog_items(query: str, limit: int = 25):
    q = (query or "").strip()
    with db() as conn:
        if q:
            rows = conn.execute(
                """
                SELECT item_id, name, item_type, market_value, sell_value
                FROM item_catalog
                WHERE name LIKE ? OR CAST(item_id AS TEXT)=?
                ORDER BY CASE WHEN CAST(item_id AS TEXT)=? THEN 0 WHEN name LIKE ? THEN 1 ELSE 2 END, name
                LIMIT ?
                """,
                (f"%{q}%", q, q, f"{q}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT item_id, name, item_type, market_value, sell_value FROM item_catalog ORDER BY name LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def fetch_item_market(api_key: str, item_id: int):
    # Classic Torn market endpoint shape. Kept isolated so API v2 can be swapped here later.
    url = f"{TORN_API_BASE}/market/{int(item_id)}"
    params = {"selections": "itemmarket", "key": api_key}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise ValueError(f"Torn API error {err.get('code')}: {err.get('error')}")
    return data


def normalize_item_market(payload):
    raw = None
    if isinstance(payload, dict):
        raw = payload.get("itemmarket") or payload.get("listings") or payload.get("market") or payload.get("items") or payload
    else:
        raw = payload
    if isinstance(raw, dict):
        rows = raw.values()
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    prices = []
    total_qty = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = row.get("price") or row.get("cost") or row.get("amount") or row.get("market_price")
        qty = row.get("quantity") or row.get("qty") or row.get("amount_available") or 1
        try:
            price = float(price)
            qty = int(float(qty))
        except Exception:
            continue
        if price > 0:
            prices.append(price)
            total_qty += max(1, qty)
    if not prices:
        return {"lowest_price": None, "avg_price": None, "listing_count": 0, "total_quantity": 0}
    return {
        "lowest_price": min(prices),
        "avg_price": sum(prices) / len(prices),
        "listing_count": len(prices),
        "total_quantity": total_qty,
    }


def catalog_market_fallback(item_id: int):
    """Use Torn's item catalog market_value when live item listings fail.
    This stops the PDA from sitting on Waiting while the backend keeps trying live scans.
    """
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT market_value, sell_value, buy_price, name FROM item_catalog WHERE item_id=?",
                (int(item_id),),
            ).fetchone()
        if not row:
            return None
        price = row["market_value"] or row["sell_value"] or row["buy_price"]
        if price is None:
            return None
        price = float(price)
        if price <= 0:
            return None
        return {"lowest_price": price, "avg_price": price, "listing_count": 0, "total_quantity": 0, "source": "catalog_fallback"}
    except Exception:
        return None


def item_history_stats(item_id: int, current_price=None):
    since_24 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    since_7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    since_365 = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    with db() as conn:
        def row_since(since):
            return conn.execute(
                """
                SELECT MIN(lowest_price) mn, MAX(lowest_price) mx, AVG(lowest_price) av, COUNT(*) c
                FROM item_market_snapshots
                WHERE item_id=? AND created_at>=? AND lowest_price IS NOT NULL
                """,
                (item_id, since),
            ).fetchone()
        r24 = row_since(since_24)
        r7 = row_since(since_7)
        r365 = row_since(since_365)
        first = conn.execute(
            "SELECT lowest_price FROM item_market_snapshots WHERE item_id=? AND lowest_price IS NOT NULL ORDER BY id ASC LIMIT 1",
            (item_id,),
        ).fetchone()
    def val(row, key, default=None):
        return row[key] if row and row[key] is not None else default
    base = float(current_price or val(r7, "av", 0) or 0)
    first_price = val(first, "lowest_price", base)
    year_pct = ((base - first_price) / first_price * 100) if first_price else 0
    return {
        "min24": val(r24, "mn", base), "max24": val(r24, "mx", base), "avg24": val(r24, "av", base), "count24": int(val(r24, "c", 0) or 0),
        "min7": val(r7, "mn", base), "max7": val(r7, "mx", base), "avg7": val(r7, "av", base), "count7": int(val(r7, "c", 0) or 0),
        "min365": val(r365, "mn", base), "max365": val(r365, "mx", base), "avg365": val(r365, "av", base), "count365": int(val(r365, "c", 0) or 0),
        "year_change_pct": round(year_pct, 2),
    }


def upsert_watch_item(torn_id: int, item_id: int, name: str, buy_zone=None, sell_zone=None):
    stamp = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO item_watchlist(torn_id, item_id, name, buy_zone, sell_zone, enabled, created_at, updated_at)
            VALUES(?,?,?,?,?,1,?,?)
            ON CONFLICT(torn_id, item_id) DO UPDATE SET
                name=excluded.name,
                buy_zone=COALESCE(excluded.buy_zone, item_watchlist.buy_zone),
                sell_zone=COALESCE(excluded.sell_zone, item_watchlist.sell_zone),
                enabled=1,
                updated_at=excluded.updated_at
            """,
            (torn_id, int(item_id), name, buy_zone, sell_zone, stamp, stamp),
        )
    return True


def maybe_create_item_signal(torn_id: int, watch, current_price, stats):
    if current_price is None:
        return None
    with db() as conn:
        alerts_enabled = get_setting_for_user(conn, torn_id, "alerts_enabled", "true") != "false"
        item_alerts_enabled = get_setting_for_user(conn, torn_id, "item_alerts_enabled", "true") != "false"
        try:
            buy_discount = float(get_setting_for_user(conn, torn_id, "item_default_buy_discount_pct", "3"))
        except Exception:
            buy_discount = 3.0
        try:
            sell_markup = float(get_setting_for_user(conn, torn_id, "item_default_sell_markup_pct", "6"))
        except Exception:
            sell_markup = 6.0
    if not alerts_enabled or not item_alerts_enabled:
        return None
    buy_discount = max(0.5, min(50, buy_discount)) / 100.0
    sell_markup = max(0.5, min(100, sell_markup)) / 100.0
    buy_zone = watch.get("buy_zone")
    sell_zone = watch.get("sell_zone")
    if buy_zone is None:
        try:
            buy_zone = float(stats.get("avg7") or current_price) * (1 - buy_discount)
        except Exception:
            buy_zone = current_price * (1 - buy_discount)
    if sell_zone is None:
        try:
            sell_zone = float(stats.get("avg7") or current_price) * (1 + sell_markup)
        except Exception:
            sell_zone = current_price * (1 + sell_markup)
    signal = "HOLD"
    reason = "Price is between buy and sell zones."
    if current_price <= float(buy_zone):
        signal = "BUY"
        reason = f"{watch['name']} hit its best buy zone. Current price is at or below target."
    elif current_price >= float(sell_zone):
        signal = "SELL"
        reason = f"{watch['name']} reached its sell zone based on your tracker."
    if signal == "HOLD":
        return None
    max_qty = int(stats.get("total_quantity") or stats.get("listing_count") or 0)
    qty_param = f"&tbmax={max_qty}" if max_qty > 0 else ""
    link = f"https://www.torn.com/imarket.php#/p=shop&step=shop&type=&searchname={requests.utils.quote(str(watch['name']))}{qty_param}"
    created = now_iso()
    with db() as conn:
        # Avoid spamming the same item alert every scan; allow a fresh alert after 6 hours.
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        existing = conn.execute(
            """
            SELECT id FROM item_signals
            WHERE torn_id=? AND item_id=? AND signal=? AND created_at>=?
            ORDER BY id DESC LIMIT 1
            """,
            (torn_id, watch["item_id"], signal, cutoff),
        ).fetchone()
        if existing:
            return None
        conn.execute(
            """
            INSERT INTO item_signals(torn_id, item_id, name, signal, current_price, buy_zone, sell_zone, reason, link, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (torn_id, watch["item_id"], watch["name"], signal, current_price, buy_zone, sell_zone, reason, link, created),
        )
        conn.execute(
            "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
            (torn_id, f"item_{signal.lower()}", f"Item Market {signal}: {watch['name']}", reason + f" Current: ${int(current_price):,}. Max seen: {max_qty if max_qty > 0 else 'unknown'}.", link, created),
        )
    return {"signal": signal, "reason": reason, "link": link, "buy_zone": buy_zone, "sell_zone": sell_zone}


def scan_item_market_for_user(torn_id: int, reason: str = "auto"):
    started = now_iso()
    rows_seen = 0
    signals = []
    try:
        key = get_api_key(torn_id)
        if not key:
            raise ValueError("No Torn API key saved.")
        with db() as conn:
            watch_rows = conn.execute(
                "SELECT id, item_id, name, buy_zone, sell_zone, enabled FROM item_watchlist WHERE torn_id=? AND enabled=1 ORDER BY updated_at DESC LIMIT 40",
                (torn_id,),
            ).fetchall()
        if not watch_rows:
            record_scan_run(torn_id, "ok", 0, "Item Market has no watched items yet.", started, module="item_market")
            return {"ok": True, "items_seen": 0, "signals": [], "message": "No watched items yet."}
        stamp = now_iso()
        with db() as conn:
            for wr in watch_rows:
                w = dict(wr)
                source = "market"
                last_error = None
                try:
                    data = fetch_item_market(key, int(w["item_id"]))
                    m = normalize_item_market(data)
                    if not m.get("lowest_price"):
                        fallback = catalog_market_fallback(int(w["item_id"]))
                        if fallback:
                            m = fallback
                            source = "catalog_fallback"
                            last_error = "Live item listings returned no prices; using Torn catalog market value."
                    rows_seen += 1
                    conn.execute(
                        """
                        INSERT INTO item_market_snapshots(item_id, name, lowest_price, avg_price, listing_count, total_quantity, source, last_error, captured_by_torn_id, created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (w["item_id"], w["name"], m.get("lowest_price"), m.get("avg_price"), m.get("listing_count") or 0, m.get("total_quantity") or 0, source, last_error, torn_id, stamp),
                    )
                    stats = item_history_stats(int(w["item_id"]), m.get("lowest_price"))
                    stats["total_quantity"] = int(m.get("total_quantity") or 0)
                    stats["listing_count"] = int(m.get("listing_count") or 0)
                    sig = maybe_create_item_signal(torn_id, w, m.get("lowest_price"), stats)
                    if sig:
                        signals.append({"item_id": w["item_id"], "name": w["name"], **sig})
                    time.sleep(0.25)
                except Exception as item_err:
                    fallback = catalog_market_fallback(int(w["item_id"]))
                    if fallback:
                        rows_seen += 1
                        msg = str(item_err)[:300]
                        conn.execute(
                            """
                            INSERT INTO item_market_snapshots(item_id, name, lowest_price, avg_price, listing_count, total_quantity, source, last_error, captured_by_torn_id, created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (w["item_id"], w["name"], fallback.get("lowest_price"), fallback.get("avg_price"), 0, 0, "catalog_fallback", msg, torn_id, stamp),
                        )
                        stats = item_history_stats(int(w["item_id"]), fallback.get("lowest_price"))
                        stats["total_quantity"] = int(fallback.get("total_quantity") or 0)
                        stats["listing_count"] = int(fallback.get("listing_count") or 0)
                        sig = maybe_create_item_signal(torn_id, w, fallback.get("lowest_price"), stats)
                        if sig:
                            signals.append({"item_id": w["item_id"], "name": w["name"], **sig})
                    else:
                        conn.execute(
                            "INSERT INTO scan_runs(torn_id, module, status, rows_seen, message, started_at, finished_at) VALUES(?,?,?,?,?,?,?)",
                            (torn_id, "item_market", "item_error", 0, f"{w['name']}: {str(item_err)[:300]}", started, now_iso()),
                        )
        record_scan_run(torn_id, "ok", rows_seen, f"Item Market scan complete. Signals: {len(signals)}", started, module="item_market")
        return {"ok": True, "items_seen": rows_seen, "signals": signals}
    except Exception as e:
        record_scan_run(torn_id, "error", rows_seen, str(e), started, module="item_market")
        return {"ok": False, "error": str(e)}


def latest_item_market_rows(torn_id: int):
    with db() as conn:
        watches = conn.execute(
            "SELECT id, item_id, name, buy_zone, sell_zone, enabled, updated_at FROM item_watchlist WHERE torn_id=? ORDER BY updated_at DESC",
            (torn_id,),
        ).fetchall()
    out = []
    with db() as conn:
        for w in watches:
            snap = conn.execute(
                "SELECT lowest_price, avg_price, listing_count, total_quantity, source, last_error, created_at FROM item_market_snapshots WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (w["item_id"],),
            ).fetchone()
            if not snap:
                fallback = catalog_market_fallback(int(w["item_id"]))
                if fallback:
                    snap = {**fallback, "created_at": None, "last_error": "No live scan yet; showing Torn catalog market value."}
            stats = item_history_stats(int(w["item_id"]), snap["lowest_price"] if snap else None)
            current = snap["lowest_price"] if snap else None
            signal = "WAITING"
            if current is not None:
                bz = w["buy_zone"] if w["buy_zone"] is not None else float(stats.get("avg7") or current) * 0.97
                sz = w["sell_zone"] if w["sell_zone"] is not None else float(stats.get("avg7") or current) * 1.06
                signal = "BUY" if current <= bz else ("SELL" if current >= sz else "HOLD")
            out.append({**dict(w), "latest": dict(snap) if snap else None, "stats": stats, "signal": signal})
    return out


# ---------- Step 4: Points Market Watcher ----------

def fetch_points_market(api_key: str):
    # Classic Torn market endpoint shape. Kept isolated so API v2 can be swapped here later.
    return torn_get("market", "pointsmarket", api_key)


def normalize_points_market(payload):
    raw = None
    if isinstance(payload, dict):
        raw = payload.get("pointsmarket") or payload.get("points") or payload.get("listings") or payload.get("market") or payload
    else:
        raw = payload
    if isinstance(raw, dict):
        rows = raw.values()
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    prices = []
    total_qty = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = row.get("price") or row.get("cost") or row.get("amount") or row.get("market_price") or row.get("point_price")
        qty = row.get("quantity") or row.get("qty") or row.get("points") or row.get("amount_available") or 1
        try:
            price = float(price)
            qty = int(float(qty))
        except Exception:
            continue
        if price > 0:
            prices.append(price)
            total_qty += max(1, qty)
    if not prices:
        return {"lowest_price": None, "avg_price": None, "listing_count": 0, "total_quantity": 0}
    return {
        "lowest_price": min(prices),
        "avg_price": sum(prices) / len(prices),
        "listing_count": len(prices),
        "total_quantity": total_qty,
    }


def points_history_stats(current_price=None):
    since_24 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    since_7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    since_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    since_365 = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    with db() as conn:
        def row_since(since):
            return conn.execute(
                """
                SELECT MIN(lowest_price) mn, MAX(lowest_price) mx, AVG(lowest_price) av, COUNT(*) c
                FROM points_market_snapshots
                WHERE created_at>=? AND lowest_price IS NOT NULL
                """,
                (since,),
            ).fetchone()
        r24 = row_since(since_24)
        r7 = row_since(since_7)
        r30 = row_since(since_30)
        r365 = row_since(since_365)
        first = conn.execute(
            "SELECT lowest_price FROM points_market_snapshots WHERE lowest_price IS NOT NULL ORDER BY id ASC LIMIT 1"
        ).fetchone()
    def val(row, key, default=None):
        return row[key] if row and row[key] is not None else default
    base = float(current_price or val(r7, "av", 0) or 0)
    first_price = val(first, "lowest_price", base)
    year_pct = ((base - first_price) / first_price * 100) if first_price else 0
    return {
        "min24": val(r24, "mn", base), "max24": val(r24, "mx", base), "avg24": val(r24, "av", base), "count24": int(val(r24, "c", 0) or 0),
        "min7": val(r7, "mn", base), "max7": val(r7, "mx", base), "avg7": val(r7, "av", base), "count7": int(val(r7, "c", 0) or 0),
        "min30": val(r30, "mn", base), "max30": val(r30, "mx", base), "avg30": val(r30, "av", base), "count30": int(val(r30, "c", 0) or 0),
        "min365": val(r365, "mn", base), "max365": val(r365, "mx", base), "avg365": val(r365, "av", base), "count365": int(val(r365, "c", 0) or 0),
        "year_change_pct": round(year_pct, 2),
    }


def maybe_create_points_signal(torn_id: int, current_price, stats):
    if current_price is None:
        return None
    with db() as conn:
        alerts_enabled = get_setting_for_user(conn, torn_id, "alerts_enabled", "true") != "false"
        points_alerts_enabled = get_setting_for_user(conn, torn_id, "points_alerts_enabled", "true") != "false"
        custom_buy = get_setting_for_user(conn, torn_id, "points_buy_zone", "")
        custom_sell = get_setting_for_user(conn, torn_id, "points_sell_zone", "")
        try:
            buy_discount = float(get_setting_for_user(conn, torn_id, "points_default_buy_discount_pct", "2"))
        except Exception:
            buy_discount = 2.0
        try:
            sell_markup = float(get_setting_for_user(conn, torn_id, "points_default_sell_markup_pct", "4"))
        except Exception:
            sell_markup = 4.0
    if not alerts_enabled or not points_alerts_enabled:
        return None
    try:
        buy_zone = float(custom_buy) if str(custom_buy).strip() else None
    except Exception:
        buy_zone = None
    try:
        sell_zone = float(custom_sell) if str(custom_sell).strip() else None
    except Exception:
        sell_zone = None
    avg = float(stats.get("avg7") or current_price)
    if buy_zone is None:
        buy_zone = avg * (1 - max(0.2, min(40, buy_discount)) / 100.0)
    if sell_zone is None:
        sell_zone = avg * (1 + max(0.2, min(80, sell_markup)) / 100.0)
    signal = "HOLD"
    reason = "Points price is between buy and sell zones."
    if float(current_price) <= float(buy_zone):
        signal = "BUY"
        reason = "Points dropped into the best buy zone based on your tracker."
    elif float(current_price) >= float(sell_zone):
        signal = "SELL"
        reason = "Points reached the sell zone based on your tracker."
    if signal == "HOLD":
        return None
    link = "https://www.torn.com/pmarket.php"
    created = now_iso()
    with db() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        existing = conn.execute(
            """
            SELECT id FROM points_signals
            WHERE torn_id=? AND signal=? AND created_at>=?
            ORDER BY id DESC LIMIT 1
            """,
            (torn_id, signal, cutoff),
        ).fetchone()
        if existing:
            return None
        conn.execute(
            """
            INSERT INTO points_signals(torn_id, signal, current_price, buy_zone, sell_zone, reason, link, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (torn_id, signal, current_price, buy_zone, sell_zone, reason, link, created),
        )
        conn.execute(
            "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
            (torn_id, f"points_{signal.lower()}", f"Points Market {signal}", reason + f" Current: ${int(current_price):,}", link, created),
        )
    return {"signal": signal, "reason": reason, "link": link, "buy_zone": buy_zone, "sell_zone": sell_zone}


def scan_points_market_for_user(torn_id: int, reason: str = "auto"):
    started = now_iso()
    rows_seen = 0
    try:
        key = get_api_key(torn_id)
        if not key:
            raise ValueError("No Torn API key saved.")
        data = fetch_points_market(key)
        m = normalize_points_market(data)
        rows_seen = int(m.get("listing_count") or 0)
        stamp = now_iso()
        with db() as conn:
            conn.execute(
                """
                INSERT INTO points_market_snapshots(lowest_price, avg_price, listing_count, total_quantity, captured_by_torn_id, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (m.get("lowest_price"), m.get("avg_price"), m.get("listing_count") or 0, m.get("total_quantity") or 0, torn_id, stamp),
            )
        stats = points_history_stats(m.get("lowest_price"))
        sig = maybe_create_points_signal(torn_id, m.get("lowest_price"), stats)
        record_scan_run(torn_id, "ok", rows_seen, "Points Market scan complete." + (" Signal: " + sig["signal"] if sig else ""), started, module="points_market")
        return {"ok": True, "points_seen": rows_seen, "latest": m, "stats": stats, "signal": sig}
    except Exception as e:
        record_scan_run(torn_id, "error", rows_seen, str(e), started, module="points_market")
        return {"ok": False, "error": str(e)}


def latest_points_market(torn_id: int):
    with db() as conn:
        snap = conn.execute(
            "SELECT lowest_price, avg_price, listing_count, total_quantity, created_at FROM points_market_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        signals = conn.execute(
            "SELECT signal, current_price, buy_zone, sell_zone, reason, link, created_at FROM points_signals WHERE torn_id=? ORDER BY id DESC LIMIT 20",
            (torn_id,),
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) AS c FROM points_market_snapshots").fetchone()["c"]
        settings = {
            "points_buy_zone": get_setting_for_user(conn, torn_id, "points_buy_zone", ""),
            "points_sell_zone": get_setting_for_user(conn, torn_id, "points_sell_zone", ""),
            "points_default_buy_discount_pct": get_setting_for_user(conn, torn_id, "points_default_buy_discount_pct", "2"),
            "points_default_sell_markup_pct": get_setting_for_user(conn, torn_id, "points_default_sell_markup_pct", "4"),
            "points_alerts_enabled": get_setting_for_user(conn, torn_id, "points_alerts_enabled", "true"),
        }
    current = snap["lowest_price"] if snap else None
    stats = points_history_stats(current)
    avg = float(stats.get("avg7") or current or 0)
    try:
        buy_zone = float(settings["points_buy_zone"]) if str(settings["points_buy_zone"]).strip() else avg * (1 - float(settings["points_default_buy_discount_pct"]) / 100.0)
    except Exception:
        buy_zone = avg * 0.98 if avg else None
    try:
        sell_zone = float(settings["points_sell_zone"]) if str(settings["points_sell_zone"]).strip() else avg * (1 + float(settings["points_default_sell_markup_pct"]) / 100.0)
    except Exception:
        sell_zone = avg * 1.04 if avg else None
    signal = "WAITING"
    if current is not None and buy_zone is not None and sell_zone is not None:
        signal = "BUY" if current <= buy_zone else ("SELL" if current >= sell_zone else "HOLD")
    return {
        "latest": dict(snap) if snap else None,
        "stats": stats,
        "signals": [dict(r) for r in signals],
        "snapshot_count": count,
        "settings": settings,
        "buy_zone": buy_zone,
        "sell_zone": sell_zone,
        "signal": signal,
        "link": "https://www.torn.com/pmarket.php",
        "server_time": now_iso(),
    }



# ---------- Step 5: Travel Profit Predictor ----------

TRAVEL_ROUTES = [
    {"country": "Mexico", "item_name": "Jaguar Plushie", "minutes": 26},
    {"country": "Mexico", "item_name": "Dahlia", "minutes": 26},
    {"country": "Canada", "item_name": "Wolverine Plushie", "minutes": 29},
    {"country": "Canada", "item_name": "Crocus", "minutes": 29},
    {"country": "Hawaii", "item_name": "Stingray Plushie", "minutes": 94},
    {"country": "Hawaii", "item_name": "Orchid", "minutes": 94},
    {"country": "United Kingdom", "item_name": "Red Fox Plushie", "minutes": 159},
    {"country": "United Kingdom", "item_name": "Heather", "minutes": 159},
    {"country": "Argentina", "item_name": "Monkey Plushie", "minutes": 167},
    {"country": "Argentina", "item_name": "Ceibo Flower", "minutes": 167},
    {"country": "Switzerland", "item_name": "Chamois Plushie", "minutes": 175},
    {"country": "Switzerland", "item_name": "Edelweiss", "minutes": 175},
    {"country": "Japan", "item_name": "Cherry Blossom", "minutes": 225},
    {"country": "China", "item_name": "Panda Plushie", "minutes": 242},
    {"country": "China", "item_name": "Peony", "minutes": 242},
    {"country": "United Arab Emirates", "item_name": "Camel Plushie", "minutes": 271},
    {"country": "United Arab Emirates", "item_name": "Tribulus Omanense", "minutes": 271},
    {"country": "South Africa", "item_name": "Lion Plushie", "minutes": 297},
    {"country": "South Africa", "item_name": "African Violet", "minutes": 297},
]


def catalog_lookup_by_name(name: str):
    with db() as conn:
        row = conn.execute(
            """
            SELECT item_id, name, buy_price, sell_value, market_value
            FROM item_catalog
            WHERE LOWER(name)=LOWER(?)
            LIMIT 1
            """,
            (name,),
        ).fetchone()
        if not row:
            row = conn.execute(
                """
                SELECT item_id, name, buy_price, sell_value, market_value
                FROM item_catalog
                WHERE name LIKE ?
                ORDER BY LENGTH(name) ASC
                LIMIT 1
                """,
                (f"%{name}%",),
            ).fetchone()
    return dict(row) if row else None


def latest_home_price_for_item(item_id: int, fallback=None):
    with db() as conn:
        row = conn.execute(
            """
            SELECT lowest_price FROM item_market_snapshots
            WHERE item_id=? AND lowest_price IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (item_id,),
        ).fetchone()
    if row and row["lowest_price"]:
        return float(row["lowest_price"])
    try:
        return float(fallback) if fallback is not None else None
    except Exception:
        return None


def travel_arrival_chance(country: str, item_name: str, profit_per_minute: float, minutes: int):
    # Step 5 starts with a smooth learned estimate. Later scans improve it from route snapshots.
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db() as conn:
        r = conn.execute(
            """
            SELECT AVG(arrival_chance) av, COUNT(*) c
            FROM travel_route_snapshots
            WHERE country=? AND item_name=? AND created_at>=?
            """,
            (country, item_name, since),
        ).fetchone()
    base = 62.0
    if r and r["c"] and r["av"] is not None:
        base = float(r["av"])
    # More profit usually means more competition; shorter trips are safer.
    competition_penalty = min(22.0, max(0.0, profit_per_minute / 30000.0))
    travel_penalty = min(14.0, max(0.0, (minutes - 120) / 18.0))
    chance = base - competition_penalty - travel_penalty
    return round(max(15.0, min(92.0, chance)), 1)


def build_travel_rows(torn_id: int):
    # Make sure catalog exists; this resolves item names to real Torn item IDs when available.
    key = get_api_key(torn_id)
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM item_catalog").fetchone()["c"]
    if count == 0 and key:
        try:
            refresh_item_catalog(key)
        except Exception:
            pass
    rows = []
    with db() as conn:
        try:
            min_profit = float(get_setting_for_user(conn, torn_id, "travel_min_profit", "50000"))
        except Exception:
            min_profit = 50000.0
        try:
            min_chance = float(get_setting_for_user(conn, torn_id, "travel_min_arrival_chance", "45"))
        except Exception:
            min_chance = 45.0
        try:
            items_per_trip = int(float(get_setting_for_user(conn, torn_id, "travel_items_per_trip", "29")))
        except Exception:
            items_per_trip = 29
    items_per_trip = max(1, min(35, items_per_trip))
    for route in TRAVEL_ROUTES:
        cat = catalog_lookup_by_name(route["item_name"])
        if not cat:
            continue
        item_id = int(cat["item_id"])
        abroad_cost = cat.get("buy_price") or cat.get("sell_value") or 0
        try:
            abroad_cost = float(abroad_cost or 0)
        except Exception:
            abroad_cost = 0.0
        fallback = cat.get("market_value") or cat.get("sell_value") or None
        home_price = latest_home_price_for_item(item_id, fallback=fallback)
        if not home_price:
            continue
        # If Torn static buy price is missing/zero, use a conservative floor so the row still ranks but is marked rough.
        rough = False
        if abroad_cost <= 0:
            abroad_cost = float(cat.get("sell_value") or 0) or home_price * 0.55
            rough = True
        profit_each = float(home_price) - float(abroad_cost)
        estimated_profit = profit_each * items_per_trip
        minutes = int(route.get("minutes") or 120)
        ppm = estimated_profit / max(1, minutes * 2)  # rough round trip profit per minute
        arrival = travel_arrival_chance(route["country"], cat["name"], ppm, minutes)
        score = (max(0.0, estimated_profit) / 100000.0) + (ppm / 2500.0) + (arrival * 0.55)
        signal = "GO" if estimated_profit >= min_profit and arrival >= min_chance else ("RISKY" if estimated_profit >= min_profit else "WAIT")
        reason = f"Estimated profit {int(estimated_profit):,} for {items_per_trip} items; arrival chance {arrival}%."
        if rough:
            reason += " Abroad cost is estimated until more route data is learned."
        rows.append({
            "country": route["country"],
            "item_id": item_id,
            "item_name": cat["name"],
            "abroad_cost": abroad_cost,
            "home_price": float(home_price),
            "estimated_profit": round(estimated_profit, 2),
            "profit_per_minute": round(ppm, 2),
            "arrival_chance": arrival,
            "score": round(score, 2),
            "signal": signal,
            "reason": reason,
            "minutes": minutes,
            "link": "https://www.torn.com/travelagency.php",
            "market_link": f"https://www.torn.com/imarket.php#/p=shop&step=shop&type=&searchname={requests.utils.quote(str(cat['name']))}",
        })
    rows.sort(key=lambda x: (x["signal"] == "GO", x["score"]), reverse=True)
    return rows


def maybe_create_travel_signal(torn_id: int, rec):
    with db() as conn:
        alerts_enabled = get_setting_for_user(conn, torn_id, "alerts_enabled", "true") != "false"
        travel_alerts_enabled = get_setting_for_user(conn, torn_id, "travel_alerts_enabled", "true") != "false"
    if not alerts_enabled or not travel_alerts_enabled or not rec or rec.get("signal") != "GO":
        return None
    created = now_iso()
    with db() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        existing = conn.execute(
            """
            SELECT id FROM travel_recommendations
            WHERE torn_id=? AND country=? AND item_name=? AND signal='GO' AND created_at>=?
            ORDER BY id DESC LIMIT 1
            """,
            (torn_id, rec["country"], rec["item_name"], cutoff),
        ).fetchone()
        if existing:
            return None
        conn.execute(
            """
            INSERT INTO travel_recommendations(torn_id, country, item_id, item_name, abroad_cost, home_price, estimated_profit,
                profit_per_minute, arrival_chance, score, signal, reason, link, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (torn_id, rec["country"], rec.get("item_id"), rec["item_name"], rec.get("abroad_cost"), rec.get("home_price"),
             rec.get("estimated_profit"), rec.get("profit_per_minute"), rec.get("arrival_chance"), rec.get("score"), rec.get("signal"),
             rec.get("reason"), rec.get("link"), created),
        )
        conn.execute(
            "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
            (torn_id, "travel_go", f"Travel Profit GO: {rec['country']}", f"{rec['item_name']} looks profitable. {rec['reason']}", rec.get("link"), created),
        )
    return rec


def scan_travel_profit_for_user(torn_id: int, reason: str = "auto"):
    started = now_iso()
    rows_seen = 0
    try:
        rows = build_travel_rows(torn_id)
        rows_seen = len(rows)
        stamp = now_iso()
        with db() as conn:
            for r in rows[:25]:
                conn.execute(
                    """
                    INSERT INTO travel_route_snapshots(country, item_id, item_name, abroad_cost, home_price, estimated_profit,
                        profit_per_minute, arrival_chance, score, signal, reason, captured_by_torn_id, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (r["country"], r.get("item_id"), r["item_name"], r.get("abroad_cost"), r.get("home_price"), r.get("estimated_profit"),
                     r.get("profit_per_minute"), r.get("arrival_chance"), r.get("score"), r.get("signal"), r.get("reason"), torn_id, stamp),
                )
        best = rows[0] if rows else None
        sig = maybe_create_travel_signal(torn_id, best)
        record_scan_run(torn_id, "ok", rows_seen, "Travel Profit scan complete." + (" Signal: GO" if sig else ""), started, module="travel_profit")
        return {"ok": True, "routes_seen": rows_seen, "best": best, "routes": rows[:12], "signal": sig}
    except Exception as e:
        record_scan_run(torn_id, "error", rows_seen, str(e), started, module="travel_profit")
        return {"ok": False, "error": str(e)}


def latest_travel_profit(torn_id: int):
    rows = build_travel_rows(torn_id)
    with db() as conn:
        recs = conn.execute(
            """
            SELECT country, item_id, item_name, abroad_cost, home_price, estimated_profit, profit_per_minute,
                   arrival_chance, score, signal, reason, link, created_at
            FROM travel_recommendations
            WHERE torn_id=?
            ORDER BY id DESC LIMIT 20
            """,
            (torn_id,),
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) AS c FROM travel_route_snapshots").fetchone()["c"]
        settings = {
            "travel_alerts_enabled": get_setting_for_user(conn, torn_id, "travel_alerts_enabled", "true"),
            "travel_min_profit": get_setting_for_user(conn, torn_id, "travel_min_profit", "50000"),
            "travel_min_arrival_chance": get_setting_for_user(conn, torn_id, "travel_min_arrival_chance", "45"),
            "travel_items_per_trip": get_setting_for_user(conn, torn_id, "travel_items_per_trip", "29"),
        }
    return {
        "best": rows[0] if rows else None,
        "routes": rows[:20],
        "recommendations": [dict(r) for r in recs],
        "snapshot_count": count,
        "settings": settings,
        "server_time": now_iso(),
    }


# ---------- Step 6: Enemy Sleep / Activity Tracker ----------

def _safe_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def normalize_ranked_war_enemy(payload, own_faction_id=None):
    ranked = payload.get("rankedwars") or payload.get("ranked_wars") or payload.get("ranked_war") or {}
    if isinstance(ranked, list):
        wars = ranked
    elif isinstance(ranked, dict):
        wars = list(ranked.values())
    else:
        wars = []
    candidates = []
    for war in wars:
        if not isinstance(war, dict):
            continue
        winner = war.get("winner") or war.get("winner_faction_id")
        end = war.get("end") or war.get("end_time") or war.get("finished")
        if winner or end:
            continue
        factions = war.get("factions") or war.get("faction") or {}
        if isinstance(factions, dict):
            f_items = list(factions.items())
            faction_rows = []
            for fid, info in f_items:
                if isinstance(info, dict):
                    row = dict(info)
                    row.setdefault("id", fid)
                    faction_rows.append(row)
        elif isinstance(factions, list):
            faction_rows = factions
        else:
            faction_rows = []
        enemy = None
        own = None
        for f in faction_rows:
            fid = _safe_int(f.get("id") or f.get("faction_id"))
            if own_faction_id and fid == int(own_faction_id):
                own = f
            else:
                enemy = f
        if enemy:
            candidates.append({"enemy": enemy, "own": own, "war": war})
    if not candidates:
        return None
    chosen = candidates[0]["enemy"]
    return {
        "enemy_faction_id": _safe_int(chosen.get("id") or chosen.get("faction_id")),
        "enemy_faction_name": str(chosen.get("name") or chosen.get("faction_name") or "Enemy Faction"),
    }


def normalize_faction_members(payload):
    members = payload.get("members") or {}
    if isinstance(members, dict):
        iterator = members.items()
    elif isinstance(members, list):
        iterator = [(m.get("id") or m.get("player_id"), m) for m in members if isinstance(m, dict)]
    else:
        iterator = []
    rows = []
    for mid, m in iterator:
        if not isinstance(m, dict):
            continue
        enemy_id = _safe_int(m.get("player_id") or m.get("id") or mid)
        if not enemy_id:
            continue
        last_action = m.get("last_action") or {}
        status = m.get("status") or {}
        la_status = str(last_action.get("status") or m.get("last_action_status") or "Unknown")
        online_status = str(m.get("online_status") or la_status or "Unknown")
        status_state = str(status.get("state") or status.get("description") or "Okay")
        status_desc = str(status.get("description") or status_state)
        until = _safe_int(status.get("until"), 0) or 0
        name = str(m.get("name") or f"Enemy {enemy_id}")
        bucket = "offline"
        low = f"{online_status} {la_status} {status_state} {status_desc}".lower()
        if "hospital" in low:
            bucket = "hospital"
        elif "travel" in low or "abroad" in low or "return" in low:
            bucket = "travel"
        elif "jail" in low:
            bucket = "jail"
        elif "online" in low:
            bucket = "online"
        elif "idle" in low:
            bucket = "idle"
        elif "offline" in low:
            bucket = "offline"
        rows.append({
            "enemy_torn_id": enemy_id,
            "enemy_name": name[:80],
            "online_status": online_status[:40],
            "status_state": status_state[:40],
            "status_description": status_desc[:120],
            "status_until": until,
            "last_action_status": la_status[:40],
            "last_action_timestamp": _safe_int(last_action.get("timestamp"), 0) or 0,
            "activity_bucket": bucket,
        })
    return rows


def get_current_enemy_for_user(torn_id: int):
    key = get_api_key(torn_id)
    if not key:
        raise ValueError("No Torn API key saved.")
    with db() as conn:
        user = conn.execute("SELECT faction_id, faction_name FROM users WHERE torn_id=?", (torn_id,)).fetchone()
    own_faction_id = user["faction_id"] if user else None
    own_faction_name = user["faction_name"] if user else None
    payload = torn_get("faction", "basic,rankedwars", key)
    if not own_faction_id:
        basic = payload.get("basic") or payload
        own_faction_id = _safe_int(basic.get("ID") or basic.get("id") or basic.get("faction_id"), None)
        own_faction_name = own_faction_name or str(basic.get("name") or "Your Faction")
    enemy = normalize_ranked_war_enemy(payload, own_faction_id)
    if not enemy or not enemy.get("enemy_faction_id"):
        raise ValueError("No active ranked war enemy found right now. Start tracking again when you are in a ranked war.")
    return {
        "faction_id": own_faction_id,
        "faction_name": own_faction_name or "Your Faction",
        **enemy,
    }


def upsert_enemy_tracking_session(torn_id: int, enemy):
    stamp = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO enemy_tracking_sessions(torn_id, faction_id, faction_name, enemy_faction_id, enemy_faction_name, enabled, started_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(torn_id, faction_id, enemy_faction_id) DO UPDATE SET
                faction_name=excluded.faction_name,
                enemy_faction_name=excluded.enemy_faction_name,
                enabled=1,
                updated_at=excluded.updated_at
            """,
            (torn_id, enemy.get("faction_id"), enemy.get("faction_name"), enemy["enemy_faction_id"], enemy.get("enemy_faction_name"), 1, stamp, stamp),
        )


def active_enemy_session(torn_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM enemy_tracking_sessions WHERE torn_id=? AND enabled=1 ORDER BY updated_at DESC LIMIT 1",
            (torn_id,),
        ).fetchone()
    return dict(row) if row else None


def scan_enemy_activity_for_user(torn_id: int, reason: str = "auto"):
    started = now_iso()
    rows_seen = 0
    try:
        key = get_api_key(torn_id)
        if not key:
            raise ValueError("No Torn API key saved.")
        sess = active_enemy_session(torn_id)
        if not sess:
            if reason == "manual_start":
                enemy = get_current_enemy_for_user(torn_id)
                upsert_enemy_tracking_session(torn_id, enemy)
                sess = active_enemy_session(torn_id)
            else:
                record_scan_run(torn_id, "skipped", 0, "Enemy Sleep tracker not started.", started, module="enemy_sleep")
                return {"ok": True, "skipped": True, "message": "Enemy Sleep tracker not started."}
        enemy_id = int(sess["enemy_faction_id"])
        payload = torn_get("faction", "basic", key, str(enemy_id))
        members = normalize_faction_members(payload)
        rows_seen = len(members)
        if not members:
            raise ValueError("No enemy members returned from Torn.")
        stamp = now_iso()
        with db() as conn:
            for m in members:
                conn.execute(
                    """
                    INSERT INTO enemy_activity_snapshots(torn_id, faction_id, enemy_faction_id, enemy_torn_id, enemy_name,
                        online_status, status_state, status_description, status_until, last_action_status, last_action_timestamp,
                        activity_bucket, captured_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (torn_id, sess.get("faction_id"), enemy_id, m["enemy_torn_id"], m["enemy_name"], m["online_status"],
                     m["status_state"], m["status_description"], m["status_until"], m["last_action_status"], m["last_action_timestamp"],
                     m["activity_bucket"], stamp),
                )
            conn.execute("UPDATE enemy_tracking_sessions SET last_scan_at=?, updated_at=? WHERE id=?", (stamp, stamp, sess["id"]))
        report = build_enemy_activity_report(torn_id, enemy_id)
        maybe_create_enemy_window_alert(torn_id, report)
        record_scan_run(torn_id, "ok", rows_seen, "Enemy Sleep scan complete.", started, module="enemy_sleep")
        return {"ok": True, "members_seen": rows_seen, "report": report}
    except Exception as e:
        record_scan_run(torn_id, "error", rows_seen, str(e), started, module="enemy_sleep")
        return {"ok": False, "error": str(e)}


def _best_three_hour_window(hour_scores, prefer="low"):
    if not hour_scores:
        return None
    best = None
    for start in range(24):
        hours = [(start + i) % 24 for i in range(3)]
        vals = [hour_scores.get(h) for h in hours if hour_scores.get(h) is not None]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        if best is None or (prefer == "low" and avg < best["score"]) or (prefer == "high" and avg > best["score"]):
            best = {"start": start, "end": (start + 3) % 24, "score": avg}
    if not best:
        return None
    return f"{best['start']:02d}:00-{best['end']:02d}:00 Torn time"


def build_enemy_activity_report(torn_id: int, enemy_faction_id=None):
    sess = active_enemy_session(torn_id)
    if not sess and not enemy_faction_id:
        return None
    enemy_id = int(enemy_faction_id or sess["enemy_faction_id"])
    try:
        with db() as conn:
            hours = int(float(get_setting_for_user(conn, torn_id, "enemy_tracking_window_hours", "72")))
    except Exception:
        hours = 72
    hours = max(24, min(168, hours))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT enemy_torn_id, enemy_name, activity_bucket, captured_at
            FROM enemy_activity_snapshots
            WHERE torn_id=? AND enemy_faction_id=? AND captured_at>=?
            ORDER BY captured_at ASC
            """,
            (torn_id, enemy_id, since),
        ).fetchall()
    sample_count = len(rows)
    if not rows:
        return {"enemy_faction_id": enemy_id, "enemy_faction_name": sess.get("enemy_faction_name") if sess else "Enemy", "window_hours": hours, "confidence": "Low", "sample_count": 0, "member_count": 0, "best_attack_window": None, "best_turtle_window": None, "summary": "No activity samples yet. Press Start Tracking or Scan Now.", "hourly": []}
    members = {r["enemy_torn_id"] for r in rows}
    active = 0
    inactive = 0
    per_hour = {h: {"active": 0, "inactive": 0, "total": 0} for h in range(24)}
    latest_by_member = {}
    for r in rows:
        dt = iso_to_dt(r["captured_at"])
        if not dt:
            continue
        h = dt.hour
        bucket = r["activity_bucket"]
        is_active = bucket in ("online", "idle")
        if is_active:
            active += 1
            per_hour[h]["active"] += 1
        else:
            inactive += 1
            per_hour[h]["inactive"] += 1
        per_hour[h]["total"] += 1
        latest_by_member[r["enemy_torn_id"]] = dict(r)
    total = max(1, active + inactive)
    hour_ratios = {}
    hourly = []
    for h, v in per_hour.items():
        if v["total"]:
            ratio = round(v["active"] / v["total"] * 100, 1)
            hour_ratios[h] = ratio
            hourly.append({"hour": h, "active_pct": ratio, "samples": v["total"]})
    best_attack = _best_three_hour_window(hour_ratios, prefer="low")
    best_turtle = _best_three_hour_window(hour_ratios, prefer="high")
    if sample_count >= max(120, len(members) * 6):
        confidence = "High"
    elif sample_count >= max(45, len(members) * 3):
        confidence = "Medium"
    else:
        confidence = "Low"
    active_ratio = round(active / total * 100, 1)
    inactive_ratio = round(inactive / total * 100, 1)
    latest_rows = list(latest_by_member.values())[-12:]
    report = {
        "enemy_faction_id": enemy_id,
        "enemy_faction_name": sess.get("enemy_faction_name") if sess else "Enemy Faction",
        "faction_id": sess.get("faction_id") if sess else None,
        "window_hours": hours,
        "best_attack_window": best_attack,
        "best_turtle_window": best_turtle,
        "confidence": confidence,
        "active_ratio": active_ratio,
        "inactive_ratio": inactive_ratio,
        "member_count": len(members),
        "sample_count": sample_count,
        "hourly": sorted(hourly, key=lambda x: x["hour"]),
        "latest": latest_rows,
        "summary": f"{len(members)} enemies tracked over {sample_count} samples. Best attack is the lowest active 3-hour window; turtle is the highest active 3-hour window.",
        "created_at": now_iso(),
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO enemy_activity_reports(torn_id, faction_id, enemy_faction_id, enemy_faction_name, window_hours,
                best_attack_window, best_turtle_window, confidence, active_ratio, inactive_ratio, member_count, sample_count, report_json, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (torn_id, report.get("faction_id"), enemy_id, report["enemy_faction_name"], hours, best_attack, best_turtle,
             confidence, active_ratio, inactive_ratio, len(members), sample_count, json.dumps(report), report["created_at"]),
        )
    return report


def maybe_create_enemy_window_alert(torn_id: int, report):
    if not report or not report.get("best_attack_window"):
        return None
    try:
        with db() as conn:
            alerts_enabled = (get_setting_for_user(conn, torn_id, "alerts_enabled", "true") != "false" and get_setting_for_user(conn, torn_id, "enemy_alerts_enabled", "true") != "false")
    except Exception:
        alerts_enabled = True
    if not alerts_enabled or report.get("confidence") == "Low":
        return None
    created = now_iso()
    title = f"Enemy Window: {report.get('enemy_faction_name', 'Enemy')}"
    body = f"Best attack window: {report.get('best_attack_window')}. Turtle window: {report.get('best_turtle_window')}. Confidence: {report.get('confidence')}."
    with db() as conn:
        dupe = conn.execute(
            "SELECT id FROM alerts WHERE torn_id=? AND alert_type='enemy_window' AND title=? AND body=? AND created_at>=? LIMIT 1",
            (torn_id, title, body, (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()),
        ).fetchone()
        if dupe:
            return None
        conn.execute(
            "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
            (torn_id, "enemy_window", title, body, "https://www.torn.com/factions.php?step=your", created),
        )
    return {"title": title, "body": body}


def latest_enemy_activity(torn_id: int):
    sess = active_enemy_session(torn_id)
    report = None
    reports = []
    with db() as conn:
        if sess:
            rows = conn.execute(
                "SELECT report_json, created_at FROM enemy_activity_reports WHERE torn_id=? AND enemy_faction_id=? ORDER BY id DESC LIMIT 8",
                (torn_id, sess["enemy_faction_id"]),
            ).fetchall()
            for r in rows:
                try:
                    item = json.loads(r["report_json"])
                    item["created_at"] = r["created_at"]
                    reports.append(item)
                except Exception:
                    pass
            if reports:
                report = reports[0]
        active_sessions = conn.execute(
            "SELECT * FROM enemy_tracking_sessions WHERE torn_id=? ORDER BY updated_at DESC LIMIT 5",
            (torn_id,),
        ).fetchall()
    if sess and not report:
        report = build_enemy_activity_report(torn_id, sess["enemy_faction_id"])
    return {"session": sess, "report": report, "reports": reports, "sessions": [dict(r) for r in active_sessions], "server_time": now_iso()}

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


def record_scan_run(torn_id: int, status: str, rows_seen: int, message: str, started_at: str, module: str = "stock_brain"):
    finished = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO scan_runs(torn_id, module, status, rows_seen, message, started_at, finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (torn_id, module, status, rows_seen, message[:500], started_at, finished),
        )



def _stock_pick_due_at(created_at: str):
    base = iso_to_dt(created_at) or datetime.now(timezone.utc)
    return (base + timedelta(hours=24)).replace(microsecond=0).isoformat()


def latest_stock_rows():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT acronym, name, current_price, created_at
            FROM stock_snapshots
            WHERE id IN (SELECT MAX(id) FROM stock_snapshots GROUP BY acronym)
            ORDER BY acronym ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def ensure_stock_move_watch(torn_id: int, pick=None):
    """Create a private 24h review timer for the user's current stock pick.
    This does not move money or buy/sell anything; it only tells the user when to review.
    """
    if not pick:
        pick = latest_active_stock_pick()
    if not pick or not pick.get("acronym"):
        return None
    now = now_iso()
    due_at = _stock_pick_due_at(pick.get("created_at") or now)
    with db() as conn:
        active = conn.execute(
            "SELECT * FROM stock_move_watches WHERE torn_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (torn_id,),
        ).fetchone()
        if active and str(active["from_acronym"]).upper() == str(pick.get("acronym")).upper():
            return dict(active)
        if active:
            conn.execute("UPDATE stock_move_watches SET status='replaced', reviewed_at=? WHERE id=?", (now, active["id"]))
        conn.execute(
            """
            INSERT INTO stock_move_watches(torn_id, from_acronym, from_name, from_price, status, reason, created_at, due_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (torn_id, pick.get("acronym"), pick.get("name"), pick.get("pick_price"), "active", "24h stock review timer started from current pick.", now, due_at),
        )
        row = conn.execute(
            "SELECT * FROM stock_move_watches WHERE torn_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (torn_id,),
        ).fetchone()
    return dict(row) if row else None


def stock_move_status(torn_id: int, ranked_all=None):
    """Return a clear hold/move/review recommendation for the user's 24h stock timer."""
    pick = latest_active_stock_pick()
    watch = ensure_stock_move_watch(torn_id, pick)
    if not watch:
        return {"status": "learning", "title": "Stock timer waiting", "message": "Run Stock Brain once to start a 24h review timer.", "due": False}
    if ranked_all is None:
        rows = latest_stock_rows()
        ranked_all = sorted([
            score_stock(dict(r) | {"stock_id": r.get("acronym"), "market_cap": None, "total_shares": None}, torn_id)
            for r in rows
        ], key=lambda x: x["score"], reverse=True)
    now_dt = datetime.now(timezone.utc)
    due_dt = iso_to_dt(watch.get("due_at")) or now_dt
    seconds_left = int((due_dt - now_dt).total_seconds())
    hours_left = max(0, seconds_left / 3600.0)
    from_acr = str(watch.get("from_acronym") or "").upper()
    current_row = None
    best = ranked_all[0] if ranked_all else None
    for row in ranked_all or []:
        if str(row.get("acronym") or "").upper() == from_acr:
            current_row = row
            break
    current_score = float((current_row or {}).get("score") or 0)
    best_score = float((best or {}).get("score") or 0)
    score_gap = max(0.0, best_score - current_score)
    due = seconds_left <= 0
    recommendation = "WAIT"
    title = "24h timer running"
    message = f"Review {from_acr} in {hours_left:.1f}h."
    if due:
        if best and str(best.get("acronym") or "").upper() != from_acr and score_gap >= 6:
            recommendation = "MOVE"
            title = "Move money review due"
            message = f"24h is up. {best.get('acronym')} is stronger than {from_acr} by {score_gap:.1f} score points. Consider moving money if this matches your plan."
        elif current_row:
            recommendation = "REVIEW"
            title = "24h stock review due"
            message = f"24h is up. {from_acr} is still close enough to the best live score. Review profit/loss and decide hold or sell."
        else:
            recommendation = "REVIEW"
            title = "24h stock review due"
            message = f"24h is up. Review {from_acr} and compare against the current best pick."
        if not watch.get("notified_at"):
            with db() as conn:
                conn.execute("UPDATE stock_move_watches SET notified_at=? WHERE id=?", (now_iso(), watch["id"]))
                conn.execute(
                    "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                    (torn_id, "stock_24h_review", title, message, "https://www.torn.com/page.php?sid=stocks", now_iso()),
                )
    return {
        "status": recommendation.lower(),
        "recommendation": recommendation,
        "title": title,
        "message": message,
        "due": bool(due),
        "hours_left": round(hours_left, 2),
        "due_at": watch.get("due_at"),
        "started_at": watch.get("created_at"),
        "from_acronym": from_acr,
        "from_name": watch.get("from_name"),
        "from_price": watch.get("from_price"),
        "current_score": round(current_score, 2),
        "best_acronym": (best or {}).get("acronym"),
        "best_name": (best or {}).get("name"),
        "best_score": round(best_score, 2),
        "score_gap": round(score_gap, 2),
        "link": "https://www.torn.com/page.php?sid=stocks",
    }


def reset_stock_move_timer(torn_id: int):
    pick = latest_active_stock_pick()
    now = now_iso()
    with db() as conn:
        conn.execute("UPDATE stock_move_watches SET status='reviewed', reviewed_at=? WHERE torn_id=? AND status='active'", (now, torn_id))
    return ensure_stock_move_watch(torn_id, pick)

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
        market_lookup = {s["acronym"]: s for s in stocks}
        holdings = fetch_user_stock_holdings(key, market_lookup)
        holdings_seen = save_user_stock_holding_snapshots(torn_id, holdings)
        result = choose_stock_pick(torn_id, stocks)
        ensure_stock_move_watch(torn_id, result.get("pick"))
        move_status = stock_move_status(torn_id, result.get("ranked") or [])
        result["move_status"] = move_status
        message = "Auto scan complete. " + str(result.get("decision") or "")
        if move_status.get("due"):
            message += " 24h stock review is due."
        if holdings_seen:
            message += f" User stock history captured: {holdings_seen}."
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
        return {"ok": True, "stocks_seen": rows_seen, "user_holdings_seen": locals().get("holdings_seen", 0), **result}
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
                # Lite focus mode: only run the modules that matter for profit prediction.
                # This keeps Render + TornPDA smoother and stops wasting calls on unused modules.
                perform_stock_scan_for_user(torn_id, reason="auto")
                time.sleep(0.8)
                scan_item_market_for_user(torn_id, reason="auto")
                time.sleep(0.8)
                scan_travel_profit_for_user(torn_id, reason="auto")
                time.sleep(1.2)
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


def _hours_ago_iso(hours: int):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _learning_weight(scope: str, module: str, signal_key: str, default: float = 1.0):
    with db() as conn:
        row = conn.execute(
            "SELECT weight_value FROM learning_weights WHERE scope=? AND module=? AND signal_key=?",
            (scope, module, signal_key),
        ).fetchone()
    return float(row["weight_value"]) if row else float(default)


def _set_learning_weight(scope: str, module: str, signal_key: str, new_weight: float, reason: str):
    new_weight = max(0.25, min(2.5, float(new_weight)))
    old = _learning_weight(scope, module, signal_key, 1.0)
    stamp = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO learning_weights(scope, module, signal_key, weight_value, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(scope, module, signal_key) DO UPDATE SET
                weight_value=excluded.weight_value,
                updated_at=excluded.updated_at
            """,
            (scope, module, signal_key, new_weight, stamp),
        )
        conn.execute(
            """
            INSERT INTO learning_adjustments(scope, module, signal_key, old_weight, new_weight, reason, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (scope, module, signal_key, old, new_weight, reason[:500], stamp),
        )
    return new_weight


def _record_accuracy_event(torn_id, scope, module, source_table, source_id, target_name, signal, predicted_value, actual_value,
                           result_pct, score_before=None, confidence_before=None, was_correct=False, notes=""):
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO accuracy_events(torn_id, scope, module, source_table, source_id, target_name, signal,
                    predicted_value, actual_value, result_pct, score_before, confidence_before, was_correct, notes, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_table, source_id) DO NOTHING
                """,
                (torn_id, scope or 'global', module, source_table, int(source_id), str(target_name)[:140], str(signal)[:40],
                 predicted_value, actual_value, result_pct, score_before, confidence_before, 1 if was_correct else 0, notes[:600], now_iso()),
            )
    except Exception:
        pass


def evaluate_stock_accuracy(limit: int = 50):
    cutoff = _hours_ago_iso(24)
    evaluated = 0
    with db() as conn:
        preds = conn.execute(
            """
            SELECT * FROM stock_predictions
            WHERE actual_24h_price IS NULL AND created_at<=?
            ORDER BY id ASC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    for p in preds:
        target_dt = iso_to_dt(p["created_at"])
        if not target_dt:
            continue
        target_iso = (target_dt + timedelta(hours=24)).isoformat()
        with db() as conn:
            snap = conn.execute(
                """
                SELECT current_price, created_at FROM stock_snapshots
                WHERE acronym=? AND created_at>=?
                ORDER BY created_at ASC LIMIT 1
                """,
                (p["acronym"], target_iso),
            ).fetchone()
        if not snap:
            continue
        pick_price = _safe_float(p["pick_price"], 0) or 0
        actual = _safe_float(snap["current_price"], None)
        if not pick_price or actual is None:
            continue
        pct = ((actual - pick_price) / pick_price) * 100.0
        was = pct > 0
        with db() as conn:
            conn.execute(
                "UPDATE stock_predictions SET result_checked_at=?, actual_24h_price=?, actual_24h_pct=?, was_profitable=? WHERE id=?",
                (now_iso(), actual, round(pct, 3), 1 if was else 0, p["id"]),
            )
        _record_accuracy_event(p["chosen_by_torn_id"], p["scope"], "stock_brain", "stock_predictions", p["id"],
                               p["acronym"], "24h_pick", pick_price, actual, round(pct, 3), p["score"], p["confidence"], was,
                               "24h stock pick checked against the first saved price after the 24h mark.")
        evaluated += 1
    return evaluated


def evaluate_item_accuracy(limit: int = 80):
    cutoff = _hours_ago_iso(24)
    evaluated = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM item_signals s
            LEFT JOIN accuracy_events a ON a.source_table='item_signals' AND a.source_id=s.id
            WHERE a.id IS NULL AND s.created_at<=?
            ORDER BY s.id ASC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    for r in rows:
        start = iso_to_dt(r["created_at"])
        if not start:
            continue
        target_iso = (start + timedelta(hours=24)).isoformat()
        with db() as conn:
            snap = conn.execute(
                """
                SELECT lowest_price FROM item_market_snapshots
                WHERE item_id=? AND created_at>=? AND lowest_price IS NOT NULL
                ORDER BY created_at ASC LIMIT 1
                """,
                (r["item_id"], target_iso),
            ).fetchone()
        if not snap:
            continue
        predicted = _safe_float(r["current_price"], 0) or 0
        actual = _safe_float(snap["lowest_price"], None)
        if not predicted or actual is None:
            continue
        pct = ((actual - predicted) / predicted) * 100.0
        sig = (r["signal"] or "").upper()
        was = (sig == "BUY" and pct > 0) or (sig == "SELL" and pct < 0)
        _record_accuracy_event(r["torn_id"], "global", "item_market", "item_signals", r["id"], r["name"], sig,
                               predicted, actual, round(pct, 3), None, None, was,
                               "Item signal checked against the first saved market price after 24h.")
        evaluated += 1
    return evaluated


def evaluate_points_accuracy(limit: int = 80):
    cutoff = _hours_ago_iso(24)
    evaluated = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM points_signals s
            LEFT JOIN accuracy_events a ON a.source_table='points_signals' AND a.source_id=s.id
            WHERE a.id IS NULL AND s.created_at<=?
            ORDER BY s.id ASC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    for r in rows:
        start = iso_to_dt(r["created_at"])
        if not start:
            continue
        target_iso = (start + timedelta(hours=24)).isoformat()
        with db() as conn:
            snap = conn.execute(
                """
                SELECT lowest_price FROM points_market_snapshots
                WHERE created_at>=? AND lowest_price IS NOT NULL
                ORDER BY created_at ASC LIMIT 1
                """,
                (target_iso,),
            ).fetchone()
        if not snap:
            continue
        predicted = _safe_float(r["current_price"], 0) or 0
        actual = _safe_float(snap["lowest_price"], None)
        if not predicted or actual is None:
            continue
        pct = ((actual - predicted) / predicted) * 100.0
        sig = (r["signal"] or "").upper()
        was = (sig == "BUY" and pct > 0) or (sig == "SELL" and pct < 0)
        _record_accuracy_event(r["torn_id"], "global", "points_watcher", "points_signals", r["id"], "Points", sig,
                               predicted, actual, round(pct, 3), None, None, was,
                               "Points signal checked against the first saved points price after 24h.")
        evaluated += 1
    return evaluated


def evaluate_travel_accuracy(limit: int = 50):
    cutoff = _hours_ago_iso(8)
    evaluated = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT r.* FROM travel_recommendations r
            LEFT JOIN accuracy_events a ON a.source_table='travel_recommendations' AND a.source_id=r.id
            WHERE a.id IS NULL AND r.created_at<=?
            ORDER BY r.id ASC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    for r in rows:
        pred = _safe_float(r["estimated_profit"], 0) or 0
        score = _safe_float(r["score"], 0) or 0
        chance = _safe_float(r["arrival_chance"], 0) or 0
        start = iso_to_dt(r["created_at"])
        if not start:
            continue
        with db() as conn:
            later = conn.execute(
                """
                SELECT AVG(estimated_profit) avg_profit, AVG(arrival_chance) avg_chance, COUNT(*) c
                FROM travel_route_snapshots
                WHERE country=? AND item_name=? AND created_at>=? AND estimated_profit IS NOT NULL
                """,
                (r["country"], r["item_name"], (start + timedelta(hours=2)).isoformat()),
            ).fetchone()
        if not later or int(later["c"] or 0) < 1:
            continue
        actual_profit = _safe_float(later["avg_profit"], 0) or 0
        avg_chance = _safe_float(later["avg_chance"], 0) or 0
        was = actual_profit > 0 and avg_chance >= 35
        pct = ((actual_profit - pred) / pred * 100.0) if pred else 0
        _record_accuracy_event(r["torn_id"], "global", "travel_profit", "travel_recommendations", r["id"],
                               f"{r['country']} - {r['item_name']}", r["signal"], pred, actual_profit, round(pct, 3), score, chance, was,
                               "Travel GO checked against later saved route snapshots.")
        evaluated += 1
    return evaluated


def rebuild_learning_weights():
    adjustments = []
    with db() as conn:
        rows = conn.execute(
            """
            SELECT module, signal, COUNT(*) total, SUM(was_correct) correct, AVG(result_pct) avg_pct
            FROM accuracy_events
            WHERE created_at>=?
            GROUP BY module, signal
            """,
            (_hours_ago_iso(24*45),),
        ).fetchall()
    for r in rows:
        total = int(r["total"] or 0)
        if total < 3:
            continue
        correct = int(r["correct"] or 0)
        rate = correct / max(1, total)
        avg_pct = _safe_float(r["avg_pct"], 0) or 0
        new_weight = 0.65 + (rate * 0.9) + max(-0.2, min(0.2, avg_pct / 50.0))
        reason = f"{r['module']} {r['signal']} accuracy {correct}/{total} ({rate*100:.0f}%), average result {avg_pct:.2f}%."
        _set_learning_weight('global', r["module"], r["signal"], new_weight, reason)
        adjustments.append({"module": r["module"], "signal": r["signal"], "new_weight": round(new_weight, 3), "reason": reason})
    return adjustments


def run_accuracy_learning():
    stock = evaluate_stock_accuracy()
    item = evaluate_item_accuracy()
    points = evaluate_points_accuracy()
    travel = evaluate_travel_accuracy()
    adjustments = rebuild_learning_weights()
    return {"stock": stock, "item": item, "points": points, "travel": travel, "adjustments": adjustments}


def accuracy_dashboard(torn_id: int):
    try:
        run_accuracy_learning()
    except Exception:
        pass
    with db() as conn:
        summary = conn.execute(
            """
            SELECT module, COUNT(*) total, SUM(was_correct) correct, AVG(result_pct) avg_pct
            FROM accuracy_events
            GROUP BY module
            ORDER BY module ASC
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT module, target_name, signal, predicted_value, actual_value, result_pct, was_correct, notes, created_at
            FROM accuracy_events
            ORDER BY id DESC LIMIT 35
            """
        ).fetchall()
        weights = conn.execute(
            """
            SELECT module, signal_key, weight_value, updated_at
            FROM learning_weights
            WHERE scope='global'
            ORDER BY module ASC, signal_key ASC
            """
        ).fetchall()
        adjustments = conn.execute(
            """
            SELECT module, signal_key, old_weight, new_weight, reason, created_at
            FROM learning_adjustments
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
    return {
        "summary": [dict(r) for r in summary],
        "recent": [dict(r) for r in recent],
        "weights": [dict(r) for r in weights],
        "adjustments": [dict(r) for r in adjustments],
        "server_time": now_iso(),
    }


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            ensure_runtime_migrations()
        except Exception:
            pass
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
        "step": "10.12-server-json-fix",
        "database": "postgres" if USE_POSTGRES else "sqlite",
        "pg_driver": PG_DRIVER if USE_POSTGRES else None,
        "message": "Backend online. PostgreSQL is used when DATABASE_URL is set; SQLite fallback stays available."
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_iso(), "version": "step10.12-server-json-fix", "database": "postgres" if USE_POSTGRES else "sqlite", "migrations": "runtime_schema_guard_active"})


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
        profile = validate_torn_key_profile(key)
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
            (torn_id, "system", "Torn Brain connected", "Stock Brain, Item Market, and Travel Profit watcher are active. This lite version focuses only on market prediction and smoother PDA performance.", None, created),
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
        item_popup_enabled = get_setting_for_user(conn, request.user["torn_id"], "item_popup_enabled", "true")
        auto = conn.execute(
            "SELECT enabled, last_scan_at, next_scan_at, last_ok, last_error, scans_completed FROM auto_scan_state WHERE torn_id=?",
            (request.user["torn_id"],),
        ).fetchone()
    return jsonify({
        "ok": True,
        "step": "10.12-server-json-fix",
        "user": request.user,
        "tabs": [
            "Overview", "Stock Brain", "Item Market", "Travel Profit", "Settings"
        ],
        "modules": {
            "stock_brain": "active",
            "item_market": "active",
            "travel_profit": "active",
            "lite_focus": "active_step_10_12"
        },
        "unread_alerts": unread,
        "item_popup_enabled": item_popup_enabled,
        "auto_scan": dict(auto) if auto else {"enabled": 1, "last_scan_at": None, "next_scan_at": None, "last_ok": 0, "last_error": None, "scans_completed": 0},
        "server_time": now_iso()
    })


@app.get("/api/dashboard")
@require_auth
def dashboard():
    """One light endpoint for the PDA overlay.
    The userscript can load one compact payload instead of hitting every module.
    """
    torn_id = request.user["torn_id"]
    with db() as conn:
        unread = conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE torn_id=? AND is_read=0",
            (torn_id,),
        ).fetchone()["c"]
        latest_alerts = conn.execute(
            "SELECT id, alert_type, title, body, link, is_read, created_at FROM alerts WHERE torn_id=? ORDER BY id DESC LIMIT 5",
            (torn_id,),
        ).fetchall()
        auto = conn.execute(
            "SELECT enabled, last_scan_at, next_scan_at, last_ok, last_error, scans_completed FROM auto_scan_state WHERE torn_id=?",
            (torn_id,),
        ).fetchone()

    dashboard_warnings = []
    def _safe(label, fallback, fn, *args, **kwargs):
        try:
            val = fn(*args, **kwargs)
            return fallback if val is None else val
        except Exception as ex:
            dashboard_warnings.append(f"{label}: {ex}")
            return fallback

    stock = _safe("stock pick", None, latest_active_stock_pick)
    stock_move = _safe("stock move", {"status":"learning", "message":"Waiting for stock timer data."}, stock_move_status, torn_id)
    items = _safe("items", [], latest_item_market_rows, torn_id)[:5]
    points = _safe("points", {"latest": None, "signal": "WAITING", "buy_zone": 0.0, "sell_zone": 0.0, "link": "https://www.torn.com/pmarket.php"}, latest_points_market, torn_id)
    travel = _safe("travel", {"best": None, "snapshot_count": 0}, latest_travel_profit, torn_id)
    enemy = _safe("enemy", {"session": None, "report": None}, latest_enemy_activity, torn_id)

    stock_learning = _safe("stock learning", {"global_results_checked":0, "user_stock_snapshots":0}, stock_learning_summary, torn_id)
    stock_samples = int(stock_learning.get("global_results_checked") or 0) + int(stock_learning.get("user_stock_snapshots") or 0)
    item_samples = sum(int((x.get("stats") or {}).get("count7") or 0) for x in items) if isinstance(items, list) else 0
    travel_samples = int((travel or {}).get("snapshot_count") or 0) if isinstance(travel, dict) else 0
    health = {
        "watcher": "Online" if auto and int(auto["last_ok"] or 0) == 1 else ("Waiting" if auto else "Starting"),
        "last_scan": auto["last_scan_at"] if auto else None,
        "next_scan": auto["next_scan_at"] if auto else None,
        "last_error": auto["last_error"] if auto else None,
        "database": "postgres" if USE_POSTGRES else "sqlite",
    }
    data_strength = {
        "stock": data_strength_label(stock_samples),
        "item": data_strength_label(item_samples),
        "travel": data_strength_label(travel_samples),
        "stock_samples": stock_samples,
        "item_samples": item_samples,
        "travel_samples": travel_samples,
    }
    brain_strength = brain_strength_summary(torn_id, stock_learning, item_samples, travel_samples, auto)

    best_move = {"label": "Learning", "detail": "Waiting for more snapshots", "signal": "WAIT"}
    try:
        if travel.get("best") and travel["best"].get("signal") == "GO":
            best_move = {
                "label": "Travel Profit",
                "detail": f"{travel['best'].get('country')} · {travel['best'].get('item_name')} · ${int(travel['best'].get('estimated_profit') or 0):,}",
                "signal": "GO",
            }
        elif points.get("signal") in ("BUY", "SELL"):
            best_move = {"label": "Points", "detail": f"{points.get('signal')} at ${int(points.get('latest',{}).get('lowest_price') or 0):,}", "signal": points.get("signal")}
        else:
            buy_items = [x for x in items if x.get("signal") == "BUY"]
            if buy_items:
                best_move = {"label": "Item Market", "detail": f"{buy_items[0].get('name')} is in buy zone", "signal": "BUY"}
            elif stock:
                best_move = {"label": "Stock Brain", "detail": f"{stock.get('acronym')} · confidence {float(stock.get('confidence') or 0):.0f}%", "signal": "PICK"}
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "step": "10.12-server-json-fix",
        "user": request.user,
        "server_time": now_iso(),
        "unread_alerts": unread,
        "auto_scan": dict(auto) if auto else None,
        "best_move": best_move,
        "stock_pick": stock,
        "stock_move": stock_move,
        "stock_learning": stock_learning,
        "health": health,
        "data_strength": data_strength,
        "brain_strength": brain_strength,
        "items": items,
        "points": {
            "latest": points.get("latest"),
            "signal": points.get("signal"),
            "buy_zone": points.get("buy_zone"),
            "sell_zone": points.get("sell_zone"),
            "link": points.get("link"),
        },
        "travel_best": travel.get("best"),
        "enemy": {
            "session": enemy.get("session"),
            "report": enemy.get("report"),
        },
        "latest_alerts": [dict(r) for r in latest_alerts],
        "dashboard_warnings": dashboard_warnings if 'dashboard_warnings' in locals() else [],
    })



@app.get("/api/tos")
@require_auth
def tos():
    return jsonify({"ok": True, "tos": TOS_TEXT, "privacy_note": "API keys and personal holdings stay private. Anonymous prediction outcomes help the global brain."})


@app.post("/api/feedback")
@require_auth
def prediction_feedback():
    payload = request.get_json(silent=True) or {}
    module = str(payload.get("module") or "general")[:40]
    target = str(payload.get("target") or payload.get("target_name") or "Unknown")[:120]
    feedback = str(payload.get("feedback") or "useful").lower()[:20]
    if feedback not in ("useful", "bad", "neutral"):
        feedback = "neutral"
    context = payload.get("context") or {}
    stamp = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO prediction_feedback(torn_id, module, target_name, feedback, context_json, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (request.user["torn_id"], module, target, feedback, json.dumps(context)[:2000], stamp),
        )
    return jsonify({"ok": True, "message": "Feedback saved. Thanks for helping the brain learn."})


@app.post("/api/quick-setup")
@require_auth
def quick_setup():
    torn_id = request.user["torn_id"]
    key = get_api_key(torn_id)
    added = []
    skipped = []
    # Step 10.10: Popular items no longer depends only on catalog search.
    # If the Torn item catalog is empty or fuzzy searches fail, we fall back to
    # stable popular item IDs so the button always adds useful watched items.
    popular = [
        {"q": "Xanax", "item_id": 206, "name": "Xanax"},
        {"q": "Donator Pack", "item_id": 62, "name": "Donator Pack"},
        {"q": "Feathery Hotel Coupon", "item_id": 366, "name": "Feathery Hotel Coupon"},
        {"q": "Erotic DVD", "item_id": 294, "name": "Erotic DVD"},
        {"q": "Can of Red Cow", "item_id": 533, "name": "Can of Red Cow"},
        {"q": "Bottle of Beer", "item_id": 180, "name": "Bottle of Beer"},
        {"q": "Morphine", "item_id": 66, "name": "Morphine"},
        {"q": "First Aid Kit", "item_id": 67, "name": "First Aid Kit"},
        {"q": "Lion Plushie", "item_id": 258, "name": "Lion Plushie"},
        {"q": "Dahlia", "item_id": 260, "name": "Dahlia"},
    ]
    try:
        # Make sure the item catalog has something to search.
        existing = find_catalog_items("Xanax", 1)
        if not existing and key:
            refresh_item_catalog(key)
    except Exception:
        pass
    stamp = now_iso()
    with db() as conn:
        for pop in popular:
            q = pop["q"]
            try:
                matches = find_catalog_items(q, 8)
                chosen = None
                if matches:
                    # Prefer exact/contains matches and avoid random unrelated results.
                    ql = q.lower()
                    for m in matches:
                        nm = str(m.get("name") or "").lower()
                        if ql == nm or ql in nm:
                            chosen = m
                            break
                    chosen = chosen or matches[0]
                if not chosen:
                    chosen = {"item_id": pop["item_id"], "name": pop["name"]}
                    # Seed catalog too, so the item shows properly even before Torn catalog refresh works.
                    try:
                        conn.execute(
                            """
                            INSERT INTO item_catalog(item_id, name, item_type, buy_price, sell_value, market_value, updated_at)
                            VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(item_id) DO UPDATE SET
                                name=COALESCE(item_catalog.name, excluded.name),
                                updated_at=excluded.updated_at
                            """,
                            (int(chosen["item_id"]), chosen["name"], "Popular", None, None, None, stamp),
                        )
                    except Exception:
                        pass
                conn.execute(
                    """
                    INSERT INTO item_watchlist(torn_id, item_id, name, buy_zone, sell_zone, enabled, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(torn_id, item_id) DO UPDATE SET
                        name=excluded.name,
                        enabled=1,
                        updated_at=excluded.updated_at
                    """,
                    (torn_id, int(chosen["item_id"]), chosen["name"], None, None, 1, stamp, stamp),
                )
                added.append({"item_id": int(chosen["item_id"]), "name": chosen["name"]})
            except Exception as exc:
                skipped.append(f"{q}: {exc}")
    enable_auto_scan(torn_id, immediate=True)
    try:
        perform_stock_scan_for_user(torn_id, reason="quick_setup")
    except Exception:
        pass
    try:
        scan_item_market_for_user(torn_id, reason="quick_setup")
    except Exception:
        pass
    try:
        scan_travel_profit_for_user(torn_id, reason="quick_setup")
    except Exception:
        pass
    return jsonify({"ok": True, "added": added, "skipped": skipped, "message": f"Quick setup complete. Added/updated {len(added)} popular watched items and started backend scanning."})


@app.get("/api/settings")
@require_auth
def get_settings():
    defaults = {
        "scan_interval_minutes": "15",
        "stock_pick_change_score_gap": "6",
        "enemy_tracking_window_hours": "72",
        "enemy_alerts_enabled": "true",
        "alerts_enabled": "true",
        "share_market_learning": "true",
        "item_alerts_enabled": "true",
        "item_popup_enabled": "true",
        "item_default_buy_discount_pct": "3",
        "item_default_sell_markup_pct": "6",
        "points_alerts_enabled": "true",
        "points_buy_zone": "",
        "points_sell_zone": "",
        "points_default_buy_discount_pct": "2",
        "points_default_sell_markup_pct": "4",
        "travel_alerts_enabled": "true",
        "travel_min_profit": "50000",
        "travel_min_arrival_chance": "45",
        "travel_items_per_trip": "29",
        "auto_scan_enabled": "true",
        "compact_mode": "true",
        "tos_accepted": "false",
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
        "enemy_alerts_enabled",
        "alerts_enabled",
        "share_market_learning",
        "item_alerts_enabled",
        "item_popup_enabled",
        "item_default_buy_discount_pct",
        "item_default_sell_markup_pct",
        "points_alerts_enabled",
        "points_buy_zone",
        "points_sell_zone",
        "points_default_buy_discount_pct",
        "points_default_sell_markup_pct",
        "travel_alerts_enabled",
        "travel_min_profit",
        "travel_min_arrival_chance",
        "travel_items_per_trip",
        "auto_scan_enabled",
        "compact_mode",
        "tos_accepted",
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
        unread = conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE torn_id=? AND is_read=0",
            (request.user["torn_id"],),
        ).fetchone()["c"]
    return jsonify({"ok": True, "alerts": [dict(r) for r in rows], "unread": int(unread or 0)})


@app.post("/api/alerts/read")
@require_auth
def mark_alerts_read():
    payload = request.get_json(silent=True) or {}
    alert_id = payload.get("id")
    with db() as conn:
        if alert_id:
            conn.execute(
                "UPDATE alerts SET is_read=1 WHERE torn_id=? AND id=?",
                (request.user["torn_id"], int(alert_id)),
            )
        else:
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
        user_hist = conn.execute(
            """
            SELECT acronym, name, COUNT(*) AS samples, AVG(estimated_profit_pct) AS avg_profit_pct,
                   MAX(created_at) AS last_seen
            FROM user_stock_holding_snapshots
            WHERE torn_id=?
            GROUP BY acronym, name
            ORDER BY last_seen DESC
            LIMIT 8
            """,
            (request.user["torn_id"],),
        ).fetchall()
        pattern_rows = conn.execute(
            """
            SELECT acronym, name, pattern_label, pattern_confidence, pattern_score,
                   support_touches, trend_1h_pct, trend_6h_pct, trend_24h_pct,
                   position_7d, volatility_24h, reason, created_at
            FROM stock_pattern_results
            WHERE id IN (SELECT MAX(id) FROM stock_pattern_results GROUP BY acronym)
            ORDER BY pattern_score DESC
            LIMIT 12
            """
        ).fetchall()
    ranked_all = []
    for r in recent:
        ranked_all.append(score_stock(dict(r) | {"stock_id": r["acronym"], "market_cap": None, "total_shares": None}, request.user["torn_id"]))
    ranked_all = sorted(ranked_all, key=lambda x: x["score"], reverse=True)
    ranked = ranked_all[:10]
    live_pick = None
    if pick:
        for row in ranked_all:
            if row.get("acronym") == pick.get("acronym"):
                live_pick = row
                break
    diagnostics = None
    if pick and ranked_all:
        age_hours = _parse_iso_age_hours(pick.get("created_at"))
        best = ranked_all[0]
        diagnostics = {
            "pick_age_hours": round(age_hours, 2),
            "active_pick_saved_score": pick.get("score"),
            "active_pick_live_score": live_pick.get("score") if live_pick else None,
            "best_live_acronym": best.get("acronym"),
            "best_live_score": best.get("score"),
            "note": "The pick now compares live scores and refreshes every 24h, so it will not stay stuck on an old saved score."
        }
    move_status = stock_move_status(request.user["torn_id"], ranked_all)
    return jsonify({"ok": True, "pick": pick, "ranked": ranked, "snapshot_count": count, "stock_move": move_status, "stock_learning": stock_learning_summary(request.user["torn_id"]), "user_stock_history": [dict(r) for r in user_hist], "stock_patterns": [dict(r) for r in pattern_rows], "diagnostics": diagnostics, "server_time": now_iso()})



@app.post("/api/stocks/reviewed")
@require_auth
def stocks_reviewed():
    row = reset_stock_move_timer(request.user["torn_id"])
    return jsonify({"ok": True, "message": "Stock 24h timer reset from the current active pick.", "watch": row, "move_status": stock_move_status(request.user["torn_id"])})


@app.get("/api/stocks/user-history")
@require_auth
def stocks_user_history():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT acronym, name, shares, average_buy_price, current_price, estimated_value,
                   estimated_profit, estimated_profit_pct, created_at
            FROM user_stock_holding_snapshots
            WHERE torn_id=?
            ORDER BY id DESC
            LIMIT 60
            """,
            (request.user["torn_id"],),
        ).fetchall()
    return jsonify({"ok": True, "holdings": [dict(r) for r in rows]})

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


@app.get("/api/items/catalog")
@require_auth
def items_catalog():
    q = (request.args.get("q") or "").strip()
    key = get_api_key(request.user["torn_id"])
    # Refresh catalog if empty or user searched and nothing is cached.
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM item_catalog").fetchone()["c"]
    if count == 0 and key:
        try:
            refresh_item_catalog(key)
        except Exception:
            pass
    items = find_catalog_items(q, limit=30)
    if q and not items and key:
        try:
            refresh_item_catalog(key)
            items = find_catalog_items(q, limit=30)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not refresh Torn item list: {e}"}), 400
    return jsonify({"ok": True, "items": items})


@app.post("/api/items/watch")
@require_auth
def items_watch():
    payload = request.get_json(silent=True) or {}
    q = str(payload.get("query") or payload.get("name") or "").strip()
    item_id = payload.get("item_id")
    buy_zone = payload.get("buy_zone")
    sell_zone = payload.get("sell_zone")
    try:
        buy_zone = float(buy_zone) if str(buy_zone or "").strip() else None
    except Exception:
        buy_zone = None
    try:
        sell_zone = float(sell_zone) if str(sell_zone or "").strip() else None
    except Exception:
        sell_zone = None
    key = get_api_key(request.user["torn_id"])
    if item_id is None and q:
        items = find_catalog_items(q, limit=1)
        if not items and key:
            refresh_item_catalog(key)
            items = find_catalog_items(q, limit=1)
        if not items:
            return jsonify({"ok": False, "error": "Item not found. Try item ID or a clearer name."}), 404
        item_id = items[0]["item_id"]
        name = items[0]["name"]
    else:
        try:
            item_id = int(item_id)
        except Exception:
            return jsonify({"ok": False, "error": "Enter an item name or item ID."}), 400
        items = find_catalog_items(str(item_id), limit=1)
        if not items and key:
            try:
                refresh_item_catalog(key)
                items = find_catalog_items(str(item_id), limit=1)
            except Exception:
                pass
        name = items[0]["name"] if items else (q or f"Item {item_id}")
    upsert_watch_item(request.user["torn_id"], int(item_id), name, buy_zone, sell_zone)
    result = scan_item_market_for_user(request.user["torn_id"], reason="watch_added")
    return jsonify({"ok": True, "item": {"item_id": int(item_id), "name": name}, "scan": result})


@app.post("/api/items/unwatch")
@require_auth
def items_unwatch():
    payload = request.get_json(silent=True) or {}
    try:
        item_id = int(payload.get("item_id"))
    except Exception:
        return jsonify({"ok": False, "error": "Missing item_id."}), 400
    with db() as conn:
        conn.execute("UPDATE item_watchlist SET enabled=0, updated_at=? WHERE torn_id=? AND item_id=?", (now_iso(), request.user["torn_id"], item_id))
    return jsonify({"ok": True})


@app.post("/api/items/scan")
@require_auth
def items_scan():
    result = scan_item_market_for_user(request.user["torn_id"], reason="manual")
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.get("/api/items/market")
@require_auth
def items_market():
    rows = latest_item_market_rows(request.user["torn_id"])
    with db() as conn:
        signals = conn.execute(
            "SELECT item_id, name, signal, current_price, buy_zone, sell_zone, reason, link, created_at FROM item_signals WHERE torn_id=? ORDER BY id DESC LIMIT 20",
            (request.user["torn_id"],),
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) AS c FROM item_market_snapshots").fetchone()["c"]
    return jsonify({"ok": True, "watchlist": rows, "signals": [dict(r) for r in signals], "snapshot_count": count, "server_time": now_iso()})


@app.post("/api/points/scan")
@require_auth
def points_scan():
    result = scan_points_market_for_user(request.user["torn_id"], reason="manual")
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.get("/api/points/market")
@require_auth
def points_market():
    return jsonify({"ok": True, **latest_points_market(request.user["torn_id"])})


@app.post("/api/points/settings")
@require_auth
def points_settings():
    payload = request.get_json(silent=True) or {}
    allowed = {"points_alerts_enabled", "points_buy_zone", "points_sell_zone", "points_default_buy_discount_pct", "points_default_sell_markup_pct"}
    changed = {}
    with db() as conn:
        for k, v in payload.items():
            if k not in allowed:
                continue
            val = str(v).strip()[:80]
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


@app.post("/api/travel/scan")
@require_auth
def travel_scan():
    result = scan_travel_profit_for_user(request.user["torn_id"], reason="manual")
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.get("/api/travel/profit")
@require_auth
def travel_profit():
    return jsonify({"ok": True, **latest_travel_profit(request.user["torn_id"])})


@app.post("/api/travel/settings")
@require_auth
def travel_settings():
    payload = request.get_json(silent=True) or {}
    allowed = {"travel_alerts_enabled", "travel_min_profit", "travel_min_arrival_chance", "travel_items_per_trip"}
    changed = {}
    with db() as conn:
        for k, v in payload.items():
            if k not in allowed:
                continue
            val = str(v).strip()[:80]
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



@app.post("/api/enemy/start")
@require_auth
def enemy_start():
    enemy = get_current_enemy_for_user(request.user["torn_id"])
    upsert_enemy_tracking_session(request.user["torn_id"], enemy)
    result = scan_enemy_activity_for_user(request.user["torn_id"], reason="manual_start")
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify({"ok": True, "enemy": enemy, **result})


@app.post("/api/enemy/scan")
@require_auth
def enemy_scan():
    result = scan_enemy_activity_for_user(request.user["torn_id"], reason="manual")
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.post("/api/enemy/stop")
@require_auth
def enemy_stop():
    stamp = now_iso()
    with db() as conn:
        conn.execute("UPDATE enemy_tracking_sessions SET enabled=0, updated_at=? WHERE torn_id=?", (stamp, request.user["torn_id"]))
    return jsonify({"ok": True})


@app.get("/api/enemy/activity")
@require_auth
def enemy_activity():
    return jsonify({"ok": True, **latest_enemy_activity(request.user["torn_id"])})


@app.post("/api/enemy/settings")
@require_auth
def enemy_settings():
    payload = request.get_json(silent=True) or {}
    allowed = {"enemy_tracking_window_hours", "enemy_alerts_enabled"}
    changed = {}
    with db() as conn:
        for k, v in payload.items():
            if k not in allowed:
                continue
            val = str(v).strip()[:80]
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

@app.post("/api/dev/test-alert")
@require_auth
def dev_test_alert():
    created = now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO alerts(torn_id, alert_type, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
            (request.user["torn_id"], "test", "Test alert", "Alerts are working. Alerts are working. Stock, item, points, travel, and enemy-window alerts can now use this alert feed.", "https://www.torn.com/", created),
        )
    return jsonify({"ok": True})



@app.post("/api/accuracy/run")
@require_auth
def accuracy_run():
    return jsonify({"ok": True, **run_accuracy_learning()})


@app.get("/api/accuracy")
@require_auth
def accuracy():
    return jsonify({"ok": True, **accuracy_dashboard(request.user["torn_id"])})


start_background_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
