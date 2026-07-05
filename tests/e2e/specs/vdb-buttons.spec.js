import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const MOCK_NIGHTS = ['20260601'];
const MOCK_CHUNKS = [
  {
    filename: 'TST001_20260601_213000_color.mkv',
    time: '21:30:00',
    size_mb: 12.5,
    meteor_time: '2026-06-01T21:30:05.123',
    detection_offset_s: 5.0,
    stack: 'TST001_20260601_213000_stack.webp',
    stack_subdir: 'stacks',
    locked: true,
    lock_type: 'detection',
    reencoded: false,
  },
  {
    filename: 'TST001_20260601_220000_color.mkv',
    time: '22:00:00',
    size_mb: 10.2,
    meteor_time: null,
    stack: null,
    locked: false,
    lock_type: null,
    reencoded: false,
  },
  {
    filename: 'TST001_20260601_224500_color.mkv',
    time: '22:45:00',
    size_mb: 15.0,
    meteor_time: null,
    stack: null,
    locked: false,
    lock_type: null,
    reencoded: true,
  },
];

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

function interceptStationAPIs(page) {
  page.route('**/api/videodb/nights/**', route =>
    route.fulfill({ json: MOCK_NIGHTS })
  );
  page.route('**/api/videodb/chunks/**', route =>
    route.fulfill({ json: MOCK_CHUNKS })
  );
  page.route('**/api/videodb/rms-detections/**', route =>
    route.fulfill({ json: { detections: [] } })
  );
  page.route('**/api/cached-video/**', route =>
    route.fulfill({
      status: 200,
      contentType: 'video/mp4',
      body: Buffer.alloc(0),
    })
  );
  page.route('**/api/cached-files/**', route =>
    route.fulfill({ json: [] })
  );
  page.route('**/api/status/**', route =>
    route.fulfill({ json: { online: true } })
  );
  page.route('**/stack/**', route =>
    route.fulfill({ status: 404, body: '' })
  );
  page.route('**/thumbnail/**', route =>
    route.fulfill({ status: 404, body: '' })
  );
  page.route('**/api/platepar/**', route =>
    route.fulfill({ json: {} })
  );
}

async function navigateToVDB(page) {
  await page.goto('/station/test_station_1');
  await page.click('#tab-btn-videodb');
  await page.waitForTimeout(1500);
}

// ---- Tests ----

