#!/usr/bin/env python3
"""
Invoice Tracker - keeps track of outgoing invoices, payment terms and overdue reminders.

Pure Python standard library (3.9+) + SQLite. No packages to install.

Usage:
    python invoice_tracker.py serve              # start the web app (http://127.0.0.1:8080)
    python invoice_tracker.py check-reminders    # run the reminder job once (for cron / Task Scheduler)
    python invoice_tracker.py init               # create the database only

Configuration is read from config.ini next to this file (see config.example.ini).
"""

from __future__ import annotations

import argparse
import configparser
import csv
import io
import json
import logging
import re
import smtplib
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

log = logging.getLogger("invoice_tracker")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "app": {
        "database": "invoices.db",
        "host": "127.0.0.1",
        "port": "8080",
        "company_name": "My Company B.V.",
        "currency": "EUR",
        "default_payment_terms_days": "30",
        "default_vat_rate": "21",
        # How often the built-in scheduler runs the reminder job while the server is up.
        "reminder_check_interval_minutes": "60",
    },
    "reminders": {
        # Repeat the overdue message every N days while the invoice stays unpaid.
        "repeat_every_days": "7",
        # Comma-separated internal recipients (finance / credit control).
        "notify_to": "",
        # If true, the overdue reminder is also sent to the invoice's contact e-mail.
        "send_to_customer": "false",
        # File where every message is logged (also used as the "outbox" when SMTP is not set).
        "outbox_file": "outbox.log",
    },
    "smtp": {
        "host": "",
        "port": "587",
        "username": "",
        "password": "",
        "use_tls": "true",
        "from_address": "",
    },
    "webhook": {
        # Optional Microsoft Teams / Slack incoming-webhook URL. Receives {"text": "..."}.
        "url": "",
    },
}


def load_config(path: Path | None = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULT_CONFIG)
    path = path or BASE_DIR / "config.ini"
    if path.exists():
        cfg.read(path, encoding="utf-8")
    return cfg


def resolve_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


# --------------------------------------------------------------------------------------
# Money / VAT helpers
# --------------------------------------------------------------------------------------

CENT = Decimal("0.01")
DUTCH_VAT_RATES = (Decimal("21"), Decimal("9"), Decimal("0"))


class ValidationError(ValueError):
    pass


def parse_amount(value) -> Decimal:
    """Parse an amount. Accepts 1234.56, 1234,56, 1.234,56, 1,234.56, '€ 1.234,56'."""
    if value is None or value == "":
        raise ValidationError("amount is required")
    if isinstance(value, (int, float, Decimal)):
        s = str(value)
    else:
        s = re.sub(r"[\s€]|EUR", "", str(value), flags=re.IGNORECASE)
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):          # 1.234,56  (Dutch)
                s = s.replace(".", "").replace(",", ".")
            else:                                     # 1,234.56  (English)
                s = s.replace(",", "")
        elif "," in s:                                # 1234,56
            s = s.replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):    # 8.000 / 1.250.000 -> Dutch thousands separator
            s = s.replace(".", "")
    try:
        return Decimal(s).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValidationError(f"invalid amount: {value!r}") from None


def parse_rate(value) -> Decimal:
    try:
        rate = Decimal(str(value).replace(",", ".").replace("%", "").strip())
    except InvalidOperation:
        raise ValidationError(f"invalid VAT percentage: {value!r}") from None
    if rate < 0 or rate > 100:
        raise ValidationError("VAT percentage must be between 0 and 100")
    return rate.normalize() if rate == rate.to_integral() else rate


