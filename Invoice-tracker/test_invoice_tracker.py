"""Run with:  python -m unittest test_invoice_tracker -v"""

import configparser
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

import invoice_tracker as it


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, recipients):
        self.sent.append((subject, body, recipients))
        return ["test"], None


def make_cfg(tmp: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(it.DEFAULT_CONFIG)
    cfg["reminders"]["notify_to"] = "finance@example.nl"
    cfg["reminders"]["outbox_file"] = str(tmp / "outbox.log")
    return cfg


class MoneyTests(unittest.TestCase):
    def test_parse_amount_formats(self):
        for raw in ("1234.56", "1234,56", "1.234,56", "1,234.56", "€ 1.234,56", 1234.56):
            self.assertEqual(it.parse_amount(raw), Decimal("1234.56"), raw)
        self.assertEqual(it.parse_amount("8.000"), Decimal("8000.00"))
        self.assertEqual(it.parse_amount("1.250.000"), Decimal("1250000.00"))
        self.assertEqual(it.parse_amount("12.5"), Decimal("12.50"))

    def test_vat_rounding_half_up(self):
        vat, gross = it.calc_vat(Decimal("10.50"), Decimal("21"))   # 2.205 -> 2.21
        self.assertEqual(vat, Decimal("2.21"))
        self.assertEqual(gross, Decimal("12.71"))
        self.assertEqual(it.calc_vat(Decimal("100"), Decimal("9")), (Decimal("9.00"), Decimal("109.00")))

    def test_fmt_eur(self):
        self.assertEqual(it.fmt_eur(123456), "€ 1.234,56")
        self.assertEqual(it.fmt_eur(-500), "€ -5,00")


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = it.Repo(it.connect(":memory:"), 30)
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.repo.conn.close()
        self.tmp.cleanup()

    def new(self, **kw):
        data = {"invoice_number": "INV-001", "customer": "Acme B.V.", "invoice_date": "2026-09-01",
                "net_amount": "1.000,00", "vat_rate": "21"}
        data.update(kw)
        return self.repo.create(data)

    def test_create_calculates_vat_and_due_date(self):
        inv = self.new(sent_date="2026-09-05")
        self.assertEqual(inv["vat_amount"], "210.00")
        self.assertEqual(inv["gross_amount"], "1210.00")
        self.assertEqual(inv["due_date"], "2026-10-05")       # sent + 30 days
        self.assertEqual(inv["status"], "SENT")

    def test_draft_without_sent_date(self):
        inv = self.new()
        self.assertEqual(inv["status"], "DRAFT")
        self.assertEqual(inv["due_date"], "2026-10-01")       # invoice date + 30 (provisional)

    def test_mark_sent_recomputes_due(self):
        inv = self.new()
        inv = self.repo.mark_sent(inv["id"], "2026-09-10")
        self.assertEqual(inv["due_date"], "2026-10-10")

    def test_explicit_due_date_kept(self):
        inv = self.new(sent_date="2026-09-05", due_date="2026-11-15")
        self.assertEqual(inv["due_date"], "2026-11-15")

    def test_duplicate_number_rejected(self):
        self.new()
        with self.assertRaises(it.ValidationError):
            self.new()

    def test_sent_before_invoice_date_rejected(self):
        with self.assertRaises(it.ValidationError):
            self.new(sent_date="2026-08-01")

    def test_mark_paid_records_date_only(self):
        inv = self.new(sent_date="2026-09-05")
        inv = self.repo.mark_paid(inv["id"], "2026-09-20")
        self.assertEqual(inv["status"], "PAID")
        self.assertEqual(inv["paid_date"], "2026-09-20")
        self.assertNotIn("paid_amount", inv)
        self.assertFalse(inv["is_overdue"])

    def test_paid_this_month_uses_invoice_total(self):
        inv = self.new(sent_date="2026-09-05")
        self.repo.mark_paid(inv["id"], date.today().isoformat())
        self.assertEqual(self.repo.summary()["paid_this_month"], "1210.00")

    def test_reopen_clears_payment_date(self):
        inv = self.new(sent_date="2026-09-05")
        self.repo.mark_paid(inv["id"], "2026-09-20")
        inv = self.repo.update(inv["id"], {"status": "SENT"})
        self.assertIsNone(inv["paid_date"])

    def test_overdue_flag_and_filter(self):
        past = (date.today() - timedelta(days=45)).isoformat()
        self.new(invoice_date=past, sent_date=past)
        self.new(invoice_number="INV-002")
        overdue = self.repo.list("OVERDUE")
        self.assertEqual([i["invoice_number"] for i in overdue], ["INV-001"])
        self.assertEqual(overdue[0]["days_overdue"], 15)
        self.assertEqual(self.repo.summary()["overdue_amount"], "1210.00")

    def test_overdue_reminder_sent_once_then_repeats(self):
        self.new(sent_date="2026-09-05")            # due 2026-10-05
        n = FakeNotifier()
        self.assertEqual(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 5)), [])  # due today: not breached
        self.assertEqual(len(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 6))), 1)
        self.assertEqual(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 6)), [])  # no duplicate
        self.assertEqual(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 12)), [])  # < 7 days
        self.assertEqual(len(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 13))), 1)
        self.assertIn("OVERDUE", n.sent[0][0])
        self.assertEqual(n.sent[0][2], ["finance@example.nl"])

    def test_no_reminder_when_paid_or_disabled(self):
        a = self.new(sent_date="2026-09-05")
        self.new(invoice_number="INV-002", sent_date="2026-09-05", reminders_enabled=False)
        self.repo.mark_paid(a["id"])
        self.assertEqual(it.run_reminders(self.repo, self.cfg, FakeNotifier(), date(2026, 12, 1)), [])

    def test_customer_copy(self):
        self.cfg["reminders"]["send_to_customer"] = "true"
        self.new(sent_date="2026-09-05", contact_email="ap@acme.nl")
        n = FakeNotifier()
        it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 10))
        self.assertEqual(n.sent[0][2], ["finance@example.nl", "ap@acme.nl"])

    def test_custom_reminder_date(self):
        self.new(sent_date="2026-09-05", reminder_date="2026-09-30")
        n = FakeNotifier()
        self.assertEqual(it.run_reminders(self.repo, self.cfg, n, date(2026, 9, 29)), [])
        sent = it.run_reminders(self.repo, self.cfg, n, date(2026, 9, 30))
        self.assertEqual([s["kind"] for s in sent], ["REMINDER"])
        self.assertEqual(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 1)), [])

    def test_moving_due_date_restarts_cycle(self):
        inv = self.new(sent_date="2026-09-05")
        n = FakeNotifier()
        it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 6))
        inv = self.repo.update(inv["id"], {"due_date": "2026-10-20"})
        self.assertEqual(inv["overdue_reminder_count"], 0)
        self.assertEqual(len(it.run_reminders(self.repo, self.cfg, n, date(2026, 10, 21))), 1)

    def test_outbox_notifier_writes_file(self):
        it.Notifier(self.cfg).send("Subj", "Body", ["x@example.nl"])
        self.assertIn("Subj", (Path(self.tmp.name) / "outbox.log").read_text())

    def test_csv_export_uses_dutch_decimals(self):
        self.new()
        csv_text = self.repo.export_csv()
        self.assertIn("1210,00", csv_text)
        self.assertIn(";", csv_text.splitlines()[0])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(Path(cls.tmp.name))
        cls.repo = it.Repo(it.connect(":memory:"), 30)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), it.make_handler(cls.repo, cfg))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.repo.conn.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_crud_flow(self):
        s, inv = self.call("POST", "/api/invoices", {"invoice_number": "H-1", "customer": "X", "invoice_date": "2026-01-01",
                                                      "net_amount": "100", "vat_rate": "9", "sent_date": "2026-01-02"})
        self.assertEqual(s, 201)
        self.assertEqual(inv["gross_amount"], "109.00")
        s, inv = self.call("POST", f"/api/invoices/{inv['id']}/paid", {"paid_date": "2026-01-20"})
        self.assertEqual((s, inv["status"]), (200, "PAID"))
        s, err = self.call("POST", "/api/invoices", {"invoice_number": "H-2"})
        self.assertEqual(s, 400)
        self.assertIn("customer", err["error"])
        s, _ = self.call("DELETE", f"/api/invoices/{inv['id']}")
        self.assertEqual(s, 200)

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Invoice Tracker", r.read())


if __name__ == "__main__":
    unittest.main()
