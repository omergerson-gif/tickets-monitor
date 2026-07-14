"""
Paylogic Resale Ticket Monitor — Railway script
=================================================
Polls the Paylogic resale API. When a ticket listing appears, it sends
a ntfy.sh notification containing the direct buy URL.

The local_buyer.py script on your Mac picks up that notification,
opens the URL in your browser, and plays a sound — so you just click Pay.

Dependencies:
    pip install requests

Railway env variables to set:
    NTFY_TOPIC      e.g. "omer-ticket-alert"  (pick any unique name)
    TICKET_TYPES    optional, comma-separated filter e.g. "Regular Entrance Ticket"
    POLL_INTERVAL   optional, default 3 (seconds)
"""

import os
import time
import requests
from datetime import datetime

# ─── CONFIG ────────────────────────────────────────────────────────────────────

RESALE_PAGE_URL  = "https://resale.paylogic.com/4f4cb390559b41f49892d0a3214d067d/"
RESALE_API_URL   = "https://shopping-api.paylogic.com/resale/4f4cb390559b41f49892d0a3214d067d"
SALE_ID          = "4f4cb390559b41f49892d0a3214d067d"

_ticket_types_env = os.environ.get("TICKET_TYPES", "")
TICKET_TYPES = [t.strip() for t in _ticket_types_env.split(",") if t.strip()]

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1"))
NTFY_TOPIC    = os.environ.get("NTFY_TOPIC", "omer-ticket-alert")

# ───────────────────────────────────────────────────────────────────────────────


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_product_url(listing: dict) -> str:
    """Constructs the frontend buy URL for a listing."""
    # Try to get product UID from the listing's self link
    # e.g. https://shopping-api.paylogic.com/products/ea796abf4e194323b32120dae681165c
    try:
        product_href = listing.get("_links", {}).get("shop:product", {}).get("href", "")
        if product_href:
            product_uid = product_href.rstrip("/").split("/")[-1]
            return f"https://resale.paylogic.com/{SALE_ID}/{product_uid}"
    except Exception:
        pass
    return RESALE_PAGE_URL


def check_listings() -> list:
    r = requests.get(
        RESALE_API_URL,
        headers={"Accept": "application/json"},
        timeout=8,
    )
    r.raise_for_status()
    data = r.json()
    listings = data.get("_embedded", {}).get("shop:resale_listing", [])

    if TICKET_TYPES:
        listings = [
            l for l in listings
            if any(t.lower() in str(l.get("name", "")).lower() for t in TICKET_TYPES)
        ]

    return listings


def notify(listing: dict):
    if not NTFY_TOPIC:
        log("ntfy not configured — skipping notification.")
        return

    name  = listing.get("name", "Unknown ticket")
    price = listing.get("price", "?")
    url   = get_product_url(listing)

    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=url.encode("utf-8"),          # message body = the buy URL
            headers={
                "Title": f"🎟️ Ticket available: {name}",
                "Priority": "urgent",
                "Tags": "rotating_light,ticket",
                "Click": url,                  # tapping the phone notification opens the URL
            },
            timeout=10,
        )
        log(f"ntfy sent → {url}")
    except Exception as e:
        log(f"ntfy failed: {e}")


def main():
    log(f"Monitoring started. Poll interval: {POLL_INTERVAL}s")
    log(f"Ticket filter: {TICKET_TYPES or 'ANY'}")
    log(f"ntfy topic: {NTFY_TOPIC or 'NOT SET'}")

    notified_listings = set()   # avoid re-notifying the same listing
    consecutive_errors = 0

    while True:
        try:
            listings = check_listings()
            consecutive_errors = 0

            new = [l for l in listings if l.get("uid") not in notified_listings]

            if new:
                for listing in new:
                    log(f"🎟️  FOUND: {listing.get('name')} | {listing.get('price')}")
                    notify(listing)
                    notified_listings.add(listing.get("uid"))
            else:
                log(f"No tickets. Next check in {POLL_INTERVAL}s...")

        except requests.RequestException as e:
            consecutive_errors += 1
            log(f"Request error ({consecutive_errors}): {e}")
            if consecutive_errors >= 10:
                log("Too many errors — exiting.")
                break

        except KeyboardInterrupt:
            log("Stopped.")
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