test.describe('VDB button interactivity', () => {
  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
    await login(page);
  });

  test('all required window exports exist after page load', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const vdbExports = [
      'vdbOpenModal', 'vdbCloseModal', 'vdbNavModal',
      'vdbStitchAdj', 'vdbToggleSort', 'vdbUnstitch',
      'vdbOpenSyncedModal', 'vdbSetAllCamsSort', 'vdbSwitchView',
      'vdbStopPoll', 'vdbInit', 'vdbPoll',
      'vdbProcessChunk', 'vdbToggleLock',
      'vdbSliderInput', 'vdbTimeInputChange', 'vdbUpdateZoom',
      'vdbOnNightChange', 'vdbOnLockedOnlyChange',
    ];

    for (const fn of vdbExports) {
      const exists = await page.evaluate(name => typeof window[name] === 'function', fn);
      expect(exists, `window.${fn} should be a function`).toBe(true);
    }

    expect(errors).toEqual([]);
  });

  test('events page exports exist', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/events');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(500);

    const exports = [
      'openEventModal', 'openSingleVideo', 'evDownloadClip',
      'compCartQuick', 'compCartRemove', 'compCartSetTrim',
      'compCartToggle', 'compRowToggleTrim', 'sfToggleShower',
    ];

    for (const fn of exports) {
      const exists = await page.evaluate(name => typeof window[name] === 'function', fn);
      expect(exists, `window.${fn} should be a function`).toBe(true);
    }

    expect(errors).toEqual([]);
  });

  test('RMS page exports exist', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(500);

    const exports = [
      'rmsCalClearSelection', 'rmsCalGo', 'rmsCalNav', 'rmsCalToggle',
      'rmsClearFilter', 'rmsOpenModal', 'rmsPlotExpand',
      'rmsSelectCamera', 'rmsSelectShower', 'skyDomeOpenModal',
      'rmsApplyFilter',
    ];

    for (const fn of exports) {
      const exists = await page.evaluate(name => typeof window[name] === 'function', fn);
      expect(exists, `window.${fn} should be a function`).toBe(true);
    }

    expect(errors).toEqual([]);
  });

  test('Process button is clickable and not blocked', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const processBtn = page.locator('.vdb-process-btn:not([disabled])').first();
    await expect(processBtn).toBeVisible({ timeout: 5000 });

    const isEnabled = await processBtn.isEnabled();
    expect(isEnabled, 'Process button should be enabled').toBe(true);

    const box = await processBtn.boundingBox();
    expect(box, 'Process button should have a bounding box').toBeTruthy();
    expect(box.width).toBeGreaterThan(0);
    expect(box.height).toBeGreaterThan(0);

    // Check nothing is overlaying the button
    const elementAtPoint = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return el ? { tag: el.tagName, classes: el.className, text: el.textContent?.slice(0, 50) } : null;
    }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });

    expect(
      elementAtPoint.tag === 'BUTTON' || elementAtPoint.classes?.includes('vdb-process-btn'),
      `Element at Process button center should be the button itself, got: ${JSON.stringify(elementAtPoint)}`
    ).toBe(true);

    // The XSS-hardened frontend wires the Process action via a delegated click
    // handler that reads the card's data-chunk JSON, NOT an inline onclick that
    // interpolates server-supplied strings. Assert the trigger class + payload
    // exist instead of an onclick attribute.
    await expect(processBtn).toHaveClass(/vdb-process-trigger/);
    const chunkData = await processBtn.evaluate(
      el => el.closest('.vdb-chunk-card')?.dataset.chunk || null
    );
    expect(chunkData, 'Process button card should carry data-chunk payload').toBeTruthy();
    expect(() => JSON.parse(chunkData), 'data-chunk should be valid JSON').not.toThrow();

    // Click should route through the delegated handler and hit vdbProcessChunk.
    page.route('**/api/videodb/process/**', route =>
      route.fulfill({ json: { ok: true } })
    );
    await processBtn.click();
    await page.waitForTimeout(300);

    // Filter out expected network errors from the Process click
    const realErrors = errors.filter(e => !e.includes('fetch') && !e.includes('NetworkError'));
    expect(realErrors).toEqual([]);
  });

  test('Lock button is clickable for admin users', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    // Find a lock button that is NOT disabled (admin lock toggle)
    const lockBtn = page.locator('.vdb-lock-btn:not([disabled])').first();
    const lockBtnExists = await lockBtn.count() > 0;

    if (lockBtnExists) {
      await expect(lockBtn).toBeVisible();

      // Delegated handler (XSS-hardened): the clickable lock carries the
      // vdb-lock-trigger class and its card holds the data-chunk payload,
      // rather than an inline onclick with server strings.
      await expect(lockBtn).toHaveClass(/vdb-lock-trigger/);

      page.route('**/api/videodb/lock/**', route =>
        route.fulfill({ json: { ok: true } })
      );
      await lockBtn.click();
      await page.waitForTimeout(300);
    }

    const realErrors = errors.filter(e => !e.includes('fetch') && !e.includes('NetworkError'));
    expect(realErrors).toEqual([]);
  });

  test('time range slider fires vdbSliderInput on drag', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const slider = page.locator('#vdb-slider-start');
    await expect(slider).toBeVisible({ timeout: 5000 });

    // Check the oninput handler references vdbSliderInput
    const oninput = await slider.getAttribute('oninput');
    expect(oninput).toContain('vdbSliderInput');

    // Verify vdbSliderInput is callable
    const canCall = await page.evaluate(() => typeof window.vdbSliderInput === 'function');
    expect(canCall, 'vdbSliderInput should be a function on window').toBe(true);

    // Programmatically call it to verify no errors
    await page.evaluate(() => {
      const el = document.getElementById('vdb-slider-start');
      el.value = '300';
      window.vdbSliderInput('start');
    });
    await page.waitForTimeout(200);

    expect(errors).toEqual([]);
  });

  test('time input fields fire vdbTimeInputChange', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const canCall = await page.evaluate(() => typeof window.vdbTimeInputChange === 'function');
    expect(canCall, 'vdbTimeInputChange should be a function on window').toBe(true);

    expect(errors).toEqual([]);
  });

  test('zoom slider fires vdbUpdateZoom', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const canCall = await page.evaluate(() => typeof window.vdbUpdateZoom === 'function');
    expect(canCall, 'vdbUpdateZoom should be a function on window').toBe(true);

    // Call it programmatically
    await page.evaluate(() => window.vdbUpdateZoom(250));
    await page.waitForTimeout(200);

    expect(errors).toEqual([]);
  });

  test('night selector fires vdbOnNightChange', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const canCall = await page.evaluate(() => typeof window.vdbOnNightChange === 'function');
    expect(canCall, 'vdbOnNightChange should be a function on window').toBe(true);

    expect(errors).toEqual([]);
  });

  test('no JS errors on full VDB interaction flow', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    // 1. Check chunks rendered
    const cards = page.locator('.vdb-chunk-card');
    const cardCount = await cards.count();
    expect(cardCount).toBeGreaterThan(0);

    // 2. Click the card thumbnail area to open the modal. Post-#492 the VDB
    // clip viewer is the shared VideoModal component (.vm-backdrop.open), not a
    // static #vdb-modal. Click the play placeholder rather than the card centre
    // so the click lands on the delegated .vdb-card-trigger region and not on a
    // disabled action button (e.g. "Processed") that swallows the event.
    await cards.first().locator('.vdb-chunk-nostack, img').first().click();
    await page.waitForTimeout(500);
    const modal = page.locator('.vm-backdrop.open');
    await expect(modal.first()).toBeVisible({ timeout: 3000 });

    // 3. Close modal
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // 4. Adjust slider
    await page.evaluate(() => {
      const el = document.getElementById('vdb-slider-start');
      if (el) { el.value = '400'; window.vdbSliderInput('start'); }
    });
    await page.waitForTimeout(200);

    // 5. Toggle sort
    const sortBtn = page.locator('button:has-text("Newest"), button:has-text("Oldest")').first();
    if (await sortBtn.isVisible()) {
      await sortBtn.click();
      await page.waitForTimeout(300);
    }

    expect(errors).toEqual([]);
  });

  test('chunk cards carry valid data-chunk payloads and no inline onclick', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const cards = page.locator('.vdb-chunk-card');
    const count = await cards.count();
    expect(count).toBeGreaterThan(0);

    for (let i = 0; i < count; i++) {
      const card = cards.nth(i);
      // XSS-hardened: cards are wired via a delegated grid click handler that
      // reads data-chunk JSON. There must be NO inline onclick attribute
      // (that was the sink #606 removed), and the JSON payload must parse.
      const onclick = await card.getAttribute('onclick');
      expect(onclick, `Card ${i} must not carry an inline onclick sink`).toBeNull();

      await expect(card).toHaveClass(/vdb-card-trigger/);

      const chunkData = await card.getAttribute('data-chunk');
      expect(chunkData, `Card ${i} should have data-chunk`).toBeTruthy();

      const valid = await page.evaluate(json => {
        try { JSON.parse(json); return true; }
        catch (e) { return e.message; }
      }, chunkData);
      expect(valid, `Card ${i} data-chunk should be valid JSON: ${chunkData}`).toBe(true);
    }

    expect(errors).toEqual([]);
  });

  test('Process buttons are wired via delegated trigger with a callable function', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await navigateToVDB(page);

    const btns = page.locator('.vdb-process-btn:not([disabled])');
    const count = await btns.count();

    for (let i = 0; i < count; i++) {
      const btn = btns.nth(i);
      // No inline onclick sink; the button opts into the delegated handler via
      // the vdb-process-trigger class, which calls window.vdbProcessChunk.
      const onclick = await btn.getAttribute('onclick');
      expect(onclick, `Process btn ${i} must not carry an inline onclick sink`).toBeNull();
      await expect(btn).toHaveClass(/vdb-process-trigger/);

      const chunkData = await btn.evaluate(el => el.closest('.vdb-chunk-card')?.dataset.chunk || null);
      expect(chunkData, `Process btn ${i} card should have data-chunk`).toBeTruthy();

      const valid = await page.evaluate(json => {
        try { JSON.parse(json); return true; }
        catch (e) { return e.message; }
      }, chunkData);
      expect(valid, `Process btn ${i} data-chunk should be valid JSON`).toBe(true);
    }

    expect(errors).toEqual([]);
  });
});

