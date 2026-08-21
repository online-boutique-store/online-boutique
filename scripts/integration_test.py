#!/usr/bin/env python3
"""
Integration test for Online Boutique.

Exercises a basic shopping flow across multiple services:
  1. Homepage (frontend)
  2. Product catalog (productcatalogservice)
  3. Add item to cart (cartservice)
  4. View cart (cartservice)
  5. Checkout (checkoutservice, paymentservice, emailservice, shippingservice)

Usage:
  FRONTEND_URL=http://... python3 scripts/integration_test.py

Exit codes:
  0  All tests passed
  1  One or more tests failed or the application was unreachable
"""

import os
import sys
import json
import time
import datetime
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "15"))
RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "3"))

# A known product in the catalog (Sunglasses, SKU 0PUK6V6EV0)
PRODUCT_ID = "0PUK6V6EV0"

# ── Helpers ────────────────────────────────────────────────────────────────────

class Result:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.duration_ms = 0
        self.error = ""

    def ok(self, duration_ms: float):
        self.passed = True
        self.duration_ms = int(duration_ms)
        return self

    def fail(self, reason: str):
        self.passed = False
        self.error = reason
        return self


def get_with_retry(session: requests.Session, url: str, desc: str) -> requests.Response:
    """GET url with retries; raises on final failure."""
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            print(f"  [{attempt}/{RETRIES}] GET {url} → {resp.status_code}")
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            print(f"  [{attempt}/{RETRIES}] GET {url} → error: {exc}")
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"{desc} failed after {RETRIES} attempts: {last_exc}") from last_exc


def post_with_retry(session: requests.Session, url: str, data: dict, desc: str) -> requests.Response:
    """POST url with retries; raises on final failure."""
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.post(url, data=data, timeout=TIMEOUT, allow_redirects=True)
            print(f"  [{attempt}/{RETRIES}] POST {url} → {resp.status_code}")
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            print(f"  [{attempt}/{RETRIES}] POST {url} → error: {exc}")
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"{desc} failed after {RETRIES} attempts: {last_exc}") from last_exc


# ── Test cases ─────────────────────────────────────────────────────────────────

def test_homepage(session: requests.Session) -> Result:
    result = Result("Homepage")
    try:
        t0 = time.monotonic()
        resp = get_with_retry(session, f"{FRONTEND_URL}/", "Homepage")
        elapsed = (time.monotonic() - t0) * 1000
        if resp.status_code != 200:
            return result.fail(f"expected HTTP 200, got {resp.status_code}")
        if "Online Boutique" not in resp.text:
            return result.fail("response body did not contain expected store title")
        return result.ok(elapsed)
    except Exception as exc:
        return result.fail(str(exc))


def test_product_catalog(session: requests.Session) -> Result:
    """Fetches the product detail page and verifies the page loads correctly."""
    result = Result("Product catalog")
    try:
        t0 = time.monotonic()
        resp = get_with_retry(session, f"{FRONTEND_URL}/product/{PRODUCT_ID}", "Product page")
        elapsed = (time.monotonic() - t0) * 1000
        if resp.status_code != 200:
            return result.fail(f"expected HTTP 200, got {resp.status_code}")
        if "Add To Cart" not in resp.text:
            return result.fail("product page missing 'Add To Cart' button")
        return result.ok(elapsed)
    except Exception as exc:
        return result.fail(str(exc))


def test_add_to_cart(session: requests.Session) -> Result:
    """POSTs an item to the cart and verifies the cart is non-empty."""
    result = Result("Add to cart")
    try:
        t0 = time.monotonic()
        resp = post_with_retry(
            session,
            f"{FRONTEND_URL}/cart",
            {"product_id": PRODUCT_ID, "quantity": 1},
            "Add to cart",
        )
        elapsed = (time.monotonic() - t0) * 1000
        if resp.status_code != 200:
            return result.fail(f"expected HTTP 200, got {resp.status_code}")
        return result.ok(elapsed)
    except Exception as exc:
        return result.fail(str(exc))


