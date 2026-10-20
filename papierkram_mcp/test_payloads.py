"""Tests für die reinen Payload-Funktionen des Papierkram-MCP (ohne API/Netz).

Aufruf aus dem Repo-Root:
    papierkram_mcp/.venv/bin/python -m unittest papierkram_mcp/test_payloads.py -v
"""

import asyncio
import json
import logging
import os
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

ANTHROPIC = 244
STRATO = 65


class SupplierPayloadTest(unittest.TestCase):
    def test_supplier_payload_uses_contact_type(self):
        payload = server._supplier_payload("OpenRouter")
        self.assertEqual(payload["contact_type"], "supplier")
        self.assertEqual(payload["name"], "OpenRouter")
        self.assertNotIn("supplier", payload)


class ProvenanceTest(unittest.TestCase):
    def test_anthropic_244_with_19_percent_is_domestic(self):
        self.assertEqual(server._resolve_provenance(ANTHROPIC, "19%"), "domestic")

    def test_anthropic_244_with_0_percent_is_foreign(self):
        self.assertEqual(server._resolve_provenance(ANTHROPIC, "0%"), "foreign")

    def test_strato_65_with_19_percent_is_domestic(self):
        self.assertEqual(server._resolve_provenance(STRATO, "19%"), "domestic")

    def test_explicit_non_eu_raises_value_error(self):
        with self.assertRaises(ValueError):
            server._resolve_provenance(ANTHROPIC, "0%", provenance="non_eu")

    def test_explicit_valid_value_wins(self):
        self.assertEqual(
            server._resolve_provenance(ANTHROPIC, "19%", provenance="foreign"),
            "foreign",
        )


class ReceiptPayloadTest(unittest.TestCase):
    def test_receipt_payload_structure(self):
        payload = server._receipt_payload(
            name="Anthropic September 2026",
            supplier_id=ANTHROPIC,
            amount_gross=214.2,
            vat_rate="19",
            date="2026-09-29",
            due_date="2026-10-13",
            category="Software/EDV (Büroartikel)",
        )
        self.assertEqual(
            payload,
            {
                "name": "Anthropic September 2026",
                "creditor": {"id": ANTHROPIC},
                "document_date": "2026-09-29",
                "due_date": "2026-10-13",
                "line_items": [
                    {
                        "name": "Anthropic September 2026",
                        "amount": 214.2,
                        "vat_rate": "19%",
                        "category": "Software/EDV (Büroartikel)",
                    }
                ],
                "provenance": "domestic",
            },
        )

    def test_receipt_payload_rejects_non_eu(self):
        with self.assertRaises(ValueError):
            server._receipt_payload(
                "x", ANTHROPIC, 1.0, "0%", "2026-09-29", "2026-10-13", "c",
                provenance="non_eu",
            )


_REAL_ASYNC_CLIENT = httpx.AsyncClient
# httpx-INFO-Logs würden die "... ok"-Zeile der -v-Ausgabe zerreißen.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _mock_api(handler):
    """Patcht httpx.AsyncClient auf MockTransport — kein Request verlässt den Prozess."""
    transport = httpx.MockTransport(handler)
    return mock.patch.object(
        server.httpx,
        "AsyncClient",
        side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
    )


def _voucher_api(requests):
    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/expense/vouchers"):
            return httpx.Response(201, json={"id": 4711, "amount": 214.2, "state": "unpaid"})
        if request.url.path.endswith("/pay"):
            return httpx.Response(200, json={"id": 4711})
        if request.method == "GET":
            return httpx.Response(200, json={"id": 4711, "state": "paid"})
        return httpx.Response(404)

    return handler


def _capture(mark_paid):
    return json.loads(
        asyncio.run(
            server.papierkram_capture_receipt(
                name="Anthropic Oktober 2026",
                supplier_id=ANTHROPIC,
                amount_gross=214.2,
                vat_rate="19%",
                date="2026-10-20",
                category="Software/EDV (Büroartikel)",
                mark_paid=mark_paid,
            )
        )
    )


