from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import sqlite3
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DB_PATH = ROOT / "fisio_app.sqlite3"

REQUIRED_STATIC = [
    "styles.css",
    "design-system.css",
    "ui-2026.css",
    "form-validation.js",
    "login-guard.js",
    "ui-polish.js",
    "doctor-profile.js",
    "service-worker.js",
    "manifest.webmanifest",
    "rehab-philosophy-logo-ui.png",
    "app-icon-192.png",
    "app-icon-512.png",
]

REQUIRED_TABLES = {
    "users",
    "slots",
    "service_types",
    "appointments",
    "payments",
    "events",
    "app_settings",
    "password_resets",
    "schema_migrations",
}

@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name, True, detail)


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name, False, detail)


def check_static_assets() -> list[CheckResult]:
    results: list[CheckResult] = []
    for asset in REQUIRED_STATIC:
        path = STATIC_DIR / asset
        if not path.exists():
            results.append(_fail(f"asset:{asset}", "mancante"))
            continue
        size = path.stat().st_size
        if asset.endswith(".png") and size > 512_000:
            results.append(_fail(f"asset:{asset}", f"troppo pesante: {size} byte"))
        else:
            results.append(_ok(f"asset:{asset}", f"{size} byte"))
    return results


def check_database() -> list[CheckResult]:
    if not DB_PATH.exists():
        return [_fail("database", "fisio_app.sqlite3 non trovato")]
    conn = sqlite3.connect(DB_PATH)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        results = [_ok("database:file", str(DB_PATH))]
        results.append(_ok("database:tables", "schema base presente") if not missing else _fail("database:tables", "mancano: " + ", ".join(missing)))
        settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall()) if "app_settings" in tables else {}
        for key in ["app_version", "schema_version", "privacy_version", "consent_version"]:
            value = settings.get(key, "")
            results.append(_ok(f"setting:{key}", value) if value else _fail(f"setting:{key}", "non impostato"))
        return results
    finally:
        conn.close()


def check_security_files() -> list[CheckResult]:
    results: list[CheckResult] = []
    secret = ROOT / "app_secret.key"
    stripe = ROOT / "stripe_secret_key.txt"
    results.append(_ok("secret:app", "presente") if secret.exists() and secret.stat().st_size >= 32 else _fail("secret:app", "mancante o troppo corto"))
    if stripe.exists() and stripe.read_text(encoding="utf-8-sig").strip().startswith(("sk_test_", "sk_live_")):
        results.append(_ok("stripe:key", "configurata"))
    else:
        results.append(_fail("stripe:key", "non configurata o non valida"))
    return results


def run_checks() -> list[CheckResult]:
    return [*check_static_assets(), *check_database(), *check_security_files()]


def write_report(results: Iterable[CheckResult], target: Path | None = None) -> Path:
    target = target or ROOT / "reports" / "preflight-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": all(item.ok for item in results),
        "checks": [asdict(item) for item in results],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def main() -> int:
    results = run_checks()
    report = write_report(results)
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")
    print(f"Report: {report}")
    return 0 if all(item.ok for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
