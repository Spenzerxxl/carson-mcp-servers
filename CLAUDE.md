<!-- COCKPIT-NIGHTLY — maschinell erzeugt, siehe Stand-Stempel -->
# Projektgedaechtnis "Carson-MCP-Servers" (CLAUDE.md / AGENTS.md — inhaltsgleich)

STAND: 2026-08-03 03:02 | COMMIT: 01e9cf1

**DELTA:** Alles, was nach Commit 01e9cf1 passiert ist, steht NICHT in dieser Datei.
Fuer den Ist-Stand: `git log --oneline 01e9cf1..HEAD` und die juengsten Recaps /
Uebergabeberichte lesen. Diese Datei enthaelt nur LANGSAMES Wissen (Architektur,
Konventionen, Pfade, Entscheidungen, Fallstricke) — keinen Tagesstand.

**VORRANG:** Widerspricht diese Datei den QUELLEN (git log, Uebergabebericht), gelten
die QUELLEN. Immer. Eine Datei ist eine Momentaufnahme, git log ist ueberpruefbar.

**VERURSACHER-PFLEGE:** Hat dein Commit eine Aussage hier unwahr gemacht, korrigiere
die betroffene Zeile im selben Commit. Den STAND-Stempel oben NICHT aendern — den
setzt die Nightly.

<!-- COCKPIT-NIGHTLY: KOPF ENDE — ab hier generierter Inhalt -->
## Architektur

- Zwei eigenständige MCP-Server (Python, FastMCP aus `mcp`, async `httpx`) — reine API-Wrapper, zustandslos, keine eigene Persistenz.
- **`rag_mcp`** → Butler-Knowledge-Graph `127.0.0.1:8100`; live `:8101` nginx-Auth-Proxy, Header `X-RAG-Key` aus Env `RAG_KEY`, zentral in `_client()`. Tools: `rag_search|store|update|delete|projects|stats`; Suche via `/api/v1/rag/context` ohne LLM-Call.
- **`papierkram_mcp`** → `https://frankrath.papierkram.de/api/v1`, Bearer aus Env `PAPIERKRAM_TOKEN`. Repo 6 Tools, live 9 (`+list_articles`, `+create_article`, `+create_invoice_draft`).
- Konsument: OpenClaw-Gateway (`~/.openclaw/openclaw.json`, `mcp.servers`). Transport im Repo `stdio`; live papierkram `streamable-http` auf `127.0.0.1:18792`.
- `rag_mcp` hat keine eigene systemd-Unit — OpenClaw startet ihn als Subprozess.

## Struktur / Einstiegspunkte

- `rag_mcp/server.py` — Einstiegspunkt RAG-Server (`mcp.run(transport="stdio")`)
- `papierkram_mcp/server.py` — Einstiegspunkt Papierkram-Server (dito im Repo; live `streamable-http`)
- `*/requirements.txt` — je `mcp` + `httpx`; je Modul eigenes `.venv/`
- `README.md` — Setup, Restart-Befehl, Geschäftsregeln
- `CLAUDE.md` / `AGENTS.md` — inhaltsgleiches Projektgedächtnis (Nightly)
- Produktivbaum: `/home/frank/carson-workspace/mcp-servers`; kein Build, kein CI/CD

## Datenmodell-Kern

**RAG-Node:** `type` ∈ `fact|decision|issue|pattern`; `content` (ohne Chat-Kontext verständlich); `project`; `source` hart `"carson"`; optional `category`+`key` (bei `fact` Pflicht); `notes`. Adressierung per `node_id`; Update → Re-Embedding; Antwort kann `duplicate_warning` tragen.

**Papierkram-Entitäten:**
- `expense/vouchers` — Eingangsbeleg: `creditor_id`→Lieferant, `line_items[]` mit `amount`/`vat_rate`, `voucher_date`, `provenance`; Zahlung via `POST …/{id}/pay`
- `contact/companies` — Kontakte; Lieferant = `supplier: true`, Kunde = `customer: true`
- `income/invoices` — Ausgangsrechnung, `/pay`, `/cancel`; live zusätzlich `income/propositions` (Stammartikel, aus Rechnungspositionen referenziert)

