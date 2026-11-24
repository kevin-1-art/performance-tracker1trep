import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_PASSWORD_LENGTH = 12


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development").lower()
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    DATABASE_URL = os.environ.get("DATABASE_URL")
    DATABASE = os.environ.get("DATABASE", os.path.join(BASE_DIR, "instance", "tracker.db"))
    EXPORT_DIR = os.environ.get("EXPORT_DIR", os.path.join(BASE_DIR, "instance", "reports"))

    # All "what day is it" decisions use this timezone.
    TIMEZONE = os.environ.get("APP_TIMEZONE", "Africa/Kampala")

    # Reports can only be downloaded on this weekday (Monday=0 ... Thursday=3).
    DOWNLOAD_WEEKDAY = int(os.environ.get("DOWNLOAD_WEEKDAY", "3"))

    CURRENCY = os.environ.get("CURRENCY", "UGX")
    CSRF_ENABLED = True
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # 2 MB uploads
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = (
        os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
        or APP_ENV == "production"
    )
