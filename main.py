import json
import os
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
    APIFY_API_TOKEN,
    APIFY_ACTOR_ID,
    SMTP_SERVER,
    SMTP_PORT,
    EMAIL_SENDER,
    EMAIL_PASSWORD,
    EMAIL_RECIPIENTS,
    EMAIL_SUBJECT,
)

PRICES_FILE = os.path.join(os.path.dirname(__file__), "data", "prices.json")


def load_previous_prices():
    """Load previous run's prices from data/prices.json.

    Returns {url: {"price": int, "currency": str}} or empty dict if file missing.
    """
    try:
        with open(PRICES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_current_prices(items):
    """Save current listings' prices to data/prices.json."""
    prices = {}
    for item in items:
        prices[item["url"]] = {
            "price": item["price"],
            "currency": item["currency"],
        }
    os.makedirs(os.path.dirname(PRICES_FILE), exist_ok=True)
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)
    print(f"Precios guardados en {PRICES_FILE} ({len(prices)} publicaciones)")


def compute_price_changes(items, previous):
    """Compare current prices to previous run and tag each item.

    Sets item["price_change"] to "up", "down", "same", or "new".
    Sets item["price_diff"] to the absolute difference (0 for new/same).
    """
    for item in items:
        url = item["url"]
        prev = previous.get(url)
        if prev is None or prev.get("currency") != item["currency"]:
            item["price_change"] = "new"
            item["price_diff"] = 0
        elif item["price"] > prev["price"]:
            item["price_change"] = "up"
            item["price_diff"] = item["price"] - prev["price"]
        elif item["price"] < prev["price"]:
            item["price_change"] = "down"
            item["price_diff"] = prev["price"] - item["price"]
        else:
            item["price_change"] = "same"
            item["price_diff"] = 0


def build_page_url(page_number):
    """Build the URL for a given page number (1-based)."""
    if page_number <= 1:
        return BASE_URL
    offset = (page_number - 1) * ITEMS_PER_PAGE + 1
    return f"{BASE_URL}_Desde_{offset}"


def _scraperapi_request(url, use_wait_for_selector=True):
    """Make a single ScraperAPI request. Returns response object."""
    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "render": "true",
        "country_code": "ar",
    }
    if use_wait_for_selector:
        params["wait_for_selector"] = "li.ui-search-layout__item"
    api_url = f"{SCRAPERAPI_URL}?{urlencode(params)}"
    return requests.get(api_url, timeout=REQUEST_TIMEOUT)


def fetch_page(url, retries=3):
    """Fetch a single page, using ScraperAPI if key is configured.

    ScraperAPI requires render=true for MercadoLibre (JS-rendered content).
    Tries with wait_for_selector first; if that fails with a server error,
    falls back to a request without it.
    """
    for attempt in range(1, retries + 1):
        try:
            if SCRAPERAPI_KEY:
                try:
                    response = _scraperapi_request(url, use_wait_for_selector=True)
                    response.raise_for_status()
                    return response.text
                except requests.RequestException as e:
                    print(f"  wait_for_selector fallo ({e}), reintentando sin el...")
                    response = _scraperapi_request(url, use_wait_for_selector=False)
                    response.raise_for_status()
                    return response.text
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

        # Extract all prices from the card
        price_strs = re.findall(
            r'class="andes-money-amount__fraction"[^>]*>([^<]+)', card
        )

        # Extract currency symbol
        currency_match = re.search(
            r'class="andes-money-amount__currency-symbol"[^>]*>([^<]+)', card
        )
        currency = currency_match.group(1).strip() if currency_match else "$"

        # Detect anticipo: check if card contains "anticipo" text
        has_anticipo = bool(re.search(r'anticipo', card, re.IGNORECASE))

        # Parse all prices to integers
        parsed_prices = []
        for ps in price_strs:
            try:
                parsed_prices.append(int(ps.strip().replace(".", "")))
            except ValueError:
                pass

        if has_anticipo and len(parsed_prices) >= 2:
            # When anticipo is present, the larger value is the full price
            # and the smaller value is the anticipo (down payment)
            price = max(parsed_prices)
            anticipo = min(parsed_prices)
        elif parsed_prices:
            price = parsed_prices[0]
            anticipo = 0
        else:
            price = 0
            anticipo = 0

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

        listings.append({
            "title": title,
            "seller": seller,
            "price": price,
            "anticipo": anticipo,
            "currency": currency,
            "location": location,
            "url": item_url,
        })

    return listings


