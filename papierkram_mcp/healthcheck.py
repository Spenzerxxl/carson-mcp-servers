#!/usr/bin/env python3
"""Healthcheck fuer papierkram-mcp.service (streamable-http, 127.0.0.1:18792/mcp).

Prueft NUR die MCP-Schicht des laufenden Dienstes, nie die Papierkram-API:
  1. initialize                  (Antwort mit mcp-session-id und serverInfo)
  2. notifications/initialized
  3. tools/list                  (Werkzeugliste nicht leer)
  4. DELETE der Session          (Aufraeumen, Fehler egal)
Es wird KEIN Werkzeug ausgefuehrt — damit kostet der Check kein einziges
Papierkram-Credit (Vorfaelle 26.07. und 26.11.2026: der alte Check rief alle
5 Minuten ein echtes Werkzeug auf und leerte das Monatskontingent).

Bei Fehler: Restart des Dienstes, hoechstens einmal pro Stunde. Die Begrenzung
steht hier im Skript: Zeitstempel-Datei ausserhalb des Repos
(PAPIERKRAM_HEALTHCHECK_STATE, Default
~/.local/state/papierkram-mcp/healthcheck-restart.json). Der Zeitstempel wird
VOR dem Restart geschrieben, damit auch ein haengender/fehlschlagender Restart
die Stunde sperrt.

Umgebung (alles beim Aufruf gelesen):
  PAPIERKRAM_MCP_URL            Default http://<HOST>:<PORT>/mcp
  PAPIERKRAM_MCP_HOST/_PORT     Default 127.0.0.1 / 18792 (wie server.py)
  PAPIERKRAM_MCP_TIMEOUT        Sekunden je HTTP-Aufruf, Default 10
  PAPIERKRAM_MCP_RESTART_CMD    Default "systemctl --user restart papierkram-mcp.service"
  PAPIERKRAM_HEALTHCHECK_STATE  Pfad der Rate-Limit-Datei (s.o.)

Exit-Codes: 0 gesund (oder nach Restart wieder gesund), 1 ungesund.
Option --nur-pruefen: nur pruefen, nie neu starten.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import httpx

PROTOKOLL_VERSION = "2025-03-26"
RESTART_MINDESTABSTAND_S = 3600
RESTART_CMD_DEFAULT = "systemctl --user restart papierkram-mcp.service"
STATE_DEFAULT = "~/.local/state/papierkram-mcp/healthcheck-restart.json"
NACH_RESTART_WARTEN_S = 3.0

# Die einzigen JSON-RPC-Methoden, die dieses Skript je sendet.
ERLAUBTE_METHODEN = ("initialize", "notifications/initialized", "tools/list")


class PruefFehler(RuntimeError):
    """Der Dienst antwortet nicht wie ein gesunder MCP-Server."""


def _url() -> str:
    url = os.environ.get("PAPIERKRAM_MCP_URL", "").strip()
    if url:
        return url
    host = os.environ.get("PAPIERKRAM_MCP_HOST", "127.0.0.1")
    port = os.environ.get("PAPIERKRAM_MCP_PORT", "18792")
    return f"http://{host}:{port}/mcp"


def _timeout() -> float:
    try:
        return float(os.environ.get("PAPIERKRAM_MCP_TIMEOUT", "10"))
    except ValueError:
        return 10.0


def _state_pfad() -> str:
    roh = os.environ.get("PAPIERKRAM_HEALTHCHECK_STATE", "").strip()
    return os.path.expanduser(roh or STATE_DEFAULT)


def _restart_cmd() -> list[str]:
    return shlex.split(os.environ.get("PAPIERKRAM_MCP_RESTART_CMD", "") or RESTART_CMD_DEFAULT)


def _header(session_id: str | None = None, protokoll: str | None = None) -> dict:
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        h["mcp-session-id"] = session_id
    if protokoll:
        h["mcp-protocol-version"] = protokoll
    return h


def _jsonrpc_antwort(resp: httpx.Response, erwartete_id: int) -> dict:
    """JSON-RPC-Antwort aus JSON- oder SSE-Body (data:-Zeilen) holen."""
    kandidaten = []
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for zeile in resp.text.splitlines():
            if zeile.startswith("data:"):
                kandidaten.append(zeile[5:].strip())
    else:
        kandidaten.append(resp.text)
    for roh in kandidaten:
        try:
            nachricht = json.loads(roh)
        except json.JSONDecodeError:
            continue
        if isinstance(nachricht, dict) and nachricht.get("id") == erwartete_id:
            if "error" in nachricht:
                raise PruefFehler(f"MCP-Fehler: {json.dumps(nachricht['error'])[:300]}")
            return nachricht.get("result") or {}
    raise PruefFehler(f"keine JSON-RPC-Antwort mit id={erwartete_id}: {resp.text[:300]!r}")


def _senden(client: httpx.Client, url: str, nachricht: dict, header: dict) -> httpx.Response:
    if nachricht.get("method") not in ERLAUBTE_METHODEN:
        raise ValueError(f"Methode {nachricht.get('method')!r} ist im Healthcheck verboten")
    return client.post(url, headers=header, json=nachricht)


def pruefen(url: str, timeout: float, transport: httpx.BaseTransport | None = None) -> str:
    """initialize + tools/list; liefert eine Kurzbeschreibung oder wirft PruefFehler."""
    kw = {"timeout": timeout}
    if transport is not None:
        kw["transport"] = transport
    try:
        with httpx.Client(**kw) as client:
            resp = _senden(client, url, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": PROTOKOLL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "papierkram-healthcheck", "version": "2.0"},
                },
            }, _header())
            if resp.status_code != 200:
                raise PruefFehler(f"initialize HTTP {resp.status_code}: {resp.text[:300]}")
            session_id = resp.headers.get("mcp-session-id")
            if not session_id:
                raise PruefFehler("initialize ohne mcp-session-id")
            init = _jsonrpc_antwort(resp, 1)
            server_name = (init.get("serverInfo") or {}).get("name")
            if not server_name:
                raise PruefFehler(f"initialize ohne serverInfo: {json.dumps(init)[:300]}")
            protokoll = init.get("protocolVersion") or PROTOKOLL_VERSION

            try:
                resp = _senden(client, url, {
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                }, _header(session_id, protokoll))
                if resp.status_code not in (200, 202):
                    raise PruefFehler(
                        f"notifications/initialized HTTP {resp.status_code}: {resp.text[:300]}"
                    )

                resp = _senden(client, url, {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
                }, _header(session_id, protokoll))
                if resp.status_code != 200:
                    raise PruefFehler(f"tools/list HTTP {resp.status_code}: {resp.text[:300]}")
                tools = _jsonrpc_antwort(resp, 2).get("tools")
                if not isinstance(tools, list) or not tools:
                    raise PruefFehler("tools/list lieferte keine Werkzeuge")
            finally:
                # Session freigeben, damit der Dienst keine Healthcheck-Sessions sammelt.
                try:
                    client.delete(url, headers=_header(session_id, protokoll))
                except httpx.HTTPError:
                    pass
            return f"{server_name}: {len(tools)} Werkzeuge, Session {session_id}"
    except httpx.TimeoutException as exc:
        raise PruefFehler(f"Timeout: {exc}") from exc
    except httpx.HTTPError as exc:
        raise PruefFehler(f"HTTP-Fehler: {exc}") from exc


def _letzter_restart(pfad: str) -> float | None:
    try:
        with open(pfad, encoding="utf-8") as fh:
            return float(json.load(fh)["letzter_restart"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Unlesbar → konservativ: so tun, als waere gerade neu gestartet worden.
        print(f"WARNUNG: Rate-Limit-Datei {pfad} unlesbar ({exc}) — kein Restart", file=sys.stderr)
        return time.time()


def _restart_merken(pfad: str, zeitpunkt: float, grund: str) -> None:
    verzeichnis = os.path.dirname(pfad) or "."
    os.makedirs(verzeichnis, exist_ok=True)
    inhalt = {
        "letzter_restart": zeitpunkt,
        "letzter_restart_iso": datetime.fromtimestamp(zeitpunkt, timezone.utc).isoformat(),
        "grund": grund[:500],
    }
    fd, tmp = tempfile.mkstemp(dir=verzeichnis, prefix=".healthcheck-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(inhalt, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, pfad)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restart_begrenzt(grund: str, pfad: str, cmd: list[str], jetzt: float | None = None) -> bool:
    """Startet neu, wenn der letzte Restart >= 1 h her ist. True = Restart ausgefuehrt."""
    jetzt = time.time() if jetzt is None else jetzt
    letzter = _letzter_restart(pfad)
    if letzter is not None and jetzt - letzter < RESTART_MINDESTABSTAND_S:
        rest = int(RESTART_MINDESTABSTAND_S - (jetzt - letzter))
        print(f"RESTART-UNTERDRUECKT: letzter Restart vor {int(jetzt - letzter)} s, "
              f"naechster fruehestens in {rest} s", file=sys.stderr)
        return False
    _restart_merken(pfad, jetzt, grund)
    try:
        subprocess.run(cmd, check=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"RESTART-FEHLGESCHLAGEN: {' '.join(cmd)}: {exc}", file=sys.stderr)
        return False
    print(f"RESTARTED: {' '.join(cmd)}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nur-pruefen", action="store_true",
                        help="nur pruefen, nie neu starten")
    args = parser.parse_args(argv)

    url, timeout = _url(), _timeout()
    try:
        print(f"OK: {pruefen(url, timeout)}")
        return 0
    except PruefFehler as exc:
        grund = str(exc)
        print(f"UNGESUND: {url}: {grund}", file=sys.stderr)

    if args.nur_pruefen:
        return 1
    if not restart_begrenzt(grund, _state_pfad(), _restart_cmd()):
        return 1

    time.sleep(NACH_RESTART_WARTEN_S)
    try:
        print(f"WIEDER-GESUND: {pruefen(url, timeout)}")
        return 0
    except PruefFehler as exc:
        print(f"NACH-RESTART-UNGESUND: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
