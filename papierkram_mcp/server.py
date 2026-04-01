#!/usr/bin/env python3
"""MCP Server for Papierkram.de Buchhaltungs-API."""

import os
import json

import httpx
from mcp.server.fastmcp import FastMCP

PAPIERKRAM_URL = os.environ.get(
    "PAPIERKRAM_URL", "https://frankrath.papierkram.de/api/v1"
)
PAPIERKRAM_TOKEN = os.environ.get("PAPIERKRAM_TOKEN", "")

SUPPLIER_MAP = {
    "hetzner": 229,
    "anthropic": 244,
    "openai": 235,
    "perplexity": 238,
    "strato": 65,
    "vodafone": 241,
    "o2": 38,
    "cursor": 256,
    "skool": 232,
    "papierkram": 13,
    "odacer": 13,
    "voicenotes": 259,
}

mcp = FastMCP("papierkram", instructions="Papierkram.de Buchhaltung — Belege, Rechnungen, Lieferanten")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {PAPIERKRAM_TOKEN}",
        "Content-Type": "application/json",
    }


@mcp.tool()
async def papierkram_find_receipts() -> str:
    """Unverarbeitete/offene Eingangsbelege (Expense Vouchers) finden.

    Gibt alle unbezahlten Belege zurück.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAPIERKRAM_URL}/expense/vouchers",
            params={"unpaid": "true"},
            headers=_headers(),
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def papierkram_capture_receipt(
    name: str,
    supplier_id: int,
    amount_gross: float,
    vat_rate: str,
    date: str,
    pdf_path: str | None = None,
    mark_paid: bool = False,
) -> str:
    """Eingangsbeleg (Expense Voucher) erfassen.

    GESCHÄFTSREGEL: NIEMALS automatisch aufrufen — IMMER erst Frank eine Rückfrage
    zeigen mit Lieferant, Betrag, Datum und auf Bestätigung warten!
    Erstellt nur Drafts, NIEMALS versenden.
    Beträge immer in Euro mit 2 Dezimalstellen.

    Bekannte Lieferanten-IDs: Hetzner=229, Anthropic=244, OpenAI=235,
    Perplexity=238, STRATO=65, Vodafone=241, O2=38, Cursor=256,
    Skool=232, Papierkram/Odacer=13, Voicenotes=259.

    Args:
        name: Beschreibung des Belegs (z.B. "Hetzner März 2026")
        supplier_id: Lieferanten-ID aus Papierkram
        amount_gross: Bruttobetrag in Euro
        vat_rate: Steuersatz ("19%", "7%", "0%")
        date: Belegdatum im Format YYYY-MM-DD
        pdf_path: Optionaler Pfad zur PDF-Datei
        mark_paid: Als bezahlt markieren (default: false)
    """
    payload = {
        "name": name,
        "creditor_id": supplier_id,
        "voucher_date": date,
        "line_items": [
            {
                "name": name,
                "amount": round(amount_gross, 2),
                "vat_rate": vat_rate,
            }
        ],
        "provenance": "api",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAPIERKRAM_URL}/expense/vouchers",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

        if mark_paid and "id" in result:
            pay_resp = await client.post(
                f"{PAPIERKRAM_URL}/expense/vouchers/{result['id']}/pay",
                headers=_headers(),
                json={"payment_date": date, "value": round(amount_gross, 2)},
            )
            if pay_resp.is_success:
                result["marked_paid"] = True

        return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def papierkram_open_invoices() -> str:
    """Offene (unbezahlte) Ausgangsrechnungen auflisten."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAPIERKRAM_URL}/income/invoices",
            params={"status": "outstanding"},
            headers=_headers(),
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def papierkram_search_supplier(query: str) -> str:
    """Lieferant in Papierkram suchen.

    Durchsucht die Papierkram-Kontaktdatenbank nach Lieferanten.
    Bekannte Lieferanten-IDs: Hetzner=229, Anthropic=244, OpenAI=235,
    Perplexity=238, STRATO=65, Vodafone=241, O2=38, Cursor=256,
    Skool=232, Papierkram/Odacer=13, Voicenotes=259.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAPIERKRAM_URL}/contact/companies",
            params={"supplier": "true", "name": query},
            headers=_headers(),
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def papierkram_create_supplier(name: str) -> str:
    """Neuen Lieferanten in Papierkram anlegen.

    GESCHÄFTSREGEL: IMMER Frank fragen bevor ein neuer Lieferant angelegt wird!
    NIEMALS automatisch anlegen.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAPIERKRAM_URL}/contact/companies",
            headers=_headers(),
            json={"name": name, "supplier": True},
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def papierkram_mark_paid(
    invoice_id: int, payment_date: str, amount: float
) -> str:
    """Ausgangsrechnung als bezahlt markieren.

    GESCHÄFTSREGEL: Nur nach EXPLIZITER Bestätigung durch Frank!
    NIEMALS automatisch buchen.
    Beträge in Euro mit 2 Dezimalstellen.

    Args:
        invoice_id: Rechnungs-ID aus Papierkram
        payment_date: Zahlungsdatum (YYYY-MM-DD)
        amount: Bezahlter Betrag in Euro
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAPIERKRAM_URL}/income/invoices/{invoice_id}/pay",
            headers=_headers(),
            json={"payment_date": payment_date, "value": round(amount, 2)},
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
