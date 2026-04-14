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

import anthropic
import requests
from supabase import create_client

from config import (
    BRANDS,
    ITEMS_PER_PAGE,
    MAX_PAGES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    HEADERS,
    SCRAPERAPI_KEY,
    SCRAPERAPI_URL,
    SCRAPINGBEE_API_KEY,
    SCRAPINGBEE_URL,
    SCRAPINGANT_API_KEY,
    SCRAPINGANT_URL,
    ZENROWS_API_KEY,
    ZENROWS_URL,
    CRAWLBASE_TOKEN,
    CRAWLBASE_URL,
    APIFY_API_TOKEN,
    APIFY_ACTOR_ID,
    SMTP_SERVER,
    SMTP_PORT,
    EMAIL_SENDER,
    EMAIL_PASSWORD,
    EMAIL_RECIPIENTS,
    EMAIL_SUBJECT,
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    HISTORY_FILE,
    HISTORY_MAX_ENTRIES,
    REPORT_FILE,
    GITHUB_PAGES_URL,
    SUPABASE_URL,
    SUPABASE_KEY,
)


def load_previous_prices(prices_file):
    """Load previous run's prices from the given file.

    Returns {url: {"price": int, "currency": str}} or empty dict if file missing.
    """
    full_path = os.path.join(os.path.dirname(__file__), prices_file)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_current_prices(items, prices_file):
    """Save current listings' prices to the given file."""
    full_path = os.path.join(os.path.dirname(__file__), prices_file)
    prices = {}
    for item in items:
        prices[item["url"]] = {
            "price": item["price"],
            "currency": item["currency"],
        }
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)
    print(f"Precios guardados en {full_path} ({len(prices)} publicaciones)")


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


def build_page_url(page_number, base_url):
    """Build the URL for a given page number (1-based)."""
    if page_number <= 1:
        return base_url
    offset = (page_number - 1) * ITEMS_PER_PAGE + 1
    return f"{base_url}_Desde_{offset}"


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


def _scrapingbee_request(url):
    """Make a single ScrapingBee request. Returns response object.

    Uses wait (ms) instead of wait_for_selector — ScrapingBee's correct param.
    premium_proxy=true helps bypass MercadoLibre antibot.
    """
    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": url,
        "render_js": "true",
        "premium_proxy": "true",
        "country_code": "ar",
        "wait": "5000",  # wait 5s for JS to render
        "block_ads": "true",
    }
    return requests.get(SCRAPINGBEE_URL, params=params, timeout=120)


def _scrapingant_request(url):
    """Make a single ScrapingAnt request. Returns response object."""
    params = {
        "x-api-key": SCRAPINGANT_API_KEY,
        "url": url,
        "browser": "true",
        "wait": "5000",
        "proxy_country": "AR",
    }
    return requests.get(SCRAPINGANT_URL, params=params, timeout=120)


def _zenrows_request(url):
    """Make a single ZenRows request. Returns response object.

    js_render + premium_proxy handles JS-heavy sites and antibot.
    """
    params = {
        "apikey": ZENROWS_API_KEY,
        "url": url,
        "js_render": "true",
        "premium_proxy": "true",
        "proxy_country": "ar",
        "wait": "5000",
    }
    return requests.get(ZENROWS_URL, params=params, timeout=120)


def _crawlbase_request(url):
    """Make a single Crawlbase request. Returns response object.

    Uses JS token (not regular token) for JS-rendered pages.
    """
    params = {
        "token": CRAWLBASE_TOKEN,
        "url": url,
        "country": "AR",
        "wait": "5000",
    }
    return requests.get(CRAWLBASE_URL, params=params, timeout=120)


