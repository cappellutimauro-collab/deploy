from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import shutil
import smtplib
import socket
import sqlite3
import sys
import tempfile
import threading
import traceback
import webbrowser
import qrcode
import qrcode.image.svg
from PIL import Image, ImageDraw, ImageFont

try:
    import stripe
except ImportError:  # pragma: no cover - optional runtime dependency
    stripe = None

try:
    from docx import Document
except ImportError:  # pragma: no cover - optional runtime dependency
    Document = None
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urlparse

from rehab_app.architecture import APP_VERSION, SCHEMA_VERSION
from rehab_app.db import (
    connect_postgres,
    db_error_types,
    db_integrity_error_types,
    db_operational_error_types,
    postgres_enabled,
)


APP_NAME = "Rehab Philosophy"
PRIVACY_VERSION = "gdpr-privacy-2026-05-31"
CONSENT_VERSION = "consenso-fisioterapico-2026-05-31"


def app_dir() -> Path:
    env_dir = os.environ.get("REHAB_APP_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name.lower() == "dist" and (exe_dir.parent / "prova.py").exists():
            return exe_dir.parent
        return exe_dir
    return Path(__file__).resolve().parent

def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
RESOURCE_DIR = resource_dir()
DB_PATH = APP_DIR / "fisio_app.sqlite3"
CONSENT_DIR = APP_DIR / "consensi informati"
EMAIL_OUTBOX_DIR = APP_DIR / "email_outbox"
EMAIL_CONFIG_PATH = APP_DIR / "email_settings.json"
STRIPE_SECRET_KEY_PATH = APP_DIR / "stripe_secret_key.txt"
STRIPE_WEBHOOK_SECRET_PATH = APP_DIR / "stripe_webhook_secret.txt"
APP_SECRET_PATH = APP_DIR / "app_secret.key"
DOCTOR_UPLOAD_DIR = APP_DIR / "static" / "uploads" / "doctors"
STUDIO_UPLOAD_DIR = APP_DIR / "static" / "uploads" / "studio"
STUDIO_PLACEHOLDER_LOGO = "/static/studio-placeholder.svg"
BOOTSTRAP_LOCK = threading.Lock()
BOOTSTRAP_DONE = False


def load_app_secret() -> bytes:
    env_secret = os.environ.get("FISIO_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    try:
        if APP_SECRET_PATH.exists():
            stored = APP_SECRET_PATH.read_text(encoding="utf-8-sig").strip()
            if stored:
                return stored.encode("utf-8")
        generated = secrets.token_urlsafe(48)
        APP_SECRET_PATH.write_text(generated, encoding="utf-8")
        try:
            os.chmod(APP_SECRET_PATH, 0o600)
        except OSError:
            pass
        return generated.encode("utf-8")
    except OSError:
        return secrets.token_urlsafe(48).encode("utf-8")


SECRET = load_app_secret()
CANCEL_LIMIT_HOURS = 1
MAX_BOOKING_DAYS = 45
SLOT_TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30"]
SLOT_PARTS = {"morning": ("Mattina", "09:00", "13:00"), "afternoon": ("Pomeriggio", "16:00", "20:00")}
DEFAULT_CAPACITY = 3
DEFAULT_APPOINTMENT_PRICE = 50.0
SESSION_MINUTES = 10
MAX_FORM_BYTES = 5_000_000
DOCTOR_ROLES = {"admin", "doctor"}
OWNER_PERMISSION = "studio_owner"
DOCTOR_PERMISSION = "doctor"
PATIENT_PERMISSION = "patient"
RATE_LIMITS: dict[str, list[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()
RUNTIME_FALLBACK_ACTIVE = False


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def today() -> dt.date:
    return dt.date.today()


def database_file_is_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open("ab"):
                pass
        else:
            probe = path.parent / f".{path.name}.write-probe"
            with probe.open("wb"):
                pass
            probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def ensure_writable_runtime_paths() -> None:
    global APP_DIR, DB_PATH, CONSENT_DIR, EMAIL_OUTBOX_DIR, EMAIL_CONFIG_PATH
    global STRIPE_SECRET_KEY_PATH, STRIPE_WEBHOOK_SECRET_PATH, APP_SECRET_PATH
    global DOCTOR_UPLOAD_DIR, STUDIO_UPLOAD_DIR, RUNTIME_FALLBACK_ACTIVE

    if RUNTIME_FALLBACK_ACTIVE or database_file_is_writable(DB_PATH):
        return

    original_app_dir = APP_DIR
    original_db_path = DB_PATH
    runtime_dir = original_app_dir / "runtime-data"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        probe = runtime_dir / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        runtime_dir = Path(tempfile.gettempdir()) / "rehab-runtime-data"
        runtime_dir.mkdir(parents=True, exist_ok=True)
    target_db = runtime_dir / "fisio_app.sqlite3"
    if original_db_path.exists() and not target_db.exists():
        shutil.copy2(original_db_path, target_db)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{original_db_path}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, Path(f"{target_db}{suffix}"))

    for source_name in ("email_settings.json", "stripe_secret_key.txt", "stripe_webhook_secret.txt", "app_secret.key"):
        source = original_app_dir / source_name
        target = runtime_dir / source_name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)

    APP_DIR = runtime_dir
    DB_PATH = target_db
    CONSENT_DIR = APP_DIR / "consensi informati"
    EMAIL_OUTBOX_DIR = APP_DIR / "email_outbox"
    EMAIL_CONFIG_PATH = APP_DIR / "email_settings.json"
    STRIPE_SECRET_KEY_PATH = APP_DIR / "stripe_secret_key.txt"
    STRIPE_WEBHOOK_SECRET_PATH = APP_DIR / "stripe_webhook_secret.txt"
    APP_SECRET_PATH = APP_DIR / "app_secret.key"
    DOCTOR_UPLOAD_DIR = APP_DIR / "static" / "uploads" / "doctors"
    STUDIO_UPLOAD_DIR = APP_DIR / "static" / "uploads" / "studio"
    RUNTIME_FALLBACK_ACTIVE = True
    print(f"Database principale non scrivibile: uso copia runtime in {DB_PATH}")


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def parse_dt(date_value: str, time_value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{date_value}T{time_value}:00")


def money(value: float | int | None) -> str:
    return f"EUR {float(value or 0):.2f}"


def normalize_phone(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit() or ch == "+")


def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in {"prod", "production"}


def is_serverless_runtime() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def consent_storage_dir() -> Path:
    if is_serverless_runtime():
        return Path(tempfile.gettempdir()) / "rehab-consensi-informati"
    return CONSENT_DIR


def protect_secret_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def check_rate_limit(key: str, max_attempts: int, window_seconds: int) -> bool:
    timestamp = now().timestamp()
    cutoff = timestamp - window_seconds
    with RATE_LIMIT_LOCK:
        hits = [hit for hit in RATE_LIMITS.get(key, []) if hit >= cutoff]
        if len(hits) >= max_attempts:
            RATE_LIMITS[key] = hits
            return False
        hits.append(timestamp)
        RATE_LIMITS[key] = hits
        return True


def initial_admin_credentials() -> tuple[str, str]:
    email = os.environ.get("ADMIN_EMAIL", "admin@fisio.local").strip() or "admin@fisio.local"
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if password:
        return email, password
    if not is_production():
        return email, "admin123"
    password_file = APP_DIR / "initial_admin_password.txt"
    if password_file.exists():
        existing = password_file.read_text(encoding="utf-8-sig").strip()
        if existing:
            return email, existing
    generated = secrets.token_urlsafe(18)
    password_file.write_text(generated, encoding="utf-8")
    protect_secret_file(password_file)
    return email, generated


def sign(value: str) -> str:
    return hmac.new(SECRET, value.encode("utf-8"), hashlib.sha256).hexdigest()


def make_csrf_token() -> str:
    nonce = secrets.token_urlsafe(24)
    return f"{nonce}.{sign(f'csrf:{nonce}')}"


def verify_csrf_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, token_sig = token.rsplit(".", 1)
    if not nonce:
        return False
    return hmac.compare_digest(sign(f"csrf:{nonce}"), token_sig)


def reset_token_digest(token: str) -> str:
    return hmac.new(SECRET, f"password-reset:{token}".encode("utf-8"), hashlib.sha256).hexdigest()


def make_token(user_id: int, remember: bool = False) -> str:
    expiry = now() + (dt.timedelta(days=30) if remember else dt.timedelta(minutes=SESSION_MINUTES))
    payload = {
        "uid": user_id,
        "exp": int(expiry.timestamp()),
        "nonce": secrets.token_urlsafe(8),
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"{raw}.{sign(raw)}"


def read_token(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    raw, token_sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sign(raw), token_sig):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(now().timestamp()):
        return None
    return int(payload["uid"])


def make_presence_token(appointment_id: int) -> str:
    raw = str(appointment_id)
    short_sig = sign(f"presence:{raw}")[:24]
    return f"{raw}.{short_sig}"


def read_presence_token(token: str) -> int | None:
    if not token or "." not in token:
        return None
    raw, token_sig = token.rsplit(".", 1)
    if not raw.isdigit():
        return None
    expected = sign(f"presence:{raw}")[:24]
    if not hmac.compare_digest(expected, token_sig):
        return None
    return int(raw)


def qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=3)
    return image.to_string(encoding="unicode")

PASSWORD_ITERATIONS = 390_000
LEGACY_PASSWORD_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) == 4:
            algorithm, iteration_value, salt, digest = parts
            iterations = int(iteration_value)
        elif len(parts) == 3:
            algorithm, salt, digest = parts
            iterations = LEGACY_PASSWORD_ITERATIONS
        else:
            return False
        if algorithm != "pbkdf2_sha256":
            return False
    except (ValueError, TypeError):
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return hmac.compare_digest(check, digest)


def password_needs_rehash(stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4:
        return True
    try:
        return int(parts[1]) < PASSWORD_ITERATIONS
    except ValueError:
        return True


def connect() -> Any:
    if postgres_enabled():
        return connect_postgres()
    ensure_writable_runtime_paths()
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not is_production() or not getattr(sys, "frozen", False):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except db_operational_error_types():
            pass
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS studios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            logo_path TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            tax_id TEXT NOT NULL DEFAULT '',
            brand_color TEXT NOT NULL DEFAULT '#004f3f',
            setup_completed_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL CHECK(role IN ('user', 'admin', 'doctor')),
            studio_id INTEGER REFERENCES studios(id),
            permissions TEXT NOT NULL DEFAULT '',
            account_status TEXT NOT NULL DEFAULT 'active',
            archived_at TEXT,
            archived_reason TEXT,
            bookable INTEGER NOT NULL DEFAULT 1,
            profile_visible INTEGER NOT NULL DEFAULT 1,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            phone TEXT NOT NULL,
            fiscal_code TEXT NOT NULL,
            gdpr_consent INTEGER NOT NULL DEFAULT 0,
            privacy_accepted_at TEXT,
            privacy_version TEXT,
            consent_signed_at TEXT,
            consent_version TEXT,
            consent_file TEXT,
            consent_file_hash TEXT,
            guardian_name TEXT,
            guardian_fiscal_code TEXT,
            guardian_relation TEXT,
            doctor_bio TEXT NOT NULL DEFAULT '',
            doctor_years_experience INTEGER NOT NULL DEFAULT 0,
            doctor_degree TEXT NOT NULL DEFAULT '',
            doctor_qualification TEXT NOT NULL DEFAULT '',
            doctor_gender TEXT NOT NULL DEFAULT '',
            doctor_profile_image TEXT NOT NULL DEFAULT '',
            doctor_location TEXT NOT NULL DEFAULT '',
            doctor_stripe_account TEXT NOT NULL DEFAULT '',
            doctor_onboarded_at TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0,
            phone_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0,
            doctor_id INTEGER REFERENCES users(id),
            UNIQUE(doctor_id, slot_date, slot_time)
        );

        CREATE TABLE IF NOT EXISTS service_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            doctor_id INTEGER REFERENCES users(id),
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(doctor_id, name)
        );

        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            doctor_id INTEGER REFERENCES users(id),
            slot_id INTEGER NOT NULL REFERENCES slots(id),
            service_type_id INTEGER REFERENCES service_types(id),
            doctor_name_snapshot TEXT,
            doctor_qualification_snapshot TEXT,
            service_name_snapshot TEXT,
            service_description_snapshot TEXT,
            price_snapshot REAL,
            consent_version_snapshot TEXT,
            chargeable INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'prenotata',
            auto_suggestion TEXT,
            checked_in_at TEXT,
            created_at TEXT NOT NULL,
            cancelled_at TEXT,
            price REAL NOT NULL DEFAULT 50.0,
            diary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL REFERENCES appointments(id),
            paid_at TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            stripe_session_id TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            actor_id INTEGER,
            target_type TEXT,
            target_id INTEGER,
            metadata TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )

    def add_column(table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    add_column("users", "privacy_accepted_at", "TEXT")
    add_column("users", "privacy_version", "TEXT")
    add_column("users", "studio_id", "INTEGER REFERENCES studios(id)")
    add_column("users", "permissions", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "account_status", "TEXT NOT NULL DEFAULT 'active'")
    add_column("users", "archived_at", "TEXT")
    add_column("users", "archived_reason", "TEXT")
    add_column("users", "bookable", "INTEGER NOT NULL DEFAULT 1")
    add_column("users", "profile_visible", "INTEGER NOT NULL DEFAULT 1")
    add_column("users", "consent_signed_at", "TEXT")
    add_column("users", "consent_version", "TEXT")
    add_column("users", "consent_file", "TEXT")
    add_column("users", "consent_file_hash", "TEXT")
    add_column("users", "guardian_name", "TEXT")
    add_column("users", "guardian_first_name", "TEXT")
    add_column("users", "guardian_last_name", "TEXT")
    add_column("users", "guardian_fiscal_code", "TEXT")
    add_column("users", "guardian_relation", "TEXT")
    add_column("users", "birth_place", "TEXT")
    add_column("users", "birth_date", "TEXT")
    add_column("users", "residence_city", "TEXT")
    add_column("users", "residence_cap", "TEXT")
    add_column("users", "address", "TEXT")
    add_column("users", "minor_or_dependent", "INTEGER NOT NULL DEFAULT 0")
    add_column("users", "guardian_birth_place", "TEXT")
    add_column("users", "guardian_birth_date", "TEXT")
    add_column("users", "guardian_residence_city", "TEXT")
    add_column("users", "guardian_phone", "TEXT")
    add_column("users", "guardian_email", "TEXT")
    add_column("users", "guardian_relation_type", "TEXT")
    add_column("users", "consent_signature_data", "TEXT")
    add_column("users", "doctor_bio", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "doctor_years_experience", "INTEGER NOT NULL DEFAULT 0")
    add_column("users", "doctor_degree", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "doctor_qualification", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "doctor_gender", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "doctor_profile_image", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "doctor_location", "TEXT NOT NULL DEFAULT ''")
    add_column("users", "doctor_onboarded_at", "TEXT")
    add_column("users", "doctor_stripe_account", "TEXT NOT NULL DEFAULT ''")
    add_column("slots", "doctor_id", "INTEGER REFERENCES users(id)")
    add_column("slots", "archived_at", "TEXT")
    add_column("service_types", "description", "TEXT NOT NULL DEFAULT ''")
    add_column("service_types", "doctor_id", "INTEGER REFERENCES users(id)")
    add_column("service_types", "archived_at", "TEXT")
    add_column("appointments", "doctor_id", "INTEGER REFERENCES users(id)")
    add_column("appointments", "service_type_id", "INTEGER REFERENCES service_types(id)")
    add_column("appointments", "doctor_name_snapshot", "TEXT")
    add_column("appointments", "doctor_qualification_snapshot", "TEXT")
    add_column("appointments", "service_name_snapshot", "TEXT")
    add_column("appointments", "service_description_snapshot", "TEXT")
    add_column("appointments", "price_snapshot", "REAL")
    add_column("appointments", "consent_version_snapshot", "TEXT")
    add_column("appointments", "chargeable", "INTEGER NOT NULL DEFAULT 1")
    add_column("appointments", "diary", "TEXT NOT NULL DEFAULT ''")
    add_column("payments", "stripe_session_id", "TEXT")
    add_column("password_resets", "token_hash", "TEXT")
    add_column("events", "actor_id", "INTEGER")
    add_column("events", "target_type", "TEXT")
    add_column("events", "target_id", "INTEGER")
    add_column("events", "metadata", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_token_hash ON password_resets(token_hash)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_stripe_session_id ON payments(stripe_session_id) "
        "WHERE stripe_session_id IS NOT NULL AND stripe_session_id != ''"
    )

    user_count = int(conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"] or 0)
    studio = conn.execute("SELECT id FROM studios ORDER BY id LIMIT 1").fetchone()
    should_create_legacy_studio = user_count > 0 or os.environ.get("REHAB_AUTO_ADMIN", "").strip() == "1"
    if not studio and should_create_legacy_studio:
        conn.execute(
            """
            INSERT INTO studios (name, logo_path, email, phone, address, tax_id, brand_color, created_at)
            VALUES ('Studio principale', '', '', '', '', '', '#004f3f', ?)
            """,
            (now().isoformat(),),
        )
        studio = conn.execute("SELECT id FROM studios ORDER BY id LIMIT 1").fetchone()
    primary_studio_id = int(studio["id"]) if studio else None

    admin = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    if not admin and user_count == 0 and os.environ.get("REHAB_AUTO_ADMIN", "").strip() == "1":
        admin_email, admin_password = initial_admin_credentials()
        conn.execute(
            """
            INSERT INTO users
            (role, studio_id, permissions, first_name, last_name, email, password_hash, phone, fiscal_code,
             gdpr_consent, privacy_accepted_at, privacy_version, email_verified, phone_verified, created_at)
            VALUES ('admin', ?, 'studio_owner,doctor', 'Giuseppe', 'Dellorusso', ?, ?, '+390000000000',
                    'ADMIN0000000000', 1, ?, ?, 1, 1, ?)
            """,
            (primary_studio_id, admin_email.lower(), hash_password(admin_password), now().isoformat(), PRIVACY_VERSION, now().isoformat()),
        )
        user_count = 1
    if primary_studio_id:
        conn.execute("UPDATE users SET studio_id = ? WHERE studio_id IS NULL", (primary_studio_id,))
    conn.execute("UPDATE users SET account_status = 'active' WHERE account_status IS NULL OR account_status = ''")
    conn.execute("UPDATE users SET permissions = 'studio_owner,doctor' WHERE role = 'admin' AND (permissions IS NULL OR permissions = '')")
    conn.execute("UPDATE users SET permissions = 'doctor' WHERE role = 'doctor' AND (permissions IS NULL OR permissions = '')")
    conn.execute("UPDATE users SET permissions = 'patient' WHERE role = 'user' AND (permissions IS NULL OR permissions = '')")
    conn.execute("UPDATE users SET bookable = 1 WHERE role IN ('admin', 'doctor') AND (bookable IS NULL)")
    conn.execute("UPDATE users SET profile_visible = 1 WHERE role IN ('admin', 'doctor') AND (profile_visible IS NULL)")
    if user_count > 0:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('studio_setup_complete', '1') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        if primary_studio_id:
            conn.execute(
                "UPDATE studios SET setup_completed_at = COALESCE(setup_completed_at, ?) WHERE id = ?",
                (now().isoformat(), primary_studio_id),
            )
    primary_doctor_candidates = conn.execute("SELECT * FROM users WHERE role IN ('admin', 'doctor') ORDER BY id").fetchall()
    primary_doctor = next((row for row in primary_doctor_candidates if DOCTOR_PERMISSION in permissions_for(row)), None)
    primary_doctor_id = int(primary_doctor["id"]) if primary_doctor else None
    if primary_doctor_id:
        conn.execute(
            """
            UPDATE users
            SET first_name = 'Giuseppe', last_name = 'Dellorusso'
            WHERE id = ? AND role IN ('admin', 'doctor') AND first_name = 'Admin' AND last_name = 'Centro'
            """,
            (primary_doctor_id,),
        )
        conn.execute(
            """
            UPDATE users
            SET doctor_qualification = CASE WHEN doctor_qualification = '' THEN 'Fisioterapista' ELSE doctor_qualification END,
                doctor_bio = CASE WHEN doctor_bio = '' THEN 'Percorsi fisioterapici personalizzati, valutazione funzionale e riabilitazione orientata al recupero del movimento.' ELSE doctor_bio END,
                doctor_location = CASE WHEN doctor_location = '' THEN COALESCE((SELECT name FROM studios ORDER BY id LIMIT 1), 'Studio') ELSE doctor_location END,
                doctor_onboarded_at = COALESCE(doctor_onboarded_at, ?)
            WHERE id = ? AND role IN ('admin', 'doctor')
            """,
            (now().isoformat(), primary_doctor_id),
        )
        conn.execute("UPDATE slots SET doctor_id = ? WHERE doctor_id IS NULL", (primary_doctor_id,))
        conn.execute("UPDATE service_types SET doctor_id = ? WHERE doctor_id IS NULL", (primary_doctor_id,))
        conn.execute(
            """
            UPDATE appointments
            SET doctor_id = COALESCE(
                doctor_id,
                (SELECT doctor_id FROM slots WHERE slots.id = appointments.slot_id),
                ?
            )
            WHERE doctor_id IS NULL
            """,
            (primary_doctor_id,),
        )

        conn.commit()

        def table_sql(table: str) -> str:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
            return row["sql"] if row and row["sql"] else ""

        slots_sql = table_sql("slots")
        if "UNIQUE(slot_date, slot_time)" in slots_sql.replace(" ", "") or "UNIQUE(slot_date,slot_time)" in slots_sql.replace(" ", ""):
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """
                CREATE TABLE slots_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_date TEXT NOT NULL,
                    slot_time TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    doctor_id INTEGER REFERENCES users(id),
                    UNIQUE(doctor_id, slot_date, slot_time)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO slots_new (id, slot_date, slot_time, capacity, blocked, doctor_id)
                SELECT id, slot_date, slot_time, capacity, blocked, COALESCE(doctor_id, ?) FROM slots
                """,
                (primary_doctor_id,),
            )
            conn.execute("DROP TABLE slots")
            conn.execute("ALTER TABLE slots_new RENAME TO slots")
            conn.execute("PRAGMA foreign_keys = ON")

        service_sql = table_sql("service_types")
        service_sql_clean = service_sql.replace(" ", "").lower()
        if "nametextnotnullunique" in service_sql_clean or "unique(name)" in service_sql_clean:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """
                CREATE TABLE service_types_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    doctor_id INTEGER REFERENCES users(id),
                    description TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(doctor_id, name)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO service_types_new (id, name, doctor_id, description, active, created_at)
                SELECT id, name, COALESCE(doctor_id, ?), COALESCE(description, ''), active, created_at FROM service_types
                """,
                (primary_doctor_id,),
            )
            conn.execute("DROP TABLE service_types")
            conn.execute("ALTER TABLE service_types_new RENAME TO service_types")
            conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_doctor_date ON slots(doctor_id, slot_date, slot_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_services_doctor ON service_types(doctor_id, active, name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_doctor ON appointments(doctor_id, status)")
    conn.execute(
        """
        UPDATE appointments
        SET price_snapshot = COALESCE(price_snapshot, price),
            consent_version_snapshot = COALESCE(
                consent_version_snapshot,
                (SELECT consent_version FROM users WHERE users.id = appointments.user_id),
                ?
            )
        WHERE price_snapshot IS NULL OR consent_version_snapshot IS NULL
        """,
        (CONSENT_VERSION,),
    )
    conn.execute(
        """
        UPDATE appointments
        SET doctor_name_snapshot = COALESCE(
                doctor_name_snapshot,
                (SELECT
                    CASE WHEN lower(COALESCE(users.doctor_gender, '')) IN ('f', 'female', 'donna') THEN 'Dott.ssa ' ELSE 'Dott. ' END
                    || users.first_name || ' ' || users.last_name
                 FROM users WHERE users.id = appointments.doctor_id)
            ),
            doctor_qualification_snapshot = COALESCE(
                doctor_qualification_snapshot,
                (SELECT NULLIF(users.doctor_qualification, '') FROM users WHERE users.id = appointments.doctor_id),
                'Fisioterapista'
            )
        WHERE doctor_name_snapshot IS NULL OR doctor_qualification_snapshot IS NULL
        """
    )
    conn.execute(
        """
        UPDATE appointments
        SET service_name_snapshot = COALESCE(
                service_name_snapshot,
                (SELECT service_types.name FROM service_types WHERE service_types.id = appointments.service_type_id)
            ),
            service_description_snapshot = COALESCE(
                service_description_snapshot,
                (SELECT service_types.description FROM service_types WHERE service_types.id = appointments.service_type_id),
                ''
            )
        WHERE service_name_snapshot IS NULL OR service_description_snapshot IS NULL
        """
    )
    applied_at = now().isoformat()
    settings_to_persist = {
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "privacy_version": PRIVACY_VERSION,
        "consent_version": CONSENT_VERSION,
    }
    for key, value in settings_to_persist.items():
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, applied_at),
    )
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    try:
        conn = connect()
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default
    except db_error_types():
        return default