class MarkPaidSwitchTest(unittest.TestCase):
    def test_switch_values(self):
        for wert in ("", "1", "true", "ja", "on"):
            with mock.patch.dict(os.environ, {"PAPIERKRAM_MARK_PAID_ERLAUBT": wert}):
                self.assertTrue(server._mark_paid_erlaubt(), wert)
        for wert in ("0", "false", "FALSE", "nein", "No", "off"):
            with mock.patch.dict(os.environ, {"PAPIERKRAM_MARK_PAID_ERLAUBT": wert}):
                self.assertFalse(server._mark_paid_erlaubt(), wert)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(server._mark_paid_erlaubt())

    def test_capture_receipt_skips_pay_when_mark_paid_disabled(self):
        requests = []
        with mock.patch.dict(os.environ, {"PAPIERKRAM_MARK_PAID_ERLAUBT": "0"}), \
                _mock_api(_voucher_api(requests)):
            result = _capture(mark_paid=True)
        self.assertEqual(requests, [("POST", "/api/v1/expense/vouchers")])
        self.assertFalse(any(path.endswith("/pay") for _, path in requests))
        self.assertIs(result["mark_paid_ignoriert"], True)
        self.assertIn("hinweis", result)
        self.assertNotIn("marked_paid", result)

    def test_capture_receipt_pays_when_switch_unset(self):
        requests = []
        env = {k: v for k, v in os.environ.items() if k != "PAPIERKRAM_MARK_PAID_ERLAUBT"}
        with mock.patch.dict(os.environ, env, clear=True), _mock_api(_voucher_api(requests)):
            result = _capture(mark_paid=True)
        self.assertIn(("POST", "/api/v1/expense/vouchers/4711/pay"), requests)
        self.assertIs(result["marked_paid"], True)
        self.assertNotIn("mark_paid_ignoriert", result)

    def test_mark_paid_tool_refuses_without_api_call_when_disabled(self):
        requests = []
        with mock.patch.dict(os.environ, {"PAPIERKRAM_MARK_PAID_ERLAUBT": "off"}), \
                _mock_api(_voucher_api(requests)):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(server.papierkram_mark_paid(42, "2026-10-20", 100.0))
        self.assertIn("PAPIERKRAM_MARK_PAID_ERLAUBT", str(ctx.exception))
        self.assertEqual(requests, [])


def _company(id_, name, contact_type="supplier", record_state="active"):
    # Voller Datensatz wie in der API — die Zusatzfelder dürfen nicht durchgereicht werden.
    return {
        "type": "company", "id": id_, "name": name, "contact_type": contact_type,
        "record_state": record_state, "email": "x@example.com", "bank_iban": "DE00",
        "notes": "lang " * 50,
    }


class SearchSupplierTest(unittest.TestCase):
    def test_search_supplier_filters_substring_and_reduces_fields(self):
        pages = {
            1: [
                _company(229, "Hetzner Online GmbH"),
                _company(244, "Anthropic PBC"),
                _company(300, "Hetzner Kunde AG", contact_type="customer"),
            ],
            2: [
                _company(301, "HETZNER Alt GmbH", record_state="archived"),
                _company(302, "Mein hetzner-Reseller"),
            ],
        }
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path, dict(request.url.params)))
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json={
                "type": "list", "page": page, "page_size": 100, "total_pages": 2,
                "total_entries": 5, "has_more": page < 2, "entries": pages[page],
            })

        with _mock_api(handler):
            result = json.loads(asyncio.run(server.papierkram_search_supplier("hETZner")))

        self.assertEqual([r[2].get("page") for r in requests], ["1", "2"])
        self.assertTrue(all(r[1] == "/api/v1/contact/companies" for r in requests))
        self.assertTrue(all(r[2].get("page_size") == "100" for r in requests))
        self.assertEqual(result["count"], 2)
        self.assertEqual([s["id"] for s in result["suppliers"]], [229, 302])
        for s in result["suppliers"]:
            self.assertEqual(set(s), {"id", "name", "contact_type"})
            self.assertEqual(s["contact_type"], "supplier")


def _list_page(pages, page):
    return httpx.Response(200, json={
        "type": "list", "page": page, "page_size": 100, "total_pages": len(pages),
        "total_entries": sum(len(e) for e in pages.values()),
        "has_more": page < len(pages), "entries": pages[page],
    })


