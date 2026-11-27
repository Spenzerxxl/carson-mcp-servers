"""Tests für papierkram_mcp/healthcheck.py — ohne Netz, ohne echten Restart.

Der MCP-Dienst wird per httpx.MockTransport simuliert, subprocess.run gemockt,
die Rate-Limit-Datei liegt in einem Temp-Verzeichnis.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import healthcheck  # noqa: E402

URL = "http://127.0.0.1:18792/mcp"


def _sse(nachricht):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "mcp-session-id": "sess-1"},
        text=f"event: message\ndata: {json.dumps(nachricht)}\n\n",
    )


class _FakeDienst:
    """Simuliert FastMCP streamable-http; protokolliert jede gesendete Methode."""

    def __init__(self, tools=("papierkram_list_articles",), init_status=200):
        self.methoden = []
        self.tools = list(tools)
        self.init_status = init_status

    def __call__(self, request):
        if request.method == "DELETE":
            self.methoden.append("DELETE")
            return httpx.Response(200)
        body = json.loads(request.content)
        methode = body.get("method")
        self.methoden.append(methode)
        if methode == "initialize":
            if self.init_status != 200:
                return httpx.Response(self.init_status, text="kaputt")
            return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "papierkram", "version": "1.26.0"},
                "capabilities": {"tools": {}},
            }})
        assert request.headers.get("mcp-session-id") == "sess-1"
        if methode == "notifications/initialized":
            return httpx.Response(202)
        if methode == "tools/list":
            return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {
                "tools": [{"name": n, "inputSchema": {}} for n in self.tools],
            }})
        return httpx.Response(400, text=f"unerwartet: {methode}")


class PruefenTest(unittest.TestCase):
    def test_gesund_nur_initialize_und_tools_list(self):
        dienst = _FakeDienst()
        ergebnis = healthcheck.pruefen(URL, 5, transport=httpx.MockTransport(dienst))
        self.assertIn("papierkram: 1 Werkzeuge", ergebnis)
        self.assertEqual(
            dienst.methoden,
            ["initialize", "notifications/initialized", "tools/list", "DELETE"],
        )
        self.assertTrue(set(dienst.methoden) <= set(healthcheck.ERLAUBTE_METHODEN) | {"DELETE"})

    def test_leere_werkzeugliste_ist_ungesund(self):
        dienst = _FakeDienst(tools=())
        with self.assertRaises(healthcheck.PruefFehler):
            healthcheck.pruefen(URL, 5, transport=httpx.MockTransport(dienst))
        self.assertIn("DELETE", dienst.methoden)  # Session trotzdem freigegeben

    def test_initialize_fehler_ist_ungesund(self):
        dienst = _FakeDienst(init_status=400)
        with self.assertRaises(healthcheck.PruefFehler):
            healthcheck.pruefen(URL, 5, transport=httpx.MockTransport(dienst))
        self.assertEqual(dienst.methoden, ["initialize"])

    def test_verbindungsfehler_ist_ungesund(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        with self.assertRaises(healthcheck.PruefFehler):
            healthcheck.pruefen(URL, 5, transport=httpx.MockTransport(handler))

    def test_andere_methoden_sind_gesperrt(self):
        with httpx.Client(transport=httpx.MockTransport(_FakeDienst())) as client:
            with self.assertRaises(ValueError):
                healthcheck._senden(client, URL, {"method": "tools/" + "call"}, {})


class RestartBegrenzungTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="papierkram-healthcheck-test-")
        self.addCleanup(tmp.cleanup)
        self.pfad = os.path.join(tmp.name, "state", "healthcheck-restart.json")
        p = mock.patch.object(healthcheck.subprocess, "run")
        self.run = p.start()
        self.addCleanup(p.stop)
        # Statuszeilen des Skripts nicht in die Testausgabe schreiben.
        for umleitung in (contextlib.redirect_stdout(io.StringIO()),
                          contextlib.redirect_stderr(io.StringIO())):
            umleitung.__enter__()
            self.addCleanup(umleitung.__exit__, None, None, None)

    def test_hoechstens_einmal_pro_stunde(self):
        cmd = ["systemctl", "--user", "restart", "papierkram-mcp.service"]
        t0 = 1_800_000_000.0
        self.assertTrue(healthcheck.restart_begrenzt("x", self.pfad, cmd, jetzt=t0))
        self.assertFalse(healthcheck.restart_begrenzt("x", self.pfad, cmd, jetzt=t0 + 60))
        self.assertFalse(healthcheck.restart_begrenzt("x", self.pfad, cmd, jetzt=t0 + 3599))
        self.assertEqual(self.run.call_count, 1)
        self.assertTrue(healthcheck.restart_begrenzt("x", self.pfad, cmd, jetzt=t0 + 3600))
        self.assertEqual(self.run.call_count, 2)
        self.run.assert_called_with(cmd, check=True, timeout=60)

    def test_zeitstempel_vor_restart_auch_bei_fehlschlag(self):
        self.run.side_effect = OSError("kein systemctl")
        t0 = 1_800_000_000.0
        self.assertFalse(healthcheck.restart_begrenzt("x", self.pfad, ["false"], jetzt=t0))
        self.assertFalse(healthcheck.restart_begrenzt("x", self.pfad, ["false"], jetzt=t0 + 10))
        self.assertEqual(self.run.call_count, 1)

    def test_unlesbare_datei_unterdrueckt_restart(self):
        os.makedirs(os.path.dirname(self.pfad))
        with open(self.pfad, "w") as fh:
            fh.write("{kaputt")
        self.assertFalse(healthcheck.restart_begrenzt("x", self.pfad, ["true"]))
        self.run.assert_not_called()

    def test_main_ungesund_startet_einmal_neu(self):
        env = {
            "PAPIERKRAM_HEALTHCHECK_STATE": self.pfad,
            "PAPIERKRAM_MCP_RESTART_CMD": "systemctl --user restart papierkram-mcp.service",
        }
        fehler = healthcheck.PruefFehler("Timeout")
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(healthcheck, "pruefen", side_effect=fehler), \
                mock.patch.object(healthcheck.time, "sleep"):
            self.assertEqual(healthcheck.main([]), 1)
            self.assertEqual(healthcheck.main([]), 1)
        self.assertEqual(self.run.call_count, 1)
        self.run.assert_called_with(
            ["systemctl", "--user", "restart", "papierkram-mcp.service"], check=True, timeout=60
        )

    def test_main_nur_pruefen_startet_nie_neu(self):
        with mock.patch.dict(os.environ, {"PAPIERKRAM_HEALTHCHECK_STATE": self.pfad}), \
                mock.patch.object(healthcheck, "pruefen",
                                  side_effect=healthcheck.PruefFehler("weg")):
            self.assertEqual(healthcheck.main(["--nur-pruefen"]), 1)
        self.run.assert_not_called()
        self.assertFalse(os.path.exists(self.pfad))

    def test_main_gesund_kein_restart(self):
        with mock.patch.dict(os.environ, {"PAPIERKRAM_HEALTHCHECK_STATE": self.pfad}), \
                mock.patch.object(healthcheck, "pruefen", return_value="papierkram: 10"):
            self.assertEqual(healthcheck.main([]), 0)
        self.run.assert_not_called()


class DefaultsTest(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
            self.assertEqual(healthcheck._url(), URL)
            self.assertEqual(
                healthcheck._restart_cmd(),
                ["systemctl", "--user", "restart", "papierkram-mcp.service"],
            )
            self.assertEqual(
                healthcheck._state_pfad(),
                os.path.join(tmp, ".local/state/papierkram-mcp/healthcheck-restart.json"),
            )
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
