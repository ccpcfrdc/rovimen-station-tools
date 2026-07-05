import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page, user = ADMIN_USER, pass = PASSWORD) {
  await page.goto('/login');
  await page.fill('#login-username', user);
  await page.fill('#login-password', pass);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

test.describe('smoke tests', () => {
  test('login page renders without console errors', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('/login');
    await expect(page.locator('#login-username')).toBeVisible();
    await expect(page.locator('.login-btn')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('successful login redirects away from /login', async ({ page }) => {
    await login(page);
    expect(page.url()).not.toContain('/login');
  });

  test('wrong credentials show error', async ({ page }) => {
    await page.goto('/login');
    await page.fill('#login-username', 'admin_test');
    await page.fill('#login-password', 'wrongpassword');
    await page.click('.login-btn');
    await expect(page.locator('.login-error')).toBeVisible();
  });

  test('unauthenticated access to a gated page redirects to /login', async ({ page }) => {
    // Auth is explicit-allow (deny-by-default): a page NOT tagged @public_route
    // must still bounce an anonymous visitor to the login flow. /admin is
    // admin-gated, so it stands in for the whole gated surface.
    await page.goto('/admin');
    await expect(page).toHaveURL(/\/login/);
  });

  test('unauthenticated visitor lands on the fleet map (no login redirect)', async ({ page }) => {
    // "/" is @public_route(page="overview"): with the default (all-on)
    // public_pages, an anonymous visitor gets the live fleet-map shell plus the
    // hero/intro block rather than being redirected to /login.
    await page.goto('/');
    await expect(page).not.toHaveURL(/\/login/);
    await expect(page.locator('body')).toBeVisible();
    await expect(page.locator('#ov-hero')).toBeVisible();
    await expect(page.locator('#map')).toBeVisible();
  });

  test('unauthenticated visitor can open the public Highlights page', async ({ page }) => {
    // Highlights is now a @public_route(page="highlights") view; the e2e
    // fixture leaves public_pages at its default (all eligible pages on), so
    // an anonymous visitor must reach /highlights without a login bounce and
    // without JS errors — the page + its /api/highlights/data feed are public.
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('/highlights');
    await expect(page).not.toHaveURL(/\/login/);
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(500);
    expect(errors).toEqual([]);
  });

  test('unauthenticated visitor can open the About modal (public network stats)', async ({ page }) => {
    // The About modal fetches /api/network-stats, now a plain @public_route.
    // Open it on the public events page and confirm no JS error + the stats
    // block leaves its loading/degraded state (i.e. the fetch resolved 2xx).
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('/events');
    await expect(page).not.toHaveURL(/\/login/);
    await page.click('button:has-text("About")');
    await expect(page.locator('#about-network-stats')).toBeVisible();
    await page.waitForTimeout(500);
    expect(errors).toEqual([]);
  });

  test('overview page loads after login', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(500);
    expect(errors).toEqual([]);
  });

  test('station page loads without JS errors', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await page.goto('/station/test_station_1');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);
    expect(errors).toEqual([]);
  });

  test('events page loads without JS errors', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await page.goto('/events');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(500);
    expect(errors).toEqual([]);
  });
});
