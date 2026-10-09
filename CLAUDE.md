<!-- COCKPIT-NIGHTLY — maschinell erzeugt, siehe Stand-Stempel -->
# Projektgedaechtnis "Carson-MCP-Servers" (CLAUDE.md / AGENTS.md — inhaltsgleich)

STAND: 2026-10-09 07:12 | COMMIT: e30aa82

**DELTA:** Alles, was nach Commit e30aa82 passiert ist, steht NICHT in dieser Datei.
Fuer den Ist-Stand: `git log --oneline e30aa82..HEAD` und die juengsten Recaps /
Uebergabeberichte lesen. Diese Datei enthaelt nur LANGSAMES Wissen (Architektur,
Konventionen, Pfade, Entscheidungen, Fallstricke) — keinen Tagesstand.

**VORRANG:** Widerspricht diese Datei den QUELLEN (git log, Uebergabebericht), gelten
die QUELLEN. Immer. Eine Datei ist eine Momentaufnahme, git log ist ueberpruefbar.

**VERURSACHER-PFLEGE:** Hat dein Commit eine Aussage hier unwahr gemacht, korrigiere
die betroffene Zeile im selben Commit. Den STAND-Stempel oben NICHT aendern — den
setzt die Nightly.

<!-- COCKPIT-NIGHTLY: KOPF ENDE — ab hier generierter Inhalt -->
## Architektur

- Zwei eigenständige MCP-Server (Python, FastMCP aus `mcp`, async `httpx`) — reine API-Wrapper, zustandslos.
- **`rag_mcp`** → Butler-Knowledge-Graph; live via `:8101` nginx-Auth-Proxy (vor `:8100`), Header `X-RAG-Key` aus Env `RAG_KEY`, zentral in `_client()`. Tools: `rag_search|store|update|delete|projects|stats`; Suche via `/api/v1/rag/context` ohne LLM-Call.
- **`papierkram_mcp`** → `https://frankrath.papierkram.de/api/v1`, Bearer aus Env `PAPIERKRAM_TOKEN`. 9 Tools: `find_receipts`, `capture_receipt`, `open_invoices`, `search_supplier`, `create_supplier`, `mark_paid`, `list_articles`, `create_article`, `create_invoice_draft`. `find_receipts` paginiert vollständig über alle API-Seiten, unterstützt `unpaid_only` + Amount-Filter (Toleranz 0,01).
- Konsument: OpenClaw-Gateway (`~/.openclaw/openclaw.json`, `mcp.servers`). Transport im Repo `stdio`; live papierkram `streamable-http` auf `127.0.0.1:18792`.
- `rag_mcp` hat keine eigene systemd-Unit — OpenClaw startet ihn als Subprozess.

## Struktur / Einstiegspunkte

- `rag_mcp/server.py` — Einstiegspunkt RAG-Server (`mcp.run(transport="stdio")`)
- `papierkram_mcp/server.py` — Einstiegspunkt Papierkram-Server; live `streamable-http`; Transport per `PAPIERKRAM_MCP_TRANSPORT`, Host/Port per `_HOST`/`_PORT`
- `*/requirements.txt` — je `mcp` + `httpx` (ungepinnt); je Modul eigenes `.venv/` (gitignored)
- `README.md` — Setup, Restart, Geschäftsregeln; listet nur 6 Tools → **veraltet**
- Produktivbaum: `/home/frank/carson-workspace/mcp-servers`; kein Build, kein CI

## Datenmodell-Kern

**RAG-Node:** `type` ∈ `fact|decision|issue|pattern`; `content`; `project`; `source` hart `"carson"`; optional `category`+`key` (bei `fact` Pflicht); `notes`. Adressierung per `node_id`; Antwort kann `duplicate_warning` tragen.

**Papierkram-Entitäten:**
- `expense/vouchers` — `creditor_id`, `line_items[]` mit `amount` (brutto), `vat_rate`, `document_date`, `due_date`, `provenance`; `/documents` (PDF), `/pay`, `state`
- `contact/companies` — `supplier: true` / `customer: true`
- `income/invoices` — Draft, `/pay`, `/cancel`; `income/propositions` (Stammartikel, `per_page=100`, `record_state`)

**`SUPPLIER_MAP`** (harte IDs): Hetzner 229, Anthropic 244, OpenAI 235, Perplexity 238, STRATO 65, Vodafone 241, O2 38, Cursor 256, Skool 232, Papierkram/Odacer 13, Freiraum24 259. `NON_EU_SUPPLIER_IDS={232,235,238,244,256}` → Code liefert `"foreign"` (nicht `"non_eu"`). Map in mehreren Docstrings dupliziert — bei Änderung alle Stellen synchron halten.

## Deploy / Betrieb

- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (je Modul)
- Restart: `export PATH="/home/frank/.npm-global/bin:$PATH"; systemctl --user daemon-reload && openclaw gateway restart`
- `~/.config/systemd/user/papierkram-mcp.service`; `papierkram-mcp-healthcheck.timer` (`OnCalendar=*:0/5`) → ~576 Aufrufe/Tag
- **Service-Restart immer manuell durch Frank** — `systemctl --user` in CC-Job-Sandboxes nicht erreichbar.
- Debug: `curl 127.0.0.1:8100/api/v1/health`, `journalctl --user -u openclaw-gateway -f`

## Konventionen

- Schreibende Papierkram-Tools und `rag_delete` **nie automatisch** ausführen — immer Rückfrage an Frank.
- Rechnungen nur als Draft, **niemals versenden**.
- `line_item.amount` ist **brutto**. `_amount_net_from_gross()` ist toter Code — nicht reaktivieren.
- Kundenkontakt per `GET /contact/companies?customer=true` ermitteln, keine hardcodierten IDs.
- **Nie `git add -A` / `commit -a`**: `.bak`-Dateien und `roadmap.yaml` untracked, nicht ignoriert.
- `recap.md` in `.git/info/exclude`; leere Commits sind Absicht.
- Fehlendes `RAG_KEY` bewusst nicht abgefangen — 401 erscheint sichtbar im Log.

## Fallstricke & No-Gos

- **Repo ≠ Produktion**: Produktivbaum kann unversionierte Änderungen enthalten; vor Edits Drift prüfen. Kein Direktpatch im Live-Checkout — CC-Jobs nur im Worktree.
- **`fa7577d` rag_mcp-Auth-Änderung**: nie durch Verify-Kette geprüft — Inhalt unbestätigt.
- **`create_invoice_draft`**: `payment_term_id=19` Magic-Number; berechnetes `due_date` wird nicht ins API-Payload übernommen.
- **`provenance`**: `EU_SUPPLIER_IDS` leer → Zweig `"eu"` unerreichbar; Nicht-`NON_EU`-Belege werden still als `"domestic"` gebucht.
- **PDF-Upload**: `POST /expense/vouchers/{id}/documents`, nicht `/pdf`.
- **`POST …/pay` unzuverlässig**: danach `GET` bis `state=="paid"` pollen; bereits bezahlt → 422.
- **Belegnummern-Race**: `_voucher_create_lock` (asyncio.Lock) serialisiert parallele POSTs — nicht entfernen.
- **Storno**: `POST /income/invoices/{id}/cancel`; `DELETE` zerstört die Rechnung — nicht für Storno verwenden.
- **Fehlerstil uneinheitlich**: `capture_receipt` wirft `RuntimeError`; andere geben `{"ok": false}`.
- **API-Kontingent 429**: blockiert GET wie POST; Healthcheck-Timer häuft Aufrufe auf; Restart hilft strukturell nicht.
