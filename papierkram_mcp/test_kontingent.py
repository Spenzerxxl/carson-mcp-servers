"""Tests für den Kontingent-Schutz des Papierkram-MCP (Zähler, Sperre, Warnung).

Ohne Netz: httpx.AsyncClient wird auf MockTransport gepatcht. Der Zustand liegt
je Test in einem eigenen Temp-Verzeichnis; _quota_pfad wird zusätzlich gepatcht,
damit nie die echte Datei unter ~/.local/state berührt wird.

Aufruf:
    cd papierkram_mcp && .venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_BERLIN = server._TZ_BERLIN
logging.getLogger("httpx").setLevel(logging.WARNING)
# Erwartete Warn-/Sperr-Logzeilen nicht in die -v-Ausgabe kippen (assertLogs geht weiter).
logging.getLogger("papierkram_mcp.kontingent").propagate = False
logging.getLogger("papierkram_mcp.kontingent").addHandler(logging.NullHandler())
_ECHTE_WARNUNG = server._kontingent_warnung_melden

QUOTA_429 = {"error_type": "too_many_requests", "message": "API quota exceeded."}


def _mock_api(handler):
    transport = httpx.MockTransport(handler)
    return mock.patch.object(
        server.httpx,
        "AsyncClient",
        side_effect=lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
    )


def _berlin(*args):
    return datetime(*args, tzinfo=_BERLIN)


class _KontingentTestBasis(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="papierkram-kontingent-test-")
        self.addCleanup(tmp.cleanup)
        self.pfad = os.path.join(tmp.name, "state", "quota.json")
        for patcher in (
            mock.patch.object(server, "_quota_pfad", return_value=self.pfad),
            mock.patch.dict(os.environ, {"PAPIERKRAM_MONATSBUDGET": "10000"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.jetzt = _berlin(2026, 11, 27, 9, 0)
        p = mock.patch.object(server, "_jetzt", side_effect=lambda: self.jetzt)
        p.start()
        self.addCleanup(p.stop)
        # Warnung nie echt melden — nur zählen, wie oft sie ausgelöst würde.
        p = mock.patch.object(server, "_kontingent_warnung_melden")
        self.warnung = p.start()
        self.addCleanup(p.stop)
        self.requests = []

    def zustand(self):
        with open(self.pfad, encoding="utf-8") as fh:
            return json.load(fh)

    def zustand_setzen(self, zustand):
        os.makedirs(os.path.dirname(self.pfad), exist_ok=True)
        with open(self.pfad, "w", encoding="utf-8") as fh:
            json.dump(zustand, fh)

    def handler_ok(self, request):
        self.requests.append((request.method, request.url.path))
        return httpx.Response(200, json=[])

    def articles(self):
        return asyncio.run(server.papierkram_list_articles())


# Alle 10 Tools mit gültigen Argumenten — jedes muss bei Sperre ohne Request abbrechen.
_ALLE_TOOLS = [
    ("papierkram_find_receipts", {}),
    ("papierkram_capture_receipt", dict(
        name="Test", supplier_id=244, amount_gross=1.0, vat_rate="19%",
        date="2026-11-27", category="Software/EDV (Büroartikel)", mark_paid=True)),
    ("papierkram_open_invoices", {}),
    ("papierkram_search_supplier", dict(query="hetzner")),
    ("papierkram_create_supplier", dict(name="Neu GmbH")),
    ("papierkram_mark_paid", dict(invoice_id=1, payment_date="2026-11-27", amount=1.0)),
    ("papierkram_mark_receipt_paid", dict(voucher_id=1, payment_date="2026-11-27")),
    ("papierkram_list_articles", {}),
    ("papierkram_create_article", dict(name="Beratung", price=100.0)),
    ("papierkram_create_invoice_draft", dict(
        customer_company_id=1, items=[{"name": "x", "quantity": 1, "price": 1.0}])),
]


class SperreTest(_KontingentTestBasis):
    def test_alle_tools_sind_geschuetzt(self):
        tools = asyncio.run(server.mcp.list_tools())
        namen = sorted(t.name for t in tools)
        self.assertEqual(namen, sorted(n for n, _ in _ALLE_TOOLS))
        for name in namen:
            fn = server.mcp._tool_manager.get_tool(name).fn
            self.assertTrue(getattr(fn, "_kontingent_geschuetzt", False), name)

    def test_aktive_sperre_kein_request_und_klare_meldung(self):
        """(a) Sperre aktiv → kein einziger HTTP-Request, klare Meldung — für alle Tools."""
        self.zustand_setzen({
            "monat": "2026-11", "zaehler": 9999,
            "sperre_bis": _berlin(2026, 12, 1).isoformat(),
        })
        with _mock_api(self.handler_ok):
            for name, kwargs in _ALLE_TOOLS:
                with self.subTest(tool=name):
                    with self.assertRaises(server.PapierkramKontingentGesperrt) as ctx:
                        asyncio.run(getattr(server, name)(**kwargs))
                    self.assertIn(
                        "Papierkram-Kontingent für November 2026 erschöpft, wieder ab 01.12.",
                        str(ctx.exception),
                    )
        self.assertEqual(self.requests, [])
        self.assertEqual(self.zustand()["zaehler"], 9999)

    def test_sperre_vor_mark_paid_schalter(self):
        """Die Sperre wird zuerst geprüft — auch vor PAPIERKRAM_MARK_PAID_ERLAUBT."""
        self.zustand_setzen({"sperre_bis": _berlin(2026, 12, 1).isoformat()})
        with mock.patch.dict(os.environ, {"PAPIERKRAM_MARK_PAID_ERLAUBT": "0"}), \
                _mock_api(self.handler_ok):
            with self.assertRaises(server.PapierkramKontingentGesperrt):
                asyncio.run(server.papierkram_mark_paid(1, "2026-11-27", 1.0))
        self.assertEqual(self.requests, [])

    def test_429_quota_exceeded_setzt_sperre_bis_monatsersten(self):
        def handler(request):
            self.requests.append((request.method, request.url.path))
            return httpx.Response(429, json=QUOTA_429)

        with _mock_api(handler):
            with self.assertRaises(server.PapierkramKontingentGesperrt) as ctx:
                self.articles()
            self.assertIn("wieder ab 01.12.", str(ctx.exception))
            self.assertEqual(len(self.requests), 1)
            zustand = self.zustand()
            self.assertEqual(
                datetime.fromisoformat(zustand["sperre_bis"]), _berlin(2026, 12, 1, 0, 0)
            )
            # Folgeaufruf: kein weiterer Request.
            with self.assertRaises(server.PapierkramKontingentGesperrt):
                self.articles()
        self.assertEqual(len(self.requests), 1)

    def test_429_quota_exceeded_gross_klein_egal(self):
        def handler(request):
            return httpx.Response(429, text="Api QUOTA Exceeded")

        with _mock_api(handler), self.assertRaises(server.PapierkramKontingentGesperrt):
            self.articles()
        self.assertIn("sperre_bis", self.zustand())

    def test_429_ohne_quota_exceeded_setzt_keine_sperre(self):
        def handler(request):
            self.requests.append(request.method)
            return httpx.Response(429, json={"message": "Rate limit, slow down"})

        with _mock_api(handler):
            with self.assertRaises(httpx.HTTPStatusError):
                self.articles()
            self.assertNotIn("sperre_bis", self.zustand())
            with self.assertRaises(httpx.HTTPStatusError):
                self.articles()
        self.assertEqual(len(self.requests), 2)

    def test_sperre_endet_am_monatsersten(self):
        self.zustand_setzen({
            "monat": "2026-11", "zaehler": 10000,
            "sperre_bis": _berlin(2026, 12, 1).isoformat(),
        })
        self.jetzt = _berlin(2026, 11, 30, 23, 59, 59)
        with _mock_api(self.handler_ok):
            with self.assertRaises(server.PapierkramKontingentGesperrt):
                self.articles()
            self.jetzt = _berlin(2026, 12, 1, 0, 0, 1)
            self.articles()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.zustand()["monat"], "2026-12")
        self.assertEqual(self.zustand()["zaehler"], 1)

    def test_sperre_im_dezember_meldet_januar(self):
        self.jetzt = _berlin(2026, 12, 15, 12, 0)
        bis = server._sperre_setzen()
        self.assertEqual(bis, _berlin(2027, 1, 1))
        self.assertEqual(
            server._sperrmeldung(bis),
            "Papierkram-Kontingent für Dezember 2026 erschöpft, wieder ab 01.01. "
            "Es wurde keine Anfrage an Papierkram gesendet.",
        )

    def test_schreibtool_laeuft_nach_429_mitten_im_aufruf_unveraendert_weiter(self):
        """capture_receipt: Beleg angelegt, /pay bekommt 429-quota → Ergebnis bleibt erhalten."""
        def handler(request):
            self.requests.append((request.method, request.url.path))
            if request.method == "POST" and request.url.path.endswith("/expense/vouchers"):
                return httpx.Response(201, json={"id": 4711, "amount": 1.0})
            return httpx.Response(429, json=QUOTA_429)

        with mock.patch.object(server.asyncio, "sleep", new=mock.AsyncMock()), \
                _mock_api(handler):
            result = json.loads(asyncio.run(
                server.papierkram_capture_receipt(**dict(_ALLE_TOOLS[1][1]))
            ))
        self.assertEqual(result["id"], 4711)
        self.assertEqual(result["pay_status_code"], 429)
        self.assertIs(result["marked_paid"], False)
        self.assertIn("sperre_bis", self.zustand())


class ZaehlerTest(_KontingentTestBasis):
    def test_jeder_request_wird_gezaehlt(self):
        def handler(request):
            self.requests.append(request.url.params.get("page"))
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json={
                "entries": [{"id": page, "name": f"Firma {page}",
                             "contact_type": "supplier", "record_state": "active"}],
                "page": page, "total_pages": 3, "has_more": page < 3,
            })

        with _mock_api(handler):
            asyncio.run(server.papierkram_search_supplier("firma"))
        self.assertEqual(self.zustand()["zaehler"], len(self.requests))
        self.assertGreaterEqual(len(self.requests), 3)

    def test_zaehler_setzt_sich_bei_monatswechsel_zurueck(self):
        """(b) Übergang in einen neuen Kalendermonat → Zähler und Warn-Merker zurück."""
        self.zustand_setzen({
            "monat": "2026-11", "zaehler": 8500, "warnung_gesendet": True,
        })
        with _mock_api(self.handler_ok):
            self.jetzt = _berlin(2026, 11, 30, 23, 59)
            self.articles()
            self.assertEqual(self.zustand()["zaehler"], 8501)
            self.jetzt = _berlin(2026, 12, 1, 0, 0)
            self.articles()
        zustand = self.zustand()
        self.assertEqual(zustand["monat"], "2026-12")
        self.assertEqual(zustand["zaehler"], 1)
        self.assertFalse(zustand["warnung_gesendet"])

    def test_monatsgrenze_gilt_in_europe_berlin(self):
        # 30.11. 23:30 UTC ist in Berlin schon der 01.12. 00:30.
        self.zustand_setzen({"monat": "2026-11", "zaehler": 42})
        self.jetzt = datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc)
        with _mock_api(self.handler_ok):
            self.articles()
        self.assertEqual(self.zustand()["monat"], "2026-12")
        self.assertEqual(self.zustand()["zaehler"], 1)

    def test_zaehler_ueberlebt_neuen_prozessstart(self):
        with _mock_api(self.handler_ok):
            self.articles()
            self.articles()
        # Nur die Datei trägt den Stand — kein Modulzustand.
        self.assertEqual(server._zustand_lesen(self.pfad)["zaehler"], 2)

    def test_atomar_ohne_temp_reste(self):
        with _mock_api(self.handler_ok):
            for _ in range(3):
                self.articles()
        self.assertEqual(os.listdir(os.path.dirname(self.pfad)), ["quota.json"])

    def test_sperrpruefung_legt_keine_datei_an(self):
        server._sperre_pruefen()
        self.assertFalse(os.path.exists(os.path.dirname(self.pfad)))


class WarnungTest(_KontingentTestBasis):
    def test_warnung_genau_einmal_pro_monat(self):
        """(c) 80-%-Schwelle: genau eine Warnung je Kalendermonat, auch bei vielen Aufrufen."""
        with mock.patch.dict(os.environ, {"PAPIERKRAM_MONATSBUDGET": "10"}), \
                _mock_api(self.handler_ok):
            for _ in range(7):
                self.articles()
            self.warnung.assert_not_called()
            self.articles()  # 8. Request = 80 % von 10
            self.warnung.assert_called_once_with("2026-11", 8, 10)
            for _ in range(5):  # weiter über der Schwelle, auch über 100 %
                self.articles()
            self.assertEqual(self.warnung.call_count, 1)
            self.assertTrue(self.zustand()["warnung_gesendet"])

            # Neuer Monat: wieder genau einmal.
            self.jetzt = _berlin(2026, 12, 2, 8, 0)
            for _ in range(12):
                self.articles()
        self.assertEqual(self.warnung.call_count, 2)
        self.warnung.assert_called_with("2026-12", 8, 10)

    def test_warnmerker_ueberlebt_neustart(self):
        self.zustand_setzen({"monat": "2026-11", "zaehler": 8000, "warnung_gesendet": True})
        with _mock_api(self.handler_ok):
            self.articles()
        self.warnung.assert_not_called()
        self.assertEqual(self.zustand()["zaehler"], 8001)

    def test_default_budget_schwelle_8000(self):
        self.zustand_setzen({"monat": "2026-11", "zaehler": 7999})
        with mock.patch.dict(os.environ, {}, clear=True), _mock_api(self.handler_ok):
            self.articles()
        self.warnung.assert_called_once_with("2026-11", 8000, 10000)

    def test_warnung_schreibt_logzeile(self):
        # Echte Meldefunktion: nur eine Logzeile, kein Push.
        with self.assertLogs("papierkram_mcp.kontingent", level="WARNING") as logs:
            _ECHTE_WARNUNG("2026-11", 8000, 10000)
        self.assertIn("8000 von 10000", logs.output[0])
        self.assertIn("Push an Frank NICHT ausgeloest", logs.output[0])


class EnvTest(unittest.TestCase):
    def test_monatsbudget_beim_aufruf_gelesen(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._monatsbudget(), 10000)
        with mock.patch.dict(os.environ, {"PAPIERKRAM_MONATSBUDGET": "500"}):
            self.assertEqual(server._monatsbudget(), 500)
        for wert in ("abc", "0", "-5"):
            with mock.patch.dict(os.environ, {"PAPIERKRAM_MONATSBUDGET": wert}), \
                    self.assertLogs("papierkram_mcp.kontingent", level="WARNING"):
                self.assertEqual(server._monatsbudget(), 10000)

    def test_quota_pfad_env_und_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PAPIERKRAM_QUOTA_DATEI": f"{tmp}/q.json"}):
                self.assertEqual(server._quota_pfad(), f"{tmp}/q.json")
            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
                self.assertEqual(
                    server._quota_pfad(),
                    os.path.join(tmp, ".local/state/papierkram-mcp/quota.json"),
                )
            self.assertEqual(os.listdir(tmp), [])  # nur berechnet, nichts angelegt

    def test_warnschwelle(self):
        self.assertEqual(server._warnschwelle(10000), 8000)
        self.assertEqual(server._warnschwelle(10), 8)
        self.assertEqual(server._warnschwelle(7), 6)  # 5,6 → aufgerundet


if __name__ == "__main__":
    unittest.main()