def fetch_page(url, retries=2):
    """Fetch a single page trying scrapers in priority order.

    Chain: ScrapingBee → ScrapingAnt → ZenRows → Crawlbase → ScraperAPI → direct
    Each is skipped if its API key is not configured.
    Falls back to Apify at a higher level if all return 0 listings.
    """
    scrapers = []
    if SCRAPINGBEE_API_KEY:
        scrapers.append(("ScrapingBee", lambda u: _scrapingbee_request(u)))
    if SCRAPINGANT_API_KEY:
        scrapers.append(("ScrapingAnt", lambda u: _scrapingant_request(u)))
    if ZENROWS_API_KEY:
        scrapers.append(("ZenRows", lambda u: _zenrows_request(u)))
    if CRAWLBASE_TOKEN:
        scrapers.append(("Crawlbase", lambda u: _crawlbase_request(u)))
    if SCRAPERAPI_KEY:
        scrapers.append(("ScraperAPI", lambda u: _scraperapi_request(u)))
    # Always include direct request as last resort
    scrapers.append((
        "direct",
        lambda u: requests.get(u, headers=HEADERS, timeout=REQUEST_TIMEOUT),
    ))

    last_exc = None
    for name, requester in scrapers:
        for attempt in range(1, retries + 1):
            try:
                response = requester(url)
                response.raise_for_status()
                print(f"  [{name}] OK")
                return response.text
            except requests.RequestException as e:
                last_exc = e
                if attempt < retries:
                    wait = attempt * 5
                    print(f"  [{name}] intento {attempt} fallo: {e} — reintentando en {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  [{name}] fallo definitivo: {e} — probando siguiente scraper...")

    raise last_exc


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

        # Extract seller name — try multiple class patterns, fall back to "Particular"
        seller_match = re.search(
            r'class="poly-component__seller[^"]*"[^>]*>([^<]+)', card
        ) or re.search(
            r'class="[^"]*seller[^"]*"[^>]*>([^<]+)', card
        ) or re.search(
            r'data-testid="[^"]*seller[^"]*"[^>]*>([^<]+)', card
        )
        seller = seller_match.group(1).strip() if seller_match else "Particular"

        # Extract location — try multiple class patterns
        location_match = re.search(
            r'class="poly-component__location[^"]*"[^>]*>([^<]+)', card
        ) or re.search(
            r'class="[^"]*location[^"]*"[^>]*>([^<]+)', card
        ) or re.search(
            r'class="[^"]*ciudad[^"]*"[^>]*>([^<]+)', card
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


def _apify_convert_item(item):
    """Convert a single Apify result to our internal listing format."""
    price_str = item.get("nuevoPrecio", "0") or "0"
    try:
        price = int(str(price_str).replace(".", ""))
    except ValueError:
        price = 0

    moneda = item.get("Moneda", "ARS $") or "ARS $"
    currency = "U$S" if "US" in moneda.upper() else "$"

    # Try multiple field names for seller; fall back to "Particular"
    seller = (
        item.get("Vendedor")
        or item.get("vendedor")
        or item.get("nombreVendedor")
        or item.get("tienda")
        or item.get("seller")
        or "Particular"
    )

    # Try multiple field names for location
    location = (
        item.get("ubicacion")
        or item.get("Ubicacion")
        or item.get("ciudad")
        or item.get("Ciudad")
        or item.get("provincia")
        or item.get("Provincia")
        or ""
    )

    return {
        "title": item.get("articuloTitulo", ""),
        "seller": seller.strip(),
        "price": price,
        "anticipo": 0,
        "currency": currency,
        "location": location.strip(),
        "url": item.get("zdireccion", ""),
    }


def fetch_via_apify(apify_keywords):
    """Fallback: fetch listings using the Apify MercadoLibre actor.

    Runs one search per keyword to maximize coverage on the free plan
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

    for keyword in apify_keywords:
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


def fetch_all_listings(brand_name, base_url, apify_keywords, min_listings_threshold=200):
    """Fetch all listings for a brand across all pages.

    Tries ScraperAPI/direct first. If that returns too few results, falls back to Apify.
    """
    all_listings = []

    if SCRAPERAPI_KEY:
        print(f"[{brand_name}] Usando ScraperAPI para las solicitudes")
    else:
        print(f"[{brand_name}] ScraperAPI no configurado, usando solicitudes directas")

    for page in range(1, MAX_PAGES + 1):
        url = build_page_url(page, base_url)
        print(f"[{brand_name}] Pagina {page}: {url}")

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

    print(f"[{brand_name}] Total publicaciones (scraping directo): {len(all_listings)}")

    # Supplement with Apify if scraping returned fewer than expected
    if len(all_listings) < min_listings_threshold:
        print(f"[{brand_name}] Pocas publicaciones ({len(all_listings)}), complementando con Apify...")
        apify_listings = fetch_via_apify(apify_keywords)

        # Merge: deduplicate by URL
        seen_urls = {item["url"] for item in all_listings if item["url"]}
        new_count = 0
        for item in apify_listings:
            if item["url"] and item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_listings.append(item)
                new_count += 1

        print(f"[{brand_name}] Apify agrego {new_count} publicaciones nuevas")

    print(f"[{brand_name}] Total publicaciones finales: {len(all_listings)}")
    return all_listings


def extract_base_model(title, known_models, brand_name):
    """Extract the base model name from a listing title.

    Uses a three-step strategy:
    1. Search for a known model as a distinct token in the title.
    2. Fall back to regex extraction + fuzzy match against known models.
    3. Default to "Otros".
    """
    # Normalise: strip non-alphanumeric (except spaces) and uppercase
    clean = re.sub(r'[^A-Z0-9\s]', '', title.upper())

    # Step 1 — look for known models (longest first to prevent short names matching inside longer ones)
    models_by_len = sorted(known_models, key=len, reverse=True)
    for model in models_by_len:
        model_clean = re.sub(r'[^A-Z0-9\s]', '', model.upper())
        if re.search(rf'(?<![A-Z]){re.escape(model_clean)}(?![A-Z0-9])', clean):
            return model

    # Step 2 — regex fallback: look for brand prefix followed by a word/number
    match = re.search(rf'{re.escape(brand_name.upper())}\s+(\w+(?:\s+\d+)?)', clean)
    if match:
        candidate = match.group(1).strip()
        known_clean = [re.sub(r'[^A-Z0-9\s]', '', m.upper()) for m in known_models]
        close = get_close_matches(candidate, known_clean, n=1, cutoff=0.6)
        if close:
            idx = known_clean.index(close[0])
            return known_models[idx]
        return candidate

    return "Otros"


def _merge_similar_groups(grouped, known_models, cutoff=0.7):
    """Merge unknown group names into the closest known model group.

    Known models are never merged with each other — only truly unknown names
    (from regex fallback) get absorbed if they're similar enough.
    """
    result = {}
    orphans = {}

    for name, items in grouped.items():
        if name in known_models:
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


def _strip_model_prefix(title, model, brand_name):
    """Remove 'Brand MODEL' prefix and redundant model name from title."""
    cleaned = re.sub(
        rf'^{re.escape(brand_name)}\s+{re.escape(model)}e?\s*',
        '', title, flags=re.IGNORECASE
    ).strip()
    cleaned = re.sub(rf'\b{re.escape(model)}e?\b', '', cleaned, flags=re.IGNORECASE).strip()
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
    (r'\b(?:Phev|Plugin|Plug.in)\b', 'PHEV'),
    (r'\b(?:Electric[oa]?|100%\s*Electrico)\b', 'Electrico'),
    (r'\b(?:Hev|Mhev|Milhybrid|Hybrid|H[ií]brida?)\b', 'Hybrid'),
]


def extract_subcategory(variant):
    """Extract a subcategory keyword from a variant name."""
    for pattern, label in _SUBCAT_PATTERNS:
        if re.search(pattern, variant, re.IGNORECASE):
            return label
    return "Otros"


def process_listings(items, known_models, brand_name):
    """Group listings by base model, merge similar groups, sort by price.

    Each item gets a 'subcategory' field added for sub-grouping.
    """
    grouped = {}

    for item in items:
        title = item.get("title", "Sin titulo") or "Sin titulo"
        model = extract_base_model(title, known_models, brand_name)
        variant = _strip_model_prefix(title, model, brand_name)
        item["subcategory"] = extract_subcategory(variant)
        grouped.setdefault(model, []).append(item)

    return _merge_similar_groups(grouped, known_models)


def _format_price(entry):
    """Format a listing's price as a display string."""
    if entry["currency"] in ("U$S", "US$"):
        return f"USD {entry['price']:,}".replace(",", ".")
    return f"${entry['price']:,}".replace(",", ".")


def _price_range_str(entries):
    """Return a 'min - max' price range string for a group of entries."""
    prices = [e for e in entries if e["price"] > 0]
    if not prices:
        return ""
    lo = min(prices, key=lambda x: x["price"])
    hi = max(prices, key=lambda x: x["price"])
    return f"{_format_price(lo)} \u2014 {_format_price(hi)}"


def format_plain_text(results_by_brand, summaries_by_brand=None):
    """Format grouped listings into a plain-text body for console output."""
    if not results_by_brand or not any(results_by_brand.values()):
        return "No se encontraron publicaciones en Mercado Libre."

    summaries_by_brand = summaries_by_brand or {}
    lines = []
    lines.append("=" * 60)
    lines.append("Reporte de Precios - Mercado Libre Argentina")
    lines.append(f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    lines.append("=" * 60)

    total_listings = 0

    for brand_name, grouped in results_by_brand.items():
        if not grouped:
            lines.append(f"\n  {brand_name}: sin publicaciones encontradas\n")
            continue

        lines.append("")
        lines.append(f"{'#' * 60}")
        lines.append(f"  {brand_name.upper()}")
        lines.append(f"{'#' * 60}")

        summary = summaries_by_brand.get(brand_name, "")
        if summary:
            lines.append(f"\nRESUMEN EJECUTIVO:\n{summary}\n")

        for model, entries in grouped.items():
            lines.append(f"\nModelo: {model} ({len(entries)} publicaciones)")
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
                lines.append(
                    f"  {entry['title']} | {entry['seller']}: "
                    f"{price_str}{change_str}{anticipo_str}{loc}"
                )
                total_listings += 1

    lines.append("")
    lines.append("=" * 60)
    lines.append(f"Total: {total_listings} publicaciones")
    lines.append("=" * 60)

    return "\n".join(lines)


def _build_brand_html_section(brand_name, grouped, brand_config, summary=""):
    """Build the HTML block for one brand (summary cards + model detail tables)."""
    if not grouped:
        return (
            f'<div style="margin-bottom:24px;padding:16px 24px;'
            f'background-color:#f5f5f5;border-radius:8px;color:#999;text-align:center;">'
            f'Sin publicaciones encontradas para {brand_name}.'
            f'</div>'
        )

    header_color = brand_config.get("header_color", "#0d47a1")
    card_color = brand_config.get("card_color", "#1a237e")
    total = sum(len(v) for v in grouped.values())
    price_range = _price_range_str([e for entries in grouped.values() for e in entries])

    # Summary cards (one per model)
    summary_cards = []
    for model, entries in grouped.items():
        anchor = f"{brand_name.lower()}-{re.sub(r'[^a-z0-9]', '-', model.lower())}"
        summary_cards.append(
            f'<td style="padding:0 6px 12px 6px;text-align:center;">'
            f'<a href="#{anchor}" style="text-decoration:none;color:#ffffff;">'
            f'<div style="background-color:{card_color};border-radius:8px;'
            f'padding:14px 18px;min-width:90px;">'
            f'<div style="font-size:18px;font-weight:700;letter-spacing:0.5px;">{model}</div>'
            f'<div style="font-size:11px;margin-top:4px;opacity:0.85;">{len(entries)} pub.</div>'
            f'</div></a></td>'
        )
    summary_cards_html = "".join(summary_cards)

    th_style = (
        'padding:10px 14px;font-weight:600;color:#37474f;'
        'border-bottom:2px solid #c5cae9;font-size:12px;'
        'text-transform:uppercase;letter-spacing:0.5px;'
    )

    # Model detail sections
    sections = []
    for model, entries in grouped.items():
        anchor = f"{brand_name.lower()}-{re.sub(r'[^a-z0-9]', '-', model.lower())}"
        model_range = _price_range_str(entries)

        # Group by subcategory
        subcats = {}
        for entry in entries:
            sc = entry.get("subcategory", "Otros")
            subcats.setdefault(sc, []).append(entry)

        subcat_html_parts = []
        for sc_name, sc_entries in subcats.items():
            sc_range = _price_range_str(sc_entries)
            range_text = f' &mdash; {sc_range}' if sc_range else ''

            rows = []
            for i, entry in enumerate(sc_entries):
                bg = "#f4f6fb" if i % 2 == 0 else "#ffffff"
                price_str = _format_price(entry)
                variant = _strip_model_prefix(entry["title"], model, brand_name)
                seller = entry["seller"] if entry["seller"] else "Particular"
                location = entry["location"] if entry["location"] else '<span style="color:#999;">Sin datos</span>'

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

                anticipo_html = ""
                if entry.get("anticipo"):
                    ant_fmt = f"{entry['anticipo']:,}".replace(",", ".")
                    anticipo_html = (
                        f'<br><span style="color:#e65100;font-size:11px;font-weight:600;">'
                        f'Anticipo: ${ant_fmt}</span>'
                    )

                nueva_badge = ""
                if change == "new":
                    nueva_badge = (
                        ' <span style="display:inline-block;background-color:#ff6f00;'
                        'color:#fff;font-size:9px;font-weight:700;padding:1px 5px;'
                        'border-radius:3px;vertical-align:middle;margin-left:4px;">NUEVA</span>'
                    )

                rows.append(
                    f'<tr style="background-color:{bg};">'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;">'
                    f'{variant}{nueva_badge}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;color:#444;">'
                    f'{seller}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;text-align:right;'
                    f'font-weight:700;color:#1b5e20;white-space:nowrap;">'
                    f'{price_str}{change_html}{anticipo_html}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;color:#555;">'
                    f'{location}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f4;text-align:center;">'
                    f'<a href="{entry["url"]}" target="_blank" style="display:inline-block;'
                    f'background-color:{header_color};color:#ffffff;padding:5px 12px;'
                    f'border-radius:4px;font-size:12px;font-weight:600;text-decoration:none;">'
                    f'Ver</a></td>'
                    f'</tr>'
                )

            subcat_html_parts.append(
                f'<tr><td colspan="5" style="background-color:#e8eaf6;padding:8px 14px;'
                f'font-weight:700;color:#283593;font-size:13px;border-bottom:1px solid #c5cae9;">'
                f'{sc_name} '
                f'<span style="font-weight:400;color:#5c6bc0;">({len(sc_entries)}){range_text}</span>'
                f'</td></tr>'
                + "\n".join(rows)
            )

        range_badge = ""
        if model_range:
            range_badge = (
                f'<span style="float:right;font-size:13px;font-weight:400;'
                f'opacity:0.9;margin-top:2px;">{model_range}</span>'
            )

        sections.append(
            f'<div id="{anchor}" style="margin-bottom:24px;">'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border:none;">'
            f'<tr><td style="background-color:{header_color};padding:12px 20px;'
            f'border-radius:10px 10px 0 0;color:#ffffff;font-size:16px;font-weight:700;">'
            f'{brand_name} {model} '
            f'<span style="font-weight:400;font-size:13px;background-color:rgba(255,255,255,0.2);'
            f'padding:2px 10px;border-radius:12px;margin-left:6px;">{len(entries)}</span>'
            f'{range_badge}'
            f'</td></tr></table>'
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;border:1px solid #dde2ea;border-top:none;font-size:13px;">'
            f'<thead><tr style="background-color:#e8eaf6;">'
            f'<th style="{th_style}text-align:left;">Variante</th>'
            f'<th style="{th_style}text-align:left;">Vendedor</th>'
            f'<th style="{th_style}text-align:right;">Precio</th>'
            f'<th style="{th_style}text-align:left;">Ubicaci\u00f3n</th>'
            f'<th style="{th_style}text-align:center;">Link</th>'
            f'</tr></thead>'
            f'<tbody>{"".join(subcat_html_parts)}</tbody>'
            f'</table>'
            f'</div>'
        )

    range_badge_total = (
        f'<span style="float:right;font-size:12px;font-weight:400;opacity:0.85;margin-top:3px;">'
        f'{price_range}</span>'
        if price_range else ''
    )

    summary_html = ""
    if summary:
        summary_html = (
            f'<div style="background-color:#fffde7;border-left:4px solid {header_color};'
            f'padding:14px 18px;margin-bottom:20px;border-radius:0 6px 6px 0;'
            f'font-size:13px;line-height:1.7;color:#333;font-style:italic;">'
            f'{summary}'
            f'</div>'
        )

    return (
        # Brand header bar
        f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">'
        f'<tr><td style="background-color:{header_color};padding:18px 24px;'
        f'border-radius:10px;color:#ffffff;">'
        f'<span style="font-size:22px;font-weight:700;letter-spacing:0.5px;">{brand_name}</span>'
        f'<span style="font-size:13px;margin-left:12px;opacity:0.85;">'
        f'{total} publicaciones &bull; {len(grouped)} modelos</span>'
        f'{range_badge_total}'
        f'</td></tr></table>'
        # Executive summary
        + summary_html
        # Summary cards
        + f'<table cellpadding="0" cellspacing="0" style="margin-bottom:20px;">'
        f'<tr>{summary_cards_html}</tr>'
        f'</table>'
        # Model sections
        + "".join(sections)
    )


def format_html_email(results_by_brand, summaries_by_brand=None):
    """Short briefing email with KPIs per brand and a CTA to the full interactive report."""
    summaries_by_brand = summaries_by_brand or {}
    date_str = datetime.now().strftime('%d/%m/%Y %H:%M')

    def kpi_td(label, value, color="#1a1a2e"):
        return (
            f'<td style="padding:10px 16px;text-align:center;border-right:1px solid #e2e8f0;">'
            f'<div style="font-size:10px;text-transform:uppercase;letter-spacing:.8px;'
            f'color:#94a3b8;font-weight:600;margin-bottom:4px;">{label}</div>'
            f'<div style="font-size:20px;font-weight:700;color:{color};">{value}</div>'
            f'</td>'
        )

    brand_blocks = []
    for brand_name, grouped in results_by_brand.items():
        brand_config = BRANDS.get(brand_name, {})
        color = brand_config.get("header_color", "#0d47a1")
        all_items = [e for entries in grouped.values() for e in entries]
        stats = _compute_brand_stats(brand_name, grouped, all_items)
        models = stats.get("models", {})

        total = stats.get("total", 0)
        avgs = [m["avg"] for m in models.values() if m.get("avg")]
        avg = f'${round(sum(avgs)/len(avgs)):,}'.replace(",", ".") if avgs else "—"
        n_up   = sum(m.get("n_up", 0)   for m in models.values())
        n_down = sum(m.get("n_down", 0) for m in models.values())
        n_new  = sum(m.get("n_new", 0)  for m in models.values())

        summary = summaries_by_brand.get(brand_name, "")
        summary_html = (
            f'<tr><td style="padding:0 24px 18px;">'
            f'<div style="background:#fffde7;border-left:4px solid {color};'
            f'padding:12px 16px;border-radius:0 6px 6px 0;font-size:13px;'
            f'line-height:1.7;color:#374151;font-style:italic;">{summary}</div>'
            f'</td></tr>'
        ) if summary else ""

        brand_blocks.append(
            f'<tr><td style="padding:20px 24px 4px;">'
            f'<div style="background:{color};border-radius:10px 10px 0 0;'
            f'padding:14px 20px;color:#fff;">'
            f'<span style="font-size:18px;font-weight:700;">{brand_name}</span>'
            f'<span style="font-size:12px;opacity:.8;margin-left:10px;">'
            f'{total} publicaciones &bull; {len(models)} modelos</span>'
            f'</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 10px 10px;'
            f'border-collapse:collapse;">'
            f'<tr>'
            + kpi_td("Total", total)
            + kpi_td("Precio Prom.", avg)
            + kpi_td("↑ Subieron", n_up, "#ef4444")
            + kpi_td("↓ Bajaron",  n_down, "#22c55e")
            + kpi_td("★ Nuevas",   n_new, "#f97316")
            + f'</tr></table></td></tr>'
            + summary_html
        )

    brands_html = "".join(brand_blocks)

    cta = (
        f'<tr><td style="padding:24px 24px 28px;text-align:center;">'
        f'<a href="{GITHUB_PAGES_URL}" target="_blank" '
        f'style="display:inline-block;background:#1a1a2e;color:#fff;'
        f'padding:14px 36px;border-radius:8px;font-size:15px;font-weight:700;'
        f'text-decoration:none;letter-spacing:.3px;">'
        f'Ver reporte interactivo completo &rarr;</a>'
        f'<p style="margin:10px 0 0;font-size:11px;color:#94a3b8;">'
        f'Con filtros, ordenamiento, gr&aacute;ficos de tendencia y m&aacute;s</p>'
        f'</td></tr>'
    )

    return (
        '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>'
        '<body style="margin:0;padding:0;background:#eef1f7;'
        'font-family:\'Segoe UI\',Arial,Helvetica,sans-serif;">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f7;padding:24px 0;">'
        '<tr><td align="center">'
        '<table width="620" cellpadding="0" cellspacing="0" '
        'style="background:#fff;border-radius:12px;overflow:hidden;'
        'box-shadow:0 2px 12px rgba(0,0,0,.08);">'
        # Header
        '<tr><td style="background:#1a1a2e;padding:26px 28px;text-align:center;">'
        '<h1 style="margin:0;font-size:22px;color:#fff;font-weight:700;">'
        'Reporte ML Argentina</h1>'
        f'<p style="margin:6px 0 0;font-size:12px;color:rgba(255,255,255,.65);">'
        f'Generado el {date_str}</p>'
        '</td></tr>'
        # Brand KPI blocks
        f'{brands_html}'
        # CTA
        f'{cta}'
        # Footer
        '<tr><td style="background:#f8fafc;padding:16px 24px;text-align:center;'
        'border-top:1px solid #e2e8f0;">'
        '<p style="margin:0;font-size:11px;color:#94a3b8;">'
        'Datos obtenidos de Mercado Libre Argentina &bull; Precios sujetos a cambio</p>'
        '</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def load_history():
    """Load the accumulated run history from data/history.json."""
    full_path = os.path.join(os.path.dirname(__file__), HISTORY_FILE)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_history(history):
    """Persist history, keeping only the most recent HISTORY_MAX_ENTRIES entries."""
    full_path = os.path.join(os.path.dirname(__file__), HISTORY_FILE)
    # Keep most recent entries only
    if len(history) > HISTORY_MAX_ENTRIES:
        keys = sorted(history.keys())[-HISTORY_MAX_ENTRIES:]
        history = {k: history[k] for k in keys}
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"Historial guardado en {full_path} ({len(history)} entradas)")


def _compute_brand_stats(brand_name, grouped, items):
    """Compute per-model and total stats for one brand run."""
    models = {}
    for model, entries in grouped.items():
        prices = [e["price"] for e in entries if e.get("price", 0) > 0]
        if not prices:
            continue
        models[model] = {
            "avg": round(sum(prices) / len(prices)),
            "min": min(prices),
            "max": max(prices),
            "count": len(entries),
            "n_up": sum(1 for e in entries if e.get("price_change") == "up"),
            "n_down": sum(1 for e in entries if e.get("price_change") == "down"),
            "n_new": sum(1 for e in entries if e.get("price_change") == "new"),
        }
    return {"total": len(items), "models": models}


def update_history(history, date_str, brand_name, stats):
    """Add today's stats for a brand into the history dict."""
    if date_str not in history:
        history[date_str] = {}
    history[date_str][brand_name] = stats
    return history


def generate_brand_summary(brand_name, grouped, items, history):
    """Call Claude Haiku to generate a one-paragraph executive summary for a brand.

    Falls back to a plain-text summary if the API key is not set or the call fails.
    """
    if not ANTHROPIC_API_KEY:
        return ""

    # Current stats
    stats = _compute_brand_stats(brand_name, grouped, items)

    # Format current data for the prompt
    def fmt_price(p):
        return f"${p:,.0f}".replace(",", ".")

    model_lines = []
    for model, s in stats["models"].items():
        model_lines.append(
            f"  - {model}: {s['count']} pub., "
            f"promedio {fmt_price(s['avg'])}, "
            f"rango {fmt_price(s['min'])}–{fmt_price(s['max'])}, "
            f"{s['n_up']} subieron / {s['n_down']} bajaron / {s['n_new']} nuevas"
        )
    current_block = f"Total: {stats['total']} publicaciones\n" + "\n".join(model_lines)

    # Format recent history (last 4 runs, excluding today)
    today = datetime.now().strftime("%Y-%m-%d")
    past_runs = sorted(
        [(d, v[brand_name]) for d, v in history.items()
         if d != today and brand_name in v],
        key=lambda x: x[0],
    )[-4:]

    history_block = ""
    if past_runs:
        history_lines = []
        for date, h_stats in past_runs:
            h_models = []
            for model, s in h_stats.get("models", {}).items():
                h_models.append(f"{model}: promedio {fmt_price(s['avg'])} ({s['count']} pub.)")
            history_lines.append(f"  {date}: " + ", ".join(h_models))
        history_block = "\nHISTORIAL RECIENTE (últimas corridas):\n" + "\n".join(history_lines)
    else:
        history_block = "\n(Primera corrida — sin historial previo)"

    prompt = (
        f"Sos un analista de mercado automotor argentino. "
        f"Con base en los siguientes datos del mercado de autos {brand_name} "
        f"en Mercado Libre Argentina, escribí UN párrafo ejecutivo conciso "
        f"(3-5 oraciones) en español. "
        f"Incluí: volumen de publicaciones, tendencia de precios respecto al historial, "
        f"modelos más activos o con mayor variación, y cualquier dato llamativo. "
        f"No uses bullet points ni título. Solo el párrafo.\n\n"
        f"DATOS ACTUALES ({today}):\n{current_block}"
        f"{history_block}"
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = message.content[0].text.strip()
        print(f"[{brand_name}] Resumen ejecutivo generado ({len(summary.split())} palabras)")
        return summary
    except Exception as e:
        print(f"[{brand_name}] Error al generar resumen con Claude: {e}")
        return ""


_REPORT_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reporte ML Argentina</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--brand:#0d47a1}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#f0f2f5;color:#1a1a2e;min-height:100vh}
/* --- Nav --- */
.topbar{position:sticky;top:0;z-index:100;background:#fff;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;gap:12px;padding:0 24px;height:56px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.topbar-logo{font-weight:700;font-size:15px;color:#1a1a2e;letter-spacing:-.3px;white-space:nowrap}
.topbar-date{font-size:11px;color:#94a3b8;margin-left:auto;white-space:nowrap}
.brand-tabs{display:flex;gap:4px}
.brand-tab{border:none;background:#f1f5f9;padding:6px 18px;border-radius:20px;font-size:13px;font-weight:600;cursor:pointer;color:#64748b;transition:all .15s}
.brand-tab.active{background:var(--brand);color:#fff}
/* --- Layout --- */
.main{max-width:1200px;margin:0 auto;padding:20px 16px}
/* --- KPI cards --- */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.kpi-card{background:#fff;border-radius:12px;padding:16px 20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#94a3b8;font-weight:600;margin-bottom:6px}
.kpi-value{font-size:24px;font-weight:700;color:#1a1a2e;letter-spacing:-.5px;line-height:1}
.kpi-sub{font-size:11px;color:#94a3b8;margin-top:5px}
.kpi-up{color:#ef4444}.kpi-down{color:#22c55e}.kpi-new{color:#f97316}
/* --- Summary --- */
.summary-card{background:#fff;border-radius:12px;padding:18px 22px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.06);border-left:4px solid var(--brand)}
.card-label{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:#94a3b8;font-weight:600;margin-bottom:10px}
.summary-text{font-size:13.5px;line-height:1.8;color:#374151;font-style:italic}
/* --- Chart --- */
.chart-card{background:#fff;border-radius:12px;padding:18px 22px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.chart-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;gap:12px;flex-wrap:wrap}
.chart-title{font-size:13px;font-weight:600;color:#374151}
.chart-model-select{border:1px solid #e2e8f0;border-radius:8px;padding:5px 12px;font-size:12px;color:#374151;background:#fff;cursor:pointer;outline:none}
.chart-wrap{position:relative;height:200px}
/* --- Controls --- */
.controls{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.search-input{flex:1;min-width:200px;border:1px solid #e2e8f0;border-radius:8px;padding:8px 14px;font-size:13px;color:#374151;outline:none;transition:border .15s}
.search-input:focus{border-color:var(--brand)}
.ctrl-select{border:1px solid #e2e8f0;border-radius:8px;padding:8px 14px;font-size:13px;color:#374151;background:#fff;cursor:pointer;outline:none}
/* --- Table --- */
.table-card{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:20px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{background:#f8fafc;padding:10px 14px;text-align:left;font-weight:600;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #e2e8f0;cursor:pointer;user-select:none;white-space:nowrap}
thead th:hover{background:#f1f5f9}
thead th.sort-asc::after{content:' ↑'}
thead th.sort-desc::after{content:' ↓'}
tbody tr:hover{background:#f8fafc}
tbody td{padding:10px 14px;border-bottom:1px solid #f1f5f9;vertical-align:middle}
.badge-model{display:inline-block;background:#e0e7ff;color:#3730a3;font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;white-space:nowrap}
.badge-new{display:inline-block;background:#f97316;color:#fff;font-size:9px;font-weight:700;padding:2px 5px;border-radius:4px;margin-left:5px;vertical-align:middle}
.price-cell{font-weight:700;color:#15803d;white-space:nowrap}
.price-delta{font-size:11px;font-weight:600;display:block}
.price-delta.up{color:#ef4444}.price-delta.down{color:#22c55e}
.btn-ver{display:inline-block;background:var(--brand);color:#fff;padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none;white-space:nowrap;transition:opacity .15s}
.btn-ver:hover{opacity:.8}
.row-count{font-size:12px;color:#94a3b8;padding:10px 14px 4px;text-align:right}
/* --- Pagination --- */
.pagination{display:flex;align-items:center;justify-content:center;gap:6px;padding:14px}
.page-btn{border:1px solid #e2e8f0;background:#fff;border-radius:8px;padding:5px 11px;font-size:13px;cursor:pointer;color:#374151;transition:all .15s}
.page-btn:hover:not(:disabled){border-color:var(--brand);color:var(--brand)}
.page-btn.active{background:var(--brand);color:#fff;border-color:var(--brand)}
.page-btn:disabled{opacity:.35;cursor:not-allowed}
/* --- Footer --- */
.footer{text-align:center;padding:24px;font-size:11px;color:#94a3b8}
</style>
</head>
<body>

<nav class="topbar">
  <span class="topbar-logo">📊 Precios ML Argentina</span>
  <div class="brand-tabs" id="brandTabs"></div>
  <span class="topbar-date">Actualizado: __DATE__</span>
</nav>
<main class="main" id="app"></main>
<footer class="footer">Datos obtenidos de Mercado Libre Argentina &bull; Precios sujetos a cambio</footer>

<script>
const DATA = __BRANDS_JSON__;
const HISTORY = __HIST_JSON__;
const PAGE_SIZE = 50;

let state = {
  brand: Object.keys(DATA)[0],
  search: '',
  model: '',
  sort: 'price',
  sortDir: 'asc',
  page: 1,
  chart: null,
};

/* --- Helpers --- */
function fmtPrice(item) {
  if (!item.price) return '—';
  const n = item.price.toLocaleString('es-AR');
  return (item.currency === 'U$S' || item.currency === 'US$') ? 'USD ' + n : '$' + n;
}
function fmtNum(n) { return n ? n.toLocaleString('es-AR') : '0'; }

/* --- Tabs --- */
function renderTabs() {
  document.getElementById('brandTabs').innerHTML = Object.keys(DATA).map(b => {
    const active = b === state.brand;
    const style = active ? 'style="background:' + DATA[b].color + ';color:#fff"' : '';
    return '<button class="brand-tab ' + (active?'active':'') + '" ' + style + ' onclick="switchBrand(\'' + b + '\')">' + b + '</button>';
  }).join('');
}

function switchBrand(b) {
  state = { ...state, brand: b, model: '', search: '', page: 1, chart: null };
  document.documentElement.style.setProperty('--brand', DATA[b].color);
  renderTabs();
  renderApp();
}

/* --- KPIs --- */
function renderKPIs(brand) {
  const models = brand.stats.models || {};
  const avgs = Object.values(models).map(m => m.avg).filter(Boolean);
  const globalAvg = avgs.length ? Math.round(avgs.reduce((a,b)=>a+b,0)/avgs.length) : 0;
  const nUp   = Object.values(models).reduce((s,m) => s+(m.n_up||0), 0);
  const nDown = Object.values(models).reduce((s,m) => s+(m.n_down||0), 0);
  const nNew  = Object.values(models).reduce((s,m) => s+(m.n_new||0), 0);
  const nModels = Object.keys(models).length;
  return '<div class="kpi-grid">' + [
    ['Publicaciones', fmtNum(brand.stats.total), nModels + ' modelos', ''],
    ['Precio Promedio', globalAvg ? '$'+fmtNum(globalAvg) : '—', 'entre todos los modelos', ''],
    ['Subieron', '↑ '+nUp, 'vs. corrida anterior', 'kpi-up'],
    ['Bajaron',  '↓ '+nDown, 'vs. corrida anterior', 'kpi-down'],
    ['Nuevas',   nNew, 'publicaciones nuevas', 'kpi-new'],
  ].map(([label, val, sub, cls]) =>
    '<div class="kpi-card"><div class="kpi-label">'+label+'</div>' +
    '<div class="kpi-value '+cls+'">'+val+'</div>' +
    '<div class="kpi-sub">'+sub+'</div></div>'
  ).join('') + '</div>';
}

/* --- Summary --- */
function renderSummary(brand) {
  if (!brand.summary) return '';
  return '<div class="summary-card"><div class="card-label">🤖 Resumen Ejecutivo</div>' +
    '<div class="summary-text">' + brand.summary + '</div></div>';
}

/* --- Chart --- */
function renderChart(brandName) {
  const hist = HISTORY[brandName] || {};
  const models = Object.keys(hist);
  if (!models.length) return '';
  const opts = models.map(m => '<option value="'+m+'">'+m+'</option>').join('');
  return '<div class="chart-card"><div class="chart-header">' +
    '<span class="chart-title">Evolución de Precio Promedio</span>' +
    '<select class="chart-model-select" id="chartModelSel" onchange="updateChart()">' + opts + '</select>' +
    '</div><div class="chart-wrap"><canvas id="trendChart"></canvas></div></div>';
}

function updateChart() {
  const hist = HISTORY[state.brand] || {};
  const sel = document.getElementById('chartModelSel');
  if (!sel) return;
  const data = hist[sel.value] || [];
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  const ctx = document.getElementById('trendChart');
  if (!ctx || !data.length) return;
  const color = DATA[state.brand].color;
  state.chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.date),
      datasets: [{
        label: sel.value,
        data: data.map(d => d.avg),
        borderColor: color,
        backgroundColor: color + '18',
        borderWidth: 2.5,
        pointRadius: 4,
        pointBackgroundColor: color,
        fill: true,
        tension: 0.35,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: ctx => '$' + ctx.parsed.y.toLocaleString('es-AR') }
      }},
      scales: {
        y: { ticks: { callback: v => '$' + (v/1e6).toFixed(1)+'M', font:{size:11} }, grid:{color:'#f1f5f9'} },
        x: { ticks: { font:{size:11} }, grid:{display:false} }
      }
    }
  });
}

/* --- Table --- */
function getFilteredItems() {
  let items = (DATA[state.brand].items || []).slice();
  if (state.model) items = items.filter(i => i.model === state.model);
  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter(i =>
      i.variant.toLowerCase().includes(q) ||
      i.seller.toLowerCase().includes(q) ||
      i.location.toLowerCase().includes(q)
    );
  }
  items.sort((a, b) => {
    let va = a[state.sort], vb = b[state.sort];
    if (typeof va === 'string') { va = va.toLowerCase(); vb = vb.toLowerCase(); }
    if (va < vb) return state.sortDir === 'asc' ? -1 : 1;
    if (va > vb) return state.sortDir === 'asc' ? 1 : -1;
    return 0;
  });
  return items;
}

function sortBy(field) {
  if (state.sort === field) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
  else { state.sort = field; state.sortDir = 'asc'; }
  state.page = 1;
  renderTable();
}

function renderTableSection() {
  const allModels = [...new Set((DATA[state.brand].items||[]).map(i=>i.model))].sort();
  const modelOpts = '<option value="">Todos los modelos</option>' +
    allModels.map(m => '<option value="'+m+'"'+(m===state.model?' selected':'')+'>'+m+'</option>').join('');
  return '<div class="controls">' +
    '<input class="search-input" type="text" placeholder="🔍 Buscar variante, vendedor, ubicación..." ' +
    'value="'+state.search.replace(/"/g,'&quot;')+'" oninput="onSearch(this.value)">' +
    '<select class="ctrl-select" onchange="onModel(this.value)">'+modelOpts+'</select>' +
    '</div>' +
    '<div class="table-card"><div class="table-wrap" id="tableWrap"></div>' +
    '<div class="pagination" id="pagination"></div></div>';
}

function renderTable() {
  const items = getFilteredItems();
  const totalPages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  if (state.page > totalPages) state.page = totalPages;
  const page = items.slice((state.page-1)*PAGE_SIZE, state.page*PAGE_SIZE);

  function th(field, label) {
    const cls = state.sort===field ? 'sort-'+state.sortDir : '';
    return '<th class="'+cls+'" onclick="sortBy(\''+field+'\')">'+label+'</th>';
  }

  const rows = page.map(item => {
    let delta = '';
    if (item.price_change === 'up' && item.price_diff)
      delta = '<span class="price-delta up">↑ +'+fmtNum(item.price_diff)+'</span>';
    else if (item.price_change === 'down' && item.price_diff)
      delta = '<span class="price-delta down">↓ -'+fmtNum(item.price_diff)+'</span>';
    const newBadge = item.price_change === 'new' ? '<span class="badge-new">NUEVA</span>' : '';
    const seller = item.seller || 'Particular';
    const loc    = item.location || '<span style="color:#94a3b8;font-size:11px;">Sin datos</span>';
    return '<tr>' +
      '<td><span class="badge-model">'+item.model+'</span></td>' +
      '<td>'+item.variant+newBadge+'</td>' +
      '<td>'+seller+'</td>' +
      '<td class="price-cell">'+fmtPrice(item)+delta+'</td>' +
      '<td style="color:#64748b">'+loc+'</td>' +
      '<td><a class="btn-ver" href="'+item.url+'" target="_blank">Ver →</a></td>' +
      '</tr>';
  }).join('');

  const wrap = document.getElementById('tableWrap');
  if (wrap) wrap.innerHTML =
    '<div class="row-count">'+items.length+' publicaciones</div>' +
    '<table><thead><tr>' +
    th('model','Modelo') + th('variant','Variante') + th('seller','Vendedor') +
    th('price','Precio') + th('location','Ubicación') + '<th>Link</th>' +
    '</tr></thead><tbody>'+rows+'</tbody></table>';

  const pag = document.getElementById('pagination');
  if (!pag) return;
  if (totalPages <= 1) { pag.innerHTML = ''; return; }
  let btns = '<button class="page-btn" onclick="goto('+(state.page-1)+')" '+(state.page===1?'disabled':'')+'>&#8249;</button>';
  for (let p=1;p<=totalPages;p++) {
    if (p===1||p===totalPages||Math.abs(p-state.page)<=2)
      btns += '<button class="page-btn'+(p===state.page?' active':'')+'" onclick="goto('+p+')">'+p+'</button>';
    else if (Math.abs(p-state.page)===3)
      btns += '<span style="color:#ccc;padding:0 4px">&hellip;</span>';
  }
  btns += '<button class="page-btn" onclick="goto('+(state.page+1)+')" '+(state.page===totalPages?'disabled':'')+'>&#8250;</button>';
  pag.innerHTML = btns;
}

function onSearch(v) { state.search = v; state.page = 1; renderTable(); }
function onModel(v)  { state.model  = v; state.page = 1; renderTable(); }
function goto(p)     { state.page   = p; renderTable(); window.scrollTo(0,0); }

/* --- Boot --- */
function renderApp() {
  const brand = DATA[state.brand];
  document.getElementById('app').innerHTML =
    renderKPIs(brand) + renderSummary(brand) + renderChart(state.brand) + renderTableSection();
  renderTable();
  setTimeout(updateChart, 50);
}

document.documentElement.style.setProperty('--brand', DATA[state.brand].color);
renderTabs();
renderApp();
</script>
</body>
</html>
"""


def generate_interactive_report(results_by_brand, summaries_by_brand, history):
    """Generate a self-contained interactive HTML dashboard and save it to docs/index.html."""
    date_str = datetime.now().strftime('%d/%m/%Y %H:%M')

    # Build brands payload
    brands_payload = {}
    for brand_name, grouped in results_by_brand.items():
        brand_config = BRANDS.get(brand_name, {})
        all_items = [e for entries in grouped.values() for e in entries]
        items = []
        for model, entries in grouped.items():
            for e in entries:
                variant = _strip_model_prefix(e.get("title", ""), model, brand_name)
                items.append({
                    "model": model,
                    "variant": variant,
                    "subcategory": e.get("subcategory", "Otros"),
                    "seller": e.get("seller", "N/A"),
                    "price": e.get("price", 0),
                    "currency": e.get("currency", "$"),
                    "price_change": e.get("price_change", "new"),
                    "price_diff": e.get("price_diff", 0),
                    "anticipo": e.get("anticipo", 0),
                    "location": e.get("location", ""),
                    "url": e.get("url", ""),
                })
        brands_payload[brand_name] = {
            "color": brand_config.get("header_color", "#0d47a1"),
            "items": items,
            "stats": _compute_brand_stats(brand_name, grouped, all_items),
            "summary": summaries_by_brand.get(brand_name, ""),
        }

    # Build history payload: {brand: {model: [{date, avg, count}]}}
    hist_payload = {}
    for date, brands in sorted(history.items()):
        for brand_name, bstats in brands.items():
            hist_payload.setdefault(brand_name, {})
            for model, mstats in bstats.get("models", {}).items():
                hist_payload[brand_name].setdefault(model, []).append({
                    "date": date,
                    "avg": mstats.get("avg", 0),
                    "count": mstats.get("count", 0),
                })

    html = (
        _REPORT_TEMPLATE
        .replace("__DATE__", date_str)
        .replace("__BRANDS_JSON__", json.dumps(brands_payload, ensure_ascii=False))
        .replace("__HIST_JSON__", json.dumps(hist_payload, ensure_ascii=False))
    )

    report_path = os.path.join(os.path.dirname(__file__), REPORT_FILE)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Reporte interactivo guardado en {report_path}")


def _get_supabase_client():
    """Return a Supabase client or None if not configured."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def save_to_supabase(brand_name, grouped, items, run_date_str):
    """Upsert current run data into Supabase (listings, model_stats, runs).

    Uses upsert with on_conflict so re-runs on the same date are idempotent.
    Skips silently if Supabase is not configured.
    """
    client = _get_supabase_client()
    if not client:
        print("Supabase no configurado, omitiendo")
        return

    print(f"[{brand_name}] Guardando en Supabase...")

    # --- listings ---
    listing_rows = []
    for model, entries in grouped.items():
        for e in entries:
            variant = _strip_model_prefix(e.get("title", ""), model, brand_name)
            listing_rows.append({
                "run_date":     run_date_str,
                "brand":        brand_name,
                "model":        model,
                "variant":      variant,
                "subcategory":  e.get("subcategory", "Otros"),
                "seller":       e.get("seller", "Particular"),
                "price":        e.get("price", 0),
                "currency":     e.get("currency", "$"),
                "price_change": e.get("price_change", "new"),
                "price_diff":   e.get("price_diff", 0),
                "anticipo":     e.get("anticipo", 0),
                "location":     e.get("location", ""),
                "url":          e.get("url", ""),
            })

    if listing_rows:
        # Batch in chunks of 500 to stay within Supabase request limits
        chunk_size = 500
        for i in range(0, len(listing_rows), chunk_size):
            chunk = listing_rows[i:i + chunk_size]
            client.table("listings").upsert(chunk, on_conflict="run_date,url").execute()
        print(f"  {len(listing_rows)} listings guardados")

    # --- model_stats ---
    stats = _compute_brand_stats(brand_name, grouped, items)
    model_rows = []
    for model, ms in stats.get("models", {}).items():
        model_rows.append({
            "run_date":  run_date_str,
            "brand":     brand_name,
            "model":     model,
            "avg_price": ms.get("avg"),
            "min_price": ms.get("min"),
            "max_price": ms.get("max"),
            "count":     ms.get("count", 0),
            "n_up":      ms.get("n_up", 0),
            "n_down":    ms.get("n_down", 0),
            "n_new":     ms.get("n_new", 0),
        })

    if model_rows:
        client.table("model_stats").upsert(
            model_rows, on_conflict="run_date,brand,model"
        ).execute()
        print(f"  {len(model_rows)} model_stats guardados")

    # --- runs summary ---
    avgs = [ms.get("avg", 0) for ms in stats.get("models", {}).values() if ms.get("avg")]
    prices = [e.get("price", 0) for e in items if e.get("price", 0) > 0]
    run_row = {
        "run_date":  run_date_str,
        "brand":     brand_name,
        "total":     stats.get("total", 0),
        "avg_price": round(sum(avgs) / len(avgs)) if avgs else None,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "n_up":      sum(ms.get("n_up", 0)   for ms in stats.get("models", {}).values()),
        "n_down":    sum(ms.get("n_down", 0) for ms in stats.get("models", {}).values()),
        "n_new":     sum(ms.get("n_new", 0)  for ms in stats.get("models", {}).values()),
        "n_same":    sum(1 for e in items if e.get("price_change") == "same"),
    }
    client.table("runs").upsert(run_row, on_conflict="run_date,brand").execute()
    print(f"  Run summary guardado")


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
    print(f"Iniciando scraper - {datetime.now()}")

    history = load_history()
    today = datetime.now().strftime("%Y-%m-%d")
    all_results = {}  # brand_name -> {"grouped": ..., "items": ..., "prices_file": ...}

    for brand_name, brand_config in BRANDS.items():
        print(f"\n{'=' * 50}")
        print(f"Procesando: {brand_name}")
        print(f"{'=' * 50}")

        try:
            items = fetch_all_listings(
                brand_name,
                brand_config["base_url"],
                brand_config["apify_keywords"],
                brand_config.get("min_listings_threshold", 200),
            )
        except Exception as e:
            print(f"ERROR al obtener publicaciones de {brand_name}: {e}")
            items = []

        previous_prices = load_previous_prices(brand_config["prices_file"])
        compute_price_changes(items, previous_prices)

        n_up = sum(1 for i in items if i.get("price_change") == "up")
        n_down = sum(1 for i in items if i.get("price_change") == "down")
        n_new = sum(1 for i in items if i.get("price_change") == "new")
        n_same = sum(1 for i in items if i.get("price_change") == "same")
        print(
            f"[{brand_name}] Cambios: {n_up} subieron, {n_down} bajaron, "
            f"{n_same} sin cambio, {n_new} nuevas"
        )

        grouped = process_listings(items, brand_config["known_models"], brand_name)

        # Accumulate stats into history
        stats = _compute_brand_stats(brand_name, grouped, items)
        update_history(history, today, brand_name, stats)

        all_results[brand_name] = {
            "grouped": grouped,
            "items": items,
            "prices_file": brand_config["prices_file"],
        }

    # Generate executive summaries via Claude Haiku
    print("\nGenerando resumenes ejecutivos...")
    summaries_by_brand = {}
    for brand_name, data in all_results.items():
        summaries_by_brand[brand_name] = generate_brand_summary(
            brand_name, data["grouped"], data["items"], history
        )

    results_by_brand = {brand: data["grouped"] for brand, data in all_results.items()}
    plain_body = format_plain_text(results_by_brand, summaries_by_brand)
    html_body = format_html_email(results_by_brand, summaries_by_brand)

    print("\n--- CONTENIDO DEL EMAIL ---")
    print(plain_body)
    print("--- FIN DEL EMAIL ---\n")

    try:
        send_email(EMAIL_SUBJECT, plain_body, html_body)
    except Exception as e:
        print(f"ERROR al enviar email: {e}")
        sys.exit(1)

    for brand_name, data in all_results.items():
        save_current_prices(data["items"], data["prices_file"])
        try:
            save_to_supabase(
                brand_name, data["grouped"], data["items"], today
            )
        except Exception as e:
            print(f"[{brand_name}] ERROR al guardar en Supabase: {e}")

    save_history(history)
    generate_interactive_report(results_by_brand, summaries_by_brand, history)
    print("Listo.")


if __name__ == "__main__":
    main()