def set_setting(key: str, value: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def primary_studio(conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    should_close = conn is None
    conn = conn or connect()
    row = conn.execute("SELECT * FROM studios ORDER BY id LIMIT 1").fetchone()
    if should_close:
        conn.close()
    return row


def studio_logo_url(default: str = STUDIO_PLACEHOLDER_LOGO) -> str:
    try:
        studio = primary_studio()
        if studio and "logo_path" in studio.keys() and studio["logo_path"]:
            return studio["logo_path"]
    except db_error_types():
        pass
    return default


def studio_display_name(default: str = "Studio") -> str:
    try:
        studio = primary_studio()
        if studio and "name" in studio.keys() and studio["name"]:
            return studio["name"]
    except db_error_types():
        pass
    return default


def studio_practitioner_label(default: str = "Professionista sanitario") -> str:
    try:
        conn = connect()
        rows = conn.execute("SELECT * FROM users WHERE role IN ('admin', 'doctor') ORDER BY id").fetchall()
        conn.close()
        doctor = next((row for row in rows if DOCTOR_PERMISSION in permissions_for(row) and account_is_active(row)), None)
        if doctor:
            return doctor_display_name(doctor)
    except db_error_types():
        pass
    return default


def studio_setup_required() -> bool:
    conn = connect()
    user_count = int(conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"] or 0)
    setting = conn.execute("SELECT value FROM app_settings WHERE key = 'studio_setup_complete'").fetchone()
    conn.close()
    return user_count == 0 and (not setting or setting["value"] != "1")


def default_slot_capacity() -> int:
    try:
        return max(int(get_setting("default_capacity", str(DEFAULT_CAPACITY))), 0)
    except ValueError:
        return DEFAULT_CAPACITY

def ensure_slots(start: dt.date | None = None, days: int = 14, doctor_id: int | None = None) -> None:
    start = start or today()
    conn = connect()
    doctors = [doctor_id] if doctor_id else [int(row["id"]) for row in all_doctors(conn)]
    if not doctors:
        fallback = primary_doctor_id(conn)
        doctors = [fallback] if fallback else []
    capacity = default_slot_capacity()
    rows: list[tuple[str, str, int, int]] = []
    for did in doctors:
        if not did:
            continue
        for offset in range(days):
            day = start + dt.timedelta(days=offset)
            if day.weekday() == 6:
                continue
            for slot_time in SLOT_TIMES:
                rows.append((day.isoformat(), slot_time, capacity, did))
    if rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO slots (slot_date, slot_time, capacity, blocked, doctor_id)
            VALUES (?, ?, ?, 0, ?)
            """,
            rows,
        )
    conn.commit()
    conn.close()


def log_event(
    kind: str,
    message: str,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn = connect()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if {"actor_id", "target_type", "target_id", "metadata"}.issubset(columns):
        conn.execute(
            """
            INSERT INTO events (user_id, kind, message, actor_id, target_type, target_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                kind,
                message,
                user_id,
                target_type,
                target_id,
                json.dumps(metadata or {}, ensure_ascii=False) if metadata else None,
                now().isoformat(),
            ),
        )
    else:
        conn.execute(
            "INSERT INTO events (user_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
            (user_id, kind, message, now().isoformat()),
        )
    conn.commit()
    conn.close()


def run_noncritical(label: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as exc:
        print(f"Rehab noncritical error [{label}]: {exc!r}", file=sys.stderr)
        traceback.print_exc()


def update_auto_suggestions() -> None:
    conn = connect()
    rows = conn.execute(
        """
        SELECT a.id, a.checked_in_at, s.slot_date, s.slot_time
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        WHERE a.status = 'prenotata'
        """
    ).fetchall()
    current = now()
    for row in rows:
        appointment_time = parse_dt(row["slot_date"], row["slot_time"])
        if current <= appointment_time + dt.timedelta(minutes=30):
            continue
        suggestion = "effettuata" if row["checked_in_at"] else "non_presentato_auto"
        conn.execute(
            "UPDATE appointments SET auto_suggestion = ? WHERE id = ?",
            (suggestion, row["id"]),
        )
    conn.commit()
    conn.close()


def user_by_id(user_id: int | None) -> sqlite3.Row | None:
    if not user_id:
        return None
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_from_cookie(headers: Any) -> sqlite3.Row | None:
    cookie = SimpleCookie(headers.get("Cookie"))
    token = cookie["session"].value if "session" in cookie else None
    return user_by_id(read_token(token))


def row_has(row: sqlite3.Row | None, key: str) -> bool:
    return bool(row and key in row.keys())


def permissions_for(user: sqlite3.Row | None) -> set[str]:
    if not user:
        return set()
    explicit = user["permissions"] if row_has(user, "permissions") and user["permissions"] else ""
    if explicit:
        return {part.strip() for part in explicit.split(",") if part.strip()}
    role = user["role"]
    if role == "admin":
        return {OWNER_PERMISSION, DOCTOR_PERMISSION}
    if role == "doctor":
        return {DOCTOR_PERMISSION}
    return {PATIENT_PERMISSION}


def has_permission(user: sqlite3.Row | None, permission: str) -> bool:
    return permission in permissions_for(user)


def account_is_active(user: sqlite3.Row | None) -> bool:
    if not user:
        return False
    status = user["account_status"] if row_has(user, "account_status") else "active"
    return status not in {"archived", "deleted"}


def is_studio_owner(user: sqlite3.Row | None) -> bool:
    return account_is_active(user) and has_permission(user, OWNER_PERMISSION)


def is_doctor_account(user: sqlite3.Row | None) -> bool:
    return account_is_active(user) and has_permission(user, DOCTOR_PERMISSION)


def is_staff_account(user: sqlite3.Row | None) -> bool:
    return is_studio_owner(user) or is_doctor_account(user)


def is_bookable_doctor(user: sqlite3.Row | None) -> bool:
    if not is_doctor_account(user):
        return False
    bookable = int(user["bookable"] or 0) if row_has(user, "bookable") else 1
    visible = int(user["profile_visible"] or 0) if row_has(user, "profile_visible") else 1
    return bookable == 1 and visible == 1


def doctor_title(row: sqlite3.Row) -> str:
    gender = (row["doctor_gender"] if "doctor_gender" in row.keys() else "").strip().lower()
    return "Dott.ssa" if gender in {"f", "female", "donna"} else "Dott."


def doctor_display_name(row: sqlite3.Row) -> str:
    return f"{doctor_title(row)} {row['first_name']} {row['last_name']}"


def doctor_photo_url(row: sqlite3.Row) -> str:
    image = row["doctor_profile_image"] if "doctor_profile_image" in row.keys() else ""
    if image and image.startswith("/static/"):
        return image
    return studio_logo_url()


def doctor_qualification(row: sqlite3.Row) -> str:
    if "doctor_qualification" in row.keys() and row["doctor_qualification"]:
        return row["doctor_qualification"]
    return "Fisioterapista"


def doctor_degree(row: sqlite3.Row) -> str:
    if "doctor_degree" in row.keys() and row["doctor_degree"]:
        return row["doctor_degree"]
    return doctor_qualification(row)


def doctor_bio(row: sqlite3.Row) -> str:
    if "doctor_bio" in row.keys() and row["doctor_bio"]:
        return row["doctor_bio"]
    return "Percorso riabilitativo personalizzato e orientato al recupero funzionale."


def doctor_location(row: sqlite3.Row) -> str:
    if "doctor_location" in row.keys() and row["doctor_location"]:
        return row["doctor_location"]
    return studio_display_name()


def all_doctors(conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    should_close = conn is None
    conn = conn or connect()
    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE role IN ('admin', 'doctor')
        ORDER BY last_name, first_name, id
        """
    ).fetchall()
    if should_close:
        conn.close()
    return [row for row in rows if is_bookable_doctor(row)]


def primary_doctor_id(conn: sqlite3.Connection | None = None) -> int | None:
    rows = all_doctors(conn)
    return int(rows[0]["id"]) if rows else None


def doctor_by_id(doctor_id: int | str | None) -> sqlite3.Row | None:
    try:
        did = int(doctor_id or 0)
    except (TypeError, ValueError):
        return None
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id = ? AND role IN ('admin', 'doctor')", (did,)).fetchone()
    conn.close()
    return row if is_bookable_doctor(row) else None


def save_doctor_profile_image(user_id: int, image_data: str) -> str:
    if not image_data:
        return ""
    match = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", image_data, re.I | re.S)
    if not match:
        raise ValueError("Immagine profilo non valida")
    ext = "jpg" if match.group(1).lower() in {"jpeg", "jpg"} else match.group(1).lower()
    raw = base64.b64decode(match.group(2), validate=True)
    if len(raw) > 2_500_000:
        raise ValueError("Immagine profilo troppo grande")
    Image.open(io.BytesIO(raw)).verify()
    if postgres_enabled():
        return image_data
    DOCTOR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = DOCTOR_UPLOAD_DIR / f"doctor_{int(user_id)}.{ext}"
    target.write_bytes(raw)
    return f"/static/uploads/doctors/{target.name}"


def save_studio_logo_image(studio_id: int, image_data: str) -> str:
    if not image_data:
        return ""
    match = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", image_data, re.I | re.S)
    if not match:
        raise ValueError("Logo studio non valido")
    ext = "jpg" if match.group(1).lower() in {"jpeg", "jpg"} else match.group(1).lower()
    raw = base64.b64decode(match.group(2), validate=True)
    if len(raw) > 2_500_000:
        raise ValueError("Logo studio troppo grande")
    Image.open(io.BytesIO(raw)).verify()
    if postgres_enabled():
        return image_data
    STUDIO_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = STUDIO_UPLOAD_DIR / f"studio_{int(studio_id)}.{ext}"
    target.write_bytes(raw)
    return f"/static/uploads/studio/{target.name}"


CHARGEABLE_STATUSES = {"effettuata"}
ONLINE_PAYABLE_STATUSES = {"prenotata", "effettuata"}


def is_chargeable_status(status: str) -> bool:
    return status in CHARGEABLE_STATUSES


def is_billable_status(status: str) -> bool:
    return is_chargeable_status(status)


def is_online_payable_status(status: str) -> bool:
    return status in ONLINE_PAYABLE_STATUSES


def chargeability_label(status: str) -> str:
    if status == "prenotata":
        return "Pagamento anticipato"
    return "Addebitabile" if is_chargeable_status(status) else "Non addebitabile"


def payment_state_class(state: str) -> str:
    normalized = state.lower()
    if "pagata" in normalized and "non" not in normalized:
        return "ok"
    if "non dovuto" in normalized:
        return ""
    return "low"


def payment_status_inline(residual: float | int, state: str) -> str:
    amount = max(float(residual or 0), 0)
    due = amount > 0.005
    label = "Da pagare" if due else ("Non dovuto" if "non dovuto" in state.lower() else "Pagata")
    state_class = "neutral" if "non dovuto" in state.lower() else ("due" if due else "paid")
    return (
        f'<span class="payment-inline {state_class}">'
        f'<strong>{money(amount)}</strong>'
        f'<span><i aria-hidden="true"></i>{html.escape(label)}</span>'
        f'</span>'
    )


def billable_amount(status: str, price: float | int | None) -> float:
    return float(price or 0) if is_chargeable_status(status) else 0.0


def appointment_payment_state(app_id: int, total: float, status: str = "effettuata") -> tuple[float, float, str]:
    conn = connect()
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE appointment_id = ?",
        (app_id,),
    ).fetchone()["total"]
    conn.close()
    due = billable_amount(status, total)
    residual = max(due - float(paid), 0)
    if not is_billable_status(status):
        state = "Non dovuto"
        return float(paid), residual, state
    if residual <= 0:
        state = "Pagato"
    elif paid > 0:
        state = "Parziale"
    else:
        state = "Non pagato"
    return float(paid), residual, state


def status_label(status: str, auto_suggestion: str | None = None) -> str:
    labels = {
        "prenotata": "Prenotata",
        "effettuata": "Effettuata",
        "non_presentato": "Non presentato",
        "non_presentato_auto": "Non presentato (auto)",
        "cancellata": "Cancellata",
    }
    if status == "prenotata" and auto_suggestion:
        return labels.get(auto_suggestion, auto_suggestion)
    return labels.get(status, status)


def date_label(value: str) -> str:
    day = parse_date(value)
    names = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    months = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
    return f"{names[day.weekday()]} {day.day} {months[day.month - 1]}"


def date_full_label(value: str) -> str:
    day = parse_date(value)
    names = ["Lunedi", "Martedi", "Mercoledi", "Giovedi", "Venerdi", "Sabato", "Domenica"]
    months = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    return f"{names[day.weekday()]} {day.day} {months[day.month - 1]} {day.year}"


def service_label(row: sqlite3.Row) -> str:
    if "service_type_name" in row.keys() and row["service_type_name"]:
        return row["service_type_name"]
    if "service_name_snapshot" in row.keys() and row["service_name_snapshot"]:
        return row["service_name_snapshot"]
    return "Non assegnata"


def active_service_types(doctor_id: int | None = None) -> list[sqlite3.Row]:
    conn = connect()
    if doctor_id:
        rows = conn.execute(
            "SELECT * FROM service_types WHERE active = 1 AND doctor_id = ? ORDER BY name",
            (doctor_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM service_types WHERE active = 1 ORDER BY name").fetchall()
    conn.close()
    return rows



def has_signed_consent(user: sqlite3.Row | None) -> bool:
    return bool(user and user["consent_signed_at"])


def payable_amount(status: str, price: float | int | None) -> float:
    return float(price or 0) if is_chargeable_status(status) else 0.0


def online_payable_amount(status: str, price: float | int | None) -> float:
    return float(price or 0) if is_online_payable_status(status) else 0.0

def full_payment_state(app_id: int, price: float | int | None, status: str) -> tuple[float, float, str]:
    conn = connect()
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE appointment_id = ?",
        (app_id,),
    ).fetchone()["total"]
    conn.close()
    due = online_payable_amount(status, price)
    residual = max(due - float(paid or 0), 0)
    if due <= 0:
        return float(paid or 0), 0.0, "Non dovuto"
    if residual <= 0:
        return float(paid or 0), 0.0, "Pagata"
    return float(paid or 0), residual, "Non pagata"


def status_filter_options() -> list[tuple[str, str]]:
    return [
        ("prenotata", "Prenotate"),
        ("effettuata", "Effettuate"),
        ("non_presentato", "Non presentato"),
        ("cancellata", "Cancellate"),
    ]


def selected_history_state(query: dict[str, list[str]] | None, default_status: str = "prenotata") -> tuple[str, str]:
    query = query or {}
    allowed = {value for value, _ in status_filter_options()} | {"valid"}
    selected_status = query.get("status", [default_status])[0]
    if selected_status not in allowed:
        selected_status = default_status
    selected_date = query.get("date", [""])[0].strip()
    if selected_date:
        selected_date = parse_date(selected_date).isoformat()
    return selected_status, selected_date


def service_description(row: sqlite3.Row | None) -> str:
    if not row or "description" not in row.keys():
        return ""
    return row["description"] or ""


def slot_times_for_part(part: str) -> list[str]:
    part = part if part in SLOT_PARTS else "morning"
    _, start_time, end_time = SLOT_PARTS[part]
    return [slot_time for slot_time in SLOT_TIMES if start_time <= slot_time < end_time]


def stripe_secret_source() -> str:
    if os.environ.get("STRIPE_SECRET_KEY", "").strip():
        return "env"
    if STRIPE_SECRET_KEY_PATH.exists() and STRIPE_SECRET_KEY_PATH.read_text(encoding="utf-8-sig").strip():
        return "file"
    return ""


def stripe_secret_key() -> str:
    env_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if STRIPE_SECRET_KEY_PATH.exists():
        return STRIPE_SECRET_KEY_PATH.read_text(encoding="utf-8-sig").strip()
    return ""


def save_stripe_secret_key(value: str) -> None:
    STRIPE_SECRET_KEY_PATH.write_text(value.strip(), encoding="utf-8")
    protect_secret_file(STRIPE_SECRET_KEY_PATH)


def stripe_key_preview() -> str:
    key = stripe_secret_key()
    if not key:
        return ""
    if len(key) <= 12:
        return "Chiave salvata"
    return f"{key[:7]}...{key[-4:]}"


def stripe_configured() -> bool:
    return stripe is not None and bool(stripe_secret_key())


def stripe_connect_destination_account() -> str:
    return get_setting("stripe_connect_destination_account", "").strip()


def doctor_stripe_connect_account(doctor_id: int | str | None) -> str:
    try:
        did = int(doctor_id or 0)
    except (TypeError, ValueError):
        return ""
    if not did:
        return ""
    conn = connect()
    row = conn.execute("SELECT doctor_stripe_account FROM users WHERE id = ? AND role IN ('admin', 'doctor')", (did,)).fetchone()
    conn.close()
    return (row["doctor_stripe_account"] if row and row_has(row, "doctor_stripe_account") else "").strip()


def stripe_webhook_source() -> str:
    if os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip():
        return "env"
    if STRIPE_WEBHOOK_SECRET_PATH.exists() and STRIPE_WEBHOOK_SECRET_PATH.read_text(encoding="utf-8-sig").strip():
        return "file"
    return ""


def stripe_webhook_secret() -> str:
    env_key = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if env_key:
        return env_key
    if STRIPE_WEBHOOK_SECRET_PATH.exists():
        return STRIPE_WEBHOOK_SECRET_PATH.read_text(encoding="utf-8-sig").strip()
    return ""


def save_stripe_webhook_secret(value: str) -> None:
    STRIPE_WEBHOOK_SECRET_PATH.write_text(value.strip(), encoding="utf-8")
    protect_secret_file(STRIPE_WEBHOOK_SECRET_PATH)


def stripe_webhook_preview() -> str:
    key = stripe_webhook_secret()
    if not key:
        return ""
    if len(key) <= 12:
        return "Secret salvato"
    return f"{key[:7]}...{key[-4:]}"


def stripe_source_label(source: str) -> str:
    return {"env": "Deploy", "file": "Pannello"}.get(source, "Non configurato")


def validate_stripe_secret_key(value: str) -> str:
    if stripe is None:
        raise ValueError("Stripe non disponibile")
    try:
        account = stripe.Account.retrieve(api_key=value)
    except Exception as exc:
        raise ValueError("Chiave Stripe non verificata") from exc
    return str(stripe_session_value(account, "id", ""))


def stripe_session_value(session: Any, key: str, default: Any = None) -> Any:
    if hasattr(session, "get"):
        return session.get(key, default)
    return getattr(session, key, default)


def record_stripe_checkout_payment(session: Any, expected_user_id: int | None = None) -> tuple[bool, str]:
    if stripe_session_value(session, "payment_status") != "paid":
        return False, "Pagamento non completato"
    metadata = stripe_session_value(session, "metadata", {}) or {}
    app_id = int(metadata.get("appointment_id", "0") or 0)
    meta_user_id = int(metadata.get("user_id", "0") or 0)
    if not app_id or not meta_user_id:
        return False, "Metadata Stripe incomplete"
    if expected_user_id is not None and meta_user_id != int(expected_user_id):
        return False, "Pagamento non associato al tuo profilo"
    session_id = stripe_session_value(session, "id", "") or ""
    if not session_id:
        return False, "Sessione Stripe non valida"
    amount_total = float(stripe_session_value(session, "amount_total", 0) or 0) / 100
    conn = connect()
    app = conn.execute(
        """
        SELECT a.id, a.price, a.status,
               COALESCE((SELECT SUM(amount) FROM payments WHERE appointment_id = a.id), 0) AS paid,
               COALESCE((SELECT COUNT(*) FROM payments WHERE stripe_session_id = ?), 0) AS already_saved
        FROM appointments a
        WHERE a.id = ? AND a.user_id = ?
        """,
        (session_id, app_id, meta_user_id),
    ).fetchone()
    if not app:
        conn.close()
        return False, "Seduta non trovata"
    if int(app["already_saved"] or 0) > 0:
        conn.close()
        return True, "Pagamento gia registrato"
    due = online_payable_amount(app["status"], app["price"])
    residual = max(due - float(app["paid"] or 0), 0)
    amount = min(residual, amount_total) if residual > 0 else amount_total
    if amount > 0:
        try:
            conn.execute(
                "INSERT INTO payments (appointment_id, paid_at, amount, method, stripe_session_id) VALUES (?, ?, ?, ?, ?)",
                (app_id, now().isoformat(), amount, "Stripe", session_id),
            )
            conn.commit()
        except db_integrity_error_types():
            conn.rollback()
            conn.close()
            return True, "Pagamento gia registrato"
    conn.close()
    return True, "Pagamento registrato"

def safe_filename(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_") or "utente"


def consent_filename(first_name: str, last_name: str) -> str:
    return f"{safe_filename(first_name)}_{safe_filename(last_name)}_consenso_informato.pdf"


def _pdf_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_names = ["arialbd.ttf", "segoeuib.ttf"] if bold else ["arial.ttf", "segoeui.ttf"]
    for name in font_names:
        font_path = Path("C:/Windows/Fonts") / name
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def _new_pdf_page(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    page = Image.new("RGB", (width, height), "#f7fbfd")
    draw = ImageDraw.Draw(page)
    draw.rounded_rectangle((46, 46, width - 46, height - 46), radius=34, fill="#ffffff", outline="#e4eef3", width=2)
    return page, draw


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_width: int,
    font: ImageFont.ImageFont,
    fill: str = "#243847",
    line_gap: int = 8,
) -> int:
    words = str(text or "").replace("\n", " \n ").split(" ")
    line = ""
    for word in words:
        if word == "\n":
            if line:
                draw.text((x, y), line, font=font, fill=fill)
                y += font.size + line_gap if hasattr(font, "size") else 22
                line = ""
            continue
        test = word if not line else f"{line} {word}"
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            line = test
        else:
            if line:
                draw.text((x, y), line, font=font, fill=fill)
                y += font.size + line_gap if hasattr(font, "size") else 22
            line = word
    if line:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap if hasattr(font, "size") else 22
    return y


def _decode_signature(signature_data: str) -> Image.Image | None:
    if not signature_data or "," not in signature_data:
        return None
    try:
        encoded = signature_data.split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")
    except Exception:
        return None


def generate_consent_pdf(user: sqlite3.Row, data: dict[str, str]) -> Path:
    consent_dir = consent_storage_dir()
    consent_dir.mkdir(parents=True, exist_ok=True)
    file_path = consent_dir / consent_filename(data["first_name"], data["last_name"])
    width, height = 1240, 1754
    margin = 96
    pages: list[Image.Image] = []
    page, draw = _new_pdf_page(width, height)
    y = 86

    title_font = _pdf_font(44, True)
    heading_font = _pdf_font(25, True)
    label_font = _pdf_font(17, True)
    body_font = _pdf_font(20)
    small_font = _pdf_font(16)
    accent = "#0b3a5c"
    teal = "#267b72"
    muted = "#647887"

    def ensure(space: int) -> None:
        nonlocal page, draw, y
        if y + space > height - margin:
            pages.append(page)
            page, draw = _new_pdf_page(width, height)
            y = margin

    def section(title: str) -> None:
        nonlocal y
        ensure(70)
        draw.text((margin, y), title.upper(), font=heading_font, fill=accent)
        y += 40
        draw.line((margin, y, width - margin, y), fill="#dbe8ee", width=2)
        y += 22

    def field(label: str, value: str, x: int, fy: int, w: int, h: int = 82) -> None:
        draw.rounded_rectangle((x, fy, x + w, fy + h), radius=16, fill="#f6fafc", outline="#dce8ee", width=2)
        draw.text((x + 18, fy + 12), label.upper(), font=small_font, fill=muted)
        _draw_wrapped(draw, value or "-", x + 18, fy + 38, w - 36, body_font, accent, 5)

    def two_fields(left: tuple[str, str], right: tuple[str, str]) -> None:
        nonlocal y
        ensure(104)
        gap = 22
        fw = (width - margin * 2 - gap) // 2
        field(left[0], left[1], margin, y, fw)
        field(right[0], right[1], margin + fw + gap, y, fw)
        y += 102

    consent_studio_label = studio_display_name("Studio")
    consent_practitioner_label = studio_practitioner_label()

    draw.text((margin, y), "CONSENSO INFORMATO", font=title_font, fill=accent)
    y += 54
    draw.text((margin, y), "AL TRATTAMENTO FISIOTERAPICO", font=heading_font, fill=teal)
    y += 42
    draw.text((margin, y), f"{consent_studio_label} - {consent_practitioner_label}", font=body_font, fill=muted)
    y += 58

    section("Dati del paziente")
    two_fields(("Cognome", data["last_name"]), ("Nome", data["first_name"]))
    two_fields(("Nato/a a", data.get("birth_place", "")), ("Data di nascita", data.get("birth_date", "")))
    two_fields(("Residente a", data.get("residence_city", "")), ("CAP", data.get("residence_cap", "")))
    two_fields(("Indirizzo", data.get("address", "")), ("Codice fiscale", data["fiscal_code"].upper()))
    two_fields(("Telefono", data["phone"]), ("Email", data["email"]))

    is_represented = data.get("minor_or_dependent") == "1"
    relation_labels = {
        "parent": "Genitore esercente la responsabilità genitoriale",
        "guardian": "Tutore legale",
        "support": "Amministratore di sostegno",
        "curator": "Curatore",
        "other": data.get("guardian_relation_other", "Altro ______________________________") or "Altro ______________________________",
    }

    section("Dati del rappresentante legale")
    y = _draw_wrapped(
        draw,
        "La presente sezione deve essere compilata esclusivamente qualora il consenso venga prestato da un soggetto diverso dal paziente.",
        margin,
        y,
        width - margin * 2,
        small_font,
        muted,
        6,
    ) + 12
    if is_represented:
        two_fields(("Cognome", data.get("guardian_last_name", "")), ("Nome", data.get("guardian_first_name", "")))
        two_fields(("Nato/a a", data.get("guardian_birth_place", "")), ("Data di nascita", data.get("guardian_birth_date", "")))
        two_fields(("Residente a", data.get("guardian_residence_city", "")), ("Codice fiscale", data.get("guardian_fiscal_code", "").upper()))
        two_fields(("Telefono", data.get("guardian_phone", "")), ("Email", data.get("guardian_email", "")))
    else:
        ensure(92)
        draw.rounded_rectangle((margin, y, width - margin, y + 72), radius=16, fill="#f6fafc", outline="#dce8ee", width=2)
        draw.text((margin + 18, y + 24), "Sezione non compilata: consenso prestato dal paziente maggiorenne.", font=body_font, fill=muted)
        y += 96

    ensure(210)
    draw.text((margin, y), "In qualit? di:", font=label_font, fill=accent)
    y += 34
    selected_relation = data.get("guardian_relation_type", "") if is_represented else ""
    relation_order = ["parent", "guardian", "support", "curator", "other"]
    for key in relation_order:
        mark = "[x]" if selected_relation == key else "[ ]"
        label = relation_labels[key]
        y = _draw_wrapped(draw, f"{mark} {label}", margin + 12, y, width - margin * 2 - 12, body_font, "#243847", 6)
    y = _draw_wrapped(draw, "del paziente sopra indicato.", margin + 12, y + 4, width - margin * 2 - 12, body_font, "#243847", 6) + 8
    if is_represented:
        y = _draw_wrapped(
            draw,
            "Il sottoscritto dichiara di agire in qualit? di rappresentante legale del paziente e di essere legittimato all'espressione del presente consenso.",
            margin,
            y,
            width - margin * 2,
            body_font,
            "#243847",
            8,
        ) + 18

    section("Dichiaro")
    declarations = [
        "di avere ricevuto informazioni chiare e comprensibili sul trattamento fisioterapico proposto;",
        "di essere stato informato sui benefici, limiti e possibili rischi del trattamento;",
        "di aver potuto porre domande e ricevere risposte soddisfacenti;",
        "di aver avuto il tempo necessario per valutare le informazioni ricevute;",
        "di essere consapevole della possibilit? di revocare il consenso in qualsiasi momento prima dell'esecuzione del trattamento;",
    ]
    for item in declarations:
        ensure(72)
        draw.text((margin, y + 3), "[x]", font=body_font, fill=teal)
        y = _draw_wrapped(draw, item, margin + 48, y, width - margin * 2 - 48, body_font, "#243847", 8) + 8

    section("Pertanto")
    y = _draw_wrapped(
        draw,
        "Dichiaro di prestare il consenso al trattamento fisioterapico del paziente sopra indicato.",
        margin,
        y,
        width - margin * 2,
        body_font,
        "#243847",
        8,
    ) + 24

    ensure(360)
    two_fields(("Luogo", "Terlizzi (BA)"), ("Data", now().strftime("%d/%m/%Y %H:%M")))
    draw.text((margin, y), "PAZIENTE MAGGIORENNE" if not is_represented else "DA COMPILARE ESCLUSIVAMENTE QUALORA IL CONSENSO VENGA PRESTATO DA UN SOGGETTO DIVERSO DAL PAZIENTE", font=label_font, fill=accent)
    y += 32
    draw.text((margin, y), "Firma del paziente" if not is_represented else "Firma del rappresentante legale", font=label_font, fill=accent)
    y += 34
    sig_box = (margin, y, width - margin, y + 190)
    draw.rounded_rectangle(sig_box, radius=20, fill="#ffffff", outline="#cfe0e8", width=2)
    sig = _decode_signature(data.get("signature_data", ""))
    if sig:
        sig.thumbnail((620, 150))
        sx = margin + 28
        sy = y + 20
        page.paste(sig, (sx, sy), sig)
    y += 212
    draw.text((margin, y), "Documento generato localmente dalla webapp sulla base del facsimile di consenso informato dello studio.", font=small_font, fill=muted)

    pages.append(page)
    pdf_pages = [p.convert("RGB") for p in pages]
    pdf_pages[0].save(file_path, "PDF", save_all=True, append_images=pdf_pages[1:], resolution=150.0)
    return file_path


def consent_form_modal(user: sqlite3.Row) -> str:
    keys = set(user.keys())

    def raw(field: str) -> str:
        return str(user[field] or "") if field in keys else ""

    def value(field: str) -> str:
        return html.escape(raw(field))

    guardian_parts = raw("guardian_name").split(" ", 1)
    guardian_first_value = raw("guardian_first_name") or (guardian_parts[0] if guardian_parts else "")
    guardian_last_value = raw("guardian_last_name") or (guardian_parts[1] if len(guardian_parts) > 1 else "")
    default_plan = "Valutazione fisioterapica, educazione al movimento, esercizio terapeutico e trattamento manuale se indicato dal quadro clinico."
    minor_checked = "checked" if ("minor_or_dependent" in keys and int(user["minor_or_dependent"] or 0)) else ""
    guardian_hidden = "" if minor_checked else "hidden"
    relation_current = value("guardian_relation_type")
    relation_options = [
        ("parent", "Genitore esercente la responsabilità genitoriale"),
        ("guardian", "Tutore legale"),
        ("support", "Amministratore di sostegno"),
        ("curator", "Curatore"),
        ("other", "Altro"),
    ]
    relation_buttons = "".join(
        f'<label class="relation-option"><input type="radio" name="guardian_relation_type" value="{key}" {"checked" if relation_current == key else ""} data-guardian-required><span><i aria-hidden="true"></i>{label}</span></label>'
        for key, label in relation_options
    )
    studio_label = html.escape(studio_display_name("Studio"))
    practitioner_label = html.escape(studio_practitioner_label())
    return f"""
    <section class="booking-modal consent-modal" data-consent-modal hidden aria-labelledby="consent-title">
        <button type="button" class="booking-backdrop" data-close-consent-modal aria-label="Chiudi"></button>
        <div class="booking-dialog consent-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-consent-modal aria-label="Chiudi">Chiudi</button>
            <p class="kicker">Documenti</p>
            <h3 id="consent-title">Consenso informato</h3>
            <div class="consent-copy compact-scroll">
                <p><strong>{studio_label} - {practitioner_label}</strong></p>
                
            </div>
            <form method="post" action="/consent/sign" class="consent-form" data-consent-form>
                <div class="form-grid consent-form-grid">
                    <div><label>Nome</label><input name="first_name" value="{value('first_name')}" required></div>
                    <div><label>Cognome</label><input name="last_name" value="{value('last_name')}" required></div>
                    <div><label>Email</label><input name="email" type="email" value="{value('email')}" required></div>
                    <div><label>Telefono</label><input name="phone" value="{value('phone')}" required></div>
                    <div><label>Codice fiscale</label><input name="fiscal_code" value="{value('fiscal_code')}" required></div>
                    <div><label>Nato/a a</label><input name="birth_place" value="{value('birth_place')}" required></div>
                    <div><label>Data di nascita</label><input name="birth_date" type="date" value="{value('birth_date')}" required></div>
                    <div><label>Residente a</label><input name="residence_city" value="{value('residence_city')}" required></div>
                    <div><label>CAP</label><input name="residence_cap" value="{value('residence_cap')}" required></div>
                    <div class="full-row"><label>Indirizzo</label><input name="address" value="{value('address')}" required></div>
                    <label class="consent-minor-toggle full-row"><input type="checkbox" name="minor_or_dependent" value="1" data-minor-toggle {minor_checked}><span>Paziente minore o a carico</span></label>
                </div>
                <div class="guardian-block" data-guardian-block {guardian_hidden}>
                    <h4>Rappresentante legale</h4>
                    <div class="form-grid consent-form-grid">
                        <div><label>Nome</label><input name="guardian_first_name" value="{html.escape(guardian_first_value)}" data-guardian-required></div>
                        <div><label>Cognome</label><input name="guardian_last_name" value="{html.escape(guardian_last_value)}" data-guardian-required></div>
                        <div><label>Nato/a a</label><input name="guardian_birth_place" value="{value('guardian_birth_place')}" data-guardian-required></div>
                        <div><label>Data di nascita</label><input name="guardian_birth_date" type="date" value="{value('guardian_birth_date')}" data-guardian-required></div>
                        <div><label>Residente a</label><input name="guardian_residence_city" value="{value('guardian_residence_city')}" data-guardian-required></div>
                        <div><label>Codice fiscale</label><input name="guardian_fiscal_code" value="{value('guardian_fiscal_code')}" data-guardian-required></div>
                        <div><label>Telefono</label><input name="guardian_phone" value="{value('guardian_phone')}" data-guardian-required></div>
                        <div><label>Email</label><input name="guardian_email" type="email" value="{value('guardian_email')}" data-guardian-required></div>
                    </div>
                    <div class="relation-grid" data-guardian-required-group>{relation_buttons}</div>
                    <div class="full-row"><label>Specificare se altro</label><input name="guardian_relation_other" value="{value('guardian_relation')}" placeholder="Indica il rapporto con il paziente"></div>
                </div>
                <div class="form-grid consent-form-grid">
                    <div class="full-row"><label>Trattamento proposto</label><textarea name="treatment_plan" maxlength="1600" rows="3" required>{html.escape(default_plan)}</textarea></div>
                </div>
                <div class="consent-checks">
                    <label class="consent-check"><input type="checkbox" name="consent_information" value="1" required><span>Ho ricevuto informazioni su finalita, modalita, benefici attesi, limiti, possibili disagi e alternative del trattamento.</span></label>
                    <label class="consent-check"><input type="checkbox" name="consent_treatment" value="1" required><span>Acconsento al trattamento fisioterapico proposto e posso chiedere chiarimenti in ogni momento.</span></label>
                    <label class="consent-check"><input type="checkbox" name="consent_data" value="1" required><span>Autorizzo il trattamento dei dati personali e sanitari per cura, appuntamenti, pagamenti e documentazione.</span></label>
                </div>
                <div class="signature-field signature-pad-field">
                    <label>Firma del paziente o del rappresentante legale</label>
                    <div class="signature-pad-wrap"><canvas data-signature-canvas width="640" height="220" aria-label="Firma digitale disegnata"></canvas></div>
                    <input type="hidden" name="signature_data" data-signature-data required>
                    <button type="button" class="secondary-button" data-clear-signature>Ripulisci firma</button>
                </div>
                <p class="missing-counter" data-missing-counter>0 campi mancanti</p>
                <button>Firma e salva consenso</button>
            </form>
        </div>
    </section>
    """

def load_email_config() -> dict[str, str]:
    config = {
        "host": "",
        "port": "587",
        "tls": "1",
        "from_email": "",
        "username": "",
        "password": "",
        "base_url": "",
    }
    if EMAIL_CONFIG_PATH.exists():
        try:
            data = json.loads(EMAIL_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in config:
                    if data.get(key) is not None:
                        config[key] = str(data.get(key, ""))
        except (OSError, json.JSONDecodeError):
            pass
    env_map = {
        "host": "SMTP_HOST",
        "port": "SMTP_PORT",
        "tls": "SMTP_TLS",
        "from_email": "SMTP_FROM",
        "username": "SMTP_USER",
        "password": "SMTP_PASSWORD",
        "base_url": "APP_BASE_URL",
    }
    for key, env_name in env_map.items():
        env_value = os.environ.get(env_name)
        if env_value:
            config[key] = env_value
    return config


def save_email_config(config: dict[str, str]) -> None:
    EMAIL_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def smtp_configured() -> bool:
    config = load_email_config()
    return bool(config.get("host") and config.get("from_email"))


def write_email_outbox(to_email: str, subject: str, body: str, note: str = "") -> Path:
    EMAIL_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"reset_{safe_filename(to_email)}_{int(now().timestamp())}.txt"
    path = EMAIL_OUTBOX_DIR / filename
    prefix = f"Note: {note}\n" if note else ""
    path.write_text(f"{prefix}To: {to_email}\nSubject: {subject}\n\n{body}", encoding="utf-8")
    return path


def send_email(to_email: str, subject: str, body: str, allow_outbox: bool = True) -> Path | None:
    config = load_email_config()
    if config.get("host") and config.get("from_email"):
        message = EmailMessage()
        message["From"] = config["from_email"]
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)
        try:
            port = int(config.get("port") or "587")
            with smtplib.SMTP(config["host"], port, timeout=20) as server:
                if config.get("tls", "1") != "0":
                    server.starttls()
                if config.get("username"):
                    server.login(config["username"], config.get("password", ""))
                server.send_message(message)
            return None
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            if not allow_outbox:
                raise ValueError(f"Invio email non riuscito: {exc}") from exc
            return write_email_outbox(to_email, subject, body, f"Errore invio SMTP: {exc}")
    if not allow_outbox:
        raise ValueError("Configura prima host SMTP e mittente email.")
    return write_email_outbox(to_email, subject, body, "SMTP non configurato")

def reset_email_body(first_name: str, reset_link: str) -> str:
    brand_name = studio_display_name(APP_NAME)
    return f"""Ciao {first_name},

abbiamo ricevuto una richiesta di reimpostazione password per il tuo account {brand_name}.
Clicca questo link per reimpostare la password entro 30 minuti:

{reset_link}

Se non hai richiesto tu il reset, ignora questa email: la password attuale restera invariata.
"""

NAV_ICON_SVGS = {
    "home": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10.5V20h13v-9.5"/><path d="M9.5 20v-6h5v6"/></svg>',
    "calendar": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3v4"/><path d="M17 3v4"/><path d="M4.5 9h15"/><path d="M5 5.5h14a1 1 0 0 1 1 1V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1z"/></svg>',
    "slots": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h5v5H5z"/><path d="M14 5h5v5h-5z"/><path d="M5 14h5v5H5z"/><path d="M14 14h5v5h-5z"/></svg>',
    "patients": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M3.8 19a5.2 5.2 0 0 1 10.4 0"/><path d="M17 11a2.5 2.5 0 1 0 0-5"/><path d="M16 14.5a4.5 4.5 0 0 1 4.2 4.5"/></svg>',
    "specialists": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 5 5"/><path d="M8.5 10.5h4"/><path d="M10.5 8.5v4"/></svg>',
    "qr": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h6v6H4z"/><path d="M14 4h6v6h-6z"/><path d="M4 14h6v6H4z"/><path d="M14 14h2"/><path d="M20 14v2"/><path d="M16 18h4"/><path d="M14 20h2"/></svg>',
    "services": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/><path d="M7 7h10v10H7z"/></svg>',
    "profile": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>',
    "documents": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3.5h7l4 4V20a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z"/><path d="M14 3.5V8h4"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>',
    "trash": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6.5 7l.8 13h9.4l.8-13"/><path d="M9 7V4.8h6V7"/></svg>',
}


def nav_icon(name: str) -> str:
    return NAV_ICON_SVGS.get(name, NAV_ICON_SVGS["home"])


def page(title: str, body: str, user: sqlite3.Row | None = None, flash: str = "") -> bytes:
    nav = ""
    body_class = "public"
    brand_logo = studio_logo_url()
    brand_name = studio_display_name(APP_NAME)
    if user:
        body_class = f"signed-in role-{user['role']}"
        if is_staff_account(user):
            links = [("/", "Home", "home")]
            if is_doctor_account(user):
                links.extend([
                    ("/book", "Prenotazioni", "calendar"),
                    ("/slots", "Slot", "slots"),
                ])
            links.extend([
                ("/patients", "Pazienti", "patients"),
                ("/scan", "Presenze", "qr"),
            ])
            if is_doctor_account(user):
                links.append(("/services", "Servizi", "services"))
            links.append(("/profile", "Impostazioni", "profile"))
        else:
            links = [
                ("/", "Home", "home"),
                ("/specialists", "Specialisti", "specialists"),
                ("/book", "Prenota", "calendar"),
                ("/profile", "Profilo", "profile"),
            ]
        nav_links = "".join(
            f'<a href="{href}" class="app-nav-link"><span class="nav-icon" aria-hidden="true">{nav_icon(icon)}</span><span>{label}</span></a>'
            for href, label, icon in links
        )
        nav = f"""
        <nav class="app-nav" aria-label="Navigazione principale">
            <strong class="brand-lockup"><img class="brand-logo" src="{html.escape(brand_logo, quote=True)}" alt="{html.escape(brand_name)}" width="54" height="54"><span class="brand-name">{html.escape(brand_name)}</span></strong>
            <div class="app-nav-links">{nav_links}</div>

        </nav>
        """
    proposal_style = '<link rel="stylesheet" href="/static/theme-light-proposal.css">' if os.environ.get("UI_PROPOSAL_THEME", "").strip().lower() == "light" else ""
    style = f'<link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/design-system.css"><link rel="stylesheet" href="/static/ui-2026.css">{proposal_style}'
    app_meta = f'<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#f3f5f2"><meta name="description" content="Web app per prenotazioni, pagamenti, documenti e gestione sedute fisioterapiche."><meta name="robots" content="noindex,nofollow"><meta name="application-name" content="{html.escape(brand_name)}"><meta name="generator" content="{APP_NAME} {APP_VERSION}"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="default"><meta name="apple-mobile-web-app-title" content="{html.escape(brand_name)}"><link rel="apple-touch-icon" href="/static/app-icon-192.png">'
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    flash_script = ""
    return f"""
    <!doctype html>
    <html lang="it">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
        <title>{html.escape(title)} - {html.escape(brand_name)}</title>
        {app_meta}
        {style}
    </head>
    <body class="{body_class}">
        <a class="skip-link" href="#main-content">Salta al contenuto</a>
        {nav}
        <main id="main-content" tabindex="-1">{flash_html}{body}</main>{flash_script}
        <aside class="install-app-banner system-notice" data-install-app-banner hidden>
            <span class="system-notice__icon" aria-hidden="true"></span>
            <span class="system-notice__text">Installa app</span>
            <button type="button" class="system-notice__action" data-install-app>Installa</button>
            <button type="button" class="banner-close" data-install-dismiss aria-label="Chiudi">x</button>
        </aside>
        <script src="/static/date-picker.js" defer></script>
        <script src="/static/rehab-editor-content.js" defer></script>
        <script src="/static/gdpr-consent.js" defer></script>
        <script src="/static/consent-form.js" defer></script>
        <script src="/static/diary-modal.js" defer></script>
        <script src="/static/service-description.js" defer></script>
        <script src="/static/pwa.js" defer></script>
        <script src="/static/notifications.js" defer></script>
        <script src="/static/login-guard.js" defer></script>
        <script src="/static/form-validation.js" defer></script>
        <script src="/static/doctor-profile.js" defer></script>
        <script src="/static/ui-polish.js" defer></script>
    </body>
    </html>
    """.encode("utf-8")


def setup_page(flash: str = "") -> bytes:
    body = f"""
    <section class="setup-shell">
        <div class="setup-brand">
            <img src="{STUDIO_PLACEHOLDER_LOGO}" alt="">
            <div>
                <p class="kicker">Primo avvio</p>
                <h1>Configura lo studio</h1>
            </div>
        </div>
        <form method="post" action="/setup" class="setup-wizard">
            <section class="setup-step-card">
                <span class="setup-step-index">1</span>
                <div class="setup-step-content">
                    <h2>Studio</h2>
                    <div class="form-grid compact-form-grid">
                        <div><label>Nome studio</label><input name="studio_name" placeholder="Nome studio" required></div>
                        <div><label>Email studio</label><input name="studio_email" type="email" placeholder="studio@email.it"></div>
                        <div><label>Telefono studio</label><input name="studio_phone" placeholder="+39 000 000 0000"></div>
                        <div><label>Partita IVA / CF</label><input name="studio_tax_id" placeholder="Partita IVA o codice fiscale"></div>
                        <div class="full-row"><label>Indirizzo</label><input name="studio_address" placeholder="Indirizzo completo dello studio"></div>
                        <div class="full-row doctor-photo-uploader studio-logo-uploader" data-doctor-photo-uploader>
                            <label>Logo studio</label>
                            <div class="studio-logo-upload-card">
                                <div class="doctor-settings-photo-row setup-logo-row">
                                    <div class="doctor-photo-preview has-image doctor-photo-preview-large studio-logo-preview" data-doctor-photo-preview style="--preview-image:url('{STUDIO_PLACEHOLDER_LOGO}'); background-image:url('{STUDIO_PLACEHOLDER_LOGO}')" aria-label="Anteprima logo studio"></div>
                                    <div class="studio-logo-copy">
                                        <strong>Anteprima logo</strong>
                                        <span>PNG, JPG o WEBP. Anteprima immediata.</span>
                                        <small data-doctor-photo-file-name>Nessun logo caricato</small>
                                    </div>
                                </div>
                                <div class="logo-upload-actions">
                                    <label class="button secondary compact-button logo-upload-button" for="studio-logo-file">Carica logo</label>
                                    <button type="button" class="button secondary compact-button logo-upload-clear" data-doctor-photo-clear hidden>Rimuovi</button>
                                </div>
                                <input id="studio-logo-file" class="logo-file-input" type="file" accept="image/png,image/jpeg,image/webp" data-doctor-photo-file>
                                <input type="hidden" name="studio_logo_data" data-doctor-photo-data>
                            </div>
                        </div>
                    </div>
                </div>
            </section>
            <section class="setup-step-card">
                <span class="setup-step-index">2</span>
                <div class="setup-step-content">
                    <h2>Profilo proprietario</h2>
                    <div class="form-grid compact-form-grid">
                        <div><label>Nome</label><input name="first_name" placeholder="Nome" required></div>
                        <div><label>Cognome</label><input name="last_name" placeholder="Cognome" required></div>
                        <div><label>Email accesso</label><input name="email" type="email" placeholder="email@studio.it" required></div>
                        <div><label>Password</label><input name="password" type="password" minlength="8" placeholder="Password" required></div>
                        <div><label>Telefono</label><input name="phone" placeholder="+39 000 000 0000" required></div>
                        <div><label>Codice fiscale</label><input name="fiscal_code" placeholder="Codice fiscale" required></div>
                        <label class="checkbox-inline full-row setup-check">
                            <input type="checkbox" name="owner_is_doctor" value="1" checked>
                            <span>Sono anche un medico prenotabile</span>
                        </label>
                        <div><label>Qualifica</label><input name="doctor_qualification" value="Fisioterapista" placeholder="Qualifica"></div>
                        <div><label>Anni esperienza</label><input name="doctor_years_experience" type="number" min="0" max="70" value="0"></div>
                        <div class="full-row"><label>Titoli di studio</label><input name="doctor_degree" placeholder="Titoli di studio"></div>
                        <div class="full-row"><label>Bio</label><textarea name="doctor_bio" rows="4" maxlength="1200" placeholder="Bio professionale"></textarea></div>
                    </div>
                </div>
            </section>
            <button class="setup-submit">Crea studio e accedi</button>
        </form>
    </section>
    """
    return page("Configurazione iniziale", body, None, flash)

def login_page(flash: str = "") -> bytes:
    privacy_text = """
    <h4>INFORMATIVA SUL TRATTAMENTO DEI DATI PERSONALI</h4>
    <p>Ai sensi dell&#x27;art. 13 del Regolamento UE 2016/679 del 27 aprile 2016 Regolamento Generale sulla protezione dei dati personali</p>
    <p>La presente informativa fornisce una descrizione sintetica delle modalità e finalità del trattamento dei Suoi dati personali da parte del Titolare del trattamento (di seguito anche "Titolare"), nonché ogni ulteriore informazione richiesta ai sensi della normativa vigente in materia di protezione dei dati personali, Il trattamento avverrà nel rispetto del Regolamento Ue 2016/679. Regolamento generale sulla protezione dei dati (di seguito il "Regolamento"). Il Regolamento prevede che per "Dato personale" debba intendersi qualsiasi informazione riguardante una persona fisica identificata o identificabile (di seguito "Interessato"). Per "Trattamento" si intende qualsiasi operazione o insieme di operazioni, compiute con o senza ausilio di processi automatizzati e applicati a dati personali o insiemi di dati personali, come la raccolta, la registrazione. La strutturazione, la conservazione, l'adattamento o la modifica, l'estrazione, la consultazione, l'uso, la comunicazione mediante trasmissione, diffusione o qualsiasi altra forma di messa a disposizione, il raffronto o l'interconnessione, la limitazione, la cancellazione o la distruzione.</p>
    <p>Ai sensi dell&#x27;art. 13 del Regolamento, Le forniamo le seguenti informazioni sul trattamento dei Suoi Dati personali.</p>
    <h4>TITOLARE DEL TRATTAMENTO</h4>
    <p>Il Titolare del trattamento è Giuseppe Dellorusso con sede legale in Terlizzi (BA), Prima Traversa Via Giovinazzo 2.</p>
    <h4>C.F. DLLGPP63L29L109X</h4>
    <h4>NATURA, MODALITÀ DEL CONFERIMENTO DEI DATI PERSONALI E BASE GIURIDICA DEL TRATTAMENTO</h4>
    <p>I Dati personali, raccolti direttamente presso l&#x27;interessato, possono essere comuni (quali ad esempio nome, cognome, indirizzo postale, e posta elettronica, ecc.) ed anche Dati personali di natura particolare (come ad esempio lo stato di salute).</p>
    <p>Il conferimento di tali dati è obbligatorio per il conseguimento delle finalità sotto elencate; pertanto il loro mancato, parziale o inesatto conferimento potrebbe avere come conseguenza l&#x27;oggettiva impossibilità per il Titolare di instaurare o di condurre regolarmente il rapporto contrattuale.</p>
    <p>Il trattamento dei Dati personali comuni può avvenire senza necessità del consenso, secondo quanto disposto dall&#x27;art. 6 del Regolamento e trova la sua base giuridica nell&#x27;adempimento di un obbligo legale e nell&#x27;adempimento di obblighi contrattuali.</p>
    <p>Il trattamento di eventuali Dati personali di natura particolare trova la sua base giuridica nel consenso da Lei rilasciato in calce al presente documento.</p>
    <h4>FINALITÀ DEL TRATTAMENTO</h4>
    <p>Il trattamento dei Suoi dati personali è finalizzato:</p>
    <p>alla corretta e completa esecuzione dell&#x27;incarico professionale connesso con le attività di osteopatia da Lei richieste;</p>
    <p>all&#x27;adempimento degli obblighi previsti da leggi, regolamenti o dalla normativa comunitaria, con particolare riferimento alla normativa civilistica, fiscale e contabile, nonché per dare attuazione a disposizioni impartite da autorità a ciò legittimate dalla legge o da organi di vigilanza e controllo;</p>
    <p>all&#x27;adempimento degli obblighi ed esercizio dei diritti derivanti dal contratto (compreso il pagamento del compenso, lo scambio di informazioni, l&#x27;attività di amministrazione e la gestione di ordini, spedizioni, fatturazione, eventuale contenzioso).</p>
    <h4>MODALITÀ DEL TRATTAMENTO</h4>
    <p>Il trattamento dei dati personali è eseguito con l&#x27;ausilio di strumenti cartacei e/o informatici, con logiche di organizzazione ed elaborazione strettamente correlate alle finalità stesse e comunque in modo da garantire la sicurezza, l&#x27;integrità e la riservatezza nel rispetto delle misure organizzative, fisiche e logiche previste dalle disposizioni vigenti.</p>
    <p>Il Titolare, in particolare, ha adottato idonee misure di sicurezza per proteggere i dati contro il rischio di perdita, abuso o alterazione dei dati personali. In particolare, ha adottato, dove possibile, le misure di cui all'art.32 del Regolamento.</p>
    <h4>PERIODO DI CONSERVAZIONE DEI DATI</h4>
    <p>I dati personali comunicati sono conservati per il tempo necessario ad adempiere alle finalità o per qualsiasi altra legittima finalità collegata con l&#x27;erogazione del servizio richiesto.</p>
    <p>Più precisamente saranno conservati per tutta la durata del contratto e, dopo la cessazione, per un massimo di 10 anni, in adempimento alle disposizioni di legge.</p>
    <h4>COMUNICAZIONE DEI DATI PERSONALI</h4>
    <p>I Suoi Dati Personali possono essere portati a conoscenza di dipendenti e collaboratori del Titolare e dei Responsabili, che operando sotto la diretta autorità del Titolare, trattano dati e sono nominati responsabili o incaricati del trattamento ai sensi dell'art.24-29 del Regolamento o Amministratori di sistema e che riceveranno al riguardo adeguate istruzioni operative dal Titolare. Lo stesso avverrà - a cura dei Responsabili nominati dal Titolare - nei confronti dei dipendenti o collaboratori dei Responsabili. Tali soggetti sono essenzialmente compresi nelle seguenti categorie: fornitori di software e relativa assistenza/manutenzione; consulenti contabili e fiscali; consulenti per la sicurezza sul lavoro; istituti di credito; consulenti. Inoltre i dati possono essere comunicati a soggetti terzi che agiscono quali autonomi titolari del trattamento quali, ad esempio: L'Agenzia delle Entrate o altre pubbliche Amministrazioni.</p>
    <h4>LUOGO DEL TRATTAMENTO DEI DATI E DATI DI CONTATTO DEL TITOLARE DEL TRATTAMENTO</h4>
    <p>I Dati Personali sono trattati principalmente presso la sede del Titolare, Prima Traversa Via Giovinazzo 2, 70038 - Terlizzi (BA), e/o nei luoghi in cui si trovano i Responsabili.</p>
    <p>Per ulteriori informazioni, gli utenti possono contattare il Titolare scrivendo all&#x27;indirizzo e-mail sotto indicato.</p>
    <h4>DIRITTI DELL&#x27;INTERESSATO</h4>
    <p>In qualunque momento, l&#x27;interessato può esercitare i diritti di cui agli artt. da 15 a 22 del Regolamento, tra cui, in sintesi, ottenere conferma dell&#x27;esistenza o meno di un trattamento di dati personali che lo riguardano e, in tal caso, ottenere l'accesso ai dati e conoscerne l' origine, le finalità e le modalità del trattamento, i destinatari o le categorie di destinatari a cui i dati personali possono essere comunicati e il periodo di conservazione; esercitare il diritto ad ottenere dal Titolare del trattamento, la rettificazione, l'aggiornamento o l'integrazione dei dati: il diritto alla cancellazione o alla trasformazione in forma anonima dei dati o la limitazione sul trattamento dei dati personali che lo riguardano; il diritto ad essere informato delle eventuali rettifiche  cancellazioni o limitazioni del trattamento effettuate in relazione ai Suoi dati personali; il diritto di opporsi in qualsiasi momento al trattamento dei dati; il diritto a ricevere in un formato strutturato, di uso comune e leggibile da dispositivo automatico i dati personali.</p>
    <p>La normativa applicabile riconosce all'interessato il diritto a proporre reclamo al Garante per la protezione dei dati personali o comunque a un'autorità di controllo competente, ove ne ricorrano i presupposti. Per esercitare i Suoi diritti, l'interessato potrà rivolgersi al Titolare del trattamento ai seguenti recapiti:</p>
    <p>E-mail: giuseppedellorusso63@gmail.com</p>
    <p>Indirizzo: Prima traversa Via Giovinazzo 2 - 70038, Terlizzi (BA).</p>
    <p>Dichiaro di aver preso visione della suddetta informativa e di acconsentire specificamente al trattamento dei dati personali relativi al mio stato di salute.</p>
    """
    login_brand = html.escape(studio_display_name("Gestionale studio"))
    body = f"""
    <div class="auth-shell auth-shell-minimal">
        <section class="auth card">
            <p class="kicker">{login_brand}</p>
            <h1>Accesso</h1>
            <form method="post" action="/login" data-login-form data-no-loading="true">
                <label>Email</label><input name="email" type="email" autocomplete="email" required>
                <label>Password</label><input name="password" type="password" autocomplete="current-password" required>
                <label class="checkbox-inline remember-login"><input type="checkbox" name="remember" value="1"> Resta connesso</label>
                <input type="hidden" name="login_confirmed" value="0" data-login-confirmed>
                <p class="muted login-guard-message" data-login-guard-message hidden>Premi Entra per accedere.</p>
                <button>Entra</button>
                <a class="auth-link" href="/forgot-password">Ho dimenticato la password</a>
            </form>
            <hr>
            <h2>Registrazione</h2>
            <div class="role-choice" data-role-choice>
                <button type="button" class="role-choice-card active" data-register-role="patient">Registrati come paziente</button>
                <button type="button" class="role-choice-card" data-register-role="doctor">Registrati come medico</button>
            </div>
            <form method="post" action="/register" class="form-grid" data-registration-form data-doctor-register-form>
                <input type="hidden" name="account_type" value="patient" data-account-type>
                <div><label>Nome</label><input name="first_name" autocomplete="given-name" placeholder="Nome" required></div>
                <div><label>Cognome</label><input name="last_name" autocomplete="family-name" placeholder="Cognome" required></div>
                <div class="full-row"><label>Email</label><input name="email" type="email" autocomplete="email" placeholder="email@esempio.it" required></div>
                <div class="full-row"><label>Password</label><input name="password" type="password" minlength="8" autocomplete="new-password" placeholder="Password" required></div>
                <div><label>Cellulare</label><input name="phone" autocomplete="tel" placeholder="+39 000 000 0000" required></div>
                <div><label>Codice fiscale</label><input name="fiscal_code" placeholder="Codice fiscale" required></div>
                <section class="doctor-registration-fields full-row" data-doctor-registration-fields hidden>
                    <div class="form-grid">
                        <div><label>Qualifica</label><input name="doctor_qualification" placeholder="Es. Fisioterapista"></div>
                        <div><label>Anni di esperienza</label><input name="doctor_years_experience" type="number" min="0" max="70" value="0"></div>
                        <div><label>Titoli di studio</label><input name="doctor_degree" placeholder="Es. Laurea in Fisioterapia"></div>
                        <div>
                            <label>Genere</label>
                            <select name="doctor_gender">
                                <option value="">Non specificato</option>
                                <option value="m">Uomo</option>
                                <option value="f">Donna</option>
                            </select>
                        </div>
                        <div class="full-row"><label>Sede / studio</label><input name="doctor_location" placeholder="Es. Nome studio, città"></div>
                        <div class="full-row"><label>Bio professionale</label><textarea name="doctor_bio" rows="5" maxlength="1200" placeholder="Descrivi approccio, specializzazioni e tipo di percorso"></textarea></div>
                        <div class="full-row doctor-photo-uploader" data-doctor-photo-uploader>
                            <label>Immagine profilo</label>
                            <input type="file" accept="image/png,image/jpeg,image/webp" data-doctor-photo-file>
                            <input type="hidden" name="doctor_profile_image_data" data-doctor-photo-data>
                            <div class="doctor-photo-preview" data-doctor-photo-preview></div>
                        </div>
                    </div>
                </section>
                <div class="full-row gdpr-consent-row">
                    <button type="button" class="button secondary" data-open-gdpr>Leggi e accetta il GDPR sulla privacy</button>
                    <input type="hidden" name="gdpr" value="0" data-gdpr-value>
                    <p class="muted" data-gdpr-status>Consenso non ancora accettato</p>
                </div>
                <div class="full-row"><button>Registrati</button></div>
            </form>
        </section>
    </div>
    <section class="booking-modal gdpr-modal" data-gdpr-modal hidden aria-labelledby="gdpr-title">
        <button type="button" class="booking-backdrop" data-close-gdpr aria-label="Chiudi"></button>
        <div class="booking-dialog gdpr-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-gdpr aria-label="Chiudi">Chiudi</button>
            <p class="kicker">Privacy</p>
            <h3 id="gdpr-title">GDPR sulla privacy</h3>
            <div class="gdpr-scroll" data-gdpr-scroll>{privacy_text}</div>
            <label class="checkbox-inline gdpr-accept" data-gdpr-accept hidden>
                <input type="checkbox" data-gdpr-checkbox> Ho letto e accetto
            </label>
            <div class="gdpr-actions">
                <button type="button" class="secondary" data-gdpr-scroll-bottom>Vai in fondo</button>
                <button type="button" data-gdpr-final-confirm hidden disabled>Conferma accettazione</button>
            </div>
        </div>
    </section>
    """
    return page("Accesso", body, None, flash)


def forgot_password_page(flash: str = "") -> bytes:
    login_brand = html.escape(studio_display_name("Gestionale studio"))
    body = f"""
    <div class="auth-shell auth-shell-minimal">
        <section class="auth card">
            <p class="kicker">{login_brand}</p>
            <h1>Reimposta password</h1>
            
            <form method="post" action="/forgot-password">
                <label>Email</label><input name="email" type="email" autocomplete="email" required>
                <button>Invia link</button>
                <a class="auth-link" href="/login">Torna al login</a>
            </form>
        </section>
    </div>
    """
    return page("Password dimenticata", body, None, flash)


def reset_password_page(token: str, flash: str = "") -> bytes:
    login_brand = html.escape(studio_display_name("Gestionale studio"))
    body = f"""
    <div class="auth-shell auth-shell-minimal">
        <section class="auth card">
            <p class="kicker">{login_brand}</p>
            <h1>Nuova password</h1>
            <form method="post" action="/reset-password">
                <input type="hidden" name="token" value="{html.escape(token)}">
                <label>Nuova password</label><input name="password" type="password" minlength="8" autocomplete="new-password" required>
                <label>Conferma nuova password</label><input name="confirm_password" type="password" minlength="8" autocomplete="new-password" required>
                <button>Salva nuova password</button>
            </form>
        </section>
    </div>
    """
    return page("Nuova password", body, None, flash)

def home_page(user: sqlite3.Row, flash: str = "", base_url: str = "") -> bytes:
    run_noncritical("home:auto_suggestions", update_auto_suggestions)
    conn = connect()
    if is_staff_account(user):
        body = admin_dashboard(user)
        conn.close()
        return page("Dashboard", body, user, flash)

    current = now()
    today_iso = today().isoformat()
    signed = has_signed_consent(user)
    future_appointments = conn.execute(
        """
        SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        WHERE a.user_id = ?
          AND a.status = 'prenotata'
          AND (s.slot_date > ? OR (s.slot_date = ? AND s.slot_time >= ?))
        ORDER BY s.slot_date, s.slot_time
        LIMIT 8
        """,
        (user["id"], today_iso, today_iso, current.strftime("%H:%M")),
    ).fetchall()
    unpaid_rows = conn.execute(
        """
        SELECT a.id, a.price, s.slot_date, s.slot_time, st.name AS service_type_name,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        LEFT JOIN payments p ON p.appointment_id = a.id
        WHERE a.user_id = ? AND a.status = 'effettuata'
        GROUP BY a.id
        HAVING (a.price - COALESCE(SUM(p.amount), 0)) > 0.001
        ORDER BY s.slot_date DESC, s.slot_time DESC
        LIMIT 4
        """,
        (user["id"],),
    ).fetchall()
    conn.close()

    total_residual = sum(max(float(row["price"] or 0) - float(row["paid"] or 0), 0) for row in unpaid_rows)
    upcoming = future_appointments[0] if future_appointments else None

    consent_notice = ""
    if not signed:
        consent_notice = """
        <section class="card consent-alert cockpit-alert">
            <div>
                <p class="kicker">Azione richiesta</p>
                <h2>Firma il consenso informato</h2>
            </div>
            <a class="button" href="/profile#documents">Firma consenso</a>
        </section>
        """

    session_cards: list[str] = []
    active_qr_anchor = ""
    for index, app in enumerate(future_appointments):
        app_time = parse_dt(app["slot_date"], app["slot_time"])
        is_next = index == 0
        qr_active = app_time - dt.timedelta(hours=1) <= current <= app_time + dt.timedelta(hours=2)
        can_cancel = current <= app_time - dt.timedelta(hours=CANCEL_LIMIT_HOURS)
        if qr_active and not active_qr_anchor:
            active_qr_anchor = f"qr-{app['id']}"
        token = make_presence_token(app["id"])
        qr_url = f"{base_url}/presence?token={quote(token)}" if base_url else f"/presence?token={quote(token)}"
        qr_panel = f"""
            <div class="timeline-qr" id="qr-{app['id']}">
                <div class="qr-card">{qr_svg(qr_url)}<p>Mostra questo QR al fisioterapista.</p></div>
            </div>
            """ if qr_active else ""
        cancel_action = f"""
            <form method="post" action="/cancel">
                <input type="hidden" name="id" value="{app['id']}">
                <button class="danger compact-button">Annulla</button>
            </form>
            """ if can_cancel else '<span class="muted">Annullamento non disponibile nell\'ultima ora.</span>'
        session_cards.append(
            f"""
            <details class="timeline-card {'is-next' if is_next else ''}" {'open' if is_next else ''}>
                <summary>
                    <span class="timeline-dot" aria-hidden="true"></span>
                    <span class="timeline-main"><strong>{date_full_label(app['slot_date'])}</strong><small>{html.escape(service_label(app))}</small></span>
                    <span class="timeline-time">{app['slot_time']}</span>
                </summary>
                <div class="timeline-card-body">
                    <div class="split">
                        <span class="pill {'ok' if is_next else ''}">{'Prossima' if is_next else 'Prenotata'}</span>
                        <span class="pill">#{app['id']}</span>
                    </div>
                    {qr_panel}
                    <div class="cockpit-actions">
                        <a class="button secondary compact-button" href="/book?move={app['id']}">Sposta</a>
                        <a class="button compact-button" href="/ics?id={app['id']}">Calendario</a>
                        {cancel_action}
                    </div>
                </div>
            </details>
            """
        )

    unpaid_items = "".join(
        f"""
        <a class="debt-mini-card payment-jump-link" href="/profile?open=payments#payments">
            <span>{row['slot_date']} {row['slot_time']}</span>
            <strong>{money(max(float(row['price'] or 0) - float(row['paid'] or 0), 0))}</strong>
            <small>{html.escape(service_label(row))}</small>
        </a>
        """
        for row in unpaid_rows
    )

    if upcoming:
        appt_time = parse_dt(upcoming["slot_date"], upcoming["slot_time"])
        delta = appt_time - current
        hours = max(int(delta.total_seconds() // 3600), 0)
        minutes = max(int((delta.total_seconds() % 3600) // 60), 0)
        date_parts = date_label(upcoming["slot_date"]).split(" ", 1)
        can_cancel = current <= appt_time - dt.timedelta(hours=CANCEL_LIMIT_HOURS)
        qr_active_next = appt_time - dt.timedelta(hours=1) <= current <= appt_time + dt.timedelta(hours=2)
        home_state = "QR presenza pronto" if qr_active_next else ("Seduta oggi" if upcoming["slot_date"] == today_iso else "Prossima seduta")
        primary_cta = (
            f'<a class="button pulse-action" href="#{active_qr_anchor or "qr-presenza"}">Mostra QR presenza</a>'
            if qr_active_next else '<a class="button" href="/book">Prenota nuova seduta</a>'
        )
        if not signed:
            primary_cta = '<a class="button pulse-action" href="/profile#documents">Firma consenso</a>'
        cancel_button = f"""
            <form method="post" action="/cancel">
                <input type="hidden" name="id" value="{upcoming['id']}">
                <button class="danger compact-button">Annulla</button>
            </form>
            """ if can_cancel else ""
        pay_cta = '<a class="button secondary payment-jump-link" href="/profile?open=payments#payments">Paga seduta</a>' if total_residual > 0 else ""
        body = f"""
        {consent_notice}
        <section class="app-home-hero cockpit-hero user-cockpit-hero">
            <div class="app-home-main">
                <p class="kicker">{home_state}</p>
                <h1>{date_parts[0]} {date_parts[1]}</h1>
                <div class="home-session-line">
                    <span>{upcoming['slot_time']}</span>
                    <span>{html.escape(service_label(upcoming))}</span>
                </div>
                <div class="home-primary-actions">
                    {primary_cta}
                    {f'<a class="button secondary compact-button" href="/book?move={upcoming["id"]}">Sposta</a>' if can_cancel else ''}
                    {cancel_button}
                    <a class="button secondary compact-button" href="/ics?id={upcoming['id']}">Calendario</a>
                    {pay_cta}
                </div>
            </div>
            <aside class="live-countdown-card interactive-countdown">
                <span>Countdown</span>
                <strong data-countdown-target="{appt_time.isoformat()}">{hours}h {minutes}m</strong>
                <small>{status_label(upcoming['status'])}</small>
            </aside>
        </section>
        """
        sticky_cta = primary_cta
    else:
        primary_cta = '<a class="button" href="/book">Prenota ora</a>' if signed else '<a class="button pulse-action" href="/profile#documents">Firma consenso</a>'
        pay_cta = '<a class="button secondary payment-jump-link" href="/profile?open=payments#payments">Paga seduta</a>' if total_residual > 0 else ""
        body = f"""
        {consent_notice}
        <section class="app-home-hero cockpit-hero no-session">
            <div class="app-home-main">
                <p class="kicker">Bentornato</p>
                <h1>Ciao, {html.escape(user['first_name'])}</h1>
                <div class="home-primary-actions">{primary_cta}{pay_cta}</div>
            </div>
            <aside class="live-countdown-card muted-card">
                <span>Agenda</span>
                <strong>Libera</strong>
                <small>Nessuna seduta attiva</small>
            </aside>
        </section>
        """
        sticky_cta = primary_cta

    timeline_empty = '<div class="empty-state"><p>Nessuna seduta prenotata.</p><a class="button" href="/book">Prenota ora</a></div>'
    debts_panel = f"""
        <section class="card cockpit-side-card" id="payments">
            <p class="kicker">Pagamenti</p>
            <h2>Residui</h2>
            <div class="debt-mini-list">{unpaid_items or '<p class="muted">Nessun residuo aperto.</p>'}</div>
            {f'<a class="button secondary payment-jump-link" href="/profile?open=payments#payments">Paga residui {money(total_residual)}</a>' if total_residual > 0 else ''}
        </section>
    """
    quick_actions = f"""
        <section class="card cockpit-side-card">
            <p class="kicker">Azioni rapide</p>
            <div class="quick-actions smart-actions">
                <a class="button" href="/book">Prenota nuova seduta</a>
                <a class="button secondary" href="/profile#documents">Documenti</a>
                <a class="button secondary payment-jump-link" href="/profile?open=payments#payments">Pagamenti</a>
                <a class="button secondary" href="/profile">Storico</a>
            </div>
        </section>
    """
    body += f"""
    <section class="home-cockpit-grid stack-top">
        <div class="card home-timeline">
            <div class="section-head compact-head"><div><p class="kicker">Agenda</p><h2>Prossime sedute</h2></div></div>
            <div class="timeline-list">{''.join(session_cards) if session_cards else timeline_empty}</div>
        </div>
        <aside class="home-side-stack">
            {quick_actions}
            {debts_panel}
        </aside>
    </section>
    <div class="sticky-mobile-cta">{sticky_cta}</div>
    """
    return page("Home", body, user, flash)


def specialist_cards_html(doctors: list[sqlite3.Row], services_by_doctor: dict[int, list[str]], cta_label: str = "Prenota") -> str:
    cards = []
    for doctor in doctors:
        did = int(doctor["id"])
        services = services_by_doctor.get(did, [])
        service_text = " · ".join(services[:2]) if services else doctor_qualification(doctor)
        years = int(doctor["doctor_years_experience"] or 0) if "doctor_years_experience" in doctor.keys() else 0
        years_badge = f'<span>{years}+ anni</span>' if years else ""
        cards.append(
            f"""
            <article class="specialist-card">
                <a class="specialist-card-main" href="/doctor?id={did}">
                    <img src="{html.escape(doctor_photo_url(doctor), quote=True)}" alt="{html.escape(doctor_display_name(doctor), quote=True)}">
                    <div>
                        <strong>{html.escape(doctor_display_name(doctor))}</strong>
                        <span>{html.escape(doctor_qualification(doctor))}</span>
                        <small>{html.escape(service_text)}</small>
                        <small>{html.escape(doctor_location(doctor))}</small>
                    </div>
                </a>
                <div class="specialist-card-foot">
                    <div class="specialist-badges">{years_badge}<span>Da {money(DEFAULT_APPOINTMENT_PRICE)}</span></div>
                    <a class="button compact-button" href="/book?doctor_id={did}">{cta_label}</a>
                </div>
            </article>
            """
        )
    return "".join(cards)


def specialists_page(user: sqlite3.Row, query: dict[str, list[str]], flash: str = "") -> bytes:
    search = query.get("q", [""])[0].strip().lower()
    conn = connect()
    doctors = all_doctors(conn)
    if search:
        doctors = [
            row for row in doctors
            if search in f"{row['first_name']} {row['last_name']} {doctor_qualification(row)} {doctor_bio(row)}".lower()
        ]
    service_rows = conn.execute(
        "SELECT doctor_id, name FROM service_types WHERE active = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    services_by_doctor: dict[int, list[str]] = {}
    for row in service_rows:
        if row["doctor_id"]:
            services_by_doctor.setdefault(int(row["doctor_id"]), []).append(row["name"])
    cards = specialist_cards_html(doctors, services_by_doctor)
    studio_label = html.escape(studio_display_name(APP_NAME))
    body = f"""
    <section class="specialists-hero">
        <p class="kicker">{studio_label}</p>
        <h1>Cerca specialisti</h1>
        <form method="get" action="/specialists" class="specialist-search">
            <span aria-hidden="true">{nav_icon('specialists')}</span>
            <input name="q" value="{html.escape(search)}" placeholder="Cerca per nome o specialità">
            <button>Cerca</button>
        </form>
    </section>
    <section class="specialist-filter-row">
        <span class="filter-chip active">Disponibilità</span>
        <span class="filter-chip">Specialità</span>
        <span class="filter-chip">Prezzo</span>
    </section>
    <section class="section-head specialist-count"><div><p class="kicker">{len(doctors)} specialisti disponibili</p></div></section>
    <section class="specialist-list stack-top">
        {cards or '<section class="card empty-state"><h2>Nessuno specialista trovato</h2></section>'}
    </section>
    """
    return page("Specialisti", body, user, flash)


def doctor_detail_page(user: sqlite3.Row, query: dict[str, list[str]], flash: str = "") -> bytes:
    doctor = doctor_by_id(query.get("id", ["0"])[0])
    if not doctor:
        return page("Specialista", "<section class='card'><h1>Specialista non trovato</h1></section>", user, flash)
    conn = connect()
    services = conn.execute(
        "SELECT * FROM service_types WHERE active = 1 AND doctor_id = ? ORDER BY name",
        (doctor["id"],),
    ).fetchall()
    next_slot = conn.execute(
        """
        SELECT s.slot_date, s.slot_time, s.capacity,
               COALESCE(SUM(CASE WHEN a.status = 'prenotata' THEN 1 ELSE 0 END), 0) AS booked
        FROM slots s
        LEFT JOIN appointments a ON a.slot_id = s.id
        WHERE s.doctor_id = ? AND (s.slot_date > ? OR (s.slot_date = ? AND s.slot_time >= ?)) AND s.blocked = 0
        GROUP BY s.id
        HAVING (s.capacity - booked) > 0
        ORDER BY s.slot_date, s.slot_time
        LIMIT 1
        """,
        (doctor["id"], today().isoformat(), today().isoformat(), now().strftime("%H:%M")),
    ).fetchone()
    patient_count = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS total FROM appointments WHERE doctor_id = ?",
        (doctor["id"],),
    ).fetchone()["total"]
    conn.close()
    service_chips = "".join(f'<span class="service-chip">{html.escape(service["name"])}</span>' for service in services)
    next_availability = (
        f"{date_full_label(next_slot['slot_date'])} alle {next_slot['slot_time']}"
        if next_slot else "Nessuna disponibilità aperta"
    )
    years = int(doctor["doctor_years_experience"] or 0) if "doctor_years_experience" in doctor.keys() else 0
    body = f"""
    <section class="doctor-detail-hero">
        <a class="doctor-back" href="/specialists">←</a>
        <img class="doctor-detail-photo" src="{html.escape(doctor_photo_url(doctor), quote=True)}" alt="{html.escape(doctor_display_name(doctor), quote=True)}">
        <div class="doctor-detail-overlay">
            <span class="doctor-badge">{html.escape(doctor_qualification(doctor))}</span>
            <h1>{html.escape(doctor_display_name(doctor))}</h1>
            <p>{html.escape(doctor_degree(doctor) if 'doctor_degree' in doctor.keys() and doctor['doctor_degree'] else doctor_qualification(doctor))}</p>
        </div>
    </section>
    <section class="doctor-stats-card">
        <div><strong>{years}+</strong><span>Anni esp.</span></div>
        <div><strong>{len(services)}</strong><span>Servizi</span></div>
        <div><strong>{int(patient_count or 0)}</strong><span>Pazienti</span></div>
    </section>
    <section class="doctor-detail-content">
        <h2>Biografia professionale</h2>
        <p>{html.escape(doctor_bio(doctor))}</p>
        <h2>Specializzazioni</h2>
        <div class="specialization-grid">{service_chips or '<span class="muted">Nessuna tipologia configurata.</span>'}</div>
        <article class="next-availability-card">
            <div>
                <h2>Prossima disponibilità</h2>
                <strong>{html.escape(next_availability)}</strong>
            </div>
            <span>{money(DEFAULT_APPOINTMENT_PRICE)}</span>
        </article>
    </section>
    <section class="sticky-booking-cta"><a class="button" href="/book?doctor_id={doctor['id']}">Prenota seduta</a></section>
    """
    return page("Specialista", body, user, flash)

def admin_booking_page(user: sqlite3.Row, query: dict[str, list[str]], flash: str = "") -> bytes:
    selected = query.get("date", [today().isoformat()])[0]
    selected_day = parse_date(selected)
    doctor_id = int(user["id"])
    conn = connect()
    appointments = conn.execute(
        """
        SELECT a.id, a.price, a.status, s.slot_date, s.slot_time, st.name AS service_type_name,
               u.first_name, u.last_name,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        JOIN users u ON u.id = a.user_id
        LEFT JOIN payments p ON p.appointment_id = a.id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        WHERE s.slot_date = ? AND a.doctor_id = ?
          AND COALESCE(u.account_status, 'active') = 'active'
        GROUP BY a.id
        ORDER BY s.slot_time, u.last_name, u.first_name
        """,
        (selected_day.isoformat(), doctor_id),
    ).fetchall()
    conn.close()
    total_paid = sum(float(row["paid"] or 0) for row in appointments)
    active_count = sum(1 for row in appointments if row["status"] != "cancellata")
    rows = "".join(
        f"""
        <tr>
            <td data-label="ID">#{row["id"]}</td>
            <td data-label="Paziente">{html.escape(row["first_name"])} {html.escape(row["last_name"])}</td>
            <td data-label="Orario">{row["slot_time"]}</td>
            <td data-label="Prestazione"><span class="service-chip">{html.escape(service_label(row))}</span></td>
            <td data-label="Stato">{status_label(row["status"])}</td>
            <td data-label="Importo pagato">{money(row["paid"])}</td>
        </tr>
        """
        for row in appointments
    )
    body = f"""
    <section class="hero-panel">
        <p class="kicker">Area medico</p>
        <h1>Prenotazioni</h1>
                <form method="get" action="/book" class="admin-date-form">
            <div>
                <label>Data</label>
                <input type="date" name="date" value="{selected_day.isoformat()}">
            </div>
            <button>Mostra prenotazioni</button>
        </form>
        <div class="metric-grid">
            <div class="metric"><span>Data</span><strong>{date_label(selected_day.isoformat())}</strong></div>
            <div class="metric"><span>Appuntamenti attivi</span><strong>{active_count}</strong></div>
            <div class="metric"><span>Pagato</span><strong>{money(total_paid)}</strong></div>
        </div>
    </section>
    <div class="section-head">
        <div><h2>Appuntamenti del giorno</h2></div>
    </div>
    <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>Paziente</th><th>Orario</th><th>Prestazione</th><th>Stato</th><th>Importo pagato</th></tr></thead>
            <tbody>{rows or '<tr><td colspan="6">Nessun appuntamento per questa data.</td></tr>'}</tbody>
        </table>
    </div>
    """
    return page("Prenotazioni", body, user, flash)


def patients_page(user: sqlite3.Row, flash: str = "") -> bytes:
    doctor_id = int(user["id"])
    conn = connect()
    patients = conn.execute(
        """
        SELECT u.*,
               COUNT(CASE WHEN a.status = 'effettuata' THEN 1 END) AS sessions,
               COALESCE(SUM(CASE WHEN a.status = 'effettuata' THEN a.price ELSE 0 END), 0) AS total_due,
               COALESCE((SELECT SUM(p.amount) FROM payments p
                         JOIN appointments ax ON ax.id = p.appointment_id
                         WHERE ax.user_id = u.id AND ax.doctor_id = ? AND ax.status = 'effettuata'), 0) AS total_paid
        FROM users u
        JOIN appointments doctor_link ON doctor_link.user_id = u.id AND doctor_link.doctor_id = ?
        LEFT JOIN appointments a ON a.user_id = u.id AND a.doctor_id = ?
        WHERE u.role = 'user' AND COALESCE(u.account_status, 'active') = 'active'
        GROUP BY u.id
        ORDER BY u.last_name, u.first_name
        """,
        (doctor_id, doctor_id, doctor_id),
    ).fetchall()
    conn.close()
    cards = "".join(
        f"""
        <article class="patient-card patient-card-manage">
            <div class="patient-card-head">
                <a class="patient-card-link" href="/patient?id={p["id"]}">
                    <strong>{html.escape(p["first_name"])} {html.escape(p["last_name"])}</strong>
                    <p>{html.escape(p["email"])}</p>
                </a>
                <form method="post" action="/admin/patient/delete" class="patient-delete-form" onsubmit="return confirm('Archiviare questo paziente? Le sedute e i pagamenti resteranno nello storico.');">
                    <input type="hidden" name="id" value="{p['id']}">
                    <button class="icon-danger" aria-label="Elimina paziente {html.escape(p['first_name'], quote=True)} {html.escape(p['last_name'], quote=True)}" title="Elimina paziente">{nav_icon('trash')}</button>
                </form>
            </div>
            <a class="patient-card-link patient-card-metrics" href="/patient?id={p["id"]}">
                <div class="split">
                    <span class="pill">{p["sessions"]} sedute</span>
                    <span class="pill">Residuo {money(p["total_due"] - p["total_paid"])}</span>
                </div>
            </a>
        </article>
        """
        for p in patients
    )
    body = f"""
    <section class="hero-panel">
        <p class="kicker">Archivio medico</p>
        <h1>Pazienti</h1>
            </section>
    <section class="patient-grid stack-top">
        {cards or '<section class="card empty-state"><h2>Nessun paziente</h2></section>'}
    </section>
    """
    return page("Pazienti", body, user, flash)


HISTORY_STATUS_FILTERS = status_filter_options()


def history_filter_state(query: dict[str, list[str]] | None, default_status: str = "prenotata") -> tuple[str, str]:
    return selected_history_state(query, default_status)


def history_filters_html(
    base_path: str,
    selected_status: str,
    selected_date: str,
    hidden_fields: dict[str, str] | None = None,
    client_side: bool = False,
) -> str:
    hidden_fields = hidden_fields or {}
    hidden_html = "".join(
        f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
        for name, value in hidden_fields.items()
    )
    status_links = []
    for value, label in HISTORY_STATUS_FILTERS:
        params = hidden_fields.copy()
        params["status"] = value
        if selected_date:
            params["date"] = selected_date
        href = base_path + "?" + urlencode(params)
        active = " active" if value == selected_status else ""
        if client_side:
            pressed = "true" if value == selected_status else "false"
            status_links.append(
                f'<button type="button" class="filter-chip{active}" '
                f'data-history-status="{html.escape(value)}" aria-pressed="{pressed}">{label}</button>'
            )
        else:
            status_links.append(f'<a class="filter-chip{active}" href="{href}">{label}</a>')
    clear_params = hidden_fields.copy()
    clear_params["status"] = "prenotata"
    clear_href = base_path + "?" + urlencode(clear_params)
    toolbar_attrs = ' data-history-toolbar' if client_side else ''
    date_input_attr = 'data-history-date' if client_side else 'data-auto-submit'
    reset_control = (
        '<button type="button" class="button secondary compact-button" data-history-reset>Reset</button>'
        if client_side
        else (f'<a class="button secondary compact-button" href="{clear_href}">Reset</a>' if selected_date else '')
    )
    return f"""
    <section class="history-toolbar" aria-label="Filtri appuntamenti"{toolbar_attrs}>
        <div class="history-filter-group">
            {''.join(status_links)}
        </div>
        <form class="date-filter-form" method="get" action="{base_path}">
            {hidden_html}
            <input type="hidden" name="status" value="{html.escape(selected_status)}">
            <label class="sr-only" for="history-date-filter">Filtra per data</label>
            <div class="date-filter-control compact-date" data-date-picker-trigger>

                <input id="history-date-filter" type="date" name="date" value="{html.escape(selected_date)}" {date_input_attr}>
                <button type="button" class="date-filter-button" data-date-picker-button>{date_full_label(selected_date) if selected_date else 'Seleziona data'}</button>
            </div>
            {reset_control}
        </form>
    </section>
    """

def patient_detail_page(user: sqlite3.Row, query: dict[str, list[str]], flash: str = "") -> bytes:
    patient_id = int(query.get("id", ["0"])[0])
    doctor_id = int(user["id"])
    conn = connect()
    patient = conn.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'user' AND COALESCE(account_status, 'active') = 'active'",
        (patient_id,),
    ).fetchone()
    linked = conn.execute(
        "SELECT 1 FROM appointments WHERE user_id = ? AND doctor_id = ? LIMIT 1",
        (patient_id, doctor_id),
    ).fetchone()
    if not patient or not linked:
        conn.close()
        return page("Paziente", "<section class='card'><h1>Paziente non trovato</h1></section>", user, flash)

    selected_status, selected_date = history_filter_state(query, "prenotata")
    all_appointments = conn.execute(
        """
        SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name, st.description AS service_type_description
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        WHERE a.user_id = ? AND a.doctor_id = ?
        ORDER BY s.slot_date DESC, s.slot_time DESC
        """,
        (patient_id, doctor_id),
    ).fetchall()

    where_parts = ["a.user_id = ?", "a.doctor_id = ?"]
    params: list[Any] = [patient_id, doctor_id]
    if selected_status == "valid":
        where_parts.append("a.status IN ('prenotata', 'effettuata')")
    else:
        where_parts.append("a.status = ?")
        params.append(selected_status)
    if selected_date:
        where_parts.append("s.slot_date = ?")
        params.append(selected_date)

    appointments = conn.execute(
        f"""
        SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name, st.description AS service_type_description
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY s.slot_date DESC, s.slot_time DESC
        """,
        tuple(params),
    ).fetchall()
    services = conn.execute("SELECT id, name, description FROM service_types WHERE active = 1 AND doctor_id = ? ORDER BY name", (doctor_id,)).fetchall()
    conn.close()

    total_due = total_paid = 0.0
    performed_count = 0
    for app in all_appointments:
        paid, residual, _ = appointment_payment_state(app["id"], app["price"], app["status"])
        due = billable_amount(app["status"], app["price"])
        total_due += due
        if is_billable_status(app["status"]):
            total_paid += paid
            performed_count += 1

    rows = []
    for app in appointments:
        paid, residual, pay_state = full_payment_state(app["id"], app["price"], app["status"])
        payment_status = payment_status_inline(residual, pay_state)
        cancel_action = (
            f"""
            <form method="post" action="/admin/cancel">
                <input type="hidden" name="id" value="{app['id']}">
                <button class="danger">Elimina</button>
            </form>
            """
            if app["status"] != "cancellata"
            else ""
        )
        diary_text = html.escape(app["diary"] or "", quote=True)
        rows.append(
            f"""
            <tr>
                <td data-label="ID">#{app['id']}</td>
                <td data-label="Data">{app['slot_date']} {app['slot_time']}</td>
                <td data-label="Prestazione"><span class="service-chip">{html.escape(service_label(app))}</span></td>
                <td data-label="Stato seduta">{status_label(app['status'], app['auto_suggestion'])}</td>
                <td data-label="Addebito"><span class="pill {'ok' if is_chargeable_status(app['status']) else ''}">{chargeability_label(app['status'])}</span></td>
                <td data-label="Residuo">{payment_status}</td>
                <td data-label="Diario"><button type="button" class="secondary compact-button" data-open-diary data-appointment-id="{app['id']}" data-diary-text="{diary_text}">Diario</button></td>
                <td data-label="Azioni">{cancel_action}</td>
            </tr>
            """
        )

    payment_options = []
    for app in all_appointments:
        _, residual, pay_state = full_payment_state(app["id"], app["price"], app["status"])
        if residual > 0 and is_online_payable_status(app["status"]):
            payment_options.append(
                f'<option value="{app["id"]}">#{app["id"]} - {app["slot_date"]} {app["slot_time"]} - {money(residual)}</option>'
            )
    payment_box = f"""
    <section class="card section-top patient-payment-box">
        <h2>Registra pagamento</h2>
        <form method="post" action="/admin/payment" class="service-form">
            <input type="hidden" name="patient_id" value="{patient_id}">
            <div>
                <label>Seduta</label>
                <select name="appointment_id" required>{''.join(payment_options)}</select>
            </div>
            <div>
                <label>Metodo</label>
                <select name="method"><option>Contanti</option><option>POS</option><option>Bonifico</option><option>Online</option></select>
            </div>
            <button {'disabled' if not payment_options else ''}>Segna come pagato</button>
        </form>
    </section>
    """

    category_buttons = "".join(
        f"""
        <button type="button" class="diary-category" data-diary-category data-service-id="{service['id']}" data-description="{html.escape(service['description'] or service['name'], quote=True)}">+ {html.escape(service['name'])}</button>
        """
        for service in services
    )
    diary_modal = f"""
    <section class="booking-modal diary-modal" data-diary-modal hidden aria-labelledby="diary-title">
        <button type="button" class="booking-backdrop" data-close-diary aria-label="Chiudi"></button>
        <div class="booking-dialog diary-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-diary aria-label="Chiudi">Chiudi</button>
            <p class="kicker">Scheda paziente</p>
            <h3 id="diary-title">Diario seduta</h3>
            <form method="post" action="/admin/diary" data-diary-form>
                <input type="hidden" name="id" data-diary-id>
                <div class="diary-category-row">{category_buttons or '<span class="muted">Nessuna tipologia attiva</span>'}</div>
                <label>Note</label>
                <textarea name="diary" rows="10" data-diary-textarea></textarea>
                <button>Salva</button>
            </form>
        </div>
    </section>
    """

    body = f"""
    <section class="hero-panel">
        <p class="kicker">Scheda paziente</p>
        <h1>{html.escape(patient['first_name'])} {html.escape(patient['last_name'])}</h1>
        <p>{html.escape(patient['email'])} - {html.escape(patient['phone'])}</p>
        <div class="metric-grid">
            <div class="metric metric-total"><span>Totale sedute effettuate</span><strong>{performed_count}</strong></div>
            <div class="metric"><span>Pagato</span><strong>{money(total_paid)}</strong></div>
            <div class="metric"><span>Residuo</span><strong>{money(max(total_due - total_paid, 0))}</strong></div>
        </div>
        <a class="button secondary" href="/patients">Torna ai pazienti</a>
    </section>
    <div class="section-head"><div><h2>Sedute</h2></div></div>
    {history_filters_html('/patient', selected_status, selected_date, {'id': str(patient_id)})}
    <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>Data <span class="th-calendar" aria-hidden="true">{nav_icon('calendar')}</span></th><th>Prestazione</th><th>Stato seduta</th><th>Addebito</th><th>Residuo</th><th>Diario</th><th>Azioni</th></tr></thead>
            <tbody>{''.join(rows) or '<tr><td colspan="8">Nessuna seduta.</td></tr>'}</tbody>
        </table>
    </div>
    {payment_box}
    {diary_modal}
    """
    return page("Paziente", body, user, flash)

def scan_page(user: sqlite3.Row, flash: str = "") -> bytes:
    body = """
    <section class="hero-panel scanner-hero">
        <p class="kicker">Presenze</p>
        <h1>Presenze</h1>
    </section>
    <section class="card qr-scanner-card" data-qr-scanner>
        <div class="camera-permission-strip" data-camera-permission>
            <strong>Scanner QR</strong>
            <span>Consenti l'accesso alla fotocamera.</span>
        </div>
        <div class="qr-video-frame">
            <video playsinline autoplay muted></video>
        </div>
        <div class="split scanner-actions">
            <button type="button" data-start-camera>Consenti fotocamera</button>
            <button type="button" class="secondary" data-stop-camera disabled>Sospendi scanner</button>
        </div>
        <p class="muted" data-scan-status>Scanner in attesa.</p>
        <hr>
        <form method="get" action="/presence" class="service-form">
            <div>
                <label>Token QR manuale</label>
                <input name="token" placeholder="Incolla token presenza">
            </div>
            <button>Conferma presenza</button>
        </form>
    </section>
    <script src="/static/jsQR.min.js" defer></script>
    <script src="/static/scanner.js" defer></script>
    """
    return page("Presenze", body, user, flash)

def services_page(user: sqlite3.Row, flash: str = "") -> bytes:
    doctor_id = int(user["id"])
    conn = connect()
    services = conn.execute("SELECT * FROM service_types WHERE active = 1 AND doctor_id = ? ORDER BY name", (doctor_id,)).fetchall()
    conn.close()
    service_cards = "".join(
        f"""
        <article class="service-admin-card service-admin-card-rich">
            <div class="service-admin-main">
                <strong>{html.escape(service['name'])}</strong>
                <p>{html.escape(service['description'] or 'Descrizione non inserita')}</p>
            </div>
            <div class="service-admin-actions">
                <button type="button" class="secondary compact-button" data-open-service-description data-service-id="{service['id']}" data-service-name="{html.escape(service['name'], quote=True)}" data-service-description="{html.escape(service['description'] or '', quote=True)}">Aggiungi descrizione</button>
                <form method="post" action="/admin/service/delete">
                    <input type="hidden" name="id" value="{service['id']}">
                    <button class="danger">Elimina</button>
                </form>
            </div>
        </article>
        """
        for service in services
    )
    body = f"""
    <section class="hero-panel">
        <p class="kicker">Configurazione medico</p>
        <h1>Tipologia servizio</h1>
        <form method="post" action="/admin/service/add" class="service-form">
            <div>
                <label>Nuova tipologia</label>
                <input name="name" placeholder="Es. Funzionale, Posturale, Riabilitazione" required>
            </div>
            <button>Aggiungi tipo prestazione</button>
        </form>
    </section>
    <div class="section-head"><div><h2>Tipologie attive</h2></div></div>
    <section class="service-admin-grid service-admin-list">
        {service_cards or '<section class="card empty-state"><h2>Nessuna tipologia</h2></section>'}
    </section>
    <section class="booking-modal service-description-modal" data-service-description-modal hidden aria-labelledby="service-description-title">
        <button type="button" class="booking-backdrop" data-close-service-description aria-label="Chiudi"></button>
        <div class="booking-dialog small-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-service-description aria-label="Chiudi">Chiudi</button>
            <p class="kicker">Tipologia servizio</p>
            <h3 id="service-description-title">Aggiungi descrizione</h3>
            <form method="post" action="/admin/service/description" data-service-description-form>
                <input type="hidden" name="id" data-service-description-id>
                <label data-service-description-label>Descrizione</label>
                <textarea name="description" maxlength="4000" rows="8" data-service-description-text></textarea>
                <p class="muted"><span data-service-description-counter>0</span>/4000 caratteri</p>
                <button>Conferma</button>
            </form>
        </div>
    </section>
    """
    return page("Tipologia servizio", body, user, flash)

def booking_page(user: sqlite3.Row, query: dict[str, list[str]], flash: str = "") -> bytes:
    if is_doctor_account(user):
        return admin_booking_page(user, query, flash)
    if not has_signed_consent(user):
        body = f"""
        <section class="hero-panel booking-hero">
            <p class="kicker">Documenti</p>
            <h1>Firma il consenso informato</h1>
            <a class="button" href="/profile#documents">Documenti</a>
        </section>
        """
        return page("Prenotazione", body, user, flash)
    selected = query.get("date", [today().isoformat()])[0]
    selected_day = max(parse_date(selected), today())
    move_id = query.get("move", [""])[0]
    doctor_id_value = query.get("doctor_id", [""])[0]
    if move_id and not doctor_id_value:
        conn = connect()
        move_row = conn.execute("SELECT doctor_id FROM appointments WHERE id = ? AND user_id = ?", (int(move_id), user["id"])).fetchone()
        conn.close()
        doctor_id_value = str(move_row["doctor_id"]) if move_row and move_row["doctor_id"] else ""
    selected_doctor = doctor_by_id(doctor_id_value)
    if not selected_doctor:
        conn = connect()
        doctors = all_doctors(conn)
        service_rows = conn.execute("SELECT doctor_id, name FROM service_types WHERE active = 1 ORDER BY name").fetchall()
        conn.close()
        services_by_doctor: dict[int, list[str]] = {}
        for row in service_rows:
            if row["doctor_id"]:
                services_by_doctor.setdefault(int(row["doctor_id"]), []).append(row["name"])
        cta_cards = specialist_cards_html(
            doctors,
            services_by_doctor,
            "Scegli"
        ).replace('href="/book?doctor_id=', f'href="/book?move={html.escape(move_id)}&doctor_id=' if move_id else 'href="/book?doctor_id=')
        body = f"""
        <section class="specialists-hero booking-specialist-select">
            <p class="kicker">Prenota sessione</p>
            <h1>Scegli specialista</h1>
        </section>
        <section class="specialist-list stack-top">{cta_cards or '<section class="card empty-state"><h2>Nessuno specialista disponibile</h2></section>'}</section>
        """
        return page("Prenotazione", body, user, flash)

    doctor_id = int(selected_doctor["id"])
    ensure_slots(selected_day, days=1, doctor_id=doctor_id)
    services = active_service_types(doctor_id)
    conn = connect()
    slots = conn.execute(
        """
        SELECT s.*,
               SUM(CASE WHEN a.status = 'prenotata' THEN 1 ELSE 0 END) AS booked
        FROM slots s
        LEFT JOIN appointments a ON a.slot_id = s.id
        WHERE s.slot_date = ? AND s.doctor_id = ?
        GROUP BY s.id
        ORDER BY s.slot_time
        """,
        (selected_day.isoformat(), doctor_id),
    ).fetchall()
    conn.close()

    service_choices = "".join(
        f"""
        <label class="service-type-tab" data-service-tab>
            <input type="radio" name="service_type_id" value="{service['id']}" data-service-description="{html.escape(service_description(service), quote=True)}" required>
            <span>{html.escape(service['name'])}</span>
        </label>
        """
        for service in services
    )

    slot_cards = []
    for slot in slots:
        booked = int(slot["booked"] or 0)
        remaining = int(slot["capacity"]) - booked
        slot_dt = parse_dt(slot["slot_date"], slot["slot_time"])
        end_dt = slot_dt + dt.timedelta(minutes=30)
        slot_range = f"{slot['slot_time']}&nbsp;-&nbsp;{end_dt.strftime('%H:%M')}"
        is_past = slot_dt < now()
        if is_past:
            state, cls = "Non disponibile", "full"
        elif slot["blocked"] or remaining <= 0:
            state, cls = "Completo", "full"
        elif remaining == 1:
            state, cls = "Ultimi posti", "low"
        else:
            state, cls = "Disponibile", "ok"

        if state not in {"Disponibile", "Ultimi posti"}:
            continue

        if state in {"Disponibile", "Ultimi posti"} and services:
            slot_label = f"{date_full_label(slot['slot_date'])} - {slot_range} - {doctor_display_name(selected_doctor)}"
            action = (
                f'<button type="button" class="button slot-open" '
                f'data-open-service-modal data-slot-id="{slot["id"]}" '
                f'data-slot-label="{html.escape(slot_label)}">Scegli</button>'
            )
        elif state in {"Disponibile", "Ultimi posti"}:
            action = '<p class="muted">Tipologia servizio non configurata.</p>'
        else:
            action = ""

        slot_cards.append(
            f"""
            <article class="booking-slot-card">
                <div>
                    <strong>{slot_range}</strong>
                    <span class="pill {cls}">{state}</span>
                </div>
                <p>{max(remaining, 0)} posti su {slot['capacity']}</p>
                {action}
            </article>
            """
        )

    next_day = selected_day + dt.timedelta(days=1)
    next_week = selected_day + dt.timedelta(days=7)
    base_nav_params = f"doctor_id={doctor_id}" + (f"&move={quote(move_id)}" if move_id else "")
    nav = f"""
    <section class="booking-date-nav" aria-label="Navigazione calendario">
        <form class="booking-date-current booking-date-picker" method="get" action="/book" data-date-picker-trigger>
            <input type="hidden" name="doctor_id" value="{doctor_id}">
            {f'<input type="hidden" name="move" value="{html.escape(move_id)}">' if move_id else ''}
            <label for="booking-date-input">Data selezionata</label>
            <input id="booking-date-input" type="date" name="date" value="{selected_day.isoformat()}" min="{today().isoformat()}" data-auto-submit>
            <strong>{date_full_label(selected_day.isoformat())}</strong>
            <span><button type="button" class="calendar-icon-button inline-calendar" data-date-picker-button aria-label="Seleziona data">{nav_icon('calendar')}</button></span>
        </form>
        <div class="booking-nav-actions">
            <a class="date-nav-btn date-nav-small" href="/book?{base_nav_params}&date={next_day.isoformat()}">
                <span class="date-arrow">&rsaquo;</span>
                <small>Giorno successivo</small>
            </a>
            <a class="date-nav-btn date-nav-large" href="/book?{base_nav_params}&date={next_week.isoformat()}">
                <span class="date-arrow">&raquo;</span>
                <small>Settimana successiva</small>
            </a>
        </div>
    </section>
    """

    service_modal = f"""
    <section class="booking-modal service-picker-modal" data-service-modal hidden aria-labelledby="service-modal-title">
        <button type="button" class="booking-backdrop" data-close-service-modal aria-label="Chiudi"></button>
        <div class="booking-dialog service-picker-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-service-modal aria-label="Chiudi">Chiudi</button>
            <p class="kicker" data-service-modal-slot>Appuntamento</p>
            <h3 id="service-modal-title">Scegli il tipo di servizio</h3>
            <div class="booking-sheet-steps"><span class="done">1 Data</span><span class="done">2 Orario</span><span>3 Servizio</span></div>
            <form method="post" action="/book" class="booking-form service-picker-form">
                <input type="hidden" name="slot_id" value="" data-service-modal-slot-id>
                <input type="hidden" name="doctor_id" value="{doctor_id}">
                <input type="hidden" name="move_id" value="{html.escape(move_id)}">
                <div class="service-picker-layout">
                    <div class="service-picker-tabs" aria-label="Tipologia servizio">{service_choices}</div>
                    <div class="service-picker-description" data-service-description-panel>
                        <strong>Descrizione</strong>
                        <p>Seleziona una tipologia.</p>
                    </div>
                </div>
                <button data-service-confirm disabled>Conferma appuntamento</button>
            </form>
        </div>
    </section>
    """ if services else ""

    body = f"""
    <section class="hero-panel booking-hero booking-flow-hero">
        <p class="kicker">Agenda paziente</p>
        <h1>Prenotazione</h1>
        <article class="booking-doctor-strip">
            <img src="{html.escape(doctor_photo_url(selected_doctor), quote=True)}" alt="{html.escape(doctor_display_name(selected_doctor), quote=True)}">
            <div><strong>{html.escape(doctor_display_name(selected_doctor))}</strong><span>{html.escape(doctor_qualification(selected_doctor))}</span></div>
            <a class="button secondary compact-button" href="/book">Cambia</a>
        </article>
        <section class="booking-stepper" aria-label="Passaggi prenotazione">
            <div class="booking-step active"><span>1</span><strong>Medico</strong></div>
            <div class="booking-step active"><span>2</span><strong>Data</strong></div>
            <div class="booking-step"><span>3</span><strong>Servizio</strong></div>
        </section>
        {nav}
    </section>
    <section class="booking-calendar stack-top">
        <section class="booking-day-card">
            <div class="booking-day-head compact">
                <p class="kicker">Slot disponibili</p>
            </div>
            <div class="booking-slot-grid booking-step-surface">{''.join(slot_cards) or '<section class="empty-state"><h2>Nessuna fascia disponibile</h2></section>'}</div>
        </section>
    </section>
    {service_modal}
    <script src="/static/booking-modal.js" defer></script>
    """
    return page("Prenotazione", body, user, flash)

def profile_page(user: sqlite3.Row, query: dict[str, list[str]] | None = None, flash: str = "") -> bytes:
    if is_staff_account(user):
        email_config = load_email_config()
        tls_enabled = email_config.get("tls", "1") != "0"
        tls_yes = "selected" if tls_enabled else ""
        tls_no = "" if tls_enabled else "selected"
        email_configured = smtp_configured()
        email_state = "Configurata" if email_configured else "Da configurare"
        email_state_class = "ok" if email_configured else "low"
        password_placeholder = "Gia salvata: lascia vuoto per non cambiarla" if email_config.get("password") else "Password app o API key"
        test_email = email_config.get("from_email") or user["email"]
        stripe_ready = stripe_configured()
        stripe_state = "Configurato" if stripe_ready else "Da configurare"
        stripe_state_class = "ok" if stripe_ready else "low"
        active_stripe_key = stripe_secret_key()
        stripe_preview = stripe_key_preview()
        stripe_mode = "Test" if active_stripe_key.startswith("sk_test_") else ("Live" if active_stripe_key.startswith("sk_live_") else "")
        stripe_source = stripe_source_label(stripe_secret_source())
        stripe_account = get_setting("stripe_account_id", "")
        stripe_placeholder = "Gia salvata" if active_stripe_key else "sk_test_..."
        stripe_webhook_placeholder = "Gia salvato" if stripe_webhook_secret() else "whsec_..."
        stripe_webhook_label = stripe_webhook_preview()
        stripe_endpoint_base = email_config.get("base_url", "").strip().rstrip("/")
        stripe_webhook_endpoint = f"{stripe_endpoint_base}/stripe/webhook" if stripe_endpoint_base else "/stripe/webhook"
        stripe_module_note = "" if stripe is not None else '<p class="muted">Stripe non disponibile su questo ambiente.</p>'
        photo_preview = doctor_photo_url(user)
        gender_value = user["doctor_gender"] if "doctor_gender" in user.keys() else ""
        gender_options = "".join(
            f'<option value="{value}" {"selected" if gender_value == value else ""}>{label}</option>'
            for value, label in [("", "Non specificato"), ("m", "Uomo"), ("f", "Donna")]
        )
        doctor_stripe_account = user["doctor_stripe_account"] if row_has(user, "doctor_stripe_account") and user["doctor_stripe_account"] else ""
        doctor_stripe_placeholder = "Gia salvato" if doctor_stripe_account else "acct_..."
        studio = primary_studio()
        studio_logo = studio_logo_url()
        studio_name = studio["name"] if studio and row_has(studio, "name") else studio_display_name(APP_NAME)
        studio_email = studio["email"] if studio and row_has(studio, "email") else ""
        studio_phone = studio["phone"] if studio and row_has(studio, "phone") else ""
        studio_address = studio["address"] if studio and row_has(studio, "address") else ""
        studio_tax_id = studio["tax_id"] if studio and row_has(studio, "tax_id") else ""
        studio_settings_html = ""
        owner_doctors_html = ""
        if is_studio_owner(user):
            studio_settings_html = f"""
            <details class="profile-box">
                <summary>Studio <span>Dati e logo</span></summary>
                <div class="profile-box-body">
                    <form method="post" action="/admin/studio-settings" class="form-grid compact-form-grid">
                        <div><label>Nome studio</label><input name="studio_name" value="{html.escape(studio_name)}" required></div>
                        <div><label>Email studio</label><input name="studio_email" type="email" value="{html.escape(studio_email)}"></div>
                        <div><label>Telefono studio</label><input name="studio_phone" value="{html.escape(studio_phone)}"></div>
                        <div><label>Partita IVA / CF studio</label><input name="studio_tax_id" value="{html.escape(studio_tax_id)}"></div>
                        <div class="full-row"><label>Indirizzo studio</label><input name="studio_address" value="{html.escape(studio_address)}"></div>
                        <div class="full-row doctor-photo-uploader studio-logo-uploader" data-doctor-photo-uploader>
                            <label>Logo studio</label>
                            <div class="doctor-settings-photo-row setup-logo-row">
                                <div class="doctor-photo-preview has-image doctor-photo-preview-large studio-logo-preview" data-doctor-photo-preview style="--preview-image:url('{html.escape(studio_logo, quote=True)}'); background-image:url('{html.escape(studio_logo, quote=True)}')" aria-label="Anteprima logo studio"></div>
                                <div class="studio-logo-copy">
                                    <strong>Anteprima logo</strong>
                                    <span>Il logo sara usato nella barra dell'app, nella PWA e nelle schermate pubbliche.</span>
                                    <small data-doctor-photo-file-name>Nessun nuovo logo selezionato</small>
                                </div>
                            </div>
                            <div class="logo-upload-actions">
                                <label class="button secondary compact-button logo-upload-button" for="studio-logo-settings-file">Carica logo</label>
                                <button type="button" class="button secondary compact-button logo-upload-clear" data-doctor-photo-clear hidden>Rimuovi selezione</button>
                            </div>
                            <input id="studio-logo-settings-file" class="logo-file-input" type="file" accept="image/png,image/jpeg,image/webp" data-doctor-photo-file>
                            <input type="hidden" name="studio_logo_data" data-doctor-photo-data>
                        </div>
                        <button>Salva studio</button>
                    </form>
                </div>
            </details>
            """
            conn = connect()
            owner_doctors = conn.execute(
                """
                SELECT *
                FROM users
                WHERE role IN ('admin', 'doctor')
                ORDER BY account_status, last_name, first_name, id
                """
            ).fetchall()
            conn.close()
            doctor_rows = []
            for doctor_row in owner_doctors:
                status = doctor_row["account_status"] if row_has(doctor_row, "account_status") else "active"
                active = status == "active"
                self_row = int(doctor_row["id"]) == int(user["id"])
                doctor_stripe_value = doctor_row["doctor_stripe_account"] if row_has(doctor_row, "doctor_stripe_account") and doctor_row["doctor_stripe_account"] else ""
                doctor_stripe_ready = bool(doctor_stripe_value)
                action = ""
                if active and not self_row:
                    action = f"""
                    <form method="post" action="/admin/doctor/archive" onsubmit="return confirm('Archiviare questo medico? Lo storico delle sedute restera disponibile.');">
                        <input type="hidden" name="id" value="{doctor_row['id']}">
                        <button class="danger compact-button">Archivia</button>
                    </form>
                    """
                stripe_form = f"""
                <form method="post" action="/admin/doctor-stripe" class="staff-doctor-stripe-form">
                    <input type="hidden" name="id" value="{doctor_row['id']}">
                    <input name="doctor_stripe_account" value="{html.escape(doctor_stripe_value)}" placeholder="acct_...">
                    <button class="compact-button">Salva Stripe</button>
                </form>
                """
                doctor_rows.append(
                    f"""
                    <article class="staff-doctor-row">
                        <img src="{html.escape(doctor_photo_url(doctor_row), quote=True)}" alt="{html.escape(doctor_display_name(doctor_row), quote=True)}">
                        <div>
                            <strong>{html.escape(doctor_display_name(doctor_row))}</strong>
                            <span>{html.escape(doctor_qualification(doctor_row))}</span>
                            <small>{'Attivo' if active else 'Archiviato'}{' · Tu' if self_row else ''} · Stripe {'ok' if doctor_stripe_ready else 'da configurare'}</small>
                        </div>
                        {stripe_form}
                        {action}
                    </article>
                    """
                )
            owner_doctors_html = f"""
            <details class="profile-box">
                <summary>Medici studio <span>Profili prenotabili e archiviati</span></summary>
                <div class="profile-box-body staff-doctor-list">
                    {''.join(doctor_rows) or '<p class="muted">Nessun medico configurato.</p>'}
                </div>
            </details>
            """
        stripe_settings_html = ""
        if is_studio_owner(user):
            stripe_settings_html = f"""
            <details class="profile-box">
                <summary>Pagamenti online <span>Stripe</span></summary>
                <div class="profile-box-body email-settings-card">
                    <div class="section-heading-row">
                        <h2>Stripe</h2>
                        <span class="pill {stripe_state_class}">{stripe_state}</span>
                    </div>
                    {stripe_module_note}
                    <div class="stripe-status-row">
                        <span class="pill">{html.escape(stripe_source)}</span>
                        {f'<span class="pill">{html.escape(stripe_mode)}</span>' if stripe_mode else ''}
                        {f'<span class="pill">{html.escape(stripe_preview)}</span>' if stripe_preview else ''}
                        {f'<span class="pill">{html.escape(stripe_account)}</span>' if stripe_account else ''}
                    </div>
                    <form method="post" action="/admin/stripe-settings" class="compact-form-grid">
                        <label>Chiave privata Stripe</label>
                        <input name="stripe_secret" type="password" placeholder="{html.escape(stripe_placeholder)}" autocomplete="off">
                        <label>Webhook Stripe</label>
                        <input name="stripe_webhook_secret" type="password" placeholder="{html.escape(stripe_webhook_placeholder)}" autocomplete="off">
                        <button>Salva Stripe</button>
                    </form>
                </div>
            </details>
            """
        body = f"""
        <section class="hero-panel profile-admin-hero">
            <p class="kicker">Medico</p>
            <h1>Impostazioni</h1>
        </section>
        <section class="profile-accordion stack-top admin-settings-stack">
            <details class="profile-box" open>
                <summary>Profilo professionale <span>Bio, qualifica e immagine</span></summary>
                <div class="profile-box-body">
                    <article class="doctor-profile-preview">
                        <img src="{html.escape(photo_preview, quote=True)}" alt="{html.escape(doctor_display_name(user), quote=True)}">
                        <div>
                            <strong>{html.escape(doctor_display_name(user))}</strong>
                            <span>{html.escape(doctor_qualification(user))}</span>
                        </div>
                    </article>
                    <div class="identity-lock-grid">
                        <div><span>Nome</span><strong>{html.escape(user['first_name'])}</strong></div>
                        <div><span>Cognome</span><strong>{html.escape(user['last_name'])}</strong></div>
                        <div><span>Codice fiscale</span><strong>{html.escape(user['fiscal_code'])}</strong></div>
                    </div>
                    <form method="post" action="/profile" class="form-grid compact-form-grid">
                        <div><label>Email</label><input name="email" type="email" value="{html.escape(user['email'])}" required></div>
                        <div><label>Telefono</label><input name="phone" value="{html.escape(user['phone'])}" required></div>
                        <div><label>Qualifica</label><input name="doctor_qualification" value="{html.escape(user['doctor_qualification'] if 'doctor_qualification' in user.keys() else '')}" placeholder="Es. Fisioterapista"></div>
                        <div><label>Anni di esperienza</label><input name="doctor_years_experience" type="number" min="0" max="70" value="{int(user['doctor_years_experience'] or 0) if 'doctor_years_experience' in user.keys() else 0}"></div>
                        <div><label>Titoli di studio</label><input name="doctor_degree" value="{html.escape(user['doctor_degree'] if 'doctor_degree' in user.keys() else '')}"></div>
                        <div><label>Genere</label><select name="doctor_gender">{gender_options}</select></div>
                        <div class="full-row"><label>Sede / studio</label><input name="doctor_location" value="{html.escape(user['doctor_location'] if 'doctor_location' in user.keys() else '')}"></div>
                        <div class="full-row"><label>Account Stripe medico</label><input name="doctor_stripe_account" value="{html.escape(doctor_stripe_account)}" placeholder="{html.escape(doctor_stripe_placeholder)}" autocomplete="off"></div>
                        <div class="full-row"><label>Bio professionale</label><textarea name="doctor_bio" rows="6" maxlength="1200">{html.escape(user['doctor_bio'] if 'doctor_bio' in user.keys() else '')}</textarea></div>
                        <div class="full-row doctor-photo-uploader" data-doctor-photo-uploader>
                            <label>Immagine profilo</label>
                            <div class="doctor-settings-photo-row">
                                <div class="doctor-photo-preview has-image doctor-photo-preview-large" data-doctor-photo-preview style="--preview-image:url('{html.escape(photo_preview, quote=True)}'); background-image:url('{html.escape(photo_preview, quote=True)}')"></div>
                                <div>
                                    <strong>Anteprima foto profilo</strong>
                                    <span>Questa immagine sara visibile nella scheda specialista e nella prenotazione paziente.</span>
                                </div>
                            </div>
                            <input type="file" accept="image/png,image/jpeg,image/webp" data-doctor-photo-file>
                            <input type="hidden" name="doctor_profile_image_data" data-doctor-photo-data>
                            <label class="checkbox-inline"><input type="checkbox" name="remove_doctor_profile_image" value="1"> Rimuovi immagine</label>
                        </div>
                        <div class="full-row"><label>Nuova password</label><input name="password" type="password" placeholder="Lascia vuoto per non cambiare"></div>
                        <button>Aggiorna account</button>
                    </form>
                    <a class="button secondary logout-profile-button" href="/logout">Esci</a>
                </div>
            </details>
            {studio_settings_html}
            {stripe_settings_html}
            {owner_doctors_html}
        </section>
        """
        return page("Impostazioni", body, user, flash)
    query = query or {}
    open_panel = query.get("open", [""])[0]
    open_anagraphics = "open" if open_panel in {"", "anagrafica"} else ""
    open_payments = "open" if open_panel == "payments" else ""
    open_sessions = "open" if open_panel == "sessions" else ""
    open_documents = "open" if open_panel == "documents" else ""
    selected_status, selected_date = history_filter_state(query, "prenotata")
    conn = connect()
    all_appointments = conn.execute(
        """
        SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name, st.description AS service_type_description
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        WHERE a.user_id = ?
        ORDER BY s.slot_date DESC, s.slot_time DESC
        """,
        (user["id"],),
    ).fetchall()
    appointments = all_appointments
    conn.close()

    total_due = total_paid = 0.0
    performed_count = 0
    payment_rows = []
    for app in all_appointments:
        paid, debt_residual, _ = appointment_payment_state(app["id"], app["price"], app["status"])
        total_due += billable_amount(app["status"], app["price"])
        if is_billable_status(app["status"]):
            total_paid += paid
            performed_count += 1
        full_paid, full_residual, full_state = full_payment_state(app["id"], app["price"], app["status"])
        payment_status = payment_status_inline(full_residual, full_state)
        pay_button = ""
        if full_residual > 0 and is_online_payable_status(app["status"]):
            pay_button = f"""
            <form method="post" action="/payment/create" class="inline-form">
                <input type="hidden" name="id" value="{app['id']}">
                <button>Paga</button>
            </form>
            """
        payment_rows.append(
            f"""
            <tr>
                <td data-label="Data">{app['slot_date']} {app['slot_time']}</td>
                <td data-label="Seduta">{status_label(app['status'])}</td>
                <td data-label="Addebito"><span class="pill {'ok' if is_chargeable_status(app['status']) else ''}">{chargeability_label(app['status'])}</span></td>
                <td data-label="Pagamento">{payment_status}</td>
                <td data-label="Azione">{pay_button}</td>
            </tr>
            """
        )

    session_rows = []
    visible_session_count = 0
    for app in appointments:
        visible_initially = app["status"] == selected_status and (not selected_date or app["slot_date"] == selected_date)
        visible_session_count += 1 if visible_initially else 0
        hidden_attr = "" if visible_initially else " hidden"
        session_rows.append(
            f"""
            <tr data-history-row data-status="{html.escape(app['status'])}" data-date="{html.escape(app['slot_date'])}"{hidden_attr}>
                <td data-label="Codice">#{app['id']}</td>
                <td data-label="Data">{app['slot_date']} {app['slot_time']}</td>
                <td data-label="Tipologia"><span class="service-chip">{html.escape(service_label(app))}</span></td>
                <td data-label="Stato">{status_label(app['status'])}</td>
            </tr>
            """
        )
    session_empty_row = f'<tr data-history-empty{" hidden" if visible_session_count else ""}><td colspan="4">Nessuna seduta.</td></tr>'

    signed = has_signed_consent(user)
    consent_status = "Firmato" if signed else "Da firmare"
    consent_file_label = "Consenso informato salvato" if signed and user["consent_file"] else ""
    body = f"""
    <section class="hero-panel">
        <p class="kicker">Area personale</p>
        <h1>Profilo</h1>
        <div class="metric-grid">
            <div class="metric metric-total"><span>Totale sedute effettuate</span><strong>{performed_count}</strong></div>
            <div class="metric"><span>Pagato</span><strong>{money(total_paid)}</strong></div>
            <div class="metric"><span>Residuo</span><strong>{money(max(total_due - total_paid, 0))}</strong></div>
        </div>
    </section>
    <section class="profile-accordion stack-top">
        <details class="profile-box" id="anagrafica" {open_anagraphics}>
            <summary>Anagrafica</summary>
            <div class="profile-box-body">
                <p>{html.escape(user['first_name'])} {html.escape(user['last_name'])}</p>
                <p>{html.escape(user['email'])} - {html.escape(user['phone'])}</p>
                <form method="post" action="/profile">
                    <label>Email</label><input name="email" type="email" value="{html.escape(user['email'])}" required>
                    <label>Telefono</label><input name="phone" value="{html.escape(user['phone'])}" required>
                    <label>Nuova password</label><input name="password" type="password" placeholder="Lascia vuoto per non cambiare">
                    <button>Aggiorna dati</button>
                </form>
                <a class="button secondary logout-profile-button" href="/logout">Esci</a>
            </div>
        </details>
        <details class="profile-box" id="payments" {open_payments}>
            <summary>Pagamenti</summary>
            <div class="profile-box-body">
                <div class="table-wrap"><table><thead><tr><th>Data</th><th>Seduta</th><th>Addebito</th><th>Pagamento</th><th>Azione</th></tr></thead><tbody>{''.join(payment_rows) or '<tr><td colspan="5">Nessuna seduta.</td></tr>'}</tbody></table></div>
            </div>
        </details>
        <details class="profile-box" id="sessions" {open_sessions}>
            <summary>Sedute</summary>
            <div class="profile-box-body">
                {history_filters_html('/profile', selected_status, selected_date, client_side=True)}
                <div class="table-wrap"><table><thead><tr><th>Codice</th><th>Data <span class="th-calendar" aria-hidden="true">{nav_icon('calendar')}</span></th><th>Tipologia</th><th>Stato</th></tr></thead><tbody>{''.join(session_rows)}{session_empty_row}</tbody></table></div>
            </div>
        </details>
        <details class="profile-box" id="documents" {open_documents}>
            <summary>Documenti <span>Firma il consenso informato</span></summary>
            <div class="profile-box-body documents-box">
                <div><span class="pill {'ok' if signed else 'low'}">{consent_status}</span>{f'<p>{consent_file_label}</p>' if consent_file_label else ''}</div>
                <button type="button" data-open-consent>{'Rivedi consenso' if signed else 'Firma consenso'}</button>
            </div>
        </details>
    </section>
    {consent_form_modal(user)}
    """
    return page("Profilo", body, user, flash)

def admin_dashboard(user: sqlite3.Row) -> str:
    run_noncritical("admin:auto_suggestions", update_auto_suggestions)
    conn = connect()
    day = today().isoformat()
    doctor_id = int(user["id"])
    appointments = conn.execute(
        """
        SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name, st.description AS service_type_description,
               u.id AS patient_id, u.first_name, u.last_name, u.email,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        JOIN users u ON u.id = a.user_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        LEFT JOIN payments p ON p.appointment_id = a.id
        WHERE s.slot_date = ? AND a.doctor_id = ?
        GROUP BY a.id
        ORDER BY s.slot_time
        """,
        (day, doctor_id),
    ).fetchall()
    pending = conn.execute(
        """
        SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name, st.description AS service_type_description,
               u.id AS patient_id, u.first_name, u.last_name,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        JOIN users u ON u.id = a.user_id
        LEFT JOIN service_types st ON st.id = a.service_type_id
        LEFT JOIN payments p ON p.appointment_id = a.id
        WHERE a.status = 'prenotata' AND a.auto_suggestion IS NOT NULL AND a.doctor_id = ?
          AND COALESCE(u.account_status, 'active') = 'active'
        GROUP BY a.id
        ORDER BY s.slot_date, s.slot_time
        """,
        (doctor_id,),
    ).fetchall()
    debt_details = conn.execute(
        """
        SELECT a.id, a.user_id, a.price, s.slot_date, s.slot_time, u.first_name, u.last_name,
               COALESCE(SUM(p.amount), 0) AS paid
        FROM appointments a
        JOIN slots s ON s.id = a.slot_id
        JOIN users u ON u.id = a.user_id
        LEFT JOIN payments p ON p.appointment_id = a.id
        WHERE a.status = 'effettuata' AND a.doctor_id = ?
          AND COALESCE(u.account_status, 'active') = 'active'
        GROUP BY a.id
        HAVING (a.price - COALESCE(SUM(p.amount), 0)) > 0.001
        ORDER BY s.slot_date DESC, s.slot_time DESC
        """,
        (doctor_id,),
    ).fetchall()
    missing_consents = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM users u
        WHERE u.role = 'user'
          AND COALESCE(u.account_status, 'active') = 'active'
          AND (u.consent_signed_at IS NULL OR u.consent_signed_at = '')
          AND EXISTS (SELECT 1 FROM appointments a WHERE a.user_id = u.id AND a.doctor_id = ?)
        """,
        (doctor_id,),
    ).fetchone()["total"]
    services = conn.execute("SELECT * FROM service_types WHERE active = 1 AND doctor_id = ? ORDER BY name", (doctor_id,)).fetchall()
    conn.close()

    pending_payments = sum(max(float(row["price"] or 0) - float(row["paid"] or 0), 0) for row in debt_details)
    active_today = [item for item in appointments if item["status"] != "cancellata"]

    category_buttons = "".join(
        f'<button type="button" class="diary-category" data-diary-category data-service-id="{service["id"]}" data-description="{html.escape(service["description"] or service["name"], quote=True)}">+ {html.escape(service["name"])}</button>'
        for service in services
    )
    diary_modal = f"""
    <section class="booking-modal diary-modal" data-diary-modal hidden aria-labelledby="diary-title">
        <button type="button" class="booking-backdrop" data-close-diary aria-label="Chiudi"></button>
        <div class="booking-dialog diary-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-diary aria-label="Chiudi">Chiudi</button>
            <p class="kicker">Agenda</p>
            <h3 id="diary-title">Diario seduta</h3>
            <form method="post" action="/admin/diary" data-diary-form>
                <input type="hidden" name="id" data-diary-id>
                <div class="diary-category-row">{category_buttons or '<span class="muted">Nessuna tipologia attiva</span>'}</div>
                <label>Note</label>
                <textarea name="diary" rows="10" data-diary-textarea></textarea>
                <button>Salva</button>
            </form>
        </div>
    </section>
    """

    def agenda_actions(row: sqlite3.Row) -> str:
        paid = float(row["paid"] or 0)
        due = payable_amount(row["status"], row["price"])
        residual = max(due - paid, 0)
        diary_text = html.escape(row["diary"] or "", quote=True)
        actions = [
            f'<button type="button" class="secondary compact-button" data-open-diary data-appointment-id="{row["id"]}" data-diary-text="{diary_text}">Diario</button>'
        ]
        if row["status"] == "prenotata":
            actions.append(f'<form method="post" action="/admin/status"><input type="hidden" name="id" value="{row["id"]}"><input type="hidden" name="status" value="effettuata"><button class="compact-button">Effettuata</button></form>')
            actions.append(f'<form method="post" action="/admin/status"><input type="hidden" name="id" value="{row["id"]}"><input type="hidden" name="status" value="non_presentato"><button class="warn compact-button">No show</button></form>')
        if residual > 0:
            actions.append(f'<form method="post" action="/admin/mark-paid"><input type="hidden" name="id" value="{row["id"]}"><input type="hidden" name="return_to" value="/"><button class="secondary compact-button">Segna pagato</button></form>')
        if row["status"] != "cancellata":
            actions.append(f'<form method="post" action="/admin/cancel"><input type="hidden" name="id" value="{row["id"]}"><button class="danger compact-button">Cancella</button></form>')
        return "".join(actions)

    agenda_items = "".join(
        f"""
        <article class="agenda-item">
            <div class="agenda-time">{row['slot_time']}</div>
            <div class="agenda-content">
                <div class="agenda-main-line">
                    <a href="/patient?id={row['patient_id']}"><strong>{html.escape(row['first_name'])} {html.escape(row['last_name'])}</strong></a>
                    <span class="pill">{status_label(row['status'], row['auto_suggestion'])}</span>
                </div>
                <div class="agenda-meta"><span class="service-chip">{html.escape(service_label(row))}</span><span>{html.escape(row['email'])}</span></div>
                <div class="agenda-actions">{agenda_actions(row)}</div>
            </div>
        </article>
        """
        for row in appointments
    )

    pending_items = "".join(
        f"""
        <article class="task-detail-card">
            <div><strong>{p['slot_date']} {p['slot_time']}</strong><span>{html.escape(p['first_name'])} {html.escape(p['last_name'])}</span></div>
            <div class="cockpit-actions">
                <form method="post" action="/admin/status"><input type="hidden" name="id" value="{p['id']}"><input type="hidden" name="status" value="effettuata"><button class="compact-button">Effettuata</button></form>
                <form method="post" action="/admin/status"><input type="hidden" name="id" value="{p['id']}"><input type="hidden" name="status" value="non_presentato"><button class="warn compact-button">Non presentato</button></form>
            </div>
        </article>
        """
        for p in pending
    )

    debt_rows = "".join(
        f"""
        <tr>
            <td data-label="Data">{row['slot_date']} {row['slot_time']}</td>
            <td data-label="Paziente">{html.escape(row['first_name'])} {html.escape(row['last_name'])}</td>
            <td data-label="Residuo">{money(max(float(row['price'] or 0) - float(row['paid'] or 0), 0))}</td>
            <td data-label="Azione"><a class="button secondary compact-button" href="/patient?id={row['user_id']}">Apri scheda</a></td>
        </tr>
        """
        for row in debt_details
    )
    debt_cards = "".join(
        f"""
        <a class="debt-mini-card" href="/patient?id={row['user_id']}">
            <span>{html.escape(row['first_name'])} {html.escape(row['last_name'])}</span>
            <strong>{money(max(float(row['price'] or 0) - float(row['paid'] or 0), 0))}</strong>
            <small>Apri scheda</small>
        </a>
        """
        for row in debt_details[:5]
    )

    task_cards = f"""
        <a class="task-card {'is-urgent' if pending else ''}" href="#pending-confirmations"><span>{len(pending)}</span><strong>Sedute da confermare</strong></a>
        <button type="button" class="task-card {'is-urgent' if debt_details else ''}" data-open-debts><span>{len(debt_details)}</span><strong>Residui aperti</strong></button>
        <a class="task-card {'is-urgent' if missing_consents else ''}" href="/patients"><span>{int(missing_consents or 0)}</span><strong>Consensi mancanti</strong></a>
        <a class="task-card" href="/scan"><span>{nav_icon('qr')}</span><strong>Scanner QR</strong></a>
    """

    return f"""
    <section class="hero-panel cockpit-hero admin-cockpit-hero">
        <div>
            <p class="kicker">{html.escape(studio_display_name(APP_NAME))}</p>
            <h1>Oggi</h1>
            <div class="home-session-line"><span>{date_full_label(day)}</span><span>{len(active_today)} appuntamenti attivi</span></div>
        </div>
        <div class="home-primary-actions">
            <a class="button pulse-action" href="/scan">Scannerizza QR</a>
            <a class="button secondary" href="/book?date={day}">Prenotazioni</a>
            <a class="button secondary" href="/slots?date={day}">Gestisci slot</a>
        </div>
    </section>

    <section class="admin-cockpit-grid stack-top">
        <section class="card agenda-panel">
            <div class="section-head compact-head"><div><p class="kicker">Agenda</p><h2>Appuntamenti di oggi</h2></div></div>
            <div class="agenda-timeline">{agenda_items or '<div class="empty-state"><p>Nessun appuntamento oggi.</p><a class="button" href="/book">Vai alle prenotazioni</a></div>'}</div>
        </section>
        <aside class="admin-side-stack">
            <section class="card cockpit-side-card">
                <p class="kicker">Da fare</p>
                <div class="task-grid">{task_cards}</div>
            </section>
            <section class="card cockpit-side-card">
                <p class="kicker">Residui critici</p>
                <div class="debt-mini-list">{debt_cards or '<p class="muted">Nessun residuo aperto.</p>'}</div>
            </section>
        </aside>
    </section>

    <section class="card stack-top pending-panel" id="pending-confirmations">
        <div class="section-head compact-head"><div><p class="kicker">Presenze</p><h2>Sedute da confermare</h2></div></div>
        <div class="task-detail-list">{pending_items or '<p class="muted">Nessuna seduta in attesa.</p>'}</div>
    </section>

    <section class="booking-modal debts-modal" data-debts-modal hidden aria-labelledby="debts-title">
        <button type="button" class="booking-backdrop" data-close-debts aria-label="Chiudi"></button>
        <div class="booking-dialog debts-dialog" role="dialog" aria-modal="true">
            <button type="button" class="modal-close" data-close-debts aria-label="Chiudi">Chiudi</button>
            <p class="kicker">Pagamenti</p>
            <h3 id="debts-title">Pagamenti residui</h3>
            <div class="table-wrap"><table><thead><tr><th>Data</th><th>Paziente</th><th>Residuo</th><th>Azione</th></tr></thead><tbody>{debt_rows or '<tr><td colspan="4">Nessun pagamento residuo.</td></tr>'}</tbody></table></div>
        </div>
    </section>
    {diary_modal}
    """
def slots_page(user: sqlite3.Row, query: dict[str, list[str]], flash: str = "") -> bytes:
    selected = query.get("date", [today().isoformat()])[0]
    selected_day = max(parse_date(selected), today())
    doctor_id = int(user["id"])
    part = query.get("part", ["morning"])[0]
    if part not in SLOT_PARTS:
        part = "morning"
    ensure_slots(selected_day, days=1, doctor_id=doctor_id)
    slot_times = slot_times_for_part(part)
    conn = connect()
    slots = conn.execute(
        f"""
        SELECT s.*, SUM(CASE WHEN a.status = 'prenotata' THEN 1 ELSE 0 END) AS booked
        FROM slots s
        LEFT JOIN appointments a ON a.slot_id = s.id
        WHERE s.slot_date = ? AND s.doctor_id = ? AND s.slot_time IN ({','.join('?' for _ in slot_times)})
        GROUP BY s.id
        ORDER BY s.slot_time
        """,
        tuple([selected_day.isoformat(), doctor_id, *slot_times]),
    ).fetchall()
    conn.close()
    day_links = []
    for offset in range(0, 7):
        day = today() + dt.timedelta(days=offset)
        active = " active" if day == selected_day else ""
        day_links.append(f'<a class="date-chip{active}" href="/slots?date={day.isoformat()}&part={part}">{date_label(day.isoformat())}</a>')
    part_links = "".join(
        f'<a class="filter-chip{" active" if key == part else ""}" href="/slots?date={selected_day.isoformat()}&part={key}">{label}</a>'
        for key, (label, _, _) in SLOT_PARTS.items()
    )
    default_capacity = default_slot_capacity()
    cards = []
    for slot in slots:
        booked = int(slot["booked"] or 0)
        end_time = (parse_dt(slot["slot_date"], slot["slot_time"]) + dt.timedelta(minutes=30)).strftime("%H:%M")
        cards.append(
            f"""
            <article class="slot-management-card slot-management-card-compact">
                <div><strong>{slot['slot_time']} - {end_time}</strong><span class="pill ok">{booked} / {slot['capacity']}</span></div>
                <form method="post" action="/admin/slot" class="slot-management-form">
                    <input type="hidden" name="id" value="{slot['id']}">
                    <label>Posti</label><input name="capacity" type="number" min="0" value="{slot['capacity']}">
                    <button>Aggiorna</button>
                </form>
            </article>            """
        )
    body = f"""
    <section class="hero-panel slots-hero">
        <p class="kicker">Area medico</p>
        <h1>Gestione slot</h1>
        <form class="booking-date-current booking-date-picker" method="get" action="/slots" data-date-picker-trigger>
            <input type="hidden" name="part" value="{part}">
            <label for="slot-date-input">Data</label>
            <input id="slot-date-input" type="date" name="date" value="{selected_day.isoformat()}" min="{today().isoformat()}" data-auto-submit>
            <strong>{date_full_label(selected_day.isoformat())}</strong>
            <span><button type="button" class="calendar-icon-button inline-calendar" data-date-picker-button aria-label="Seleziona data">{nav_icon('calendar')}</button></span>
        </form>
        <div class="date-slider">{''.join(day_links)}</div>
        <form method="post" action="/admin/default-capacity" class="default-capacity-box">
            <label>Imposta limite di default</label>
            <input name="capacity" type="number" min="0" value="{default_capacity}">
            <button>Applica</button>
        </form>
        <div class="history-filter-group slot-part-selector">{part_links}</div>
    </section>
    <section class="slot-management-grid stack-top">{''.join(cards) or '<section class="card empty-state"><h2>Nessuno slot</h2></section>'}</section>
    """
    return page("Gestione slot", body, user, flash)


def backup_database_if_needed() -> None:
    if not DB_PATH.exists():
        return
    backup_dir = APP_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"fisio_app_{today().isoformat()}.sqlite3"
    if target.exists():
        return
    source = sqlite3.connect(DB_PATH)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def bootstrap_app(force: bool = False) -> None:
    global BOOTSTRAP_DONE
    if BOOTSTRAP_DONE and not force:
        return
    with BOOTSTRAP_LOCK:
        if BOOTSTRAP_DONE and not force:
            return
        init_db()
        BOOTSTRAP_DONE = True


class App(BaseHTTPRequestHandler):
    def runtime_error(self, exc: BaseException) -> None:
        print("Rehab runtime error:", repr(exc), file=sys.stderr)
        traceback.print_exc()
        title = "Configurazione app da completare"
        detail = html.escape(type(exc).__name__)
        body = f"""
        <!doctype html>
        <html lang="it">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{title}</title>
            <style>
                body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f7faf6; color: #0b3029; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
                main {{ width: min(92vw, 620px); padding: 28px; border: 1px solid #dbe8e1; border-radius: 24px; background: #fff; box-shadow: 0 22px 70px rgba(2, 37, 30, .12); }}
                h1 {{ margin: 0 0 12px; font-size: clamp(28px, 5vw, 42px); line-height: 1.05; }}
                p {{ margin: 0 0 12px; color: #5f7169; font-size: 16px; line-height: 1.55; }}
                code {{ display: inline-block; padding: 4px 8px; border-radius: 8px; background: #edf8f2; color: #004f3f; }}
            </style>
        </head>
        <body>
            <main>
                <h1>{title}</h1>
                <p>L'app non riesce a inizializzare il database. Controlla <strong>DATABASE_URL</strong> e <strong>FISIO_SECRET</strong> su Vercel, poi fai redeploy.</p>
                <p>Errore tecnico: <code>{detail}</code>. Il dettaglio completo e nei log Vercel.</p>
            </main>
        </body>
        </html>
        """
        data = body.encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def bootstrap_or_error(self) -> bool:
        try:
            bootstrap_app()
            return True
        except Exception as exc:
            self.runtime_error(exc)
            return False

    def healthz(self) -> None:
        payload: dict[str, Any] = {
            "ok": True,
            "database_url": bool(os.environ.get("DATABASE_URL", "").strip()),
            "fisio_secret": bool(os.environ.get("FISIO_SECRET", "").strip()),
            "postgres": postgres_enabled(),
        }
        try:
            bootstrap_app()
        except Exception as exc:
            print("Rehab healthz failed:", repr(exc), file=sys.stderr)
            traceback.print_exc()
            payload.update({"ok": False, "error": type(exc).__name__})
        self.json_response(payload, HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self.healthz()
            return
        if parsed.path == "/__visual_login":
            self.visual_login(query)
            return
        if parsed.path == "/service-worker.js":
            self.serve_static("/static/service-worker.js")
            return
        if parsed.path == "/favicon.ico":
            self.serve_static("/static/app-icon.ico")
            return
        if parsed.path == "/robots.txt":
            self.text_response("User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
            return
        if parsed.path == "/sitemap.xml":
            self.text_response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>\n', "application/xml; charset=utf-8")
            return
        if parsed.path == "/manifest.webmanifest":
            if not self.bootstrap_or_error():
                return
            self.manifest_response()
            return
        if parsed.path.startswith("/static/"):
            self.serve_static(parsed.path)
            return
        if not self.bootstrap_or_error():
            return
        flash = query.get("flash", [""])[0]
        if parsed.path == "/setup":
            if not studio_setup_required():
                self.redirect("/login")
                return
            self.html(setup_page(flash))
            return
        if studio_setup_required():
            self.redirect("/setup")
            return
        user = get_user_from_cookie(self.headers)
        if parsed.path == "/payment/success":
            self.payment_success(user, query)
            return
        if parsed.path == "/payment/cancel":
            target = "/profile?flash=Pagamento%20annullato" if user else "/login?flash=Pagamento%20annullato"
            self.redirect(target)
            return
        if parsed.path in {"/login", "/register"} and user:
            self.redirect("/")
        elif parsed.path in {"/login", "/register"}:
            self.html(login_page(flash))
        elif parsed.path == "/forgot-password" and not user:
            self.html(forgot_password_page(flash))
        elif parsed.path == "/reset-password":
            self.html(reset_password_page(query.get("token", [""])[0], flash))
        elif parsed.path == "/logout":
            self.redirect("/login", clear=True)
        elif parsed.path == "/api/notifications":
            if not user:
                self.json_response({"events": []}, HTTPStatus.UNAUTHORIZED)
                return
            self.json_response({"events": self.notification_events(user)})
        elif not user:
            self.html(login_page("Accedi o registrati per continuare."))
        elif parsed.path == "/":
            self.html(home_page(user, flash, self.base_url()))
        elif parsed.path == "/book":
            self.html(booking_page(user, query, flash))
        elif parsed.path == "/specialists":
            self.html(specialists_page(user, query, flash))
        elif parsed.path == "/doctor":
            self.html(doctor_detail_page(user, query, flash))
        elif parsed.path == "/slots":
            if not is_doctor_account(user):
                self.redirect("/?flash=Accesso%20medico%20richiesto")
                return
            self.html(slots_page(user, query, flash))
        elif parsed.path == "/profile":
            self.html(profile_page(user, query, flash))
        elif parsed.path == "/scan":
            if not is_staff_account(user):
                self.redirect("/?flash=Accesso%20staff%20richiesto")
                return
            self.html(scan_page(user, flash))
        elif parsed.path == "/presence":
            if not is_staff_account(user):
                self.redirect("/login?flash=Accedi%20come%20staff%20per%20confermare%20la%20presenza")
                return
            self.confirm_presence(query)
        elif parsed.path == "/services":
            if not is_doctor_account(user):
                self.redirect("/?flash=Accesso%20medico%20richiesto")
                return
            self.html(services_page(user, flash))
        elif parsed.path == "/patients":
            if not is_staff_account(user):
                self.redirect("/?flash=Accesso%20staff%20richiesto")
                return
            self.html(patients_page(user, flash))
        elif parsed.path == "/patient":
            if not is_staff_account(user):
                self.redirect("/?flash=Accesso%20staff%20richiesto")
                return
            self.html(patient_detail_page(user, query, flash))
        elif parsed.path == "/admin":
            self.redirect("/")
        elif parsed.path == "/ics":
            self.download_ics(user, query)
        else:
            self.error(HTTPStatus.NOT_FOUND, "Pagina non trovata")

    def visual_login(self, query: dict[str, list[str]]) -> None:
        expected_token = os.environ.get("VISUAL_TEST_TOKEN", "").strip()
        supplied_token = query.get("token", [""])[0]
        if not expected_token or not hmac.compare_digest(expected_token, supplied_token):
            self.error(HTTPStatus.NOT_FOUND, "Pagina non trovata")
            return
        init_db()
        ensure_slots()
        role = query.get("role", ["admin"])[0]
        if role not in {"admin", "user"}:
            role = "admin"
        next_path = query.get("next", ["/"])[0] or "/"
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        conn = connect()
        user = conn.execute("SELECT * FROM users WHERE role = ? ORDER BY id LIMIT 1", (role,)).fetchone()
        if role == "user" and not user:
            created_at = now().isoformat()
            conn.execute(
                """
                INSERT INTO users
                (role, first_name, last_name, email, password_hash, phone, fiscal_code,
                 gdpr_consent, privacy_accepted_at, privacy_version, email_verified, phone_verified, created_at)
                VALUES ('user', 'Visual', 'Test', 'visual-test@rehab.local', ?, '+390000000001',
                        'VSLTST80A01H501A', 1, ?, ?, 1, 1, ?)
                """,
                (hash_password(secrets.token_urlsafe(20)), created_at, PRIVACY_VERSION, created_at),
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email = 'visual-test@rehab.local'").fetchone()
        conn.close()
        if not user:
            self.error(HTTPStatus.NOT_FOUND, "Utente visuale non disponibile")
            return
        self.redirect(next_path, token=make_token(user["id"], remember=False))
    def post_is_same_origin(self) -> bool:
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip().lower()
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        candidate = origin or referer
        if not candidate:
            return True
        parsed = urlparse(candidate)
        return parsed.netloc.lower() == host

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/stripe/webhook":
            if not self.bootstrap_or_error():
                return
            self.stripe_webhook()
            return
        if not self.bootstrap_or_error():
            return
        if not self.post_is_same_origin():
            self.error(HTTPStatus.FORBIDDEN, "Richiesta non valida")
            return
        try:
            form = self.form()
            if not self.csrf_is_valid(form):
                self.error(HTTPStatus.FORBIDDEN, "Sessione di sicurezza scaduta. Ricarica la pagina e riprova.")
                return
            user = get_user_from_cookie(self.headers)
            if parsed.path == "/setup":
                self.setup_studio(form)
            elif studio_setup_required():
                self.redirect("/setup")
            elif parsed.path == "/login":
                self.login(form)
            elif parsed.path == "/register":
                self.register(form)
            elif parsed.path == "/forgot-password":
                self.forgot_password(form)
            elif parsed.path == "/reset-password":
                self.reset_password(form)
            elif not user:
                self.redirect("/login?flash=Sessione scaduta")
            elif parsed.path == "/book":
                self.create_booking(user, form)
            elif parsed.path == "/cancel":
                self.cancel_booking(user, form)
            elif parsed.path == "/checkin":
                self.checkin(user, form)
            elif parsed.path == "/profile":
                self.update_profile(user, form)
            elif parsed.path == "/consent/sign":
                self.sign_consent(user, form)
            elif parsed.path == "/payment/create":
                self.create_payment_checkout(user, form)
            elif parsed.path == "/admin/studio-settings":
                self.require_admin(user)
                self.admin_studio_settings(form)
            elif parsed.path == "/admin/stripe-settings":
                self.require_admin(user)
                self.admin_stripe_settings(form)
            elif parsed.path == "/admin/email-settings":
                self.require_admin(user)
                self.admin_email_settings(form)
            elif parsed.path == "/admin/email-test":
                self.require_admin(user)
                self.admin_email_test(form)
            elif parsed.path == "/admin/status":
                self.require_admin(user)
                self.admin_status(form)
            elif parsed.path == "/admin/slot":
                self.require_admin(user)
                self.admin_slot(form)
            elif parsed.path == "/admin/default-capacity":
                self.require_admin(user)
                self.admin_default_capacity(form)
            elif parsed.path == "/admin/payment":
                self.require_admin(user)
                self.admin_payment(form)
            elif parsed.path == "/admin/mark-paid":
                self.require_admin(user)
                self.admin_mark_paid(form)
            elif parsed.path == "/admin/price":
                self.require_admin(user)
                self.admin_price(form)
            elif parsed.path == "/admin/cancel":
                self.require_admin(user)
                self.admin_cancel(form)
            elif parsed.path == "/admin/service/add":
                self.require_admin(user)
                self.admin_service_add(form)
            elif parsed.path == "/admin/service/delete":
                self.require_admin(user)
                self.admin_service_delete(form)
            elif parsed.path == "/admin/service/description":
                self.require_admin(user)
                self.admin_service_description(form)
            elif parsed.path == "/admin/patient/delete":
                self.require_admin(user)
                self.admin_patient_delete(form)
            elif parsed.path == "/admin/doctor/archive":
                self.require_admin(user)
                self.admin_doctor_archive(form)
            elif parsed.path == "/admin/doctor-stripe":
                self.require_admin(user)
                self.admin_doctor_stripe(form)
            elif parsed.path == "/admin/diary":
                self.require_admin(user)
                self.admin_diary(form)
            else:
                self.error(HTTPStatus.NOT_FOUND, "Pagina non trovata")
        except ValueError as exc:
            self.flash_redirect(self.headers.get("Referer", "/"), str(exc))
        except Exception as exc:
            self.runtime_error(exc)

    def form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_FORM_BYTES:
            raise ValueError("Richiesta troppo grande")
        raw = self.rfile.read(length).decode("utf-8")
        return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def setup_studio(self, form: dict[str, str]) -> None:
        if not studio_setup_required():
            self.redirect("/login")
            return
        studio_name = form.get("studio_name", "").strip()
        if not studio_name:
            raise ValueError("Inserisci il nome dello studio")
        email = form.get("email", "").lower().strip()
        password = form.get("password", "")
        required_owner_fields = ["first_name", "last_name", "phone", "fiscal_code"]
        if any(not form.get(field, "").strip() for field in required_owner_fields):
            raise ValueError("Compila i dati obbligatori del proprietario")
        if "@" not in email:
            raise ValueError("Email proprietario non valida")
        if len(password) < 8:
            raise ValueError("Password troppo corta")
        owner_is_doctor = form.get("owner_is_doctor") == "1"
        doctor_qualification_value = form.get("doctor_qualification", "").strip() if owner_is_doctor else ""
        if owner_is_doctor and not doctor_qualification_value:
            raise ValueError("Inserisci la qualifica professionale")
        try:
            years_experience = max(int(form.get("doctor_years_experience", "0") or 0), 0)
        except ValueError as exc:
            raise ValueError("Anni di esperienza non validi") from exc
        conn = connect()
        try:
            studio = primary_studio(conn)
            if studio:
                studio_id = int(studio["id"])
                conn.execute(
                    """
                    UPDATE studios
                    SET name = ?, email = ?, phone = ?, address = ?, tax_id = ?,
                        setup_completed_at = COALESCE(setup_completed_at, ?)
                    WHERE id = ?
                    """,
                    (
                        studio_name,
                        form.get("studio_email", "").strip(),
                        normalize_phone(form.get("studio_phone", "")),
                        form.get("studio_address", "").strip(),
                        form.get("studio_tax_id", "").strip(),
                        now().isoformat(),
                        studio_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO studios (name, email, phone, address, tax_id, setup_completed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        studio_name,
                        form.get("studio_email", "").strip(),
                        normalize_phone(form.get("studio_phone", "")),
                        form.get("studio_address", "").strip(),
                        form.get("studio_tax_id", "").strip(),
                        now().isoformat(),
                        now().isoformat(),
                    ),
                )
                studio_id = int(cur.lastrowid)
            permissions = f"{OWNER_PERMISSION},{DOCTOR_PERMISSION}" if owner_is_doctor else OWNER_PERMISSION
            cur = conn.execute(
                """
                INSERT INTO users
                (role, studio_id, permissions, account_status, bookable, profile_visible,
                 first_name, last_name, email, password_hash, phone, fiscal_code,
                 gdpr_consent, privacy_accepted_at, privacy_version, email_verified, phone_verified, created_at,
                 doctor_bio, doctor_years_experience, doctor_degree, doctor_qualification, doctor_gender,
                 doctor_location, doctor_onboarded_at)
                VALUES ('admin', ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?,
                        1, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    studio_id,
                    permissions,
                    1 if owner_is_doctor else 0,
                    1 if owner_is_doctor else 0,
                    form.get("first_name", "").strip(),
                    form.get("last_name", "").strip(),
                    email,
                    hash_password(password),
                    normalize_phone(form.get("phone", "")),
                    form.get("fiscal_code", "").upper().strip(),
                    now().isoformat(),
                    PRIVACY_VERSION,
                    now().isoformat(),
                    form.get("doctor_bio", "").strip() if owner_is_doctor else "",
                    years_experience if owner_is_doctor else 0,
                    form.get("doctor_degree", "").strip() if owner_is_doctor else "",
                    doctor_qualification_value,
                    form.get("doctor_gender", "").strip() if owner_is_doctor else "",
                    studio_name if owner_is_doctor else "",
                    now().isoformat() if owner_is_doctor else None,
                ),
            )
            owner_id = int(cur.lastrowid)
            logo_data = form.get("studio_logo_data", "")
            if logo_data:
                logo_url = save_studio_logo_image(studio_id, logo_data)
                conn.execute("UPDATE studios SET logo_path = ? WHERE id = ?", (logo_url, studio_id))
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ('studio_setup_complete', '1') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            conn.commit()
        except db_integrity_error_types() as exc:
            conn.rollback()
            raise ValueError("Email gia registrata") from exc
        finally:
            conn.close()
        run_noncritical(
            "setup:event",
            lambda: log_event("studio_setup", f"Studio configurato: {studio_name}", owner_id),
        )
        self.redirect("/", token=make_token(owner_id, remember=True), max_age=30 * 24 * 60 * 60)

    def admin_studio_settings(self, form: dict[str, str]) -> None:
        actor = get_user_from_cookie(self.headers)
        if not is_studio_owner(actor):
            raise ValueError("Solo il proprietario dello studio puo modificare i dati dello studio")
        studio_name = form.get("studio_name", "").strip()
        if not studio_name:
            raise ValueError("Inserisci il nome dello studio")
        conn = connect()
        studio = primary_studio(conn)
        if not studio:
            conn.close()
            raise ValueError("Studio non configurato")
        studio_id = int(studio["id"])
        logo_url = studio["logo_path"] if row_has(studio, "logo_path") else ""
        logo_data = form.get("studio_logo_data", "").strip()
        if logo_data:
            logo_url = save_studio_logo_image(studio_id, logo_data)
        conn.execute(
            """
            UPDATE studios
            SET name = ?, email = ?, phone = ?, address = ?, tax_id = ?, logo_path = ?
            WHERE id = ?
            """,
            (
                studio_name,
                form.get("studio_email", "").strip(),
                normalize_phone(form.get("studio_phone", "")),
                form.get("studio_address", "").strip(),
                form.get("studio_tax_id", "").strip(),
                logo_url,
                studio_id,
            ),
        )
        conn.commit()
        conn.close()
        log_event("admin_studio_settings", f"Studio aggiornato: {studio_name}", actor["id"] if actor else None)
        self.flash_redirect("/profile", "Studio aggiornato")

    def admin_stripe_settings(self, form: dict[str, str]) -> None:
        actor = get_user_from_cookie(self.headers)
        if not is_studio_owner(actor):
            raise ValueError("Solo il proprietario dello studio puo modificare Stripe")
        if not check_rate_limit(f"stripe-settings:{self.client_address[0]}", 8, 60 * 60):
            raise ValueError("Troppi tentativi di configurazione Stripe. Riprova piu tardi")
        key = form.get("stripe_secret", "").strip()
        webhook_secret = form.get("stripe_webhook_secret", "").strip()
        changed = []
        if key:
            if stripe_secret_source() == "env":
                raise ValueError("Stripe gestito dal deploy")
            if not key.startswith(("sk_test_", "sk_live_")):
                raise ValueError("Chiave Stripe non valida")
            account_id = ""
            try:
                account_id = validate_stripe_secret_key(key)
            except ValueError:
                account_id = ""
            save_stripe_secret_key(key)
            if account_id:
                set_setting("stripe_account_id", account_id)
            changed.append("chiave")
        elif not stripe_secret_key() and not webhook_secret:
            raise ValueError("Inserisci la chiave privata Stripe")
        if webhook_secret:
            if stripe_webhook_source() == "env":
                raise ValueError("Webhook Stripe gestito dal deploy")
            if not webhook_secret.startswith("whsec_"):
                raise ValueError("Webhook Stripe non valido")
            save_stripe_webhook_secret(webhook_secret)
            changed.append("webhook")
        if not changed:
            self.flash_redirect("/profile", "Configurazione Stripe gia salvata")
            return
        mode = "test" if stripe_secret_key().startswith("sk_test_") else ("live" if stripe_secret_key().startswith("sk_live_") else "")
        suffix = f" ({mode})" if mode else ""
        log_event("admin_stripe_settings", f"Configurazione Stripe aggiornata{suffix}", actor["id"] if actor else None)
        self.flash_redirect("/profile", f"Configurazione Stripe aggiornata{suffix}")
    def admin_email_settings(self, form: dict[str, str]) -> None:
        actor = get_user_from_cookie(self.headers)
        if not is_studio_owner(actor):
            raise ValueError("Solo il proprietario dello studio puo modificare email")
        existing = load_email_config()
        host = form.get("host", "").strip()
        from_email = form.get("from_email", "").lower().strip()
        port_value = form.get("port", "587").strip() or "587"
        try:
            port = int(port_value)
        except ValueError as exc:
            raise ValueError("Porta SMTP non valida") from exc
        if port < 1 or port > 65535:
            raise ValueError("Porta SMTP non valida")
        if not host or not from_email:
            raise ValueError("Inserisci server SMTP ed email mittente")
        password = form.get("password", "")
        base_url = form.get("base_url", "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("URL pubblica app non valida")
        config = {
            "host": host,
            "port": str(port),
            "tls": "1" if form.get("tls", "1") != "0" else "0",
            "from_email": from_email,
            "username": form.get("username", "").strip(),
            "password": password if password else existing.get("password", ""),
            "base_url": base_url,
        }
        save_email_config(config)
        log_event("admin_email_settings", "Configurazione email aggiornata", actor["id"] if actor else None)
        self.flash_redirect("/profile", "Configurazione email salvata")

    def admin_email_test(self, form: dict[str, str]) -> None:
        actor = get_user_from_cookie(self.headers)
        if not is_studio_owner(actor):
            raise ValueError("Solo il proprietario dello studio puo testare email")
        to_email = form.get("email", "").lower().strip()
        if not to_email or "@" not in to_email:
            raise ValueError("Inserisci una email test valida")
        brand_name = studio_display_name(APP_NAME)
        body = f"Questa email conferma che il reset password di {brand_name} e configurato correttamente."
        send_email(to_email, f"Test email {brand_name}", body, allow_outbox=False)
        self.flash_redirect("/profile", "Email di test inviata")

    def forgot_password(self, form: dict[str, str]) -> None:
        email = form.get("email", "").lower().strip()
        if not check_rate_limit(f"reset:{self.client_address[0]}:{email}", 5, 60 * 60):
            raise ValueError("Troppi tentativi di reset. Riprova piu tardi")
        conn = connect()
        conn.execute("DELETE FROM password_resets WHERE expires_at < ?", ((now() - dt.timedelta(days=7)).isoformat(),))
        user = conn.execute("SELECT id, first_name, email FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            token_hash = reset_token_digest(token)
            issued_at = now().isoformat()
            conn.execute(
                "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                (issued_at, user["id"]),
            )
            conn.execute(
                "INSERT INTO password_resets (user_id, token, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], token_hash, token_hash, (now() + dt.timedelta(minutes=30)).isoformat(), issued_at),
            )
            conn.commit()
            reset_link = f"{self.base_url()}/reset-password?token={quote(token)}"
            outbox_path = send_email(user["email"], f"Reimposta password {studio_display_name(APP_NAME)}", reset_email_body(user["first_name"], reset_link))
            if outbox_path:
                log_event("password_reset_outbox", f"Email reset salvata in {outbox_path}", user["id"])
        conn.commit()
        conn.close()
        self.redirect("/login?flash=Se%20l'email%20esiste,%20riceverai%20il%20link%20di%20reset")

    def reset_password(self, form: dict[str, str]) -> None:
        token = form.get("token", "")
        password = form.get("password", "")
        confirm_password = form.get("confirm_password", "")
        if not check_rate_limit(f"reset-submit:{self.client_address[0]}", 10, 60 * 60):
            raise ValueError("Troppi tentativi di reset. Riprova piu tardi")
        if password != confirm_password:
            raise ValueError("Le password non coincidono")
        if len(password) < 8:
            raise ValueError("Password troppo corta")
        token_hash = reset_token_digest(token)
        conn = connect()
        row = conn.execute(
            """
            SELECT pr.*, u.id AS user_id
            FROM password_resets pr JOIN users u ON u.id = pr.user_id
            WHERE (pr.token_hash = ? OR pr.token = ?) AND pr.used_at IS NULL
            """,
            (token_hash, token),
        ).fetchone()
        if not row or dt.datetime.fromisoformat(row["expires_at"]) < now():
            conn.close()
            raise ValueError("Link reset non valido o scaduto")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row["user_id"]))
        conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now().isoformat(), row["user_id"]),
        )
        conn.commit()
        conn.close()
        self.redirect("/login?flash=Password%20aggiornata%20con%20successo.%20Puoi%20accedere", clear=True)

    def login(self, form: dict[str, str]) -> None:
        if form.get("login_confirmed") != "1":
            self.html(login_page("Premi Entra per accedere."))
            return
        email = form.get("email", "").lower().strip()
        if not check_rate_limit(f"login:{self.client_address[0]}:{email}", 8, 15 * 60):
            raise ValueError("Troppi tentativi di accesso. Riprova tra qualche minuto")
        conn = connect()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not verify_password(form["password"], user["password_hash"]):
            log_event("login_failed", f"Tentativo fallito per {email}", user["id"] if user else None)
            conn.close()
            self.html(login_page("Credenziali non valide."))
            return
        if not account_is_active(user):
            log_event("login_archived", f"Tentativo accesso account archiviato {email}", user["id"])
            conn.close()
            self.html(login_page("Account non attivo."))
            return
        if password_needs_rehash(user["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(form["password"]), user["id"]))
            conn.commit()
        log_event("login_success", "Accesso effettuato", user["id"])
        conn.close()
        remember = form.get("remember") == "1"
        self.redirect("/", token=make_token(user["id"], remember), max_age=(30 * 24 * 60 * 60 if remember else None))

    def register(self, form: dict[str, str]) -> None:
        if form.get("gdpr") != "1":
            raise ValueError("Consenso GDPR obbligatorio")
        account_type = form.get("account_type", "patient").strip()
        is_doctor_registration = account_type == "doctor"
        role = "doctor" if is_doctor_registration else "user"
        permissions = DOCTOR_PERMISSION if is_doctor_registration else PATIENT_PERMISSION
        doctor_qualification_value = form.get("doctor_qualification", "").strip()
        if is_doctor_registration and not doctor_qualification_value:
            raise ValueError("Inserisci la qualifica professionale")
        try:
            years_experience = max(int(form.get("doctor_years_experience", "0") or 0), 0)
        except ValueError as exc:
            raise ValueError("Anni di esperienza non validi") from exc
        conn = connect()
        studio = primary_studio(conn)
        studio_id = int(studio["id"]) if studio else None
        try:
            cur = conn.execute(
                """
                INSERT INTO users
                (role, studio_id, permissions, account_status, bookable, profile_visible,
                 first_name, last_name, email, password_hash, phone, fiscal_code,
                 gdpr_consent, privacy_accepted_at, privacy_version, email_verified, phone_verified, created_at,
                 doctor_bio, doctor_years_experience, doctor_degree, doctor_qualification, doctor_gender,
                 doctor_location, doctor_onboarded_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    role,
                    studio_id,
                    permissions,
                    1 if is_doctor_registration else 0,
                    1 if is_doctor_registration else 0,
                    form["first_name"].strip(),
                    form["last_name"].strip(),
                    form["email"].lower().strip(),
                    hash_password(form["password"]),
                    normalize_phone(form["phone"]),
                    form["fiscal_code"].upper().strip(),
                    now().isoformat(),
                    PRIVACY_VERSION,
                    now().isoformat(),
                    form.get("doctor_bio", "").strip() if is_doctor_registration else "",
                    years_experience if is_doctor_registration else 0,
                    form.get("doctor_degree", "").strip() if is_doctor_registration else "",
                    doctor_qualification_value if is_doctor_registration else "",
                    form.get("doctor_gender", "").strip() if is_doctor_registration else "",
                    form.get("doctor_location", "").strip() if is_doctor_registration else "",
                    now().isoformat() if is_doctor_registration else None,
                ),
            )
            user_id = cur.lastrowid
            if is_doctor_registration and form.get("doctor_profile_image_data"):
                image_url = save_doctor_profile_image(user_id, form.get("doctor_profile_image_data", ""))
                conn.execute("UPDATE users SET doctor_profile_image = ? WHERE id = ?", (image_url, user_id))
            conn.commit()
        except db_integrity_error_types():
            conn.close()
            self.html(login_page("Email gia registrata."))
            return
        conn.close()
        if is_doctor_registration:
            ensure_slots(today(), days=14, doctor_id=int(user_id))
        log_event("email_verification", "Link verifica email simulato inviato", user_id)
        log_event("phone_otp", "OTP telefono simulato inviato", user_id)
        self.redirect("/", token=make_token(user_id))

    def create_booking(self, user: sqlite3.Row, form: dict[str, str]) -> None:
        if not has_signed_consent(user):
            raise ValueError("Firma il consenso informato prima di prenotare")
        slot_id = int(form["slot_id"])
        service_type_id = int(form.get("service_type_id", "0"))
        doctor_id = int(form.get("doctor_id", "0") or 0)
        selected_doctor = doctor_by_id(doctor_id)
        if not selected_doctor:
            raise ValueError("Seleziona uno specialista valido")
        conn = connect()
        service = conn.execute(
            "SELECT * FROM service_types WHERE id = ? AND active = 1 AND doctor_id = ?",
            (service_type_id, doctor_id),
        ).fetchone()
        if not service:
            conn.close()
            raise ValueError("Seleziona una tipologia servizio valida")
        slot = conn.execute("SELECT * FROM slots WHERE id = ? AND doctor_id = ?", (slot_id, doctor_id)).fetchone()
        if not slot:
            conn.close()
            raise ValueError("Slot non trovato")
        if parse_dt(slot["slot_date"], slot["slot_time"]) < now():
            conn.close()
            raise ValueError("Errore: hai provato a selezionare una data non valida")
        if parse_date(slot["slot_date"]) > today() + dt.timedelta(days=MAX_BOOKING_DAYS):
            conn.close()
            raise ValueError("Prenotazione troppo avanti nel tempo")
        booked = conn.execute(
            "SELECT COUNT(*) AS total FROM appointments WHERE slot_id = ? AND doctor_id = ? AND status = 'prenotata'",
            (slot_id, doctor_id),
        ).fetchone()["total"]
        if slot["blocked"] or booked >= slot["capacity"]:
            conn.close()
            raise ValueError("Slot completo")
        move_id = form.get("move_id")
        if move_id:
            old = conn.execute(
                """
                SELECT a.id, s.slot_date, s.slot_time
                FROM appointments a JOIN slots s ON s.id = a.slot_id
                WHERE a.id = ? AND a.user_id = ? AND a.status = 'prenotata' AND a.doctor_id = ?
                """,
                (int(move_id), user["id"], doctor_id),
            ).fetchone()
            if not old:
                conn.close()
                raise ValueError("Appuntamento da spostare non trovato")
            if now() > parse_dt(old["slot_date"], old["slot_time"]) - dt.timedelta(hours=CANCEL_LIMIT_HOURS):
                conn.close()
                raise ValueError("Spostamento non consentito nell'ultima ora")
            conn.execute(
                "UPDATE appointments SET status = 'cancellata', cancelled_at = ? WHERE id = ? AND user_id = ?",
                (now().isoformat(), int(move_id), user["id"]),
            )
        price = DEFAULT_APPOINTMENT_PRICE
        consent_version_snapshot = user["consent_version"] if row_has(user, "consent_version") and user["consent_version"] else CONSENT_VERSION
        conn.execute(
            """
            INSERT INTO appointments
            (user_id, doctor_id, slot_id, service_type_id,
             doctor_name_snapshot, doctor_qualification_snapshot, service_name_snapshot,
             service_description_snapshot, price_snapshot, consent_version_snapshot,
             status, created_at, price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prenotata', ?, ?)
            """,
            (
                user["id"],
                doctor_id,
                slot_id,
                service_type_id,
                doctor_display_name(selected_doctor),
                doctor_qualification(selected_doctor),
                service["name"],
                service["description"] if "description" in service.keys() else "",
                price,
                consent_version_snapshot,
                now().isoformat(),
                price,
            ),
        )
        conn.commit()
        conn.close()
        log_event("booking_confirmation", f"Prenotazione confermata per {slot['slot_date']} {slot['slot_time']}", user["id"])
        self.redirect("/?flash=Prenotazione confermata")

    def cancel_booking(self, user: sqlite3.Row, form: dict[str, str]) -> None:
        app_id = int(form["id"])
        conn = connect()
        row = conn.execute(
            """
            SELECT a.*, s.slot_date, s.slot_time
            FROM appointments a JOIN slots s ON s.id = a.slot_id
            WHERE a.id = ? AND a.user_id = ?
            """,
            (app_id, user["id"]),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("Appuntamento non trovato")
        if now() > parse_dt(row["slot_date"], row["slot_time"]) - dt.timedelta(hours=CANCEL_LIMIT_HOURS):
            conn.close()
            raise ValueError("Cancellazione non consentita nell'ultima ora")
        conn.execute(
            "UPDATE appointments SET status = 'cancellata', cancelled_at = ? WHERE id = ?",
            (now().isoformat(), app_id),
        )
        conn.commit()
        conn.close()
        log_event("booking_cancelled", "Appuntamento cancellato", user["id"])
        self.redirect("/?flash=Appuntamento cancellato")

    def checkin(self, user: sqlite3.Row, form: dict[str, str]) -> None:
        app_id = int(form["id"])
        conn = connect()
        row = conn.execute(
            """
            SELECT a.*, s.slot_date, s.slot_time
            FROM appointments a JOIN slots s ON s.id = a.slot_id
            WHERE a.id = ? AND a.user_id = ?
            """,
            (app_id, user["id"]),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("Appuntamento non trovato")
        if row["status"] != "prenotata":
            conn.close()
            self.redirect("/scan?flash=Seduta%20gia%20gestita")
            return
        appointment_time = parse_dt(row["slot_date"], row["slot_time"])
        if not (appointment_time - dt.timedelta(hours=2) <= now() <= appointment_time):
            conn.close()
            raise ValueError("Check-in attivo solo nelle 2 ore precedenti")
        conn.execute("UPDATE appointments SET checked_in_at = ? WHERE id = ?", (now().isoformat(), app_id))
        conn.commit()
        conn.close()
        self.redirect("/?flash=Check-in registrato")

    def update_profile(self, user: sqlite3.Row, form: dict[str, str]) -> None:
        conn = connect()
        password = form.get("password", "")
        doctor_image_update = ""
        if is_doctor_account(user):
            if form.get("remove_doctor_profile_image") == "1":
                doctor_image_update = ""
            elif form.get("doctor_profile_image_data"):
                doctor_image_update = save_doctor_profile_image(int(user["id"]), form.get("doctor_profile_image_data", ""))
            try:
                doctor_years = max(int(form.get("doctor_years_experience", "0") or 0), 0)
            except ValueError as exc:
                conn.close()
                raise ValueError("Anni di esperienza non validi") from exc
            doctor_stripe_account = form.get("doctor_stripe_account", "").strip()
            if doctor_stripe_account and not doctor_stripe_account.startswith("acct_"):
                conn.close()
                raise ValueError("Account Stripe medico non valido")
            image_sql = ", doctor_profile_image = ?" if doctor_image_update or form.get("remove_doctor_profile_image") == "1" else ""
            params: list[Any] = [
                form["email"].lower(),
                normalize_phone(form["phone"]),
                form.get("doctor_bio", "").strip(),
                doctor_years,
                form.get("doctor_degree", "").strip(),
                form.get("doctor_qualification", "").strip(),
                form.get("doctor_gender", "").strip(),
                form.get("doctor_location", "").strip(),
                doctor_stripe_account,
            ]
            if password:
                password_sql = ", password_hash = ?"
                params.append(hash_password(password))
            else:
                password_sql = ""
            if image_sql:
                params.append(doctor_image_update)
            params.append(user["id"])
            conn.execute(
                f"""
                UPDATE users
                SET email = ?, phone = ?, doctor_bio = ?, doctor_years_experience = ?,
                    doctor_degree = ?, doctor_qualification = ?, doctor_gender = ?,
                    doctor_location = ?, doctor_stripe_account = ?{password_sql}{image_sql}
                WHERE id = ?
                """,
                tuple(params),
            )
            conn.commit()
            conn.close()
            self.redirect("/profile?flash=Profilo%20medico%20aggiornato")
            return
        if password:
            conn.execute(
                "UPDATE users SET email = ?, phone = ?, password_hash = ? WHERE id = ?",
                (form["email"].lower(), normalize_phone(form["phone"]), hash_password(password), user["id"]),
            )
        else:
            conn.execute(
                "UPDATE users SET email = ?, phone = ? WHERE id = ?",
                (form["email"].lower(), normalize_phone(form["phone"]), user["id"]),
            )
        conn.commit()
        conn.close()
        self.redirect("/profile?flash=Dati aggiornati")

    def admin_status(self, form: dict[str, str]) -> None:
        status = form["status"]
        if status not in {"effettuata", "non_presentato"}:
            raise ValueError("Stato non valido")
        app_id = int(form["id"])
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute("SELECT status FROM appointments WHERE id = ? AND doctor_id = ?", (app_id, doctor["id"])).fetchone()
        if not row:
            conn.close()
            raise ValueError("Seduta non trovata")
        if row["status"] != "prenotata":
            conn.close()
            self.flash_redirect(self.headers.get("Referer", "/"), "Seduta gia gestita")
            return
        conn.execute(
            "UPDATE appointments SET status = ?, auto_suggestion = NULL WHERE id = ?",
            (status, app_id),
        )
        conn.commit()
        conn.close()
        log_event("admin_status", f"Seduta {app_id} impostata a {status}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.flash_redirect(self.headers.get("Referer", "/"), "Stato confermato")

    def admin_slot(self, form: dict[str, str]) -> None:
        capacity = max(int(form["capacity"]), 0)
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        conn.execute("UPDATE slots SET capacity = ?, blocked = 0 WHERE id = ? AND doctor_id = ?", (capacity, int(form["id"]), doctor["id"]))
        conn.commit()
        conn.close()
        log_event("admin_slot", f"Slot {int(form['id'])} aggiornato a capacita {capacity}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        referer = self.headers.get("Referer", "/slots")
        self.flash_redirect(referer, "Slot aggiornato")

    def admin_default_capacity(self, form: dict[str, str]) -> None:
        capacity = max(int(form["capacity"]), 0)
        set_setting("default_capacity", str(capacity))
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        conn.execute("UPDATE slots SET capacity = ?, blocked = 0 WHERE doctor_id = ?", (capacity, doctor["id"]))
        conn.commit()
        conn.close()
        log_event("admin_default_capacity", f"Capacita default aggiornata a {capacity}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        referer = self.headers.get("Referer", "/slots")
        self.flash_redirect(referer, "Limite di default aggiornato")
    def admin_payment(self, form: dict[str, str]) -> None:
        app_id = int(form["appointment_id"])
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute(
            """
            SELECT a.id, a.price, a.status, COALESCE(SUM(p.amount), 0) AS paid
            FROM appointments a
            LEFT JOIN payments p ON p.appointment_id = a.id
            WHERE a.id = ? AND a.doctor_id = ?
            GROUP BY a.id
            """,
            (app_id, doctor["id"]),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("Appuntamento non trovato")
        residual = max(payable_amount(row["status"], row["price"]) - float(row["paid"] or 0), 0)
        if residual <= 0:
            conn.close()
            raise ValueError("Seduta gia pagata o pagamento non dovuto")
        conn.execute(
            "INSERT INTO payments (appointment_id, paid_at, amount, method) VALUES (?, ?, ?, ?)",
            (app_id, now().isoformat(), residual, form["method"]),
        )
        conn.commit()
        conn.close()
        log_event("admin_payment", f"Pagamento manuale seduta {app_id} per EUR {residual:.2f}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        patient_id = form.get("patient_id", "")
        if patient_id:
            self.redirect(f"/patient?id={int(patient_id)}&flash=Pagamento%20registrato")
        else:
            self.redirect("/?flash=Pagamento%20registrato")


    def admin_mark_paid(self, form: dict[str, str]) -> None:
        app_id = int(form["id"])
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute(
            """
            SELECT a.id, a.user_id, a.price, a.status, COALESCE(SUM(p.amount), 0) AS paid
            FROM appointments a
            LEFT JOIN payments p ON p.appointment_id = a.id
            WHERE a.id = ? AND a.doctor_id = ?
            GROUP BY a.id
            """,
            (app_id, doctor["id"]),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("Appuntamento non trovato")
        due = billable_amount(row["status"], row["price"])
        if due <= 0:
            conn.close()
            raise ValueError("Pagamento non dovuto per questa seduta")
        residual = max(due - float(row["paid"] or 0), 0)
        return_to = form.get("return_to", "").strip()
        target = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/patient?id={row['user_id']}"
        if residual <= 0:
            conn.close()
            self.flash_redirect(target, "Seduta gia pagata")
            return
        conn.execute(
            "INSERT INTO payments (appointment_id, paid_at, amount, method) VALUES (?, ?, ?, ?)",
            (app_id, now().isoformat(), residual, "Segna come pagato"),
        )
        conn.commit()
        conn.close()
        log_event("admin_mark_paid", f"Seduta {app_id} segnata come pagata", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.flash_redirect(target, "Seduta segnata come pagata")
    def admin_price(self, form: dict[str, str]) -> None:
        price = max(float(form["price"]), 0)
        app_id = int(form["id"])
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute("SELECT user_id FROM appointments WHERE id = ? AND doctor_id = ?", (app_id, doctor["id"])).fetchone()
        if not row:
            conn.close()
            raise ValueError("Appuntamento non trovato")
        conn.execute("UPDATE appointments SET price = ? WHERE id = ? AND doctor_id = ?", (price, app_id, doctor["id"]))
        conn.commit()
        conn.close()
        log_event("admin_price", f"Importo seduta {app_id} aggiornato a EUR {price:.2f}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.flash_redirect(f"/patient?id={row['user_id']}", "Importo aggiornato")

    def admin_cancel(self, form: dict[str, str]) -> None:
        app_id = int(form["id"])
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute("SELECT user_id FROM appointments WHERE id = ? AND doctor_id = ?", (app_id, doctor["id"])).fetchone()
        if not row:
            conn.close()
            raise ValueError("Appuntamento non trovato")
        conn.execute(
            "UPDATE appointments SET status = 'cancellata', cancelled_at = ?, auto_suggestion = NULL WHERE id = ? AND doctor_id = ?",
            (now().isoformat(), app_id, doctor["id"]),
        )
        conn.commit()
        conn.close()
        log_event("admin_cancel", f"Seduta {app_id} cancellata da admin", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        referer = self.headers.get("Referer", "/")
        self.flash_redirect(referer, "Seduta eliminata")

    def admin_service_add(self, form: dict[str, str]) -> None:
        name = form.get("name", "").strip()
        if not name:
            raise ValueError("Inserisci il nome della tipologia")
        doctor = get_user_from_cookie(self.headers)
        if not is_doctor_account(doctor):
            raise ValueError("Accesso medico richiesto")
        conn = connect()
        existing = conn.execute(
            "SELECT id FROM service_types WHERE lower(name) = lower(?) AND doctor_id = ?",
            (name, doctor["id"]),
        ).fetchone()
        if existing:
            conn.execute("UPDATE service_types SET active = 1 WHERE id = ? AND doctor_id = ?", (existing["id"], doctor["id"]))
        else:
            conn.execute(
                "INSERT INTO service_types (name, doctor_id, active, created_at) VALUES (?, ?, 1, ?)",
                (name, doctor["id"], now().isoformat()),
            )
        conn.commit()
        conn.close()
        log_event("admin_service_add", f"Tipologia servizio aggiornata: {name}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.redirect("/services?flash=Tipologia aggiornata")

    def admin_service_delete(self, form: dict[str, str]) -> None:
        service_id = int(form["id"])
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        conn.execute(
            "UPDATE service_types SET active = 0, archived_at = ? WHERE id = ? AND doctor_id = ?",
            (now().isoformat(), service_id, doctor["id"]),
        )
        conn.commit()
        conn.close()
        log_event("admin_service_delete", f"Tipologia servizio {service_id} disattivata", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.redirect("/services?flash=Tipologia eliminata")

    def admin_patient_delete(self, form: dict[str, str]) -> None:
        patient_id = int(form.get("id", "0"))
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        patient = conn.execute(
            "SELECT * FROM users WHERE id = ? AND role = 'user' AND COALESCE(account_status, 'active') = 'active'",
            (patient_id,),
        ).fetchone()
        if not patient:
            conn.close()
            raise ValueError("Paziente non trovato")
        linked = conn.execute("SELECT 1 FROM appointments WHERE user_id = ? AND doctor_id = ? LIMIT 1", (patient_id, doctor["id"])).fetchone()
        if not linked:
            conn.close()
            raise ValueError("Paziente non collegato al medico corrente")
        other_doctors = conn.execute(
            "SELECT COUNT(DISTINCT doctor_id) AS total FROM appointments WHERE user_id = ? AND doctor_id IS NOT NULL AND doctor_id != ?",
            (patient_id, doctor["id"]),
        ).fetchone()["total"]
        if int(other_doctors or 0) > 0:
            conn.close()
            raise ValueError("Questo paziente ha sedute con altri medici: elimina solo dalla scheda corretta o chiedi una gestione centralizzata")
        archived_at = now().isoformat()
        archived_email = f"archived-patient-{patient_id}@rehab.local"
        archived_phone = ""
        archived_reason = form.get("reason", "").strip() or "Archiviazione da scheda paziente"
        archived_label = f"Paziente archiviato #{patient_id}"
        conn.execute(
            """
            UPDATE users
            SET account_status = 'archived',
                archived_at = ?,
                archived_reason = ?,
                email = ?,
                phone = ?,
                password_hash = ?,
                first_name = 'Paziente',
                last_name = ?,
                gdpr_consent = 0,
                email_verified = 0,
                phone_verified = 0
            WHERE id = ? AND role = 'user'
            """,
            (
                archived_at,
                archived_reason,
                archived_email,
                archived_phone,
                hash_password(secrets.token_urlsafe(32)),
                f"archiviato {patient_id}",
                patient_id,
            ),
        )
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (patient_id,))
        conn.commit()
        conn.close()

        admin = get_user_from_cookie(self.headers)
        label = f"{patient['first_name']} {patient['last_name']}"
        log_event(
            "admin_patient_archive",
            f"Paziente archiviato: {label} (id {patient_id})",
            admin["id"] if admin else None,
            target_type="patient",
            target_id=patient_id,
            metadata={"reason": archived_reason, "label": archived_label},
        )
        self.flash_redirect("/patients", "Paziente archiviato")

    def admin_doctor_archive(self, form: dict[str, str]) -> None:
        owner = get_user_from_cookie(self.headers)
        if not is_studio_owner(owner):
            raise ValueError("Solo il proprietario dello studio puo archiviare un medico")
        doctor_id = int(form.get("id", "0"))
        if doctor_id == int(owner["id"]):
            raise ValueError("Non puoi archiviare il tuo stesso profilo")
        conn = connect()
        doctor = conn.execute(
            "SELECT * FROM users WHERE id = ? AND role IN ('admin', 'doctor') AND COALESCE(account_status, 'active') = 'active'",
            (doctor_id,),
        ).fetchone()
        if not doctor:
            conn.close()
            raise ValueError("Medico non trovato")
        future_active = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM appointments a
            JOIN slots s ON s.id = a.slot_id
            WHERE a.doctor_id = ? AND a.status = 'prenotata'
              AND (s.slot_date > ? OR (s.slot_date = ? AND s.slot_time >= ?))
            """,
            (doctor_id, today().isoformat(), today().isoformat(), now().strftime("%H:%M")),
        ).fetchone()["total"]
        if int(future_active or 0) > 0:
            conn.close()
            raise ValueError("Prima sposta o cancella le prenotazioni future di questo medico")
        conn.execute(
            """
            UPDATE users
            SET account_status = 'archived',
                archived_at = ?,
                archived_reason = ?,
                bookable = 0,
                profile_visible = 0,
                password_hash = ?
            WHERE id = ?
            """,
            (
                now().isoformat(),
                form.get("reason", "").strip() or "Medico non piu attivo nello studio",
                hash_password(secrets.token_urlsafe(32)),
                doctor_id,
            ),
        )
        conn.execute("UPDATE slots SET blocked = 1, archived_at = ? WHERE doctor_id = ? AND slot_date >= ?", (now().isoformat(), doctor_id, today().isoformat()))
        conn.commit()
        conn.close()
        log_event(
            "admin_doctor_archive",
            f"Medico archiviato: {doctor_display_name(doctor)} (id {doctor_id})",
            owner["id"],
            target_type="doctor",
            target_id=doctor_id,
        )
        self.flash_redirect("/profile", "Medico archiviato")

    def admin_doctor_stripe(self, form: dict[str, str]) -> None:
        owner = get_user_from_cookie(self.headers)
        if not is_studio_owner(owner):
            raise ValueError("Solo il proprietario dello studio puo modificare Stripe dei medici")
        doctor_id = int(form.get("id", "0"))
        account_id = form.get("doctor_stripe_account", "").strip()
        if account_id and not account_id.startswith("acct_"):
            raise ValueError("Account Stripe medico non valido")
        conn = connect()
        doctor = conn.execute(
            "SELECT * FROM users WHERE id = ? AND role IN ('admin', 'doctor')",
            (doctor_id,),
        ).fetchone()
        if not doctor:
            conn.close()
            raise ValueError("Medico non trovato")
        conn.execute("UPDATE users SET doctor_stripe_account = ? WHERE id = ?", (account_id, doctor_id))
        conn.commit()
        conn.close()
        log_event(
            "admin_doctor_stripe",
            f"Stripe medico aggiornato: {doctor_display_name(doctor)}",
            owner["id"],
            target_type="doctor",
            target_id=doctor_id,
        )
        self.flash_redirect("/profile", "Stripe medico aggiornato")

    def sign_consent(self, user: sqlite3.Row, form: dict[str, str]) -> None:
        if user["role"] != "user":
            raise ValueError("Consenso disponibile solo per i pazienti")
        minor_or_dependent = form.get("minor_or_dependent") == "1"
        data = {key: form.get(key, "").strip() for key in [
            "first_name", "last_name", "email", "phone", "fiscal_code", "birth_place",
            "birth_date", "residence_city", "residence_cap", "address", "guardian_first_name",
            "guardian_last_name", "guardian_birth_place", "guardian_birth_date",
            "guardian_residence_city", "guardian_fiscal_code", "guardian_phone", "guardian_email",
            "guardian_relation_type", "guardian_relation_other", "treatment_plan", "signature_data",
            "consent_information", "consent_treatment", "consent_data",
        ]}
        data["minor_or_dependent"] = "1" if minor_or_dependent else "0"
        required = [
            "first_name", "last_name", "email", "phone", "fiscal_code", "birth_place",
            "birth_date", "residence_city", "residence_cap", "address", "treatment_plan",
            "signature_data", "consent_information", "consent_treatment", "consent_data",
        ]
        if minor_or_dependent:
            required.extend([
                "guardian_first_name", "guardian_last_name", "guardian_birth_place",
                "guardian_birth_date", "guardian_residence_city", "guardian_fiscal_code",
                "guardian_phone", "guardian_email", "guardian_relation_type",
            ])
            if data.get("guardian_relation_type") == "other":
                required.append("guardian_relation_other")
        missing = [field for field in required if not data.get(field)]
        if missing:
            raise ValueError("Compila tutti i campi obbligatori del consenso")
        if not data["signature_data"].startswith("data:image/"):
            raise ValueError("Disegna la firma prima di salvare il consenso")
        guardian_full_name = " ".join(
            part for part in [data.get("guardian_first_name"), data.get("guardian_last_name")] if part
        )
        guardian_relation = data.get("guardian_relation_other") if data.get("guardian_relation_type") == "other" else data.get("guardian_relation_type", "")
        data["guardian_name"] = guardian_full_name
        data["guardian_relation"] = guardian_relation
        file_path = generate_consent_pdf(user, data)
        consent_file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        consent_file_value = consent_filename(data["first_name"], data["last_name"]) if is_serverless_runtime() else str(file_path)
        conn = connect()
        conn.execute(
            """
            UPDATE users
            SET first_name = ?, last_name = ?, email = ?, phone = ?, fiscal_code = ?,
                birth_place = ?, birth_date = ?, residence_city = ?, residence_cap = ?, address = ?,
                minor_or_dependent = ?, guardian_name = ?, guardian_first_name = ?, guardian_last_name = ?,
                guardian_fiscal_code = ?, guardian_relation = ?, guardian_birth_place = ?, guardian_birth_date = ?, guardian_residence_city = ?,
                guardian_phone = ?, guardian_email = ?, guardian_relation_type = ?,
                consent_signature_data = ?, consent_signed_at = ?, consent_version = ?, consent_file = ?, consent_file_hash = ?
            WHERE id = ?
            """,
            (
                data["first_name"],
                data["last_name"],
                data["email"].lower(),
                normalize_phone(data["phone"]),
                data["fiscal_code"].upper(),
                data.get("birth_place", ""),
                data.get("birth_date", ""),
                data.get("residence_city", ""),
                data.get("residence_cap", ""),
                data.get("address", ""),
                1 if minor_or_dependent else 0,
                guardian_full_name,
                data.get("guardian_first_name", ""),
                data.get("guardian_last_name", ""),
                data.get("guardian_fiscal_code", "").upper(),
                guardian_relation,
                data.get("guardian_birth_place", ""),
                data.get("guardian_birth_date", ""),
                data.get("guardian_residence_city", ""),
                normalize_phone(data.get("guardian_phone", "")) if data.get("guardian_phone") else "",
                data.get("guardian_email", "").lower(),
                data.get("guardian_relation_type", ""),
                data["signature_data"],
                now().isoformat(),
                CONSENT_VERSION,
                consent_file_value,
                consent_file_hash,
                user["id"],
            ),
        )
        conn.commit()
        conn.close()
        log_event("consent_signed", f"Consenso informato salvato ({CONSENT_VERSION})", user["id"])
        self.redirect("/profile?flash=Consenso%20informato%20salvato")

    def create_payment_checkout(self, user: sqlite3.Row, form: dict[str, str]) -> None:
        if user["role"] != "user":
            raise ValueError("Pagamento disponibile solo per i pazienti")
        if not check_rate_limit(f"checkout:{self.client_address[0]}:{user['id']}", 12, 10 * 60):
            raise ValueError("Troppi tentativi di pagamento. Riprova tra qualche minuto")
        app_id = int(form["id"])
        conn = connect()
        app = conn.execute(
            """
            SELECT a.*, s.slot_date, s.slot_time, st.name AS service_type_name,
                   COALESCE(SUM(p.amount), 0) AS paid
            FROM appointments a
            JOIN slots s ON s.id = a.slot_id
            LEFT JOIN service_types st ON st.id = a.service_type_id
            LEFT JOIN payments p ON p.appointment_id = a.id
            WHERE a.id = ? AND a.user_id = ?
            GROUP BY a.id
            """,
            (app_id, user["id"]),
        ).fetchone()
        conn.close()
        if not app:
            raise ValueError("Seduta non trovata")
        due = online_payable_amount(app["status"], app["price"])
        residual = max(due - float(app["paid"] or 0), 0)
        if residual <= 0:
            self.redirect("/profile?flash=Seduta%20gia%20pagata")
            return
        if not stripe_configured():
            raise ValueError("Stripe non configurato: aggiungi STRIPE_SECRET_KEY o stripe_secret_key.txt nella cartella dell'app")
        stripe.api_key = stripe_secret_key()
        connect_destination = doctor_stripe_connect_account(app["doctor_id"])
        if not connect_destination:
            raise ValueError("Account Stripe del medico non configurato")
        log_event("stripe_checkout_created", f"Checkout Stripe creato per seduta {app_id}", user["id"])
        stripe_metadata = {"appointment_id": str(app_id), "user_id": str(user["id"])}
        session_payload = {
            "mode": "payment",
            "line_items": [
                {
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": f"Seduta #{app_id} - {studio_display_name(APP_NAME)}"},
                        "unit_amount": int(round(residual * 100)),
                    },
                    "quantity": 1,
                }
            ],
            "client_reference_id": str(app_id),
            "customer_email": user["email"],
            "locale": "it",
            "metadata": dict(stripe_metadata),
            "payment_intent_data": {"metadata": dict(stripe_metadata)},
            "success_url": f"{self.base_url()}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{self.base_url()}/payment/cancel",
        }
        if connect_destination:
            session_payload["payment_intent_data"]["transfer_data"] = {"destination": connect_destination}
            session_payload["metadata"]["stripe_destination_account"] = connect_destination
            session_payload["payment_intent_data"]["metadata"]["stripe_destination_account"] = connect_destination
        try:
            session = stripe.checkout.Session.create(**session_payload)
        except Exception as exc:
            raise ValueError("Checkout Stripe non avviato. Verifica chiave Stripe, connessione e URL pubblico dell'app.") from exc
        if not stripe_session_value(session, "url"):
            raise ValueError("Checkout Stripe non avviato: URL di pagamento non ricevuto")
        self.redirect(session.url)

    def payment_success(self, user: sqlite3.Row | None, query: dict[str, list[str]]) -> None:
        session_id = query.get("session_id", [""])[0]
        if not session_id:
            target = "/profile?flash=Pagamento%20non%20verificabile" if user else "/login?flash=Pagamento%20non%20verificabile"
            self.redirect(target)
            return
        if not stripe_configured():
            target = "/profile?flash=Stripe%20non%20configurato" if user else "/login?flash=Stripe%20non%20configurato"
            self.redirect(target)
            return
        stripe.api_key = stripe_secret_key()
        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except Exception:
            target = "/profile?flash=Pagamento%20non%20verificabile" if user else "/login?flash=Pagamento%20non%20verificabile"
            self.redirect(target)
            return
        expected_user_id = int(user["id"]) if user else None
        ok, message = record_stripe_checkout_payment(session, expected_user_id=expected_user_id)
        if not ok:
            target = f"/profile?flash={quote(message)}" if user else f"/login?flash={quote(message)}"
            self.redirect(target)
            return
        metadata = stripe_session_value(session, "metadata", {}) or {}
        logged_user_id = int(user["id"]) if user else int(metadata.get("user_id", "0") or 0) or None
        log_event("stripe_payment_success", message, logged_user_id)
        target = "/profile?open=payments&flash=Pagamento%20registrato#payments" if user else "/login?flash=Pagamento%20registrato.%20Accedi%20per%20vedere%20lo%20storico"
        self.redirect(target)

    def stripe_webhook(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        if stripe is None or not stripe_webhook_secret():
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Stripe webhook non configurato")
            return
        signature = self.headers.get("Stripe-Signature", "")
        try:
            event = stripe.Webhook.construct_event(payload, signature, stripe_webhook_secret())
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Firma webhook non valida")
            return
        event_type = event.get("type")
        if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            session = event.get("data", {}).get("object", {})
            record_stripe_checkout_payment(session)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received": true}')
    def admin_service_description(self, form: dict[str, str]) -> None:
        description = form.get("description", "").strip()
        if len(description) > 4000:
            raise ValueError("Descrizione troppo lunga")
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        conn.execute("UPDATE service_types SET description = ? WHERE id = ? AND doctor_id = ?", (description, int(form["id"]), doctor["id"]))
        conn.commit()
        conn.close()
        log_event("admin_service_description", f"Descrizione tipologia {int(form['id'])} aggiornata", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.redirect("/services?flash=Descrizione%20aggiornata")

    def admin_diary(self, form: dict[str, str]) -> None:
        app_id = int(form["id"])
        diary = form.get("diary", "").strip()
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute("SELECT user_id FROM appointments WHERE id = ? AND doctor_id = ?", (app_id, doctor["id"])).fetchone()
        if not row:
            conn.close()
            raise ValueError("Seduta non trovata")
        conn.execute("UPDATE appointments SET diary = ? WHERE id = ? AND doctor_id = ?", (diary, app_id, doctor["id"]))
        conn.commit()
        conn.close()
        log_event("admin_diary", f"Diario seduta {app_id} aggiornato", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.flash_redirect(f"/patient?id={row['user_id']}", "Diario salvato")

    def confirm_presence(self, query: dict[str, list[str]]) -> None:
        token = query.get("token", [""])[0]
        app_id = read_presence_token(token)
        if not app_id:
            self.redirect("/scan?flash=Errore:%20QR%20non%20valido")
            return
        doctor = get_user_from_cookie(self.headers)
        conn = connect()
        row = conn.execute(
            """
            SELECT a.id, a.status, s.slot_date, s.slot_time
            FROM appointments a JOIN slots s ON s.id = a.slot_id
            WHERE a.id = ? AND a.doctor_id = ?
            """,
            (app_id, doctor["id"]),
        ).fetchone()
        if not row:
            conn.close()
            self.redirect("/scan?flash=Errore:%20seduta%20non%20trovata")
            return
        if row["status"] != "prenotata":
            conn.close()
            self.redirect("/scan?flash=Seduta%20gia%20gestita")
            return
        appointment_time = parse_dt(row["slot_date"], row["slot_time"])
        if not (appointment_time - dt.timedelta(hours=1) <= now() <= appointment_time + dt.timedelta(hours=2)):
            conn.close()
            self.redirect("/scan?flash=Errore:%20QR%20fuori%20finestra%20oraria")
            return
        conn.execute(
            "UPDATE appointments SET status = 'effettuata', checked_in_at = ?, auto_suggestion = NULL WHERE id = ? AND doctor_id = ?",
            (now().isoformat(), app_id, doctor["id"]),
        )
        conn.commit()
        conn.close()
        log_event("qr_presence_confirmed", f"Presenza confermata via QR per seduta {app_id}", get_user_from_cookie(self.headers)["id"] if get_user_from_cookie(self.headers) else None)
        self.redirect("/scan?flash=Presenza%20confermata:%20seduta%20effettuata")
    def download_ics(self, user: sqlite3.Row, query: dict[str, list[str]]) -> None:
        app_id = int(query.get("id", ["0"])[0])
        conn = connect()
        row = conn.execute(
            """
            SELECT a.*, s.slot_date, s.slot_time
            FROM appointments a JOIN slots s ON s.id = a.slot_id
            WHERE a.id = ? AND (a.user_id = ? OR ? IN ('admin', 'doctor'))
            """,
            (app_id, user["id"], user["role"]),
        ).fetchone()
        conn.close()
        if not row:
            self.error(HTTPStatus.NOT_FOUND, "Appuntamento non trovato")
            return
        start = parse_dt(row["slot_date"], row["slot_time"])
        end = start + dt.timedelta(hours=1)
        ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Fisio App//IT
BEGIN:VEVENT
UID:appointment-{row['id']}@fisio.local
DTSTAMP:{now().strftime('%Y%m%dT%H%M%S')}
DTSTART:{start.strftime('%Y%m%dT%H%M%S')}
DTEND:{end.strftime('%Y%m%dT%H%M%S')}
SUMMARY:Seduta fisioterapica
END:VEVENT
END:VCALENDAR
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="appuntamento.ics"')
        self.end_headers()
        self.wfile.write(ics.encode("utf-8"))

    def require_admin(self, user: sqlite3.Row) -> None:
        if not is_staff_account(user):
            raise ValueError("Accesso staff richiesto")

    def notification_events(self, user: sqlite3.Row) -> list[dict[str, str]]:
        current = now()
        events: list[dict[str, str]] = []
        conn = connect()
        if user["role"] == "user":
            rows = conn.execute(
                """
                SELECT a.id, s.slot_date, s.slot_time, st.name AS service_type_name
                FROM appointments a
                JOIN slots s ON s.id = a.slot_id
                LEFT JOIN service_types st ON st.id = a.service_type_id
                WHERE a.user_id = ? AND a.status = 'prenotata'
                  AND (s.slot_date > ? OR (s.slot_date = ? AND s.slot_time >= ?))
                ORDER BY s.slot_date, s.slot_time
                LIMIT 8
                """,
                (user["id"], today().isoformat(), today().isoformat(), current.strftime("%H:%M")),
            ).fetchall()
            for row in rows:
                start = parse_dt(row["slot_date"], row["slot_time"])
                service = service_label(row)
                specs = [
                    ("24h", start - dt.timedelta(hours=24), "Promemoria seduta", f"Domani alle {row['slot_time']} hai {service}."),
                    ("qr", start - dt.timedelta(hours=1), "QR presenza pronto", f"Manca un'ora alla seduta delle {row['slot_time']}: apri la Home e mostra il QR."),
                ]
                for suffix, due, title, body in specs:
                    if current <= start and due <= current + dt.timedelta(days=7):
                        send_at = due if due > current else current + dt.timedelta(seconds=8)
                        events.append({
                            "id": f"user-{user['id']}-app-{row['id']}-{suffix}",
                            "due_at": send_at.isoformat(),
                            "title": title,
                            "body": body,
                            "url": "/",
                            "tag": f"rehab-{row['id']}-{suffix}",
                        })
            unpaid = conn.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(residual), 0) AS total
                FROM (
                    SELECT MAX(payable_amount, 0) AS residual
                    FROM (
                        SELECT a.id, a.price - COALESCE(SUM(p.amount), 0) AS payable_amount
                        FROM appointments a
                        LEFT JOIN payments p ON p.appointment_id = a.id
                        WHERE a.user_id = ? AND a.status = 'effettuata'
                        GROUP BY a.id
                    )
                    WHERE payable_amount > 0.001
                )
                """,
                (user["id"],),
            ).fetchone()
            if unpaid and float(unpaid["total"] or 0) > 0:
                events.append({
                    "id": f"user-{user['id']}-debt-{current.strftime('%G-W%V')}",
                    "due_at": (current + dt.timedelta(seconds=12)).isoformat(),
                    "title": "Pagamenti da completare",
                    "body": f"Hai {int(unpaid['count'] or 0)} sedute con residuo aperto per {money(unpaid['total'])}.",
                    "url": "/profile",
                    "tag": f"rehab-debt-{user['id']}",
                })
        else:
            doctor_id = int(user["id"])
            rows = conn.execute(
                """
                SELECT a.id, s.slot_date, s.slot_time, st.name AS service_type_name, u.first_name, u.last_name
                FROM appointments a
                JOIN slots s ON s.id = a.slot_id
                JOIN users u ON u.id = a.user_id
                LEFT JOIN service_types st ON st.id = a.service_type_id
                WHERE a.status = 'prenotata' AND a.doctor_id = ?
                  AND (s.slot_date > ? OR (s.slot_date = ? AND s.slot_time >= ?))
                ORDER BY s.slot_date, s.slot_time
                LIMIT 20
                """,
                (doctor_id, today().isoformat(), today().isoformat(), current.strftime("%H:%M")),
            ).fetchall()
            for row in rows:
                start = parse_dt(row["slot_date"], row["slot_time"])
                due = start - dt.timedelta(hours=1)
                if current <= start and due <= current + dt.timedelta(days=7):
                    send_at = due if due > current else current + dt.timedelta(seconds=8)
                    events.append({
                        "id": f"doctor-app-{row['id']}-1h",
                        "due_at": send_at.isoformat(),
                        "title": "Seduta in arrivo",
                        "body": f"{row['first_name']} {row['last_name']} alle {row['slot_time']} - {service_label(row)}.",
                        "url": f"/book?date={row['slot_date']}",
                        "tag": f"rehab-doctor-app-{row['id']}",
                    })
            pending_confirm = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM appointments a JOIN slots s ON s.id = a.slot_id
                WHERE a.status = 'prenotata' AND a.doctor_id = ?
                  AND (s.slot_date < ? OR (s.slot_date = ? AND s.slot_time < ?))
                """,
                (doctor_id, today().isoformat(), today().isoformat(), current.strftime("%H:%M")),
            ).fetchone()
            if pending_confirm and int(pending_confirm["count"] or 0) > 0:
                events.append({
                    "id": f"doctor-confirm-{user['id']}-{current.strftime('%Y%m%d')}",
                    "due_at": (current + dt.timedelta(seconds=10)).isoformat(),
                    "title": "Sedute da confermare",
                    "body": f"Hai {int(pending_confirm['count'])} sedute da confermare in dashboard.",
                    "url": "/",
                    "tag": f"rehab-doctor-confirm-{user['id']}",
                })
            paid_rows = conn.execute(
                """
                SELECT p.id, p.amount, p.paid_at, u.first_name, u.last_name
                FROM payments p
                JOIN appointments a ON a.id = p.appointment_id
                JOIN users u ON u.id = a.user_id
                WHERE p.paid_at >= ? AND a.doctor_id = ?
                ORDER BY p.paid_at DESC
                LIMIT 5
                """,
                ((current - dt.timedelta(hours=24)).isoformat(), doctor_id),
            ).fetchall()
            for row in paid_rows:
                events.append({
                    "id": f"doctor-payment-{row['id']}",
                    "due_at": (current + dt.timedelta(seconds=14)).isoformat(),
                    "title": "Pagamento ricevuto",
                    "body": f"{row['first_name']} {row['last_name']} ha pagato {money(row['amount'])}.",
                    "url": "/patients",
                    "tag": f"rehab-doctor-payment-{row['id']}",
                })
            unpaid = conn.execute(
                """
                SELECT COALESCE(SUM(residual), 0) AS total
                FROM (
                    SELECT a.price - COALESCE(SUM(p.amount), 0) AS residual
                    FROM appointments a
                    LEFT JOIN payments p ON p.appointment_id = a.id
                    WHERE a.status = 'effettuata' AND a.doctor_id = ?
                    GROUP BY a.id
                    HAVING residual > 0.001
                )
                """,
                (doctor_id,),
            ).fetchone()
            if unpaid and float(unpaid["total"] or 0) > 0:
                events.append({
                    "id": f"doctor-debt-{user['id']}-{current.strftime('%G-W%V')}",
                    "due_at": (current + dt.timedelta(seconds=16)).isoformat(),
                    "title": "Residui da incassare",
                    "body": f"Totale residui aperti: {money(unpaid['total'])}.",
                    "url": "/",
                    "tag": f"rehab-doctor-debt-{user['id']}",
                })
        conn.close()
        return events

    def text_response(self, body: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status.value)
        self.security_headers(no_store=True)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def json_response(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status.value)
        self.security_headers(no_store=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def manifest_response(self) -> None:
        brand_name = studio_display_name("Studio")
        manifest = {
            "name": brand_name,
            "short_name": brand_name[:12] or "Studio",
            "description": "Prenotazioni, presenze, pagamenti e documenti dello studio.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait-primary",
            "background_color": "#f3f5f2",
            "theme_color": "#f3f5f2",
            "icons": [
                {
                    "src": "/static/app-icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/static/app-icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        }
        self.text_response(json.dumps(manifest, ensure_ascii=False), "application/manifest+json; charset=utf-8")

    def base_url(self) -> str:
        configured_url = load_email_config().get("base_url", "").strip().rstrip("/")
        if configured_url:
            return configured_url
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "127.0.0.1")
        if host.startswith("0.0.0.0"):
            host = "127.0.0.1" + host.removeprefix("0.0.0.0")
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
        return f"{proto}://{host}"

    def is_secure_request(self) -> bool:
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        forwarded = self.headers.get("Forwarded", "").lower()
        return forwarded_proto == "https" or "proto=https" in forwarded

    def cookie_secure_suffix(self) -> str:
        return "; Secure" if self.is_secure_request() or is_production() else ""

    def csrf_cookie_token(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("csrf_token")
        value = token.value if token else ""
        return value if verify_csrf_token(value) else make_csrf_token()

    def inject_csrf_tokens(self, html_text: str, token: str) -> str:
        hidden = f'<input type="hidden" name="csrf_token" value="{html.escape(token)}">'
        return re.sub(
            r'(<form\b(?=[^>]*\bmethod=["\\\']post["\\\'])[^>]*>)',
            lambda match: match.group(1) + "\n" + hidden,
            html_text,
            flags=re.IGNORECASE,
        )

    def csrf_is_valid(self, form: dict[str, str]) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        cookie_token = cookie.get("csrf_token")
        form_token = form.get("csrf_token", "")
        if not cookie_token or not form_token:
            return False
        return hmac.compare_digest(cookie_token.value, form_token) and verify_csrf_token(form_token)

    def security_headers(self, html_response: bool = False, no_store: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Origin-Agent-Cluster", "?1")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=(), payment=(self)")
        if self.is_secure_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if html_response:
            csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            if self.is_secure_request():
                csp += "; upgrade-insecure-requests"
            self.send_header("Content-Security-Policy", csp)
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, max-age=0, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")

    def redirect(self, location: str, token: str | None = None, clear: bool = False, max_age: int | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        secure_cookie = self.cookie_secure_suffix()
        if token:
            cookie_age = max_age or (SESSION_MINUTES * 60)
            self.send_header("Set-Cookie", f"session={token}; Max-Age={cookie_age}; HttpOnly; SameSite=Lax; Path=/{secure_cookie}")
        if clear:
            self.send_header("Set-Cookie", f"session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/{secure_cookie}")
        self.end_headers()

    def flash_redirect(self, location: str, message: str) -> None:
        separator = "&" if "?" in location else "?"
        self.redirect(f"{location}{separator}flash={quote(message)}")

    def html(self, data: bytes) -> None:
        csrf_token = self.csrf_cookie_token()
        html_text = self.inject_csrf_tokens(data.decode("utf-8"), csrf_token)
        data = html_text.encode("utf-8")
        self.send_response(200)
        self.security_headers(html_response=True, no_store=True)
        self.send_header("Set-Cookie", f"csrf_token={csrf_token}; Max-Age=7200; HttpOnly; SameSite=Lax; Path=/{self.cookie_secure_suffix()}")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self, request_path: str) -> None:
        relative = request_path.removeprefix("/static/").strip("/")
        if not relative or ".." in Path(relative).parts:
            self.error(HTTPStatus.NOT_FOUND, "Asset non trovato")
            return
        asset = RESOURCE_DIR / "static" / relative
        if not asset.exists() or not asset.is_file():
            upload_asset = APP_DIR / "static" / relative
            if upload_asset.exists() and upload_asset.is_file():
                asset = upload_asset
            else:
                self.error(HTTPStatus.NOT_FOUND, "Asset non trovato")
                return
        content_types = {
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
            ".webmanifest": "application/manifest+json; charset=utf-8",
        }
        data = asset.read_bytes()
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", content_types.get(asset.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        if asset.name == "service-worker.js":
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Cache-Control", "no-cache")
        elif asset.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico"}:
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: HTTPStatus, message: str) -> None:
        body = page(str(status.value), f"<section class='card'><h1>{status.value}</h1><p>{html.escape(message)}</p></section>")
        body = self.inject_csrf_tokens(body.decode("utf-8"), self.csrf_cookie_token()).encode("utf-8")
        self.send_response(status.value)
        self.security_headers(html_response=True, no_store=True)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_available_port(preferred: int = 8000) -> int:
    for candidate in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run(host: str = "127.0.0.1", port: int | None = None, open_browser: bool = True) -> None:
    init_db()
    backup_database_if_needed()
    ensure_slots(days=MAX_BOOKING_DAYS)
    selected_port = port or find_available_port(8000)
    server = ThreadingHTTPServer((host, selected_port), App)
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    server_url = f"http://{host}:{selected_port}"
    browser_url = f"http://{browser_host}:{selected_port}"
    print(f"{APP_NAME} avviato su {browser_url}")
    if browser_url != server_url:
        print(f"Server in ascolto su {server_url}")
    print(f"Database: {DB_PATH}")
    if studio_setup_required():
        print("Primo avvio: completa il setup guidato nel browser.")
    else:
        print("Accesso: usa un account configurato nello studio.")
    print("Chiudi questa finestra per fermare l'app.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(browser_url)).start()
    server.serve_forever()

if __name__ == "__main__":
    env_port = os.environ.get("PORT", "").strip()
    selected_port = int(sys.argv[1]) if len(sys.argv) > 1 else (int(env_port) if env_port else None)
    default_host = "0.0.0.0" if is_production() else "127.0.0.1"
    selected_host = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("FISIO_HOST", default_host)
    run(host=selected_host, port=selected_port, open_browser=not is_production())

























