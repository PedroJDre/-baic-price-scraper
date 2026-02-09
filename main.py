import re
import sys
import time
import smtplib
import ssl
from datetime import datetime
from difflib import get_close_matches, SequenceMatcher
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urlencode

import requests

from config import (
    BASE_URL,
    ITEMS_PER_PAGE,
    MAX_PAGES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    HEADERS,
    SCRAPERAPI_KEY,
    SCRAPERAPI_URL,
    SMTP_SERVER,
    SMTP_PORT,
    EMAIL_SENDER,
    EMAIL_PASSWORD,
    EMAIL_RECIPIENTS,
    EMAIL_SUBJECT,
)


def build_page_url(page_number):
    """Build the URL for a given page number (1-based)."""
    if page_number <= 1:
        return BASE_URL
    offset = (page_number - 1) * ITEMS_PER_PAGE + 1
    return f"{BASE_URL}_Desde_{offset}"


def fetch_page(url, retries=3):
    """Fetch a single page, using ScraperAPI if key is configured.

    ScraperAPI requires render=true for MercadoLibre (JS-rendered content).
    Retries on failure to handle occasional timeouts during pagination.
    """
    for attempt in range(1, retries + 1):
        try:
            if SCRAPERAPI_KEY:
                params = urlencode({
                    "api_key": SCRAPERAPI_KEY,
                    "url": url,
                    "render": "true",
                    "country_code": "ar",
                    "wait_for_selector": "li.ui-search-layout__item",
                })
                api_url = f"{SCRAPERAPI_URL}?{params}"
                response = requests.get(api_url, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == retries:
                raise
            wait = attempt * 5
            print(f"  Reintentando en {wait}s (intento {attempt}/{retries})...")
            time.sleep(wait)


def parse_page(html):
    """Extract listing data from a page's HTML using regex.

    Each listing is inside a poly-card component within an
    <li class="ui-search-layout__item"> element.
    """
    listings = []

    # Split HTML into individual listing cards
    cards = re.findall(
        r'<li class="ui-search-layout__item[^"]*"[^>]*>(.*?)</li>',
        html,
        re.DOTALL,
    )

    for card in cards:
        # Extract item URL
        url_match = re.search(
            r'href="(https://auto\.mercadolibre\.com\.ar/MLA-[^"]+)"', card
        )
        if not url_match:
            continue

        item_url = url_match.group(1)

        # Extract title
        title_match = re.search(
            r'class="poly-component__title[^"]*"[^>]*>(.*?)</(?:a|h\d|div|span)',
            card,
            re.DOTALL,
        )
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""

        # Extract price (fraction part)
        price_match = re.search(
            r'class="andes-money-amount__fraction"[^>]*>([^<]+)', card
        )
        price_str = price_match.group(1).strip() if price_match else "0"

        # Extract currency symbol
        currency_match = re.search(
            r'class="andes-money-amount__currency-symbol"[^>]*>([^<]+)', card
        )
        currency = currency_match.group(1).strip() if currency_match else "$"

        # Extract seller name
        seller_match = re.search(
            r'class="poly-component__seller[^"]*"[^>]*>([^<]+)', card
        )
        seller = seller_match.group(1).strip() if seller_match else "N/A"

        # Extract location
        location_match = re.search(
            r'class="poly-component__location[^"]*"[^>]*>([^<]+)', card
        )
        location = location_match.group(1).strip() if location_match else ""

        # Parse price to integer (format: "28.500.000" -> 28500000)
        try:
            price = int(price_str.replace(".", ""))
        except ValueError:
            price = 0

        listings.append({
            "title": title,
            "seller": seller,
            "price": price,
            "currency": currency,
            "location": location,
            "url": item_url,
        })

    return listings


def fetch_all_listings():
    """Fetch all BAIC listings across all pages."""
    all_listings = []

    if SCRAPERAPI_KEY:
        print("Usando ScraperAPI para las solicitudes")
    else:
        print("ScraperAPI no configurado, usando solicitudes directas")

    for page in range(1, MAX_PAGES + 1):
        url = build_page_url(page)
        print(f"Pagina {page}: {url}")

        try:
            html = fetch_page(url)
        except requests.RequestException as e:
            print(f"  Error en pagina {page}: {e}")
            break

        listings = parse_page(html)
        print(f"  {len(listings)} publicaciones encontradas")

        if not listings:
            if page == 1:
                # Debug: show what ScraperAPI returned so we can diagnose
                print(f"  DEBUG: HTML length = {len(html)}")
                print(f"  DEBUG: Has 'ui-search-layout': {'ui-search-layout' in html}")
                print(f"  DEBUG: Has 'poly-card': {'poly-card' in html}")
                print(f"  DEBUG: First 500 chars: {html[:500]}")
            break

        all_listings.extend(listings)

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total publicaciones obtenidas: {len(all_listings)}")
    return all_listings


KNOWN_MODELS = ["BJ30", "BJ40", "BJ60", "EU5", "U5", "X25", "X35", "X55"]
# Sorted longest-first so EU5 is checked before U5, etc.
_MODELS_BY_LEN = sorted(KNOWN_MODELS, key=len, reverse=True)


def extract_base_model(title):
    """Extract the base model name (e.g. BJ30, X35, EU5) from a listing title.

    Uses a three-step strategy:
    1. Search for a known model as a distinct token in the title.
    2. Fall back to regex extraction + fuzzy match against known models.
    3. Default to "Otros".
    """
    # Normalise: strip non-alphanumeric (except spaces) and uppercase
    clean = re.sub(r'[^A-Z0-9\s]', '', title.upper())

    # Step 1 — look for known models (longest first to prevent U5 matching EU5)
    for model in _MODELS_BY_LEN:
        # Boundary check: not preceded by a letter, not followed by a letter/digit
        if re.search(rf'(?<![A-Z]){re.escape(model)}(?![A-Z0-9])', clean):
            return model

    # Step 2 — regex fallback + fuzzy match to known models
    match = re.search(r'BAIC\s+(\w+\d+)', clean)
    if match:
        candidate = match.group(1)
        close = get_close_matches(candidate, KNOWN_MODELS, n=1, cutoff=0.6)
        if close:
            return close[0]
        return candidate

    return "Otros"


def _merge_similar_groups(grouped, cutoff=0.7):
    """Merge unknown group names into the closest known model group.

    Known models are never merged with each other — only truly unknown names
    (from regex fallback) get absorbed if they're similar enough.
    """
    result = {}
    orphans = {}

    for name, items in grouped.items():
        if name in KNOWN_MODELS:
            result[name] = list(items)
        else:
            orphans[name] = items

    for name, items in orphans.items():
        targets = list(result.keys()) or list(grouped.keys())
        close = get_close_matches(name, targets, n=1, cutoff=cutoff)
        if close:
            result[close[0]].extend(items)
        else:
            result[name] = list(items)

    for model in result:
        result[model].sort(key=lambda x: x["price"])

    return dict(sorted(result.items()))


def process_listings(items):
    """Group listings by base model, merge similar groups, sort by price."""
    grouped = {}

    for item in items:
        title = item.get("title", "Sin titulo") or "Sin titulo"
        model = extract_base_model(title)
        grouped.setdefault(model, []).append(item)

    # Merge groups with similar names (e.g. BJ30E into BJ30)
    return _merge_similar_groups(grouped)


def _format_price(entry):
    """Format a listing's price as a display string."""
    if entry["currency"] in ("U$S", "US$"):
        return f"USD {entry['price']:,}".replace(",", ".")
    return f"${entry['price']:,}".replace(",", ".")


def format_plain_text(grouped):
    """Format grouped listings into a plain-text body for console output."""
    if not grouped:
        return "No se encontraron publicaciones de BAIC en Mercado Libre."

    lines = []
    lines.append("=" * 60)
    lines.append("BAIC - Precios en Mercado Libre Argentina")
    lines.append(f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    lines.append("=" * 60)
    lines.append("")

    total_listings = 0

    for model, entries in grouped.items():
        lines.append(f"Modelo: {model} ({len(entries)} publicaciones)")
        lines.append("-" * 40)
        for entry in entries:
            price_str = _format_price(entry)
            loc = f"  ({entry['location']})" if entry["location"] else ""
            lines.append(f"  {entry['title']} | {entry['seller']}: {price_str}{loc}")
            total_listings += 1
        lines.append("")

    lines.append("=" * 60)
    lines.append(f"Total: {len(grouped)} modelos, {total_listings} publicaciones")
    lines.append("=" * 60)

    return "\n".join(lines)


def _strip_model_prefix(title, model):
    """Remove 'Baic MODEL' prefix from title to get just the variant info."""
    cleaned = re.sub(rf'^Baic\s+{re.escape(model)}\s*', '', title, flags=re.IGNORECASE).strip()
    return cleaned or title


def _price_range_str(entries):
    """Return a 'min - max' price range string for a group of entries."""
    prices = [e for e in entries if e["price"] > 0]
    if not prices:
        return ""
    lo = min(prices, key=lambda x: x["price"])
    hi = max(prices, key=lambda x: x["price"])
    return f"{_format_price(lo)} — {_format_price(hi)}"


def format_html_email(grouped):
    """Format grouped listings into a styled HTML email body."""
    if not grouped:
        return (
            "<!DOCTYPE html><html><body>"
            "<p>No se encontraron publicaciones de BAIC en Mercado Libre.</p>"
            "</body></html>"
        )

    date_str = datetime.now().strftime('%d/%m/%Y %H:%M')
    total_listings = sum(len(v) for v in grouped.values())

    # --- summary cards (one per model) ---
    summary_cards = []
    for model, entries in grouped.items():
        summary_cards.append(
            f'<td style="padding:0 6px 12px 6px;text-align:center;">'
            f'<div style="background-color:#1a237e;border-radius:8px;padding:14px 18px;min-width:100px;">'
            f'<div style="font-size:20px;font-weight:700;letter-spacing:0.5px;">{model}</div>'
            f'<div style="font-size:12px;margin-top:4px;opacity:0.85;">'
            f'{len(entries)} pub.</div>'
            f'</div></td>'
        )
    summary_cards_html = "".join(summary_cards)

    # --- model sections ---
    sections = []
    for model, entries in grouped.items():
        price_range = _price_range_str(entries)

        rows = []
        for i, entry in enumerate(entries):
            bg = "#f4f6fb" if i % 2 == 0 else "#ffffff"
            price_str = _format_price(entry)
            variant = _strip_model_prefix(entry["title"], model)
            seller = entry["seller"] if entry["seller"] != "N/A" else '<span style="color:#999;">—</span>'
            location = entry["location"] if entry["location"] else '<span style="color:#999;">—</span>'

            rows.append(
                f'<tr style="background-color:{bg};">'
                f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;">{variant}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;color:#444;">{seller}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;text-align:right;'
                f'font-weight:700;color:#1b5e20;white-space:nowrap;">{price_str}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;color:#555;">{location}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;text-align:center;">'
                f'<a href="{entry["url"]}" target="_blank" style="display:inline-block;'
                f'background-color:#2962ff;color:#ffffff;padding:5px 12px;border-radius:4px;'
                f'font-size:12px;font-weight:600;text-decoration:none;">Ver</a></td>'
                f'</tr>'
            )
        rows_html = "\n".join(rows)

        range_badge = ""
        if price_range:
            range_badge = (
                f'<span style="float:right;font-size:13px;font-weight:400;'
                f'opacity:0.9;margin-top:2px;">{price_range}</span>'
            )

        sections.append(
            f'<div style="margin-bottom:32px;">'
            # section header
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border:none;">'
            f'<tr><td style="background-color:#0d47a1;padding:14px 20px;'
            f'border-radius:10px 10px 0 0;color:#ffffff;font-size:17px;font-weight:700;">'
            f'BAIC {model} '
            f'<span style="font-weight:400;font-size:13px;background-color:rgba(255,255,255,0.2);'
            f'padding:2px 10px;border-radius:12px;margin-left:6px;">{len(entries)}</span>'
            f'{range_badge}'
            f'</td></tr></table>'
            # data table
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;border:1px solid #dde2ea;border-top:none;font-size:13px;">'
            f'<thead><tr style="background-color:#e8eaf6;">'
            f'<th style="padding:10px 14px;text-align:left;font-weight:600;color:#37474f;'
            f'border-bottom:2px solid #c5cae9;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">Variante</th>'
            f'<th style="padding:10px 14px;text-align:left;font-weight:600;color:#37474f;'
            f'border-bottom:2px solid #c5cae9;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">Vendedor</th>'
            f'<th style="padding:10px 14px;text-align:right;font-weight:600;color:#37474f;'
            f'border-bottom:2px solid #c5cae9;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">Precio</th>'
            f'<th style="padding:10px 14px;text-align:left;font-weight:600;color:#37474f;'
            f'border-bottom:2px solid #c5cae9;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">Ubicaci\u00f3n</th>'
            f'<th style="padding:10px 14px;text-align:center;font-weight:600;color:#37474f;'
            f'border-bottom:2px solid #c5cae9;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">Link</th>'
            f'</tr></thead>'
            f'<tbody>{rows_html}</tbody>'
            f'</table>'
            f'</div>'
        )

    sections_html = "\n".join(sections)

    return (
        '<!DOCTYPE html>'
        '<html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '</head>'
        '<body style="margin:0;padding:0;background-color:#eef1f7;-webkit-font-smoothing:antialiased;">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#eef1f7;padding:24px 0;">'
        '<tr><td align="center">'
        # main container
        '<table width="680" cellpadding="0" cellspacing="0" '
        'style="background-color:#ffffff;border-radius:12px;overflow:hidden;'
        'box-shadow:0 2px 12px rgba(0,0,0,0.08);font-family:\'Segoe UI\',Arial,Helvetica,sans-serif;">'
        # HEADER
        '<tr><td style="background-color:#0d47a1;padding:32px 30px 24px;text-align:center;">'
        '<h1 style="margin:0;font-size:26px;color:#ffffff;font-weight:700;letter-spacing:0.3px;">'
        'BAIC &mdash; Precios Mercado Libre</h1>'
        f'<p style="margin:8px 0 0;font-size:14px;color:rgba(255,255,255,0.8);">'
        f'Reporte generado el {date_str}</p>'
        '</td></tr>'
        # SUMMARY ROW
        '<tr><td style="padding:20px 24px 8px;">'
        '<table cellpadding="0" cellspacing="0" style="width:100%;">'
        f'<tr>{summary_cards_html}</tr>'
        '</table>'
        '</td></tr>'
        # TOTAL BADGE
        '<tr><td style="padding:4px 30px 20px;text-align:center;">'
        f'<span style="display:inline-block;background-color:#e8eaf6;color:#283593;'
        f'padding:6px 20px;border-radius:20px;font-size:13px;font-weight:600;">'
        f'{total_listings} publicaciones en {len(grouped)} modelos</span>'
        '</td></tr>'
        # SECTIONS
        f'<tr><td style="padding:0 24px 24px;">{sections_html}</td></tr>'
        # FOOTER
        '<tr><td style="background-color:#f5f6fa;padding:20px 30px;text-align:center;'
        'border-top:1px solid #e0e3eb;">'
        '<p style="margin:0;font-size:12px;color:#90949e;">'
        'Datos obtenidos de Mercado Libre Argentina &bull; Precios sujetos a cambio</p>'
        '</td></tr>'
        '</table>'
        '</td></tr></table>'
        '</body></html>'
    )


def send_email(subject, plain_body, html_body):
    """Send the email via Gmail SMTP with HTML + plain text fallback."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print("AVISO: Credenciales de email no configuradas. Mostrando resultado en consola.")
        return

    recipients = [r.strip() for r in EMAIL_RECIPIENTS if r.strip()]
    if not recipients:
        print("AVISO: No hay destinatarios configurados.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())

    print(f"Email enviado a: {', '.join(recipients)}")


def main():
    print(f"Iniciando scraper BAIC - {datetime.now()}")

    try:
        items = fetch_all_listings()
    except Exception as e:
        print(f"ERROR al obtener publicaciones: {e}")
        sys.exit(1)

    grouped = process_listings(items)
    plain_body = format_plain_text(grouped)
    html_body = format_html_email(grouped)

    print("\n--- CONTENIDO DEL EMAIL ---")
    print(plain_body)
    print("--- FIN DEL EMAIL ---\n")

    try:
        send_email(EMAIL_SUBJECT, plain_body, html_body)
    except Exception as e:
        print(f"ERROR al enviar email: {e}")
        sys.exit(1)

    print("Listo.")


if __name__ == "__main__":
    main()
