import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from flask import current_app

PRN_RE = re.compile(r"^[0-9]{13}$")


# ---------- time ----------
def now() -> datetime:
    """Current time in the app timezone. Tests can inject NOW_FUNC."""
    fn = current_app.config.get("NOW_FUNC")
    if fn:
        return fn()
    return datetime.now(ZoneInfo(current_app.config["TIMEZONE"]))


def today() -> date:
    return now().date()


def week_start_of(d: date) -> date:
    """Friday of the reporting week containing d."""
    return d - timedelta(days=(d.weekday() - 4) % 7)


def last_completed_week_start(ref: date) -> date:
    """Friday of the most recently completed Friday-Thursday reporting week."""
    return week_start_of(ref) - timedelta(days=7)


def is_download_day() -> bool:
    return now().weekday() == current_app.config["DOWNLOAD_WEEKDAY"]


def next_download_date() -> date:
    t = today()
    delta = (current_app.config["DOWNLOAD_WEEKDAY"] - t.weekday()) % 7
    return t + timedelta(days=delta)


# ---------- money ----------
def to_minor(value) -> int:
    """'1,250.50' -> 125050. Raises ValueError on bad input."""
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError("Amount must be a number")
    if not d.is_finite():
        raise ValueError("Amount must be a number")
    if d < 0:
        raise ValueError("Amount cannot be negative")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_money(minor) -> str:
    return f"{(minor or 0) / 100:,.2f}"


# ---------- parsing / validation ----------
def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("Date must look like 2026-11-18 or 18/11/2026")


def clean_prn(value) -> str:
    if isinstance(value, bool):
        prn = ""
    elif isinstance(value, int):
        prn = str(value)
    elif isinstance(value, float) and value.is_integer():
        prn = str(int(value))
    else:
        prn = str(value or "").strip()
    if not PRN_RE.match(prn):
        raise ValueError("PRN must be exactly 13 digits")
    return prn


def validate_application(prn, date_str, amount_str):
    """Returns (prn, iso_date, amount_minor) or raises ValueError."""
    prn = clean_prn(prn)
    d = parse_date(date_str)
    if d > today():
        raise ValueError("Date of application cannot be in the future")
    return prn, d.isoformat(), to_minor(amount_str)
