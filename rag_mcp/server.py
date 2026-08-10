#!/usr/bin/env python3
"""MCP Server for Butler RAG Knowledge Graph API."""

import os
import json

import httpx
from mcp.server.fastmcp import FastMCP

BUTLER_API = os.environ.get("BUTLER_API_URL", "http://127.0.0.1:8101")

# Proxy-Schluessel aus der Umgebung (openclaw.json mcp.servers.rag.env, nie im Code).
# Fehlt er, geht der Request bewusst trotzdem raus und laeuft in den 401-Zweig —
# sichtbar im Log statt still am Auth vorbei.
RAG_KEY = os.environ.get("RAG_KEY", "")


def _client() -> httpx.AsyncClient:
    """Client fuer jeden RAG-Request. EINE Quelle fuer Basis-URL und Auth-Header,
    damit Lese- und Schreibweg nicht auseinanderlaufen — beide gehen ueber den
    Auth-Proxy auf 8101, der ohne X-RAG-Key mit 401 antwortet.
    """
    return httpx.AsyncClient(
        base_url=BUTLER_API,
        timeout=30,
        headers={"X-RAG-Key": RAG_KEY},
    )


mcp = FastMCP("rag", instructions="Butler Knowledge Graph (RAG) — 58.000+ Nodes")


@mcp.tool()
async def rag_search(query: str, max_results: int = 5) -> str:
    """Semantische Suche im Knowledge Graph (Projekte, Server, Entscheidungen, Lessons Learned).

    Nutzt /rag/context (ohne LLM-Call, spart Kosten). Ergebnisse nach Score sortiert.
    """
    async with _client() as client:
        resp = await client.post(
            "/api/v1/rag/context",
            json={"query": query, "max_results": max_results},
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def rag_store(
    type: str,
    content: str,
    project: str,
    category: str | None = None,
    key: str | None = None,
    notes: str | None = None,
) -> str:
    """Neuen Eintrag im Knowledge Graph speichern.

    GESCHÄFTSREGELN:
    - source wird automatisch auf 'carson' gesetzt
    - content MUSS eigenständig verständlich sein (ohne Chat-Kontext)
    - Bei duplicate_warning=true in der Antwort: IMMER Frank fragen ob trotzdem speichern
    - type: fact | decision | issue | pattern
    - Bei facts: category und key sind Pflicht
    """
    payload: dict = {
        "type": type,
        "content": content,
        "project": project,
        "source": "carson",
    }
    if category is not None:
        payload["category"] = category
    if key is not None:
        payload["key"] = key
    if notes is not None:
        payload["notes"] = notes

    async with _client() as client:
        resp = await client.post("/api/v1/rag/store", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def rag_update(
    node_id: str,
    content: str | None = None,
    notes: str | None = None,
) -> str:
    """Bestehenden Knowledge-Graph-Eintrag aktualisieren. Re-embedding erfolgt automatisch."""
    payload: dict = {}
    if content is not None:
        payload["content"] = content
    if notes is not None:
        payload["notes"] = notes

    async with _client() as client:
        resp = await client.patch(f"/api/v1/rag/update/{node_id}", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def rag_delete(node_id: str) -> str:
    """Eintrag aus dem Knowledge Graph löschen.

    GESCHÄFTSREGEL: IMMER Frank fragen bevor gelöscht wird! Niemals eigenständig löschen.
    """
    async with _client() as client:
        resp = await client.delete(f"/api/v1/rag/delete/{node_id}")
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def rag_projects() -> str:
    """Liste aller Projekte im Knowledge Graph."""
    async with _client() as client:
        resp = await client.get("/api/v1/rag/projects")
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def rag_stats() -> str:
    """Graph-Statistiken: Anzahl Nodes nach Typ, Relationships, etc."""
    async with _client() as client:
        resp = await client.get("/api/v1/graph/stats")
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
