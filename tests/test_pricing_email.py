import importlib.util
import sys
import types
import unittest


if importlib.util.find_spec("anthropic") is None:
    anthropic_stub = types.ModuleType("anthropic")
    anthropic_stub.Anthropic = lambda *args, **kwargs: None
    sys.modules["anthropic"] = anthropic_stub

if importlib.util.find_spec("supabase") is None:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.create_client = lambda *args, **kwargs: None
    sys.modules["supabase"] = supabase_stub

import main


def listing(seller, price, currency="U$S", change="new", suffix=""):
    return {
        "title": f"BAIC BJ30 {suffix}",
        "seller": seller,
        "price": price,
        "currency": currency,
        "url": f"https://example.com/{seller}-{price}-{suffix}",
        "price_change": change,
        "price_diff": 0,
    }


class PricingEmailTests(unittest.TestCase):
    def test_dominant_price_stats_do_not_mix_currencies(self):
        stats = main._dominant_price_stats([
            listing("BAIC San Jorge", 100, "U$S"),
            listing("ICARS", 200, "USD"),
            listing("Particular", 10_000_000, "$"),
        ])

        self.assertEqual(stats["currency"], "USD")
        self.assertEqual(stats["avg"], 150)
        self.assertEqual(stats["ignored_currency_count"], 1)
        self.assertEqual(stats["avg_label"], "USD 150")

    def test_own_sellers_are_compared_against_dealer_competitors(self):
        entries = [
            listing("BAIC San Jorge", 100, suffix="own-1"),
            listing("BAIC by One Fan", 110, suffix="own-2"),
            listing("ICARS", 120, suffix="dealer-competitor"),
            listing("Particular", 130, suffix="private-market"),
        ]

        stats = main._compute_brand_stats("BAIC", {"BJ30": entries}, entries)
        model = stats["models"]["BJ30"]

        self.assertEqual(model["own_count"], 2)
        self.assertEqual(model["own_avg"], 105)
        self.assertEqual(model["competitor_count"], 2)
        self.assertEqual(model["dealer_competitor_count"], 1)
        self.assertEqual(model["benchmark_label"], "competencia concesionaria")
        self.assertEqual(model["benchmark_avg"], 120)

    def test_benchmark_falls_back_to_market_when_no_dealer_competitor_exists(self):
        entries = [
            listing("NationBaic", 100, suffix="own"),
            listing("Particular", 90, suffix="private-market"),
        ]

        stats = main._compute_brand_stats("BAIC", {"U5": entries}, entries)
        model = stats["models"]["U5"]

        self.assertEqual(model["own_count"], 1)
        self.assertEqual(model["dealer_competitor_count"], 0)
        self.assertEqual(model["benchmark_label"], "mercado ML")
        self.assertEqual(model["benchmark_avg"], 90)

    def test_email_centers_our_price_vs_competition_action(self):
        entries = [
            listing("BAIC San Jorge", 100, suffix="own-1"),
            listing("BAIC by One Fan", 110, suffix="own-2"),
            listing("ICARS", 120, suffix="dealer-competitor"),
        ]

        html = main.format_html_email({"BAIC": {"BJ30": entries}}, {})

        self.assertIn("Nosotros vs competencia por modelo", html)
        self.assertIn("Lectura / accion", html)
        self.assertIn("Estamos -12.5% abajo", html)
        self.assertIn("Hay margen para subir", html)
        self.assertIn("competencia concesionaria", html)
        self.assertNotIn("particulares ML", html)

    def test_email_flags_models_priced_above_competition(self):
        entries = [
            listing("BAIC San Jorge", 130, suffix="own"),
            listing("ICARS", 100, suffix="dealer-competitor"),
        ]

        html = main.format_html_email({"BAIC": {"BJ40": entries}}, {})

        self.assertIn("Estamos +30.0% arriba", html)
        self.assertIn("Revisar precio o propuesta comercial", html)

    def test_empty_price_save_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
            main.save_current_prices([], "data/test_empty_prices.json")


if __name__ == "__main__":
    unittest.main()