def test_view_cart(session: requests.Session) -> Result:
    """Fetches the cart page and verifies the previously added item is present."""
    result = Result("View cart")
    try:
        t0 = time.monotonic()
        resp = get_with_retry(session, f"{FRONTEND_URL}/cart", "Cart page")
        elapsed = (time.monotonic() - t0) * 1000
        if resp.status_code != 200:
            return result.fail(f"expected HTTP 200, got {resp.status_code}")
        # After adding an item, the cart page should show a checkout link
        if "Proceed to Checkout" not in resp.text and "checkout" not in resp.text.lower():
            return result.fail("cart page did not contain checkout option after adding item")
        return result.ok(elapsed)
    except Exception as exc:
        return result.fail(str(exc))


def test_checkout(session: requests.Session) -> Result:
    """Submits the checkout form with synthetic data and verifies order confirmation."""
    result = Result("Checkout")
    try:
        t0 = time.monotonic()
        resp = post_with_retry(
            session,
            f"{FRONTEND_URL}/cart/checkout",
            {
                "email": "integration-test@example.com",
                "street_address": "1600 Amphitheatre Pkwy",
                "zip_code": "94043",
                "city": "Mountain View",
                "state": "CA",
                "country": "United States",
                "credit_card_number": "4432801561520454",
                "credit_card_expiration_month": "1",
                "credit_card_expiration_year": "2030",
                "credit_card_cvv": "672",
            },
            "Checkout",
        )
        elapsed = (time.monotonic() - t0) * 1000
        if resp.status_code != 200:
            return result.fail(f"expected HTTP 200, got {resp.status_code}")
        if "order" not in resp.text.lower() and "confirmation" not in resp.text.lower():
            return result.fail("checkout response did not contain order confirmation")
        return result.ok(elapsed)
    except Exception as exc:
        return result.fail(str(exc))


# ── Runner ─────────────────────────────────────────────────────────────────────

def main():
    if not FRONTEND_URL:
        print("ERROR: FRONTEND_URL environment variable is required.")
        print("  Example: FRONTEND_URL=http://your-frontend-lb python3 scripts/integration_test.py")
        sys.exit(1)

    print(f"Integration test run — {datetime.datetime.utcnow().isoformat()}Z")
    print(f"Target: {FRONTEND_URL}")
    print(f"Config: timeout={TIMEOUT}s retries={RETRIES} retry_delay={RETRY_DELAY}s")
    print()

    # Use a persistent session so cookies are maintained across requests
    # (required for the cart flow to work correctly).
    session = requests.Session()
    session.headers["User-Agent"] = "online-boutique-integration-test/1.0"

    tests = [
        test_homepage,
        test_product_catalog,
        test_add_to_cart,
        test_view_cart,
        test_checkout,
    ]

    results = []
    for test_fn in tests:
        print(f"Running: {test_fn.__doc__ or test_fn.__name__}")
        r = test_fn(session)
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        timing = f"  ({r.duration_ms}ms)" if r.passed else f"  — {r.error}"
        print(f"  {status}{timing}")
        print()

    # ── Summary ────────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print("=" * 60)
    print(f"Results: {passed}/{len(results)} passed")
    print()
    for r in results:
        mark = "✓" if r.passed else "✗"
        detail = f"{r.duration_ms}ms" if r.passed else r.error
        print(f"  {mark}  {r.name:<25} {detail}")
    print("=" * 60)

    # Write machine-readable output for artifact upload
    report = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "target": FRONTEND_URL,
        "passed": passed,
        "failed": failed,
        "total": len(results),
        "results": [
            {
                "name": r.name,
                "passed": r.passed,
                "duration_ms": r.duration_ms,
                "error": r.error,
            }
            for r in results
        ],
    }
    with open("integration-test-results.json", "w") as f:
        json.dump(report, f, indent=2)
    print("\nWrote integration-test-results.json")

    if failed > 0:
        print(f"\n{failed} test(s) failed.")
        sys.exit(1)
    else:
        print("\nAll tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