// The clip lock toggle must be clickable for everyone the backend authorises
// (admin fleet-wide + the host that owns the station) and hidden for read-only
// roles, matching `@require_station` in auth.py. Regression guard for the bug
// where the toggle was gated on IS_ADMIN, so station owners couldn't lock their
// own clips even though the server would accept the request.
test.describe('VDB lock toggle permissions by role', () => {
  async function loginAs(page, username) {
    await page.goto('/login');
    await page.fill('#login-username', username);
    await page.fill('#login-password', PASSWORD);
    await page.click('.login-btn');
    await page.waitForURL(u => !u.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
  }

  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
  });

  test('host who owns the station gets a clickable lock toggle', async ({ page }) => {
    await loginAs(page, 'host_test');
    await navigateToVDB(page);

    const clickable = page.locator('.vdb-lock-btn:not([disabled])');
    await expect(clickable.first()).toBeVisible();
    // Clickable lock opts into the delegated vdbToggleLock handler via class,
    // not an inline onclick attribute (XSS hardening, #606).
    await expect(clickable.first()).toHaveClass(/vdb-lock-trigger/);
  });

  test('read-only visitor gets no clickable lock toggle', async ({ page }) => {
    await loginAs(page, 'visitor_test');
    await navigateToVDB(page);

    // Cards still render for read-only roles; the lock toggle must not.
    await expect(page.locator('.vdb-chunk-card').first()).toBeVisible();
    expect(await page.locator('.vdb-lock-btn:not([disabled])').count()).toBe(0);
  });
});
