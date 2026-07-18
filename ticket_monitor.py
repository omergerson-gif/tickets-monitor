"""
Paylogic Resale Ticket Monitor — Railway script (with Browserbase)
===================================================================
Polls the Paylogic resale API. When a ticket listing appears:
  1. Creates a Browserbase cloud browser session
  2. Navigates to the listing page and clicks Buy automatically
  3. Sends you the live session URL via ntfy so you can take over and pay
     from any device (phone, laptop) — just open the link and enter your card

No local script needed. Everything runs on Railway.

Dependencies:
    pip install requests playwright

Browserbase setup (free tier: 100 min/month):
    1. Sign up at https://www.browserbase.com
    2. Get your API Key and Project ID from the dashboard
    3. Set them as Railway env variables (see CONFIG below)

Railway env variables:
    BROWSERBASE_API_KEY     from browserbase.com dashboard (API key only — no project ID needed)
    NTFY_TOPIC              e.g. "omer-ticket-alert"
    TICKET_TYPES            optional, comma-separated e.g. "Regular Entrance Ticket"
    POLL_INTERVAL           optional, default 1 (seconds)
"""

import os
import time
import requests
from datetime import datetime
try:
    from curl_cffi import requests as cf_requests
    USE_CURL_CFFI = True
except ImportError:
    USE_CURL_CFFI = False

# ─── CONFIG ────────────────────────────────────────────────────────────────────

RESALE_PAGE_URL = "https://resale.paylogic.com/4f4cb390559b41f49892d0a3214d067d/"
# shopping-api.paylogic.com requires HTTP/2 (returns 421 with HTTP/1.1 requests library).
# shopping-api.protected.paylogic.com requires browser cookies (returns 403 from Railway).
# Solution: use httpx with HTTP/2 support for the polling request.
RESALE_API_URL  = "https://shopping-api.paylogic.com/resale/4f4cb390559b41f49892d0a3214d067d"
SALE_ID         = "4f4cb390559b41f49892d0a3214d067d"

# Browser-like headers
API_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://resale.paylogic.com/",
    "Origin": "https://resale.paylogic.com",
}

_ticket_types_env = os.environ.get("TICKET_TYPES", "")
TICKET_TYPES = [t.strip() for t in _ticket_types_env.split(",") if t.strip()]

POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL", "1"))
NTFY_TOPIC          = os.environ.get("NTFY_TOPIC", "")
BROWSERBASE_API_KEY = os.environ.get("BROWSERBASE_API_KEY", "")

BUY_BUTTON_SELECTORS = [
    "button:has-text('Buy')",
    "button:has-text('Kopen')",
    "a:has-text('Buy')",
    ".btn-primary",
    "[data-testid='buy-button']",
    "button[type='submit']",
]

# ───────────────────────────────────────────────────────────────────────────────


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def check_listings() -> list:
    """
    Polls the resale API. Returns a list of available listings when tickets are on sale.
    Uses top-level statistics.available to detect availability (fast, one request).
    """
    # curl_cffi impersonates Chrome's TLS fingerprint, bypassing Cloudflare bot detection.
    # Falls back to plain requests if not installed (will likely 421 in that case).
    if USE_CURL_CFFI:
        r = cf_requests.get(RESALE_API_URL, headers=API_HEADERS, impersonate="chrome120", timeout=12)
    else:
        log("WARNING: curl_cffi not available, falling back to requests (may 421)")
        r = requests.get(RESALE_API_URL, headers=API_HEADERS, timeout=12)
    r.raise_for_status()
    data = r.json()

    stats = data.get("statistics", {})
    available = stats.get("available", 0)

    if available == 0:
        return []

    log(f"Tickets available! statistics={stats}")

    # Filter products by TICKET_TYPES
    products = data.get("_embedded", {}).get("shop:product", [])
    for p in products:
        name_dict = p.get("name", {})
        name = name_dict.get("en", "") if isinstance(name_dict, dict) else str(name_dict)
        uid = p.get("uid", "")

        if TICKET_TYPES and not any(t.lower() in name.lower() for t in TICKET_TYPES):
            continue

        # Use a time-bucketed uid so re-alerts fire every 2 min if purchase fails
        import time as _time
        bucket = int(_time.time() // 120)
        return [{"uid": f"avail_{bucket}", "name": name, "price": p.get("price", {})}]

    log(f"Tickets available but none match TICKET_TYPES filter: {TICKET_TYPES}")
    return []


def create_browserbase_session() -> tuple[str, str]:
    """Creates a Browserbase session. Returns (session_id, live_url)."""
    r = requests.post(
        "https://www.browserbase.com/v1/sessions",
        headers={"X-BB-API-Key": BROWSERBASE_API_KEY},
        json={},  # API key alone resolves the project — no projectId needed
        timeout=15,
    )
    r.raise_for_status()
    session = r.json()
    session_id = session["id"]
    live_url = f"https://www.browserbase.com/sessions/{session_id}"
    return session_id, live_url


def notify(message: str, live_url: str = ""):
    if not NTFY_TOPIC:
        log("ntfy not configured — skipping.")
        return
    click_url = live_url or RESALE_PAGE_URL
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": "Ticket available - open to pay!",
                "Priority": "urgent",
                "Tags": "rotating_light,ticket",
                "Click": click_url,
            },
            timeout=10,
        )
        log(f"ntfy sent → {click_url}")
    except Exception as e:
        log(f"ntfy failed: {e}")