def _voucher(id_, state="unpaid", record_state="active", creditor=True):
    # Voller VoucherFlat-Datensatz wie in der API — Zusatzfelder dürfen nicht durch.
    v = {
        "type": "expense_voucher", "id": id_, "name": f"Beleg {id_}",
        "due_date": "2026-10-31", "document_date": "2026-10-01",
        "description": "lang " * 50, "flagged": False, "provenance": "domestic",
        "voucher_no": f"B-{id_:05d}", "state": state, "record_state": record_state,
        "amount": 119.0, "invoice_amount": 0.0, "entertainment_persons": [],
    }
    if creditor:
        v["creditor"] = {"type": "company", "id": 229, "name": "Hetzner Online GmbH",
                         "contact_type": "supplier", "email": "x@example.com"}
    return v


class FindReceiptsUnpaidTest(unittest.TestCase):
    def test_find_receipts_unpaid_only_scans_all_pages_slim(self):
        pages = {
            1: [_voucher(1), _voucher(2, state="paid")],
            2: [_voucher(3, record_state="archived"), _voucher(4, creditor=False)],
        }
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path, dict(request.url.params)))
            if request.url.path == "/api/v1/expense/vouchers":
                return _list_page(pages, int(request.url.params.get("page", "1")))
            if request.url.path == "/api/v1/expense/vouchers/4":
                detail = _voucher(4)
                detail["creditor"] = {"type": "company", "id": 244, "name": "Anthropic PBC"}
                return httpx.Response(200, json=detail)
            return httpx.Response(404)

        with _mock_api(handler):
            result = json.loads(asyncio.run(server.papierkram_find_receipts(unpaid_only=True)))

        list_pages = [r[2].get("page") for r in requests if r[1] == "/api/v1/expense/vouchers"]
        self.assertEqual(list_pages, ["1", "2"])
        self.assertTrue(all(r[0] == "GET" for r in requests))
        self.assertEqual(result["api_pages_fetched"], 2)
        self.assertEqual(result["count"], 2)
        self.assertEqual([r["id"] for r in result["entries"]], [1, 4])
        for r in result["entries"]:
            self.assertEqual(
                set(r),
                {"id", "voucher_no", "name", "supplier", "document_date", "due_date",
                 "amount", "state"},
            )
            self.assertEqual(set(r["supplier"]), {"id", "name"})
            self.assertEqual(r["state"], "unpaid")
            self.assertEqual(r["amount"], 119.0)
        self.assertEqual(result["entries"][0]["supplier"], {"id": 229, "name": "Hetzner Online GmbH"})
        self.assertEqual(result["entries"][1]["supplier"], {"id": 244, "name": "Anthropic PBC"})


def _invoice(id_, state="unpaid", record_state="active", due_date="2026-10-10"):
    # Voller InvoiceFlat-Datensatz wie in der API (ohne customer).
    return {
        "type": "invoice", "id": id_, "name": f"Rechnung {id_}",
        "description": "lang " * 50, "document_date": "2026-09-26", "due_date": due_date,
        "invoice_no": f"RE-{id_:05d}", "state": state, "record_state": record_state,
        "total_net": 100.0, "total_vat": 19.0, "total_gross": 119.0,
        "outstanding_amount": "119.0",
        "billing": {"company": f"Kunde {id_} GmbH", "street": "Dotzheimer Str. 36"},
    }


class OpenInvoicesTest(unittest.TestCase):
    def test_open_invoices_only_unpaid_active_all_pages(self):
        pages = {
            1: [_invoice(10), _invoice(11, state="paid"), _invoice(12, state="draft")],
            2: [_invoice(13, record_state="archived"), _invoice(14, due_date="2026-10-25")],
        }
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path, dict(request.url.params)))
            if request.url.path == "/api/v1/income/invoices":
                return _list_page(pages, int(request.url.params.get("page", "1")))
            invoice_id = int(request.url.path.rsplit("/", 1)[1])
            detail = _invoice(invoice_id)
            detail["customer"] = {"type": "company", "id": 900 + invoice_id,
                                  "name": f"Kunde {invoice_id} GmbH", "contact_type": "customer"}
            return httpx.Response(200, json=detail)

        heute = server.datetime(2026, 10, 20).date()
        with _mock_api(handler), mock.patch.object(server, "_heute", return_value=heute):
            result = json.loads(asyncio.run(server.papierkram_open_invoices()))

        list_pages = [r[2].get("page") for r in requests if r[1] == "/api/v1/income/invoices"]
        self.assertEqual(list_pages, ["1", "2"])
        self.assertTrue(all(r[0] == "GET" for r in requests))
        self.assertEqual(result["count"], 2)
        self.assertEqual([i["id"] for i in result["invoices"]], [10, 14])
        for i in result["invoices"]:
            self.assertEqual(
                set(i),
                {"id", "invoice_no", "name", "customer", "invoice_date", "due_date",
                 "total_gross", "state", "tage_ueberfaellig"},
            )
            self.assertEqual(i["state"], "unpaid")
            self.assertEqual(i["total_gross"], 119.0)
            self.assertEqual(i["invoice_date"], "2026-09-26")
        self.assertEqual(result["invoices"][0]["customer"], {"id": 910, "name": "Kunde 10 GmbH"})
        self.assertEqual(result["invoices"][0]["tage_ueberfaellig"], 10)
        self.assertEqual(result["invoices"][1]["tage_ueberfaellig"], -5)


