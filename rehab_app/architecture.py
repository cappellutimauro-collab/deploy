from __future__ import annotations

APP_VERSION = "0.9.0-predeploy"
SCHEMA_VERSION = "2026.06.03.1"

FEATURE_BOUNDARIES = {
    "auth": ["login", "register", "password reset", "session cookies", "csrf"],
    "booking": ["slots", "appointments", "service selection", "cancellations"],
    "admin": ["patients", "payments", "qr scanner", "slot management"],
    "documents": ["privacy", "informed consent", "signed files", "document versions"],
    "payments": ["stripe checkout", "webhook", "residual balances"],
}