_APIFY_KEYWORDS = [
    "Baic",
    "Baic BJ30 2wd",
    "Baic BJ30 4wd",
    "Baic BJ40",
    "Baic BJ60",
    "Baic EU5",
    "Baic U5",
    "Baic X25",
    "Baic X35",
    "Baic X55",
]


def _apify_convert_item(item):
    """Convert a single Apify result to our internal listing format."""
    price_str = item.get("nuevoPrecio", "0") or "0"
    try:
        price = int(str(price_str).replace(".", ""))
    except ValueError:
        price = 0

    moneda = item.get("Moneda", "ARS $") or "ARS $"
    currency = "U$S" if "US" in moneda.upper() else "$"

    return {
        "title": item.get("articuloTitulo", ""),
        "seller": item.get("Vendedor", "") or "N/A",
        "price": price,
        "anticipo": 0,
        "currency": currency,
        "location": "",
        "url": item.get("zdireccion", ""),
    }


def fetch_via_apify():
    """Fallback: fetch BAIC listings using the Apify MercadoLibre actor.

    Runs one search per BAIC model to maximize coverage on the free plan
    (which limits each run to 1 page / ~50 results).
    """
    if not APIFY_API_TOKEN:
        print("Apify no configurado, omitiendo fallback")
        return []

    print("Usando Apify como fallback...")
    api_base = "https://api.apify.com/v2"
    headers = {"Authorization": f"Bearer {APIFY_API_TOKEN}"}
    all_listings = []
    seen_urls = set()

    for keyword in _APIFY_KEYWORDS:
        print(f"  Apify buscando: '{keyword}'")
        run_input = {
            "country": "https://listado.mercadolibre.com.ar/",
            "keyword": keyword,
            "pages": 1,
            "promoted": False,
        }

        try:
            resp = requests.post(
                f"{api_base}/acts/{APIFY_ACTOR_ID}/runs?waitForFinish=120",
                headers=headers,
                json=run_input,
                timeout=150,
            )
            resp.raise_for_status()
            run_data = resp.json()["data"]

            # Poll if still running
            if run_data["status"] not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                run_id = run_data["id"]
                for _ in range(6):
                    time.sleep(10)
                    poll = requests.get(
                        f"{api_base}/actor-runs/{run_id}",
                        headers=headers,
                        timeout=30,
                    )
                    poll.raise_for_status()
                    run_data = poll.json()["data"]
                    if run_data["status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                        break

            if run_data["status"] != "SUCCEEDED":
                print(f"    Status: {run_data['status']}, omitiendo")
                continue

            dataset_id = run_data["defaultDatasetId"]
            resp = requests.get(
                f"{api_base}/datasets/{dataset_id}/items?format=json",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json()

            # Deduplicate by URL
            new_count = 0
            for item in items:
                url = item.get("zdireccion", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_listings.append(_apify_convert_item(item))
                    new_count += 1

            print(f"    {len(items)} resultados, {new_count} nuevos")

        except Exception as e:
            print(f"    Error para '{keyword}': {e}")
            continue

    print(f"  Apify total: {len(all_listings)} publicaciones unicas")
    return all_listings


def fetch_all_listings():
    """Fetch all BAIC listings across all pages.

    Tries ScraperAPI/direct first. If that returns 0 results, falls back to Apify.
    """
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
                print(f"  DEBUG: HTML length = {len(html)}")
                print(f"  DEBUG: Has 'ui-search-layout': {'ui-search-layout' in html}")
                print(f"  DEBUG: Has 'poly-card': {'poly-card' in html}")
            break

        all_listings.extend(listings)

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total publicaciones (scraping directo): {len(all_listings)}")

    # Supplement with Apify if scraping returned fewer than expected
    if len(all_listings) < 200:
        print(f"Pocas publicaciones ({len(all_listings)}), complementando con Apify...")
        apify_listings = fetch_via_apify()

        # Merge: deduplicate by URL
        seen_urls = {item["url"] for item in all_listings if item["url"]}
        new_count = 0
        for item in apify_listings:
            if item["url"] and item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_listings.append(item)
                new_count += 1

        print(f"Apify agrego {new_count} publicaciones nuevas")

    print(f"Total publicaciones finales: {len(all_listings)}")
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


def _strip_model_prefix(title, model):
    """Remove 'Baic MODEL' prefix and redundant model name from title."""
    # Strip "Baic MODEL" or "Baic MODELe" prefix (e.g. Bj30e)
    cleaned = re.sub(rf'^Baic\s+{re.escape(model)}e?\s*', '', title, flags=re.IGNORECASE).strip()
    # Remove any remaining occurrences of the model name (e.g. "1.5t Bj30 4wd")
    cleaned = re.sub(rf'\b{re.escape(model)}e?\b', '', cleaned, flags=re.IGNORECASE).strip()
    # Collapse multiple spaces left by removals
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned or title


_SUBCAT_PATTERNS = [
    (r'\bPlus\b', 'Plus'),
    (r'\bPro\b', 'Pro'),
    (r'\b(?:Ii|Il|II)\b', 'II'),
    (r'\bSe\b', 'SE'),
    (r'\bElite\b', 'Elite'),
    (r'\bHonor\b', 'Honor'),
    (r'\bFashion\b', 'Fashion'),
    (r'\b(?:Comfort|Confort)\b', 'Comfort'),
    (r'\b4[wx][d4]\b', '4x4'),
    (r'\b(?:2wd|4x2)\b', '4x2'),
    (r'\b(?:Electric[oa]?|100%\s*Electrico)\b', 'Electrico'),
    (r'\b(?:Mhev|Milhybrid|Hybrid|H[ií]brida?)\b', 'Hybrid'),
]


def extract_subcategory(variant):
    """Extract a subcategory keyword from a variant name."""
    for pattern, label in _SUBCAT_PATTERNS:
        if re.search(pattern, variant, re.IGNORECASE):
            return label
    return "Otros"


def process_listings(items):
    """Group listings by base model, merge similar groups, sort by price.

    Each item gets a 'subcategory' field added for sub-grouping.
    """
    grouped = {}

    for item in items:
        title = item.get("title", "Sin titulo") or "Sin titulo"
        model = extract_base_model(title)
        variant = _strip_model_prefix(title, model)
        item["subcategory"] = extract_subcategory(variant)
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
            change = entry.get("price_change", "new")
            diff = entry.get("price_diff", 0)
            change_str = ""
            if change == "up":
                diff_fmt = f"{diff:,}".replace(",", ".")
                change_str = f" (\u2191 +{diff_fmt})"
            elif change == "down":
                diff_fmt = f"{diff:,}".replace(",", ".")
                change_str = f" (\u2193 -{diff_fmt})"
            anticipo_str = ""
            if entry.get("anticipo"):
                ant_fmt = f"{entry['anticipo']:,}".replace(",", ".")
                anticipo_str = f" (Anticipo: ${ant_fmt})"
            loc = f"  ({entry['location']})" if entry["location"] else ""
            lines.append(f"  {entry['title']} | {entry['seller']}: {price_str}{change_str}{anticipo_str}{loc}")
            total_listings += 1
        lines.append("")

    lines.append("=" * 60)
    lines.append(f"Total: {len(grouped)} modelos, {total_listings} publicaciones")
    lines.append("=" * 60)

    return "\n".join(lines)


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

    # --- summary cards with anchor links ---
    summary_cards = []
    for model, entries in grouped.items():
        anchor = model.lower()
        summary_cards.append(
            f'<td style="padding:0 6px 12px 6px;text-align:center;">'
            f'<a href="#{anchor}" style="text-decoration:none;color:#ffffff;">'
            f'<div style="background-color:#1a237e;border-radius:8px;padding:14px 18px;min-width:100px;">'
            f'<div style="font-size:20px;font-weight:700;letter-spacing:0.5px;">{model}</div>'
            f'<div style="font-size:12px;margin-top:4px;opacity:0.85;">'
            f'{len(entries)} pub.</div>'
            f'</div></a></td>'
        )
    summary_cards_html = "".join(summary_cards)

    # Column header template
    th_style = ('padding:10px 14px;font-weight:600;color:#37474f;'
                'border-bottom:2px solid #c5cae9;font-size:12px;'
                'text-transform:uppercase;letter-spacing:0.5px;')

    # --- model sections with subcategories ---
    sections = []
    for model, entries in grouped.items():
        anchor = model.lower()
        price_range = _price_range_str(entries)

        # Group entries by subcategory
        subcats = {}
        for entry in entries:
            sc = entry.get("subcategory", "Otros")
            subcats.setdefault(sc, []).append(entry)

        # Build sub-tables for each subcategory
        subcat_html_parts = []
        for sc_name, sc_entries in subcats.items():
            sc_count = len(sc_entries)
            sc_range = _price_range_str(sc_entries)
            range_text = f' &mdash; {sc_range}' if sc_range else ''

            rows = []
            for i, entry in enumerate(sc_entries):
                bg = "#f4f6fb" if i % 2 == 0 else "#ffffff"
                price_str = _format_price(entry)
                variant = _strip_model_prefix(entry["title"], model)
                seller = entry["seller"] if entry["seller"] != "N/A" else '<span style="color:#999;">\u2014</span>'
                location = entry["location"] if entry["location"] else '<span style="color:#999;">\u2014</span>'

                # Price change indicator
                change = entry.get("price_change", "new")
                diff = entry.get("price_diff", 0)
                change_html = ""
                if change == "up":
                    diff_fmt = f"{diff:,}".replace(",", ".")
                    change_html = (
                        f'<br><span style="color:#c62828;font-size:11px;font-weight:600;">'
                        f'\u2191 +{diff_fmt}</span>'
                    )
                elif change == "down":
                    diff_fmt = f"{diff:,}".replace(",", ".")
                    change_html = (
                        f'<br><span style="color:#2e7d32;font-size:11px;font-weight:600;">'
                        f'\u2193 -{diff_fmt}</span>'
                    )

                # Anticipo indicator
                anticipo_html = ""
                if entry.get("anticipo"):
                    ant_fmt = f"{entry['anticipo']:,}".replace(",", ".")
                    anticipo_html = (
                        f'<br><span style="color:#e65100;font-size:11px;font-weight:600;">'
                        f'Anticipo: ${ant_fmt}</span>'
                    )

                # NUEVA badge for new listings
                nueva_badge = ""
                if change == "new":
                    nueva_badge = (
                        ' <span style="display:inline-block;background-color:#ff6f00;color:#fff;'
                        'font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;'
                        'vertical-align:middle;margin-left:4px;">NUEVA</span>'
                    )

                rows.append(
                    f'<tr style="background-color:{bg};">'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;">'
                    f'{variant}{nueva_badge}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;color:#444;">{seller}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;text-align:right;'
                    f'font-weight:700;color:#1b5e20;white-space:nowrap;">{price_str}{change_html}{anticipo_html}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;color:#555;">{location}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;text-align:center;">'
                    f'<a href="{entry["url"]}" target="_blank" style="display:inline-block;'
                    f'background-color:#2962ff;color:#ffffff;padding:5px 12px;border-radius:4px;'
                    f'font-size:12px;font-weight:600;text-decoration:none;">Ver</a></td>'
                    f'</tr>'
                )
            rows_html = "\n".join(rows)

            # Subcategory header row + table
            subcat_html_parts.append(
                # sub-header
                f'<tr><td colspan="5" style="background-color:#e8eaf6;padding:8px 14px;'
                f'font-weight:700;color:#283593;font-size:13px;border-bottom:1px solid #c5cae9;">'
                f'{sc_name} '
                f'<span style="font-weight:400;color:#5c6bc0;">({sc_count}){range_text}</span>'
                f'</td></tr>'
                + rows_html
            )

        all_rows_html = "\n".join(subcat_html_parts)

        range_badge = ""
        if price_range:
            range_badge = (
                f'<span style="float:right;font-size:13px;font-weight:400;'
                f'opacity:0.9;margin-top:2px;">{price_range}</span>'
            )

        sections.append(
            f'<div id="{anchor}" style="margin-bottom:32px;">'
            # section header
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border:none;">'
            f'<tr><td style="background-color:#0d47a1;padding:14px 20px;'
            f'border-radius:10px 10px 0 0;color:#ffffff;font-size:17px;font-weight:700;">'
            f'BAIC {model} '
            f'<span style="font-weight:400;font-size:13px;background-color:rgba(255,255,255,0.2);'
            f'padding:2px 10px;border-radius:12px;margin-left:6px;">{len(entries)}</span>'
            f'{range_badge}'
            f'</td></tr></table>'
            # data table with subcategory rows
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;border:1px solid #dde2ea;border-top:none;font-size:13px;">'
            f'<thead><tr style="background-color:#e8eaf6;">'
            f'<th style="{th_style}text-align:left;">Variante</th>'
            f'<th style="{th_style}text-align:left;">Vendedor</th>'
            f'<th style="{th_style}text-align:right;">Precio</th>'
            f'<th style="{th_style}text-align:left;">Ubicaci\u00f3n</th>'
            f'<th style="{th_style}text-align:center;">Link</th>'
            f'</tr></thead>'
            f'<tbody>{all_rows_html}</tbody>'
            f'</table>'
            f'</div>'
        )

    sections_html = "\n".join(sections)

    # --- price changes summary ---
    all_entries = [e for entries in grouped.values() for e in entries]
    n_up = sum(1 for e in all_entries if e.get("price_change") == "up")
    n_down = sum(1 for e in all_entries if e.get("price_change") == "down")
    n_new = sum(1 for e in all_entries if e.get("price_change") == "new")
    n_same = sum(1 for e in all_entries if e.get("price_change") == "same")

    changes_summary = ""
    if n_up or n_down or n_new:
        parts = []
        if n_up:
            parts.append(
                f'<span style="color:#c62828;font-weight:600;">\u2191 {n_up} subieron</span>'
            )
        if n_down:
            parts.append(
                f'<span style="color:#2e7d32;font-weight:600;">\u2193 {n_down} bajaron</span>'
            )
        if n_same:
            parts.append(f'<span style="color:#555;">= {n_same} sin cambio</span>')
        if n_new:
            parts.append(
                f'<span style="color:#ff6f00;font-weight:600;">{n_new} nuevas</span>'
            )
        changes_summary = (
            '<tr><td style="padding:0 30px 16px;text-align:center;">'
            '<div style="background-color:#f5f6fa;border-radius:8px;padding:10px 16px;'
            'font-size:13px;">'
            f'{" &bull; ".join(parts)}'
            '</div></td></tr>'
        )

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
        # PRICE CHANGES SUMMARY
        f'{changes_summary}'
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

    # Price change tracking
    previous_prices = load_previous_prices()
    compute_price_changes(items, previous_prices)

    n_up = sum(1 for i in items if i.get("price_change") == "up")
    n_down = sum(1 for i in items if i.get("price_change") == "down")
    n_new = sum(1 for i in items if i.get("price_change") == "new")
    n_same = sum(1 for i in items if i.get("price_change") == "same")
    print(f"Cambios de precio: {n_up} subieron, {n_down} bajaron, {n_same} sin cambio, {n_new} nuevas")

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

    save_current_prices(items)
    print("Listo.")


if __name__ == "__main__":
    main()
