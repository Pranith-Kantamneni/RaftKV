import re
import time
from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:8080"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})

    def kv_empty(route):
        if route.request.method != "GET":
            route.continue_()
            return
        time.sleep(1.2)
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"items":{},"applied_index":0,"source":"leader","stale":false,"commit_index":0}',
        )

    page.route(re.compile(r".*/api/kv$"), kv_empty)
    page.goto(f"{BASE}/kv", wait_until="domcontentloaded", timeout=30000)
    expect(page.get_by_text("Reading state machine…", exact=True)).to_be_visible(timeout=3000)
    expect(page.get_by_text("The state machine is empty", exact=True)).to_be_visible(timeout=5000)
    print("KV loading and empty states OK")
    page.unroute(re.compile(r".*/api/kv$"))

    def reports_empty(route):
        time.sleep(1.2)
        route.fulfill(status=200, content_type="application/json", body='{"experiments":[]}')

    page.route(re.compile(r".*/api/experiments$"), reports_empty)
    page.goto(f"{BASE}/reports", wait_until="domcontentloaded", timeout=30000)
    expect(page.get_by_text("Loading reports…", exact=True)).to_be_visible(timeout=3000)
    expect(page.get_by_text("No reports yet", exact=True)).to_be_visible(timeout=5000)
    print("Reports loading and empty states OK")
    page.unroute(re.compile(r".*/api/experiments$"))

    browser.close()
