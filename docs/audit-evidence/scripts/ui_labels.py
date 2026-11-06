from playwright.sync_api import sync_playwright, expect
with sync_playwright() as p:
    b=p.chromium.launch(headless=True); page=b.new_page(viewport={'width':1440,'height':1000})
    page.goto('http://127.0.0.1:8080/metrics', wait_until='networkidle', timeout=30000)
    expect(page.get_by_text('Commit rate', exact=True).first).to_be_visible(timeout=15000)
    expect(page.get_by_text('Successful writes', exact=True).first).to_be_visible(timeout=15000)
    expect(page.get_by_text('leader commit-index growth / second (includes no-op entries)', exact=False)).to_be_visible(timeout=15000)
    expect(page.get_by_title('WebSocket connected')).to_be_visible(timeout=15000)
    print('metrics labels and live websocket OK')
    b.close()
