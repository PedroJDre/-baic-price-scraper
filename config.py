import os

# --- HTML Scraping ---
BASE_URL = "https://autos.mercadolibre.com.ar/baic/"
ITEMS_PER_PAGE = 48
MAX_PAGES = 20  # Safety cap: 20 pages * 48 = 960 listings max
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
}

# --- Email ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")  # Gmail App Password
EMAIL_RECIPIENTS = os.environ.get("EMAIL_RECIPIENTS", "").split(",")
EMAIL_SUBJECT = "BAIC Precios - Mercado Libre Argentina"
