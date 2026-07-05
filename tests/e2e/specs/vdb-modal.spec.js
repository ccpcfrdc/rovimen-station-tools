import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const MOCK_NIGHTS = ['20260601'];
const MOCK_CHUNKS = [
  {
    filename: 'TST001_20260601_213000_color.mkv',
    time: '21:30:00',
    size_mb: 12.5,
    meteor_time: null,
    stack: null,
  },
  {
    filename: 'TST001_20260601_220000_color.mkv',
    time: '22:00:00',
    size_mb: 10.2,
    meteor_time: null,
    stack: null,
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
  page.route('**/api/cached-video/**', route =>
    route.fulfill({
      status: 200,
      contentType: 'video/mp4',
      body: Buffer.alloc(0),
    })
  );
}

test.describe('VDB modal lifecycle', () => {
  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
    await login(page);
  });

  test('VDB tab shows camera buttons and clip grid', async ({ page }) => {
    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    const camBtns = page.locator('#pane-videodb .cam-btn');
    await expect(camBtns.first()).toBeVisible({ timeout: 5000 });
    const count = await camBtns.count();
    expect(count).toBe(2);
  });

  test('no console errors on VDB tab with mock data', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(1000);

    expect(errors).toEqual([]);
  });

  test('clicking a clip opens the modal without errors', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(500);

    const clip = page.locator('.vdb-tile').first();
    if (await clip.isVisible()) {
      await clip.click();
      await page.waitForTimeout(300);

      const modal = page.locator('#vdb-modal');
      await expect(modal).toBeVisible({ timeout: 2000 });
    }

    expect(errors).toEqual([]);
  });

  test('closing and reopening modal does not throw', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(500);

    const clips = page.locator('.vdb-tile');
    if (await clips.first().isVisible()) {
      await clips.first().click();
      await page.waitForTimeout(300);

      await page.keyboard.press('Escape');
      await page.waitForTimeout(200);

      await clips.first().click();
      await page.waitForTimeout(300);
    }

    expect(errors).toEqual([]);
  });

  test('switching cameras in VDB does not throw', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(500);

    const cam2 = page.locator('#pane-videodb .cam-btn').nth(1);
    if (await cam2.isVisible()) {
      await cam2.click();
      await page.waitForTimeout(500);
    }

    expect(errors).toEqual([]);
  });

  test('switching between all station tabs does not throw', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.waitForLoadState('domcontentloaded');

    const tabIds = ['tab-btn-videodb', 'tab-btn-fdp', 'tab-btn-archive',
                    'tab-btn-settings', 'tab-btn-rms'];
    for (const id of tabIds) {
      await page.click(`#${id}`);
      await page.waitForTimeout(300);
    }

    expect(errors).toEqual([]);
  });
});
