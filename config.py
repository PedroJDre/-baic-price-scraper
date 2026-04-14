import os

# --- Scraping settings ---
ITEMS_PER_PAGE = 48
MAX_PAGES = 20  # Safety cap: 20 pages * 48 = 960 listings max
REQUEST_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT = 90  # ScraperAPI with render=true is slower

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
}

# --- Supabase ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role key

# --- Interactive report (GitHub Pages) ---
REPORT_FILE = "docs/index.html"
GITHUB_PAGES_URL = "https://pedrojdre.github.io/-baic-price-scraper/"

# --- Claude API (executive summary) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
HISTORY_FILE = "data/history.json"
HISTORY_MAX_ENTRIES = 12  # ~6 weeks of data

# --- ScraperAPI ---
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")
SCRAPERAPI_URL = "https://api.scraperapi.com"

# --- ScrapingBee ---
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"

# --- ScrapingAnt ---
SCRAPINGANT_API_KEY = os.environ.get("SCRAPINGANT_API_KEY", "")
SCRAPINGANT_URL = "https://api.scrapingant.com/v2/general"

# --- Apify (fallback) ---
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
APIFY_ACTOR_ID = "karamelo~mercadolibre-scraper-espanol-castellano"

# --- Email ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")  # Gmail App Password
EMAIL_RECIPIENTS = os.environ.get("EMAIL_RECIPIENTS", "").split(",")
EMAIL_SUBJECT = "Reporte Diario Mercado Libre"

# --- Brands to scrape ---
BRANDS = {
    "BAIC": {
        "base_url": "https://autos.mercadolibre.com.ar/baic/",
        "known_models": ["BJ30", "BJ40", "BJ60", "EU5", "U5", "X25", "X35", "X55"],
        "apify_keywords": [
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
        ],
        "prices_file": "data/prices_baic.json",
        "min_listings_threshold": 200,
        "header_color": "#0d47a1",
        "card_color": "#1a237e",
    },
    "Chery": {
        "base_url": "https://autos.mercadolibre.com.ar/chery/",
        "known_models": [
            "Tiggo 2", "Tiggo 3", "Tiggo 4", "Tiggo 7", "Tiggo 8",
            "Arrizo 6", "Arrizo 8",
        ],
        "apify_keywords": [
            "Chery Tiggo 4",
            "Chery Tiggo 4 Pro",
            "Chery Tiggo 7",
            "Chery Tiggo 7 Pro",
            "Chery Tiggo 8",
            "Chery Tiggo 8 Pro",
            "Chery Arrizo 6",
            "Chery Arrizo 8",
        ],
        "prices_file": "data/prices_chery.json",
        "min_listings_threshold": 100,
        "header_color": "#b71c1c",
        "card_color": "#7f0000",
    },
}