def run_purchase(listing: dict):
    """
    Opens a Browserbase cloud browser, navigates to the resale page,
    clicks Buy for the matching ticket type, then sends the cart URL via ntfy.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

    listing_name = listing.get("name", "")

    if not BROWSERBASE_API_KEY:
        log("Browserbase not configured — sending URL only.")
        notify(f"Ticket found! Open to buy: {RESALE_PAGE_URL}", RESALE_PAGE_URL)
        return

    log("Creating Browserbase session...")
    try:
        session_id, live_url = create_browserbase_session()
        log(f"Session: {live_url}")
    except Exception as e:
        log(f"Browserbase session failed: {e} — falling back to URL notify.")
        notify(f"Ticket found! Open to buy: {RESALE_PAGE_URL}", RESALE_PAGE_URL)
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}&sessionId={session_id}"
            )
            context = browser.contexts[0]
            page = context.pages[0]

            # Navigate to main resale page (product-specific pages use frontend UIDs
            # that differ from API UIDs — easier to click by name on the main page)
            page.goto(RESALE_PAGE_URL, wait_until="networkidle", timeout=20000)
            log(f"Loaded resale page, looking for: {listing_name or 'any'}")

            clicked = False

            # Strategy 1: find Buy button inside the product row matching the ticket name
            if listing_name:
                for name_variant in [listing_name] + TICKET_TYPES:
                    if not name_variant:
                        continue
                    try:
                        # Locate the element containing the product name
                        name_el = page.locator(f"text={name_variant}").first
                        # Walk up to find an ancestor that also contains a Buy button
                        for xpath in [
                            "xpath=ancestor::*[.//button[contains(., 'Buy') or contains(., 'Kopen')]][1]",
                            "xpath=ancestor::*[.//a[contains(., 'Buy') or contains(., 'Kopen')]][1]",
                        ]:
                            try:
                                container = name_el.locator(xpath)
                                btn = container.locator("button:has-text('Buy'), button:has-text('Kopen'), a:has-text('Buy')").first
                                btn.wait_for(timeout=3000)
                                btn.click()
                                log(f"Clicked Buy near '{name_variant}'")
                                clicked = True
                                break
                            except Exception:
                                pass
                        if clicked:
                            break
                    except Exception:
                        pass

            # Strategy 2: first visible Buy button on the page
            if not clicked:
                for selector in BUY_BUTTON_SELECTORS:
                    try:
                        page.wait_for_selector(selector, timeout=3000)
                        page.click(selector)
                        log(f"Clicked Buy ({selector})")
                        clicked = True
                        break
                    except PlaywrightTimeout:
                        continue

            if not clicked:
                log("Buy button not found — sending page URL.")
                notify("Ticket found! Open to buy:", RESALE_PAGE_URL)
                browser.close()
                return

            # Wait for cart/checkout
            try:
                page.wait_for_selector("a[href*='shopping-cart']", timeout=5000)
                page.click("a[href*='shopping-cart']")
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass

            checkout_url = page.url
            log(f"Checkout URL: {checkout_url}")
            notify("Ticket in cart! Open to pay:", checkout_url)
            browser.close()

    except Exception as e:
        log(f"Browser error: {e}")
        notify("Ticket found but automation failed. Buy manually:", RESALE_PAGE_URL)


def main():
    log(f"Monitoring started. Poll interval: {POLL_INTERVAL}s")
    log(f"Ticket filter: {TICKET_TYPES or 'ANY'}")
    log(f"ntfy topic: {NTFY_TOPIC or 'NOT SET'}")
    log(f"Browserbase: {'configured ✓' if BROWSERBASE_API_KEY else 'NOT configured'}")

    notified_listings = set()
    consecutive_errors = 0

    while True:
        try:
            listings = check_listings()
            consecutive_errors = 0

            new = [l for l in listings if l.get("uid") not in notified_listings]

            if new:
                for listing in new:
                    log(f"FOUND: {listing.get('name')} | {listing.get('price')}")
                    notified_listings.add(listing.get("uid"))
                    run_purchase(listing)
            else:
                log(f"No tickets. Next check in {POLL_INTERVAL}s...")

        except Exception as e:
            consecutive_errors += 1
            log(f"Request error ({consecutive_errors}): {e}")
            # Never exit — just back off slightly and keep monitoring.
            # The API can go unreachable during high-traffic sale periods;
            # stopping then would be the worst time to stop.
            error_sleep = min(consecutive_errors * 2, 15)
            time.sleep(error_sleep)
            continue

        except KeyboardInterrupt:
            log("Stopped.")
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
