# Carson MCP-Server

Zwei MCP-Server (Model Context Protocol) fuer OpenClaw Carson.

## Server

### rag (Knowledge Graph)
Wrapper um die butler-api (localhost:8100) fuer den Knowledge Graph.

**Tools:** `rag_search`, `rag_store`, `rag_update`, `rag_delete`, `rag_projects`, `rag_stats`

### papierkram (Buchhaltung)
Direkter API-Zugriff auf Papierkram.de.

**Tools:** `papierkram_find_receipts`, `papierkram_capture_receipt`, `papierkram_open_invoices`, `papierkram_search_supplier`, `papierkram_create_supplier`, `papierkram_mark_paid`

## Setup

```bash
# Venvs erstellen und Dependencies installieren
cd rag_mcp && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd ../papierkram_mcp && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## OpenClaw-Konfiguration

In `~/.openclaw/openclaw.json` unter `mcp.servers` eingetragen. Nach Aenderungen:

```bash
export PATH="/home/frank/.npm-global/bin:$PATH"
systemctl --user daemon-reload && openclaw gateway restart
```

## Troubleshooting

```bash
# Server manuell testen
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' | .venv/bin/python server.py

# Butler-API Health Check
curl -s http://127.0.0.1:8100/api/v1/health

# OpenClaw Logs
journalctl --user -u openclaw-gateway -f
```

## Geschaeftsregeln

- **Schreibende Papierkram-Tools**: NIEMALS automatisch ausfuehren — IMMER Rueckfrage an Frank
- **rag_delete**: IMMER Frank fragen bevor geloescht wird
- **rag_store**: source immer "carson", content eigenstaendig verstaendlich
- Belege/Rechnungen nur als Draft, NIEMALS versenden
