import re
import sys
import time
import smtplib
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

from config import (
    BASE_URL,
    ITEMS_PER_PAGE,
    MAX_PAGES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    HEADERS,
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


def fetch_page(url):
    """Fetch a single page and return its HTML content."""
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


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
            break

        all_listings.extend(listings)

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total publicaciones obtenidas: {len(all_listings)}")
    return all_listings


def process_listings(items):
    """Group listings by title (model), sort each group by price ascending."""
    grouped = {}

    for item in items:
        title = item.get("title", "Sin titulo")
        if not title:
            title = "Sin titulo"

        grouped.setdefault(title, []).append(item)

    # Sort each group by price ascending
    for title in grouped:
        grouped[title].sort(key=lambda x: x["price"])

    # Sort groups alphabetically by title
    return dict(sorted(grouped.items()))


def format_email(grouped):
    """Format grouped listings into a plain-text email body."""
    if not grouped:
        return "No se encontraron publicaciones de BAIC en Mercado Libre."

    lines = []
    lines.append("=" * 60)
    lines.append("BAIC - Precios en Mercado Libre Argentina")
    lines.append(f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    lines.append("=" * 60)
    lines.append("")

    total_listings = 0

    for title, entries in grouped.items():
        lines.append(f"Modelo: {title}")
        lines.append("-" * 40)
        for entry in entries:
            if entry["currency"] == "U$S" or entry["currency"] == "US$":
                price_str = f"USD {entry['price']:,}".replace(",", ".")
            else:
                price_str = f"${entry['price']:,}".replace(",", ".")
            loc = f"  ({entry['location']})" if entry["location"] else ""
            lines.append(f"  {entry['seller']}: {price_str}{loc}")
            total_listings += 1
        lines.append("")

    lines.append("=" * 60)
    lines.append(f"Total: {len(grouped)} modelos, {total_listings} publicaciones")
    lines.append("=" * 60)

    return "\n".join(lines)


def send_email(subject, body):
    """Send the email via Gmail SMTP. Falls back to stdout if not configured."""
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
    msg.attach(MIMEText(body, "plain", "utf-8"))

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
    body = format_email(grouped)

    print("\n--- CONTENIDO DEL EMAIL ---")
    print(body)
    print("--- FIN DEL EMAIL ---\n")

    try:
        send_email(EMAIL_SUBJECT, body)
    except Exception as e:
        print(f"ERROR al enviar email: {e}")
        sys.exit(1)

    print("Listo.")


if __name__ == "__main__":
    main()
