#!/usr/bin/env python3
"""MCP Server for Papierkram.de Buchhaltungs-API."""

import asyncio
import os
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

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
    "freiraum24": 259,
}

NON_EU_SUPPLIER_IDS = {232, 235, 238, 244, 256}
EU_SUPPLIER_IDS = set()

mcp = FastMCP(
    "papierkram",
    instructions="Papierkram.de Buchhaltung — Belege, Rechnungen, Lieferanten",
    host=os.environ.get("PAPIERKRAM_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("PAPIERKRAM_MCP_PORT", "18792")),
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {PAPIERKRAM_TOKEN}",
        "Content-Type": "application/json",
    }


def _normalize_vat_rate(vat_rate: str) -> str:
    normalized = vat_rate.strip().replace(",", ".")
    if normalized in {"0", "0.0", "0.00"}:
        return "0%"
    if normalized in {"7", "7.0", "7.00"}:
        return "7%"
    if normalized in {"19", "19.0", "19.00"}:
        return "19%"
    if normalized in {"0%", "7%", "19%"}:
        return normalized
    raise ValueError(f"Ungültiger Steuersatz: {vat_rate}")


def _amount_net_from_gross(amount_gross: float, vat_rate: str) -> float:
    gross = Decimal(str(amount_gross))
    normalized = _normalize_vat_rate(vat_rate)
    if normalized == "0%":
        return float(gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    rate = Decimal(normalized.rstrip("%")) / Decimal("100")
    try:
        net = gross / (Decimal("1") + rate)
    except InvalidOperation:
        return float(gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return float(net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# Serialisiert Beleg-Anlagen: Papierkram vergibt bei parallelen POSTs
# dieselbe Belegnummer (Race Condition im Nummernkreis, siehe B-00358/B-00367/B-00396).
_voucher_create_lock = asyncio.Lock()


def _rounded_amount(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _provenance_for_supplier(supplier_id: int) -> str:
    if supplier_id in NON_EU_SUPPLIER_IDS:
        return "foreign"
    if supplier_id in EU_SUPPLIER_IDS:
        return "eu"
    return "domestic"


def _error_payload(resp: httpx.Response) -> str:
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text[:1000]
    return json.dumps(
        {
            "status_code": resp.status_code,
            "error": detail,
        },
        ensure_ascii=False,
        indent=2,
    )


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

        if isinstance(data, dict):
            result = dict(data)
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
    category: str,
    pdf_path: str | None = None,
    due_date: str | None = None,
    mark_paid: bool = False,
) -> str:
    """Eingangsbeleg (Expense Voucher) erfassen.

    GESCHÄFTSREGEL: NIEMALS automatisch aufrufen — IMMER erst Frank eine Rückfrage
    zeigen mit Lieferant, Betrag, Datum und auf Bestätigung warten!
    Erstellt nur Drafts, NIEMALS versenden.
    Beträge immer in Euro mit 2 Dezimalstellen.

    Bekannte Lieferanten-IDs: Hetzner=229, Anthropic=244, OpenAI=235,
    Perplexity=238, STRATO=65, Vodafone=241, O2=38, Cursor=256,
    Skool=232, Papierkram/Odacer=13, Freiraum24=259.

    Args:
        name: Beschreibung des Belegs (z.B. "Hetzner März 2026")
        supplier_id: Lieferanten-ID aus Papierkram
        amount_gross: Bruttobetrag in Euro
        vat_rate: Steuersatz ("19%", "7%", "0%")
        date: Belegdatum im Format YYYY-MM-DD
        category: Papierkram-Kategorie (z.B. "Software/EDV (Büroartikel)")
        pdf_path: Optionaler Pfad zur PDF-Datei
        due_date: Fälligkeitsdatum (YYYY-MM-DD). Default: date + 14 Tage.
        mark_paid: Als bezahlt markieren (default: false)
    """
    if due_date is None:
        due_date = (
            datetime.strptime(date, "%Y-%m-%d") + timedelta(days=14)
        ).strftime("%Y-%m-%d")

    normalized_vat_rate = _normalize_vat_rate(vat_rate)
    # WICHTIG: Papierkram erwartet im line_item "amount" den BRUTTObetrag.
    # Frueher wurde hier faelschlich in Netto umgerechnet (Bug: Belege wie
    # B-00409/B-00410 wurden mit Nettobetrag angelegt). NICHT umrechnen!
    line_item_amount = _rounded_amount(amount_gross)
    payload = {
        "name": name,
        "creditor": {"id": supplier_id},
        "document_date": date,
        "due_date": due_date,
        "line_items": [
            {
                "name": name,
                "amount": line_item_amount,
                "vat_rate": normalized_vat_rate,
                "category": category,
            }
        ],
        "provenance": _provenance_for_supplier(supplier_id),
    }

    async with _voucher_create_lock, httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAPIERKRAM_URL}/expense/vouchers",
            headers=_headers(),
            json=payload,
        )
        if not resp.is_success:
            raise RuntimeError(_error_payload(resp))
        result = resp.json()

        if pdf_path and "id" in result:
            with open(pdf_path, "rb") as fh:
                files = {"file": (os.path.basename(pdf_path), fh, "application/pdf")}
                upload_resp = await client.post(
                    f"{PAPIERKRAM_URL}/expense/vouchers/{result['id']}/documents",
                    headers={"Authorization": f"Bearer {PAPIERKRAM_TOKEN}"},
                    files=files,
                )
                result["pdf_upload_status"] = upload_resp.status_code
                result["pdf_uploaded"] = upload_resp.is_success

        if mark_paid and "id" in result:
            pay_value = _rounded_amount(result.get("amount", line_item_amount))
            pay_resp = await client.post(
                f"{PAPIERKRAM_URL}/expense/vouchers/{result['id']}/pay",
                headers=_headers(),
                json={"payment_date": date, "value": pay_value},
            )
            result["pay_status_code"] = pay_resp.status_code
            result["pay_value"] = pay_value
            if not pay_resp.is_success:
                try:
                    result["pay_error"] = pay_resp.json()
                except Exception:
                    result["pay_error"] = pay_resp.text[:500]
            # /pay liefert manchmal 422 wenn schon bezahlt und kann eventual-consistent
            # sein — bis zu 3× pollen, bevor wir den Status festschreiben.
            state = None
            verify_resp = None
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(0.5)
                verify_resp = await client.get(
                    f"{PAPIERKRAM_URL}/expense/vouchers/{result['id']}",
                    headers=_headers(),
                )
                if verify_resp.is_success:
                    state = verify_resp.json().get("state")
                    if state == "paid":
                        break
            if verify_resp is not None and verify_resp.is_success:
                result["marked_paid"] = (state == "paid")
                result["verified_state"] = state
            else:
                result["marked_paid"] = pay_resp.is_success

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
    Skool=232, Papierkram/Odacer=13, Freiraum24=259.
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


_TIME_UNIT_DEFAULT_NAMES = {
    "piece": ("Stück", "Stück"),
    "hour": ("Stunde", "Stunden"),
    "day": ("Tag", "Tage"),
    "month": ("Monat", "Monate"),
    "year": ("Jahr", "Jahre"),
}


@mcp.tool()
async def papierkram_list_articles(
    proposition_type: str | None = None,
    name_query: str | None = None,
    include_archived: bool = False,
) -> str:
    """Artikel (Waren/Dienstleistungen) aus Papierkram auflisten.

    Args:
        proposition_type: Filter "service" oder "goods" (optional).
        name_query: Substring-Suche im Namen (Client-seitig, case-insensitive).
        include_archived: True → auch archivierte Artikel zurückgeben (Default: nur active).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAPIERKRAM_URL}/income/propositions",
            params={"per_page": 100},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            items = data.get("entries") or data.get("propositions") or []
        else:
            items = data
        out = []
        q = name_query.lower() if name_query else None
        for it in items:
            if not include_archived and it.get("record_state") != "active":
                continue
            if proposition_type and it.get("proposition_type") != proposition_type:
                continue
            if q and q not in (it.get("name") or "").lower():
                continue
            out.append(it)
        return json.dumps({"count": len(out), "propositions": out}, ensure_ascii=False, indent=2)


@mcp.tool()
async def papierkram_create_article(
    name: str,
    price: float,
    proposition_type: str = "service",
    vat_rate: float = 0.19,
    description: str | None = None,
    article_no: str | None = None,
    time_unit: str = "piece",
    unit_name_1: str | None = None,
    unit_name_n: str | None = None,
) -> str:
    """Neuen Artikel (Ware/Dienstleistung) in Papierkram anlegen.

    GESCHÄFTSREGEL: Frank bestätigen lassen vor dem Anlegen.
    Default-Einheitennamen werden bei Bedarf aus time_unit abgeleitet
    (piece→Stück, hour→Stunde, day→Tag, month→Monat, year→Jahr).

    Args:
        name: Artikelname (Pflicht)
        price: Preis netto in Euro (Pflicht)
        proposition_type: "service" oder "goods" (Default: service)
        vat_rate: Umsatzsteuersatz als Dezimal, z.B. 0.19 oder 0.07 (Default 0.19)
        description: Lange Beschreibung (optional)
        article_no: Artikelnummer (optional)
        time_unit: Abrechnungseinheit ("piece", "hour", "day", "month", "year")
        unit_name_1: Singular der Einheit (optional, sonst Default)
        unit_name_n: Plural der Einheit (optional, sonst Default)
    """
    default_1, default_n = _TIME_UNIT_DEFAULT_NAMES.get(time_unit, ("Stück", "Stück"))
    if not article_no:
        article_no = "AN-" + datetime.now().strftime("%y%m%d%H%M%S")
    payload = {
        "name": name,
        "article_no": article_no,
        "description": description or "",
        "price": f"{round(price, 2)}",
        "proposition_type": proposition_type,
        "vat_rate": f"{vat_rate}",
        "time_unit": time_unit,
        "unit_name_1": unit_name_1 or default_1,
        "unit_name_n": unit_name_n or default_n,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAPIERKRAM_URL}/income/propositions",
            headers=_headers(),
            json=payload,
        )
        if not resp.is_success:
            return json.dumps(
                {
                    "ok": False,
                    "status": resp.status_code,
                    "body": resp.text[:1000],
                    "sent": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        result = resp.json()
        return json.dumps({"ok": True, "proposition": result}, ensure_ascii=False, indent=2)




@mcp.tool()
async def papierkram_create_invoice_draft(
    customer_company_id: int,
    items: list[dict],
    subject: str | None = None,
    notes: str | None = None,
    document_date: str | None = None,
    due_date: str | None = None,
    payment_term_id: int = 19,
) -> str:
    """Ausgangsrechnung als ENTWURF (Draft) anlegen.

    GESCHÄFTSREGEL: NIEMALS sofort versenden — nur Draft. Frank verschickt
    Rechnungen selbst aus der Papierkram-UI. Vorher IMMER Rückfrage mit
    Kunde, Positionen und Summe.

    Args:
        customer_company_id: ID des bereits angelegten Kunden (siehe
            `papierkram_search_supplier` oder UI-Liste).
        items: Liste von Positionen. Jeder Eintrag entweder:
            - {"proposition_id": <int>, "quantity": <float>, "description": <str optional>}
              → übernimmt Name, Preis, VAT vom Stammartikel.
            - {"name": <str>, "quantity": <float>, "unit_price": <float>,
               "vat_rate": <float optional>, "description": <str optional>}
              → Freitext-Position.
        subject: Rechnungs-Betreff/Name (optional).
        notes: Anmerkungs-Text (optional).
        document_date: Belegdatum YYYY-MM-DD (Default: heute).
        due_date: Fälligkeit YYYY-MM-DD (Default: document_date + 14 Tage).
    """
    if not document_date:
        document_date = datetime.now().strftime("%Y-%m-%d")
    if not due_date:
        due_date = (
            datetime.strptime(document_date, "%Y-%m-%d") + timedelta(days=14)
        ).strftime("%Y-%m-%d")

    line_items = []
    for it in items:
        if "proposition_id" in it:
            li = {
                "proposition": {"id": int(it["proposition_id"])},
                "quantity": float(it.get("quantity", 1.0)),
            }
            if it.get("description"):
                li["description"] = it["description"]
        else:
            vat = it.get("vat_rate", 0.19)
            li = {
                "name": it["name"],
                "description": it.get("description", ""),
                "quantity": float(it.get("quantity", 1.0)),
                "price": round(float(it["unit_price"]), 2),
                "vat_rate": float(vat),
            }
        line_items.append(li)

    payload = {
        "customer": {"id": int(customer_company_id)},
        "document_date": document_date,
        "payment_term": {"id": int(payment_term_id)},
        "line_items": line_items,
    }
    if subject:
        payload["name"] = subject
    if notes:
        payload["description"] = notes

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{PAPIERKRAM_URL}/income/invoices",
            headers=_headers(),
            json=payload,
        )
        if not resp.is_success:
            return json.dumps(
                {
                    "ok": False,
                    "status": resp.status_code,
                    "body": resp.text[:1500],
                    "sent": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        result = resp.json()
        return json.dumps(
            {
                "ok": True,
                "draft_id": result.get("id"),
                "invoice_no": result.get("invoice_no"),
                "state": result.get("state"),
                "sent_on": result.get("sent_on"),
                "invoice": result,
            },
            ensure_ascii=False,
            indent=2,
        )


if __name__ == "__main__":
    transport = os.environ.get("PAPIERKRAM_MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
