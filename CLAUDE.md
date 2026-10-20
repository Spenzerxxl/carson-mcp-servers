<!-- COCKPIT-NIGHTLY — maschinell erzeugt, siehe Stand-Stempel -->
# Projektgedaechtnis "Carson-MCP-Servers" (CLAUDE.md / AGENTS.md — inhaltsgleich)

STAND: 2026-10-20 08:36 | COMMIT: 1829a8e

**DELTA:** Alles, was nach Commit 1829a8e passiert ist, steht NICHT in dieser Datei.
Fuer den Ist-Stand: `git log --oneline 1829a8e..HEAD` und die juengsten Recaps /
Uebergabeberichte lesen. Diese Datei enthaelt nur LANGSAMES Wissen (Architektur,
Konventionen, Pfade, Entscheidungen, Fallstricke) — keinen Tagesstand.

**VORRANG:** Widerspricht diese Datei den QUELLEN (git log, Uebergabebericht), gelten
die QUELLEN. Immer. Eine Datei ist eine Momentaufnahme, git log ist ueberpruefbar.

**VERURSACHER-PFLEGE:** Hat dein Commit eine Aussage hier unwahr gemacht, korrigiere
die betroffene Zeile im selben Commit. Den STAND-Stempel oben NICHT aendern — den
setzt die Nightly.

<!-- COCKPIT-NIGHTLY: KOPF ENDE — ab hier generierter Inhalt -->
## Architektur

- Zwei eigenständige MCP-Server (Python, FastMCP aus `mcp` 1.x, async `httpx`) — reine API-Wrapper, zustandslos.
- **`rag_mcp`** → Butler-Knowledge-Graph; live via `:8101` nginx-Auth-Proxy (vor `:8100`), Header `X-RAG-Key` aus Env `RAG_KEY`, zentral in `_client()`. Tools: `rag_search|store|update|delete|projects|stats`; Suche via `/api/v1/rag/context` ohne LLM-Call.
- **`papierkram_mcp`** → `https://frankrath.papierkram.de/api/v1`, Bearer aus Env `PAPIERKRAM_TOKEN`. 9 Tools: `find_receipts`, `capture_receipt`, `open_invoices`, `search_supplier`, `create_supplier`, `mark_paid`, `list_articles`, `create_article`, `create_invoice_draft`.
- Konsument: OpenClaw-Gateway (`~/.openclaw/openclaw.json`, `mcp.servers`). Transport im Repo `stdio`; live papierkram `streamable-http` auf `127.0.0.1:18792`.
- `rag_mcp` hat keine eigene systemd-Unit — OpenClaw startet ihn als Subprozess.

## Struktur / Einstiegspunkte

- `rag_mcp/server.py` — RAG-Server (`mcp.run(transport="stdio")`)
- `papierkram_mcp/server.py` — Papierkram-Server; Transport per `PAPIERKRAM_MCP_TRANSPORT`, Host/Port per `_HOST`/`_PORT`
- `*/requirements.txt` — je `mcp` + `httpx` (ungepinnt); je Modul eigenes `.venv/` (gitignored)
- `papierkram_mcp/test_payloads.py` — Unit-Tests; Aufruf aus Repo-Root: `papierkram_mcp/.venv/bin/python -m unittest papierkram_mcp/test_payloads.py -v`
- `README.md` — listet nur 6 Tools → **veraltet**
- Produktivbaum: `/home/frank/carson-workspace/mcp-servers`; kein Build, kein CI

## Datenmodell-Kern

**RAG-Node:** `type` ∈ `fact|decision|issue|pattern`; `content`; `project`; `source` hart `"carson"`; optional `category`+`key` (bei `fact` Pflicht); `notes`. Adressierung per `node_id`; Antwort kann `duplicate_warning` tragen.

**Papierkram-Entitäten:**
- `expense/vouchers` — Pflicht POST: `name`, `provenance`, `line_items[]`; `amount` = brutto, `vat_rate`; `/documents` (PDF), `/pay`, `state`
- `contact/companies` — POST: `name` + `contact_type` ∈ `customer|supplier`; GET kennt nur `page`, `page_size` (≤100), `order_by`/`order_direction` — kein Namens-/Typ-/Aktiv-Filter; `search_supplier` liest alle Seiten, filtert im MCP
- `income/invoices` — Draft, `/pay`, `/cancel`; `income/propositions` (Stammartikel)

