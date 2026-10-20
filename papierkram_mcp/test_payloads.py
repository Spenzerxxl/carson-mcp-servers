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


if __name__ == "__main__":
    unittest.main()