**`SUPPLIER_MAP`** — harte ID-Zuordnung im Code (Hetzner 229, Anthropic 244, OpenAI 235, STRATO 65, Vodafone 241, O2 38, Cursor 256 …), in mehreren Docstrings dupliziert → bei Änderung alle Stellen synchron halten.

## Deploy / Betrieb

- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (je Modul)
- Restart: `export PATH="/home/frank/.npm-global/bin:$PATH"; systemctl --user daemon-reload && openclaw gateway restart`
- `~/.config/systemd/user/papierkram-mcp.service` startet `…/papierkram_mcp/server.py`; Env: `PAPIERKRAM_URL`, `PAPIERKRAM_TOKEN`, `PAPIERKRAM_MCP_TRANSPORT`, `_HOST`/`_PORT`
- `papierkram-mcp-healthcheck.timer` (`OnCalendar=*:0/5`): MCP-Handshake gegen `127.0.0.1:18792`, ~576 Aufrufe/Tag; Restart bei 429 hilft strukturell nicht — kann Monatskontingent erschöpfen.
- Debug: `curl 127.0.0.1:8100/api/v1/health`, `journalctl --user -u openclaw-gateway -f`

## Konventionen

- Schreibende Papierkram-Tools und `rag_delete` **nie automatisch** ausführen — immer Rückfrage an Frank.
- Rechnungen nur als Draft anlegen, **niemals versenden**.
- `line_item.amount` ist **brutto**. `_amount_net_from_gross()` ist toter Code — nicht reaktivieren.
- Kundenkontakt per `GET /contact/companies?customer=true` ermitteln, nicht auf hardcodierte IDs verlassen.
- Sicherungs-Commits: nur `rag_mcp/server.py` und `papierkram_mcp/server.py` explizit angeben; `.bak`-Dateien und `roadmap.yaml` bleiben draußen.
- `recap.md` steht in `.git/info/exclude`; leere Commits sind Absicht.
- Fehlendes `RAG_KEY` wird bewusst nicht abgefangen — 401 erscheint im Log (absichtlich sichtbar).

## Fallstricke & No-Gos

- **Repo ≠ Produktion**: Dienst läuft auf uncommittetem Arbeitsbaum; immer prüfen, welchen Stand man liest.
- **Nie `git add -A` / `commit -a`**: `.bak`-Dateien und `roadmap.yaml` sind untracked, nicht gitignored → Pfade explizit nennen.
- **PDF-Upload**: `POST /expense/vouchers/{id}/documents`, nicht `/pdf` — falscher Pfad erzeugt stumme `documents:[]`-Einträge.
- **`POST …/pay` unzuverlässig**: danach `GET /expense/vouchers/{id}` bis `state=="paid"` prüfen; bereits bezahlt → 422.
- **Belegnummern-Race**: parallele POSTs können dieselbe Nummer erhalten; im Live-Code prozessweiter `asyncio.Lock` serialisiert.
- **`EU_SUPPLIER_IDS` ist leer**: `provenance`-Zweig `"eu"` unerreichbar; Nicht-`non_eu`-Belege werden still als `domestic` gebucht.
- **Storno**: `POST /income/invoices/{id}/cancel` (kein Body) → `state=canceled`; `DELETE /income/invoices/{id}` zerstört die Rechnung (204) — **nicht** für Storno verwenden.
- **Fehlerstil uneinheitlich**: `capture_receipt` wirft `RuntimeError`; `create_article`/`create_invoice_draft` geben `{"ok": false, …}` zurück; `payment_term_id=19` ist undokumentierte Magic-Number.
- **API-Kontingent 429**: erschöpftes Kontingent blockiert GET wie POST; `papierkram-mcp-healthcheck.timer` häuft Aufrufe auf — Restart bei 429 hilft strukturell nicht.