def calc_vat(net: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """Return (vat_amount, gross_amount), rounded half-up to the cent."""
    vat = (net * rate / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    return vat, net + vat


def to_cents(d: Decimal) -> int:
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_cents(c: int | None) -> str | None:
    if c is None:
        return None
    return str((Decimal(c) / 100).quantize(CENT))


def fmt_eur(cents: int) -> str:
    """Dutch formatting: € 1.234,56"""
    s = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {'-' if cents < 0 else ''}{s}"


def parse_date(value, field: str, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"{field} is required")
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    raise ValidationError(f"{field}: invalid date {value!r} (use YYYY-MM-DD or DD-MM-YYYY)")


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number       TEXT    NOT NULL UNIQUE,
    customer             TEXT    NOT NULL,
    contact_email        TEXT,
    invoice_date         TEXT    NOT NULL,
    net_cents            INTEGER NOT NULL,
    vat_rate             TEXT    NOT NULL,
    vat_cents            INTEGER NOT NULL,
    gross_cents          INTEGER NOT NULL,
    sent_date            TEXT,
    payment_terms_days   INTEGER NOT NULL DEFAULT 30,
    due_date             TEXT,
    status               TEXT    NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','SENT','PAID')),
    paid_date            TEXT,
    reminders_enabled    INTEGER NOT NULL DEFAULT 1,
    reminder_date        TEXT,
    reminder_date_done   INTEGER NOT NULL DEFAULT 0,
    overdue_reminder_count INTEGER NOT NULL DEFAULT 0,
    last_overdue_reminder  TEXT,
    notes                TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invoices_status_due ON invoices(status, due_date);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,           -- OVERDUE | REMINDER
    recipients  TEXT,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    channels    TEXT NOT NULL,           -- e.g. "email,webhook,outbox"
    error       TEXT,
    created_at  TEXT NOT NULL
);
"""

_db_lock = threading.Lock()


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


EDITABLE_FIELDS = (
    "invoice_number", "customer", "contact_email", "invoice_date", "net_amount", "vat_rate",
    "sent_date", "payment_terms_days", "due_date", "status", "paid_date",
    "reminders_enabled", "reminder_date", "notes",
)


def build_record(data: dict, existing: sqlite3.Row | None, default_terms: int) -> dict:
    """Validate input and return the column values to store. `existing` is used for partial updates."""
    def get(key, fallback=None):
        if key in data:
            return data[key]
        return fallback

    ex = dict(existing) if existing else {}

    number = str(get("invoice_number", ex.get("invoice_number")) or "").strip()
    if not number:
        raise ValidationError("invoice number is required")
    customer = str(get("customer", ex.get("customer")) or "").strip()
    if not customer:
        raise ValidationError("customer / supplier name is required")
    email = (get("contact_email", ex.get("contact_email")) or "").strip() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValidationError(f"invalid e-mail address: {email}")

    inv_date = parse_date(get("invoice_date", ex.get("invoice_date")), "invoice date", required=True)

    net = parse_amount(get("net_amount", from_cents(ex.get("net_cents"))))
    rate = parse_rate(get("vat_rate", ex.get("vat_rate", "21")))
    vat, gross = calc_vat(net, rate)

    sent = parse_date(get("sent_date", ex.get("sent_date")), "sent date")
    if sent and sent < inv_date:
        raise ValidationError("sent date cannot be before the invoice date")

    terms_raw = get("payment_terms_days", ex.get("payment_terms_days", default_terms))
    try:
        terms = int(terms_raw if terms_raw not in (None, "") else default_terms)
    except (TypeError, ValueError):
        raise ValidationError("payment terms must be a whole number of days") from None
    if terms < 0 or terms > 365:
        raise ValidationError("payment terms must be between 0 and 365 days")

    # Due date: explicit value wins; otherwise sent date (or invoice date) + payment terms.
    due_in = data.get("due_date") if "due_date" in data else None
    due = parse_date(due_in, "due date")
    if due is None:
        base_changed = any(k in data for k in ("sent_date", "invoice_date", "payment_terms_days"))
        if ex.get("due_date") and not base_changed and "due_date" not in data:
            due = parse_date(ex["due_date"], "due date")
        else:
            due = (sent or inv_date) + timedelta(days=terms)

    status = str(get("status", ex.get("status")) or ("SENT" if sent else "DRAFT")).upper()
    if status not in ("DRAFT", "SENT", "PAID"):
        raise ValidationError("status must be DRAFT, SENT or PAID")
    if status == "SENT" and not sent:
        sent = date.today()
        if "due_date" not in data or not data.get("due_date"):
            due = sent + timedelta(days=terms)

    paid_date = parse_date(get("paid_date", ex.get("paid_date")), "payment received date")
    if status == "PAID":
        paid_date = paid_date or date.today()
        if paid_date < inv_date:
            raise ValidationError("payment date cannot be before the invoice date")
    else:
        paid_date = None

    reminders_enabled = get("reminders_enabled", ex.get("reminders_enabled", 1))
    reminders_enabled = 1 if str(reminders_enabled).lower() in ("1", "true", "yes", "on") else 0

    reminder_date = parse_date(get("reminder_date", ex.get("reminder_date")), "reminder date")
    reminder_done = ex.get("reminder_date_done", 0)
    if "reminder_date" in data and (reminder_date.isoformat() if reminder_date else None) != ex.get("reminder_date"):
        reminder_done = 0   # new reminder date -> fire again

    overdue_count = ex.get("overdue_reminder_count", 0)
    last_overdue = ex.get("last_overdue_reminder")
    if ex and due and ex.get("due_date") != due.isoformat():
        overdue_count, last_overdue = 0, None   # due date moved -> restart reminder cycle

    return {
        "invoice_number": number,
        "customer": customer,
        "contact_email": email,
        "invoice_date": inv_date.isoformat(),
        "net_cents": to_cents(net),
        "vat_rate": str(rate),
        "vat_cents": to_cents(vat),
        "gross_cents": to_cents(gross),
        "sent_date": sent.isoformat() if sent else None,
        "payment_terms_days": terms,
        "due_date": due.isoformat() if due else None,
        "status": status,
        "paid_date": paid_date.isoformat() if paid_date else None,
        "reminders_enabled": reminders_enabled,
        "reminder_date": reminder_date.isoformat() if reminder_date else None,
        "reminder_date_done": reminder_done,
        "overdue_reminder_count": overdue_count,
        "last_overdue_reminder": last_overdue,
        "notes": (get("notes", ex.get("notes")) or "").strip() or None,
    }


def serialize(row: sqlite3.Row, today: date | None = None) -> dict:
    today = today or date.today()
    d = dict(row)
    d.pop("paid_cents", None)   # column from earlier versions, no longer used
    due = date.fromisoformat(d["due_date"]) if d["due_date"] else None
    is_open = d["status"] == "SENT"
    overdue_days = (today - due).days if (is_open and due and today > due) else 0
    d.update(
        net_amount=from_cents(d.pop("net_cents")),
        vat_amount=from_cents(d.pop("vat_cents")),
        gross_amount=from_cents(d.pop("gross_cents")),
        reminders_enabled=bool(d["reminders_enabled"]),
        reminder_date_done=bool(d["reminder_date_done"]),
        is_overdue=overdue_days > 0,
        days_overdue=overdue_days,
        days_until_due=(due - today).days if (is_open and due and today <= due) else None,
        display_status="OVERDUE" if overdue_days > 0 else d["status"],
    )
    return d


class Repo:
    def __init__(self, conn: sqlite3.Connection, default_terms: int = 30):
        self.conn = conn
        self.default_terms = default_terms

    def list(self, status: str | None = None, q: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM invoices", []
        where = []
        if q:
            where.append("(invoice_number LIKE ? OR customer LIKE ? OR notes LIKE ?)")
            args += [f"%{q}%"] * 3
        today = date.today().isoformat()
        if status:
            s = status.upper()
            if s == "OVERDUE":
                where.append("status = 'SENT' AND due_date < ?")
                args.append(today)
            elif s == "OPEN":
                where.append("status = 'SENT'")
            elif s in ("DRAFT", "SENT", "PAID"):
                where.append("status = ?")
                args.append(s)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE status WHEN 'SENT' THEN 0 WHEN 'DRAFT' THEN 1 ELSE 2 END, due_date, invoice_number"
        with _db_lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [serialize(r) for r in rows]

    def get_row(self, inv_id: int) -> sqlite3.Row | None:
        with _db_lock:
            return self.conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()

    def get(self, inv_id: int) -> dict | None:
        row = self.get_row(inv_id)
        return serialize(row) if row else None

    def create(self, data: dict) -> dict:
        rec = build_record(data, None, self.default_terms)
        rec["created_at"] = rec["updated_at"] = now_iso()
        cols = ", ".join(rec)
        marks = ", ".join("?" for _ in rec)
        try:
            with _db_lock, self.conn:
                cur = self.conn.execute(f"INSERT INTO invoices ({cols}) VALUES ({marks})", list(rec.values()))
        except sqlite3.IntegrityError:
            raise ValidationError(f"invoice number {rec['invoice_number']} already exists") from None
        return self.get(cur.lastrowid)

    def update(self, inv_id: int, data: dict) -> dict | None:
        existing = self.get_row(inv_id)
        if not existing:
            return None
        data = {k: v for k, v in data.items() if k in EDITABLE_FIELDS}
        rec = build_record(data, existing, self.default_terms)
        rec["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in rec)
        try:
            with _db_lock, self.conn:
                self.conn.execute(f"UPDATE invoices SET {sets} WHERE id = ?", [*rec.values(), inv_id])
        except sqlite3.IntegrityError:
            raise ValidationError(f"invoice number {rec['invoice_number']} already exists") from None
        return self.get(inv_id)

    def mark_paid(self, inv_id: int, paid_date=None) -> dict | None:
        return self.update(inv_id, {"status": "PAID", "paid_date": paid_date or date.today().isoformat()})

    def mark_sent(self, inv_id: int, sent_date=None) -> dict | None:
        return self.update(inv_id, {"status": "SENT", "sent_date": sent_date or date.today().isoformat(),
                                    "due_date": None})

    def delete(self, inv_id: int) -> bool:
        with _db_lock, self.conn:
            return self.conn.execute("DELETE FROM invoices WHERE id = ?", (inv_id,)).rowcount > 0

    def summary(self, today: date | None = None) -> dict:
        today = today or date.today()
        t = today.isoformat()
        month_start = today.replace(day=1).isoformat()
        with _db_lock:
            r = self.conn.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN status='SENT' THEN gross_cents END),0)                     AS open_cents,
                     COUNT(CASE WHEN status='SENT' THEN 1 END)                                         AS open_count,
                     COALESCE(SUM(CASE WHEN status='SENT' AND due_date < ? THEN gross_cents END),0)   AS overdue_cents,
                     COUNT(CASE WHEN status='SENT' AND due_date < ? THEN 1 END)                       AS overdue_count,
                     COALESCE(SUM(CASE WHEN status='PAID' AND paid_date >= ? THEN gross_cents END),0) AS paid_month_cents,
                     COUNT(CASE WHEN status='DRAFT' THEN 1 END)                                        AS draft_count,
                     COALESCE(SUM(CASE WHEN status='SENT' THEN vat_cents END),0)                       AS open_vat_cents
                   FROM invoices""",
                (t, t, month_start),
            ).fetchone()
        d = dict(r)
        return {
            "open_amount": from_cents(d["open_cents"]), "open_count": d["open_count"],
            "overdue_amount": from_cents(d["overdue_cents"]), "overdue_count": d["overdue_count"],
            "paid_this_month": from_cents(d["paid_month_cents"]), "draft_count": d["draft_count"],
            "open_vat_amount": from_cents(d["open_vat_cents"]), "today": t,
        }

    def notifications(self, limit: int = 50) -> list[dict]:
        with _db_lock:
            rows = self.conn.execute(
                """SELECT n.*, i.invoice_number FROM notifications n
                   LEFT JOIN invoices i ON i.id = n.invoice_id ORDER BY n.id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def export_csv(self) -> str:
        buf = io.StringIO()
        cols = ["invoice_number", "customer", "contact_email", "invoice_date", "net_amount", "vat_rate",
                "vat_amount", "gross_amount", "sent_date", "payment_terms_days", "due_date", "display_status",
                "days_overdue", "paid_date", "overdue_reminder_count", "notes"]
        w = csv.writer(buf, delimiter=";")   # semicolon: opens correctly in Dutch Excel
        w.writerow(cols)
        for inv in self.list():
            w.writerow(["" if inv.get(c) is None else str(inv.get(c)).replace(".", ",")
                        if c in ("net_amount", "vat_amount", "gross_amount", "vat_rate")
                        else inv.get(c) for c in cols])
        return buf.getvalue()


# --------------------------------------------------------------------------------------
# Notifications / reminders
# --------------------------------------------------------------------------------------

class Notifier:
    """Sends a message via SMTP e-mail and/or webhook. Everything is always written to the outbox log."""

    def __init__(self, cfg: configparser.ConfigParser):
        self.cfg = cfg

    def send(self, subject: str, body: str, recipients: list[str]) -> tuple[list[str], str | None]:
        channels, errors = [], []
        smtp = self.cfg["smtp"]
        if smtp.get("host") and recipients:
            try:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = smtp.get("from_address") or smtp.get("username")
                msg["To"] = ", ".join(recipients)
                msg.set_content(body)
                with smtplib.SMTP(smtp["host"], smtp.getint("port"), timeout=30) as s:
                    if smtp.getboolean("use_tls"):
                        s.starttls()
                    if smtp.get("username"):
                        s.login(smtp["username"], smtp.get("password", ""))
                    s.send_message(msg)
                channels.append("email")
            except Exception as e:  # noqa: BLE001 - report any delivery failure, keep going
                errors.append(f"email: {e}")
        hook = self.cfg["webhook"].get("url")
        if hook:
            try:
                req = urllib.request.Request(
                    hook, data=json.dumps({"text": f"**{subject}**\n\n{body}"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=15).close()
                channels.append("webhook")
            except Exception as e:  # noqa: BLE001
                errors.append(f"webhook: {e}")
        outbox = resolve_path(self.cfg["reminders"]["outbox_file"])
        with outbox.open("a", encoding="utf-8") as f:
            f.write(f"--- {now_iso()} | to: {', '.join(recipients) or '(none)'}\nSubject: {subject}\n\n{body}\n\n")
        channels.append("outbox")
        return channels, "; ".join(errors) or None


def overdue_message(inv: dict, company: str) -> tuple[str, str]:
    gross = to_cents(Decimal(inv["gross_amount"]))
    subject = f"OVERDUE: invoice {inv['invoice_number']} ({inv['customer']}) - {inv['days_overdue']} day(s) past due"
    body = (
        f"Invoice {inv['invoice_number']} has passed its due date and no payment has been registered.\n\n"
        f"  Customer / supplier : {inv['customer']}\n"
        f"  Invoice date        : {inv['invoice_date']}\n"
        f"  Sent on             : {inv['sent_date'] or '-'}\n"
        f"  Payment terms       : {inv['payment_terms_days']} days\n"
        f"  Due date            : {inv['due_date']}\n"
        f"  Days overdue        : {inv['days_overdue']}\n"
        f"  Net amount          : {fmt_eur(to_cents(Decimal(inv['net_amount'])))}\n"
        f"  {('VAT (' + inv['vat_rate'] + '%)').ljust(20)}: {fmt_eur(to_cents(Decimal(inv['vat_amount'])))}\n"
        f"  Amount due (incl.)  : {fmt_eur(gross)}\n\n"
        f"Please follow up with the customer, or mark the invoice as paid in the Invoice Tracker "
        f"once the amount has been received.\n\n-- {company} Invoice Tracker"
    )
    return subject, body


def reminder_message(inv: dict, company: str) -> tuple[str, str]:
    subject = f"Reminder: invoice {inv['invoice_number']} ({inv['customer']}) due {inv['due_date']}"
    body = (
        f"This is the reminder you set for invoice {inv['invoice_number']}.\n\n"
        f"  Customer / supplier : {inv['customer']}\n"
        f"  Status              : {inv['display_status']}\n"
        f"  Due date            : {inv['due_date']}\n"
        f"  Amount (incl. VAT)  : {fmt_eur(to_cents(Decimal(inv['gross_amount'])))}\n"
        f"  Notes               : {inv['notes'] or '-'}\n\n-- {company} Invoice Tracker"
    )
    return subject, body


def run_reminders(repo: Repo, cfg: configparser.ConfigParser, notifier: Notifier | None = None,
                  today: date | None = None) -> list[dict]:
    """
    1. Custom reminders: every non-paid invoice whose reminder_date <= today fires once.
    2. Overdue messages: every SENT invoice with due_date < today and reminders enabled fires,
       then again every `repeat_every_days` days until it is paid.
    Safe to run as often as you like - it never sends duplicates.
    """
    today = today or date.today()
    notifier = notifier or Notifier(cfg)
    company = cfg["app"]["company_name"]
    rcfg = cfg["reminders"]
    repeat = max(1, rcfg.getint("repeat_every_days"))
    internal = [a.strip() for a in rcfg.get("notify_to", "").split(",") if a.strip()]
    to_customer = rcfg.getboolean("send_to_customer")
    sent: list[dict] = []

    def record(inv, kind, recipients, subject, body, channels, error):
        with _db_lock, repo.conn:
            repo.conn.execute(
                "INSERT INTO notifications (invoice_id, kind, recipients, subject, body, channels, error, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (inv["id"], kind, ", ".join(recipients), subject, body, ",".join(channels), error, now_iso()))
        sent.append({"invoice_number": inv["invoice_number"], "kind": kind, "channels": channels, "error": error})

    with _db_lock:
        rows = repo.conn.execute(
            "SELECT * FROM invoices WHERE status != 'PAID' AND reminder_date IS NOT NULL "
            "AND reminder_date <= ? AND reminder_date_done = 0", (today.isoformat(),)).fetchall()
    for row in rows:
        inv = serialize(row, today)
        subject, body = reminder_message(inv, company)
        channels, err = notifier.send(subject, body, internal)
        with _db_lock, repo.conn:
            repo.conn.execute("UPDATE invoices SET reminder_date_done = 1 WHERE id = ?", (inv["id"],))
        record(inv, "REMINDER", internal, subject, body, channels, err)

    with _db_lock:
        rows = repo.conn.execute(
            "SELECT * FROM invoices WHERE status = 'SENT' AND reminders_enabled = 1 AND due_date < ?",
            (today.isoformat(),)).fetchall()
    for row in rows:
        inv = serialize(row, today)
        last = inv["last_overdue_reminder"]
        if last and (today - date.fromisoformat(last)).days < repeat:
            continue
        recipients = list(internal)
        if to_customer and inv["contact_email"]:
            recipients.append(inv["contact_email"])
        subject, body = overdue_message(inv, company)
        channels, err = notifier.send(subject, body, recipients)
        with _db_lock, repo.conn:
            repo.conn.execute(
                "UPDATE invoices SET overdue_reminder_count = overdue_reminder_count + 1, "
                "last_overdue_reminder = ? WHERE id = ?", (today.isoformat(), inv["id"]))
        record(inv, "OVERDUE", recipients, subject, body, channels, err)

    if sent:
        log.info("reminder job sent %d message(s)", len(sent))
    return sent


def start_scheduler(repo: Repo, cfg: configparser.ConfigParser) -> threading.Thread:
    interval = max(1, cfg["app"].getint("reminder_check_interval_minutes")) * 60

    def loop():
        while True:
            try:
                run_reminders(repo, cfg)
            except Exception:  # noqa: BLE001
                log.exception("reminder job failed")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="reminder-scheduler", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------------------
# HTTP API + static UI
# --------------------------------------------------------------------------------------

def make_handler(repo: Repo, cfg: configparser.ConfigParser):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InvoiceTracker/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers --
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

        def _error(self, status: int, message: str):
            self._json({"error": message}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValidationError("JSON object expected")
            return data

        def _route(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            return parts, dict(
                kv.split("=", 1) if "=" in kv else (kv, "")
                for kv in (urlparse(self.path).query or "").split("&") if kv)

        def _dispatch(self, method: str):
            try:
                parts, query = self._route()
                if method == "GET" and parts in ([], ["index.html"]):
                    return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if not parts or parts[0] != "api":
                    return self._error(404, "not found")
                api = parts[1:]

                if api == ["config"] and method == "GET":
                    a = cfg["app"]
                    return self._json({
                        "company_name": a["company_name"], "currency": a["currency"],
                        "default_payment_terms_days": a.getint("default_payment_terms_days"),
                        "default_vat_rate": a["default_vat_rate"],
                        "vat_rates": [str(r) for r in DUTCH_VAT_RATES],
                        "repeat_every_days": cfg["reminders"].getint("repeat_every_days"),
                        "notify_to": cfg["reminders"]["notify_to"],
                        "email_configured": bool(cfg["smtp"]["host"]),
                        "webhook_configured": bool(cfg["webhook"]["url"]),
                    })
                if api == ["summary"] and method == "GET":
                    return self._json(repo.summary())
                if api == ["notifications"] and method == "GET":
                    return self._json(repo.notifications())
                if api == ["reminders", "run"] and method == "POST":
                    return self._json({"sent": run_reminders(repo, cfg)})
                if api == ["export.csv"] and method == "GET":
                    body = ("﻿" + repo.export_csv()).encode("utf-8")
                    return self._send(200, body, "text/csv; charset=utf-8", {
                        "Content-Disposition": f'attachment; filename="invoices-{date.today()}.csv"'})
                if api == ["invoices"]:
                    if method == "GET":
                        from urllib.parse import unquote_plus
                        return self._json(repo.list(query.get("status") or None,
                                                    unquote_plus(query.get("q", "")) or None))
                    if method == "POST":
                        return self._json(repo.create(self._body()), 201)
                if len(api) >= 2 and api[0] == "invoices" and api[1].isdigit():
                    inv_id = int(api[1])
                    action = api[2] if len(api) > 2 else None
                    result = None
                    if action is None and method == "GET":
                        result = repo.get(inv_id)
                    elif action is None and method == "PUT":
                        result = repo.update(inv_id, self._body())
                    elif action is None and method == "DELETE":
                        return self._json({"deleted": True}) if repo.delete(inv_id) else self._error(404, "invoice not found")
                    elif action == "paid" and method == "POST":
                        b = self._body()
                        result = repo.mark_paid(inv_id, b.get("paid_date"))
                    elif action == "sent" and method == "POST":
                        result = repo.mark_sent(inv_id, self._body().get("sent_date"))
                    elif action == "reopen" and method == "POST":
                        result = repo.update(inv_id, {"status": "SENT"})
                    else:
                        return self._error(405, "method not allowed")
                    return self._json(result) if result else self._error(404, "invoice not found")
                return self._error(404, "not found")
            except ValidationError as e:
                return self._error(400, str(e))
            except json.JSONDecodeError:
                return self._error(400, "invalid JSON")
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                return self._error(500, f"internal error: {e}")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Invoice Tracker")
    parser.add_argument("--config", type=Path, help="path to config.ini")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the web app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-scheduler", action="store_true", help="do not run the reminder job in the background")
    sub.add_parser("check-reminders", help="run the reminder / overdue job once and exit")
    sub.add_parser("init", help="create the database")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    conn = connect(resolve_path(cfg["app"]["database"]))
    repo = Repo(conn, cfg["app"].getint("default_payment_terms_days"))

    if args.cmd == "init":
        print(f"Database ready: {resolve_path(cfg['app']['database'])}")
    elif args.cmd == "check-reminders":
        sent = run_reminders(repo, cfg)
        for m in sent:
            print(f"{m['kind']:9} {m['invoice_number']:15} via {','.join(m['channels'])}"
                  + (f"  ERROR: {m['error']}" if m["error"] else ""))
        print(f"{len(sent)} message(s) sent.")
        return 1 if any(m["error"] for m in sent) else 0
    elif args.cmd == "serve":
        host = args.host or cfg["app"]["host"]
        port = args.port or cfg["app"].getint("port")
        if not args.no_scheduler:
            start_scheduler(repo, cfg)
        httpd = ThreadingHTTPServer((host, port), make_handler(repo, cfg))
        log.info("Invoice Tracker running on http://%s:%d  (Ctrl+C to stop)", host, port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
