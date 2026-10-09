"""Tests für die reinen Payload-Funktionen des Papierkram-MCP (ohne API/Netz).

Aufruf aus dem Repo-Root:
    papierkram_mcp/.venv/bin/python -m unittest papierkram_mcp/test_payloads.py -v
"""

import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