def _pay_api(requests, pay_response, voucher_amount=238.004):
    states = iter(["unpaid", "paid", "paid", "paid"])

    def handler(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/pay"):
            return pay_response
        if request.method == "GET":
            return httpx.Response(200, json={
                "type": "expense_voucher", "id": 4711, "state": next(states),
                "record_state": "active", "amount": voucher_amount,
            })
        return httpx.Response(404)

    return handler


class MarkReceiptPaidTest(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "PAPIERKRAM_MARK_PAID_ERLAUBT"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mark_receipt_paid_uses_voucher_amount(self):
        requests = []
        with _mock_api(_pay_api(requests, httpx.Response(200, json={"id": 4711}))):
            result = json.loads(asyncio.run(server.papierkram_mark_receipt_paid(4711, "2026-10-20")))
        self.assertEqual(requests[0][:2], ("GET", "/api/v1/expense/vouchers/4711"))
        pay = [r for r in requests if r[1].endswith("/pay")]
        self.assertEqual(pay, [("POST", "/api/v1/expense/vouchers/4711/pay",
                                {"payment_date": "2026-10-20", "value": 238.0})])
        self.assertEqual(requests[-1][:2], ("GET", "/api/v1/expense/vouchers/4711"))
        self.assertEqual(result["value"], 238.0)
        self.assertEqual(result["verified_state"], "paid")
        self.assertIs(result["bezahlt_bestaetigt"], True)
        self.assertIs(result["ok"], True)
        self.assertNotIn("bereits_bezahlt", result)

    def test_mark_receipt_paid_422_difference_reason_already_paid(self):
        requests = []
        resp_422 = httpx.Response(422, json={
            "error": "unprocessable_entity", "message": "Difference reason ist kein gültiger Wert",
        })
        with _mock_api(_pay_api(requests, resp_422)):
            result = json.loads(asyncio.run(server.papierkram_mark_receipt_paid(4711, "2026-10-20")))
        self.assertIs(result["bereits_bezahlt"], True)
        self.assertIn("bereits bezahlt", result["meldung"])
        self.assertEqual(result["pay_status_code"], 422)
        self.assertEqual(requests[-1][:2], ("GET", "/api/v1/expense/vouchers/4711"))
        self.assertEqual(result["verified_state"], "paid")
        self.assertIs(result["ok"], True)

    def test_mark_receipt_paid_other_422_raises(self):
        requests = []
        resp_422 = httpx.Response(422, json={"message": "Payment date ist ungültig"})
        with _mock_api(_pay_api(requests, resp_422)):
            with self.assertRaises(RuntimeError):
                asyncio.run(server.papierkram_mark_receipt_paid(4711, "2026-10-20"))

    def test_mark_receipt_paid_switch_off_no_pay(self):
        requests = []
        with mock.patch.dict(os.environ, {"PAPIERKRAM_MARK_PAID_ERLAUBT": "0"}), \
                _mock_api(_pay_api(requests, httpx.Response(200, json={}))):
            result = json.loads(asyncio.run(server.papierkram_mark_receipt_paid(4711, "2026-10-20")))
        self.assertEqual(requests, [])
        self.assertIs(result["ok"], False)
        self.assertIs(result["mark_paid_ignoriert"], True)
        self.assertIn("PAPIERKRAM_MARK_PAID_ERLAUBT", result["hinweis"])


if __name__ == "__main__":
    unittest.main()
