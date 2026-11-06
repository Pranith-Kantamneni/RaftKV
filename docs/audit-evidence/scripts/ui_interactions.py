from playwright.sync_api import sync_playwright, expect
import json, time

base = 'http://127.0.0.1:8080'
out = {'base': base, 'steps': {}, 'errors': []}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    page.add_init_script("""
        (() => {
          const Native = window.WebSocket;
          function Wrapped(url, protocols) {
            const socket = protocols === undefined ? new Native(url) : new Native(url, protocols);
            window.__raftSockets = window.__raftSockets || [];
            window.__raftSockets.push(socket);
            return socket;
          }
          Wrapped.prototype = Native.prototype;
          Wrapped.CONNECTING = Native.CONNECTING;
          Wrapped.OPEN = Native.OPEN;
          Wrapped.CLOSING = Native.CLOSING;
          Wrapped.CLOSED = Native.CLOSED;
          window.WebSocket = Wrapped;
        })();
    """)
    errors = []
    page.on('console', lambda msg: errors.append(f'console:{msg.type}:{msg.text}') if msg.type == 'error' else None)
    page.on('pageerror', lambda exc: errors.append(f'pageerror:{exc}'))

    # KV write/read/delete through the actual UI.
    page.goto(base + '/kv', wait_until='networkidle', timeout=30000)
    page.get_by_role('button', name='New key').click()
    page.locator('#kv-key').fill('ui-audit-key')
    page.locator('#kv-value').fill('ui-audit-value')
    page.get_by_role('button', name='Commit SET').click()
    expect(page.get_by_text('committed at index', exact=False)).to_be_visible(timeout=15000)
    out['steps']['kv_write_committed'] = True
    row = page.locator('tbody tr').filter(has_text='ui-audit-key').first
    expect(row).to_be_visible(timeout=10000)
    row.get_by_title('Read through leader').click()
    expect(page.get_by_text('Leader read result')).to_be_visible(timeout=15000)
    expect(page.get_by_text('read-index', exact=False)).to_be_visible(timeout=10000)
    out['steps']['kv_read_mode'] = 'read-index'
    row.get_by_title('Delete key').click()
    page.locator('div.fixed.inset-0').get_by_role('button', name='Delete key', exact=True).click()
    expect(page.get_by_text('committed at index', exact=False)).to_be_visible(timeout=15000)
    out['steps']['kv_delete_committed'] = True

    # Kill and restart a follower through the simulator UI.
    page.goto(base + '/simulator', wait_until='networkidle', timeout=30000)
    process_section = page.locator('section').filter(has_text='Process failures').first
    row = process_section.locator('div.divide-y > div').filter(has_text='n2').first
    expect(row).to_be_visible(timeout=10000)
    row.get_by_role('button', name='Kill').click()
    page.get_by_role('button', name='Kill process').click()
    expect(process_section.locator('div.divide-y > div').filter(has_text='n2').first).to_contain_text('offline', timeout=20000)
    out['steps']['simulator_kill'] = True
    row = process_section.locator('div.divide-y > div').filter(has_text='n2').first
    row.get_by_role('button', name='Restart').click()
    page.get_by_role('button', name='Restart node').click()
    expect(process_section.locator('div.divide-y > div').filter(has_text='n2').first).not_to_contain_text('offline', timeout=30000)
    out['steps']['simulator_restart'] = True

    # WebSocket disconnect/reconnect: browser goes offline, then online.
    page.goto(base + '/', wait_until='networkidle', timeout=30000)
    expect(page.get_by_title('WebSocket connected')).to_be_visible(timeout=15000)
    page.evaluate("window.__raftSockets[window.__raftSockets.length - 1].close()")
    expect(page.get_by_title('WebSocket reconnecting')).to_be_visible(timeout=15000)
    out['steps']['websocket_offline_detected'] = True
    expect(page.get_by_title('WebSocket connected')).to_be_visible(timeout=20000)
    out['steps']['websocket_reconnected'] = True

    # API error state in the KV list.
    def fail_kv_list(route):
        if route.request.method == 'GET':
            route.fulfill(status=500, body='audit injected failure', headers={'content-type': 'text/plain'})
        else:
            route.continue_()
    page.route('**/api/kv', fail_kv_list)
    page.goto(base + '/kv', wait_until='networkidle', timeout=30000)
    expect(page.get_by_text('audit injected failure', exact=False).first).to_be_visible(timeout=15000)
    out['steps']['api_error_state'] = True
    page.unroute('**/api/kv')

    # Reports list and detail interaction.
    page.goto(base + '/reports', wait_until='networkidle', timeout=30000)
    report_buttons = page.locator('div.panel button').filter(has_text='normal')
    if report_buttons.count() == 0:
        report_buttons = page.locator('button').filter(has_text='normal')
    if report_buttons.count():
        report_buttons.first.click()
        expect(page.locator('main h2').first).to_be_visible(timeout=15000)
        out['steps']['reports_select'] = True
    else:
        out['steps']['reports_select'] = 'no reports present'
    # Open the run dialog, inspect controls, close without mutating the cluster.
    page.get_by_role('button', name='Run experiment').click()
    expect(page.get_by_text('Execute experiment against live cluster?')).to_be_visible(timeout=10000)
    out['steps']['reports_run_dialog'] = True
    page.get_by_role('button', name='Cancel').click()

    out['errors'] = errors
    browser.close()

print(json.dumps(out, indent=2))
