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
async def papierkram_find_receipts(
    supplier_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    unpaid_only: bool = True,
    amount: float | None = None,
    limit: int | None = None,
    offset: int = 0,
    page: int | None = None,
) -> str:
    """Unverarbeitete/offene Eingangsbelege (Expense Vouchers) finden.

    Gibt standardmaessig alle unbezahlten Belege zurueck.
    Sobald Filter oder Pagination aktiv sind, werden alle API-Seiten iteriert und
    die Ergebnisse client-seitig auf dem vollstaendigen Bestand gefiltert.
    """
    from datetime import datetime

    def _parse_iso_date(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.strptime(value[:10], "%Y-%m-%d")

    def _receipt_supplier_id(receipt: dict) -> int | None:
        creditor = receipt.get("creditor")
        if isinstance(creditor, dict) and creditor.get("id") is not None:
            return int(creditor["id"])
        for key in ("creditor_id", "supplier_id"):
            if receipt.get(key) is not None:
                return int(receipt[key])
        supplier = receipt.get("supplier")
        if isinstance(supplier, dict) and supplier.get("id") is not None:
            return int(supplier["id"])
        return None

    def _receipt_amount(receipt: dict) -> float | None:
        for key in ("amount", "gross_amount", "total_amount"):
            value = receipt.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        line_items = receipt.get("line_items")
        if isinstance(line_items, list):
            total = 0.0
            found_amount = False
            for item in line_items:
                if not isinstance(item, dict) or item.get("amount") is None:
                    continue
                try:
                    total += float(item["amount"])
                    found_amount = True
                except (TypeError, ValueError):
                    continue
            if found_amount:
                return total
        return None

    def _entry_list(data: dict | list) -> list | None:
        if isinstance(data, dict):
            for key in ("entries", "vouchers", "expense_vouchers"):
                items = data.get(key)
                if items is not None:
                    return items
            return None
        return data

    filter_date_from = _parse_iso_date(date_from)
    filter_date_to = _parse_iso_date(date_to)
    if limit is not None and limit < 1:
        raise ValueError("limit muss >= 1 sein")
    if offset < 0:
        raise ValueError("offset darf nicht negativ sein")
    if page is not None and page < 1:
        raise ValueError("page muss >= 1 sein")
    if page is not None and limit is None:
        raise ValueError("page erfordert auch limit")
    if filter_date_from and filter_date_to and filter_date_from > filter_date_to:
        raise ValueError("date_from darf nicht nach date_to liegen")

    has_filters = any(
        value is not None for value in (supplier_id, date_from, date_to, amount)
    )
    has_pagination = limit is not None or offset != 0 or page is not None
    requires_full_scan = (not unpaid_only) or has_filters or has_pagination

    async with httpx.AsyncClient(timeout=30) as client:
        api_page = 1
        all_entries = []
        first_page_data = None
        pages_fetched = 0
        total_entries_reported = None

        while True:
            params = {"page": api_page} if requires_full_scan else {}
            if unpaid_only:
                params["unpaid"] = "true"

            resp = await client.get(
                f"{PAPIERKRAM_URL}/expense/vouchers",
                params=params,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            if first_page_data is None:
                first_page_data = data
                if not requires_full_scan:
                    return json.dumps(data, ensure_ascii=False, indent=2)

            items = _entry_list(data)
            if not isinstance(items, list):
                return json.dumps(data, ensure_ascii=False, indent=2)

            pages_fetched += 1
            all_entries.extend(items)

            if isinstance(data, dict):
                if total_entries_reported is None and isinstance(data.get("total_entries"), int):
                    total_entries_reported = data["total_entries"]
                total_pages = data.get("total_pages")
                if isinstance(total_pages, int) and api_page >= total_pages:
                    break
                if data.get("has_more") is False:
                    break

            if not items:
                break

            api_page += 1

        filtered = []
        for receipt in all_entries:
            if supplier_id is not None and _receipt_supplier_id(receipt) != supplier_id:
                continue
            receipt_date = _parse_iso_date(
                receipt.get("document_date")
                or receipt.get("voucher_date")
                or receipt.get("date")
                or receipt.get("created_at")
            )
            if filter_date_from and (receipt_date is None or receipt_date < filter_date_from):
                continue
            if filter_date_to and (receipt_date is None or receipt_date > filter_date_to):
                continue
            if amount is not None:
                receipt_amount = _receipt_amount(receipt)
                if receipt_amount is None or round(abs(receipt_amount - amount), 2) > 0.01:
                    continue
            filtered.append(receipt)

        effective_offset = offset
        if page is not None and limit is not None:
            effective_offset = (page - 1) * limit

        paginated = filtered[effective_offset:]
        if limit is not None:
            paginated = paginated[:limit]

        if isinstance(first_page_data, dict):
            result = dict(first_page_data)
            if "entries" in result and isinstance(result["entries"], list):
                result["entries"] = paginated
            elif "vouchers" in result and isinstance(result["vouchers"], list):
                result["vouchers"] = paginated
            elif "expense_vouchers" in result and isinstance(result["expense_vouchers"], list):
                result["expense_vouchers"] = paginated
            else:
                result["entries"] = paginated
            result["count"] = len(paginated)
            result["filtered_count"] = len(filtered)
            result["offset"] = effective_offset
            result["unpaid_only"] = unpaid_only
            result["api_pages_fetched"] = pages_fetched
            result["api_entries_fetched"] = len(all_entries)
            if total_entries_reported is not None:
                result["total_entries"] = total_entries_reported
            if amount is not None:
                result["amount"] = amount
            if limit is not None:
                result["limit"] = limit
            if page is not None:
                result["page"] = page
        else:
            result = paginated

        return json.dumps(result, ensure_ascii=False, indent=2)


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
