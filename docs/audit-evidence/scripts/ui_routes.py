from playwright.sync_api import sync_playwright
import json, sys
base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
routes = ["/", "/cluster", "/simulator", "/kv", "/timeline", "/metrics", "/reports"]
out = {"base": base, "routes": {}}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    for route in routes:
        before = len(errors)
        resp = page.goto(base + route, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        title = page.title()
        h1 = page.locator("h1").first.inner_text() if page.locator("h1").count() else ""
        body = page.locator("body").inner_text()[:500]
        route_errors = errors[before:]
        out["routes"][route] = {"status": resp.status if resp else None, "title": title, "h1": h1, "errors": route_errors, "body_head": body}
        page.screenshot(path=f"/tmp/raftkv-audit-backup/ui-{route.strip('/') or 'overview'}.png", full_page=True)
    out["errors"] = errors
    browser.close()
print(json.dumps(out, indent=2))