**`SUPPLIER_MAP`** (harte IDs): Hetzner 229, Anthropic 244, OpenAI 235, Perplexity 238, STRATO 65, Vodafone **226**, O2 38, Cursor 256, Skool 232, Papierkram/Odacer 13, Freiraum24 259. `NON_EU_SUPPLIER_IDS={232,235,238,244,256}` → `"foreign"`. Map in Docstrings dupliziert — bei Änderung alle Stellen synchron halten.

## Deploy / Betrieb

- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (je Modul); `mcp` ungepinnt → installiert 2.x; Produktion braucht 1.x.
- Restart: `export PATH="/home/frank/.npm-global/bin:$PATH"; systemctl --user daemon-reload && openclaw gateway restart`
- `~/.config/systemd/user/papierkram-mcp.service`; `papierkram-mcp-healthcheck.timer` (`OnCalendar=*:0/5`).
- **Service-Restart immer manuell durch Frank** — `systemctl --user` in CC-Job-Sandboxes nicht erreichbar.
- Debug: `curl 127.0.0.1:8100/api/v1/health`, `journalctl --user -u openclaw-gateway -f`

## Konventionen

- Schreibende Papierkram-Tools und `rag_delete` **nie automatisch** — immer Rückfrage an Frank.
- Rechnungen nur als Draft, **niemals versenden**.
- `line_item.amount` ist **brutto**. `_amount_net_from_gross()` ist toter Code — nicht reaktivieren.
- Kundenkontakt per `GET /contact/companies?customer=true` ermitteln, keine hardcodierten IDs.
- **Nie `git add -A` / `commit -a`**: `.bak`-Dateien und `roadmap.yaml` untracked, nicht ignoriert.
- Fehlendes `RAG_KEY` bewusst nicht abgefangen — 401 erscheint sichtbar im Log.

## Fallstricke & No-Gos

- **Repo ≠ Produktion**: vor Edits Drift prüfen; kein Direktpatch im Live-Checkout — CC-Jobs nur im Worktree.
- **`fa7577d` rag_mcp-Auth-Änderung**: nie durch Verify-Kette geprüft — Inhalt unbestätigt.
- **`create_invoice_draft`**: `payment_term_id=19` Magic-Number; berechnetes `due_date` nicht im API-Payload.
- **`provenance`**: Pflicht (`domestic|eu|foreign`, sonst `ValueError`); ohne Angabe: Steuersatz > 0 % → `"domestic"`, 0 % → Supplier-Map. `EU_SUPPLIER_IDS` leer → `"eu"` nur explizit erreichbar.
- **PDF-Upload**: `POST /expense/vouchers/{id}/documents`, nicht `/pdf`.
- **`POST …/pay` unzuverlässig**: danach `GET` bis `state=="paid"` pollen; bereits bezahlt → 422.
- **Belegnummern-Race**: `_voucher_create_lock` serialisiert parallele POSTs — nicht entfernen.
- **Storno**: `POST /income/invoices/{id}/cancel`; `DELETE` zerstört die Rechnung.
- **Fehlerstil uneinheitlich**: `capture_receipt`/`create_supplier` werfen `RuntimeError`; andere geben `{"ok": false}`.
- **API-Kontingent 429**: blockiert GET+POST; Healthcheck-Timer häuft Aufrufe.
- **`PAPIERKRAM_MARK_PAID_ERLAUBT`**: Default an; `0/false/nein/no/off` sperrt `mark_paid` und `/pay` in `capture_receipt` (meldet `mark_paid_ignoriert=true`). Schalter beim Aufruf gelesen.

## Gelernte Regeln

- `mcp` in `requirements.txt` auf `<2` pinnen — ungepinnt installiert 2.x, dort fehlt `mcp.server.fastmcp`.
- Swagger vor POSTs prüfen: `GET /api/v1/api-docs/api/v1/swagger.json` ohne Token. `POST /contact/companies` verlangt `name` + `contact_type`; `POST /expense/vouchers` verlangt `name`, `provenance`, `line_items`.
[auf Ziellaenge gekuerzt]
