// Run with Node and Playwright available in NODE_PATH.
const {chromium} = require('playwright');
const {readFileSync} = require('node:fs');
const {createServer} = require('node:http');
const {join} = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const server = createServer((req, res) => {
    const file = req.url === '/' ? 'index.html' : req.url.slice(1);
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(readFileSync(join(__dirname, '../rescue/web', file)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true, channel:'chrome'});
    const page = await browser.newPage();
    const status = {connected:false, destination:null, volume_error:'', free_bytes:null,
      counts:{original:{},preview:{}}, catalog_files:0, catalog_bytes:0, verified_bytes:0,
      active_jobs:0, transferred_bytes:0, speed_bps:0, running:false, scanning:false,
      retry_in:0, safe_to_disconnect:true, previews:true, snapshot:null, pause_reason:null};
    await page.route('**/api/**', route => route.fulfill({json:route.request().url().endsWith('/bootstrap') ? {secret:'test'} : status}));
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const visible = id => page.locator('#'+id).isVisible();
    await page.waitForTimeout(100);
    assert.equal(await visible('setup-fields'), true, 'unconfigured setup stays expanded');
    status.connected = true;
    await page.waitForTimeout(2200);
    assert.equal(await visible('token'), false, 'connected account collapses before destination is ready');
    assert.equal(await visible('destination'), true, 'destination remains editable');
    await page.locator('#edit-connection').click();
    assert.equal(await visible('token'), true, 'connection can be edited independently');
    await page.locator('#token').fill('fixture-token');
    status.operation = {kind:'connect',state:'done',id:1};
    await page.locator('#connect-button').click();
    await page.waitForTimeout(100);
    assert.equal(await visible('token'), false, 'successful reconnect collapses the token form');
    status.destination = '/Volumes/Archive/Rescue';
    status.free_bytes = 240000000000;
    await page.waitForTimeout(2200);
    assert.equal(await visible('setup-fields'), false, 'validated setup collapses');
    assert.match(await page.locator('#setup-summary').innerText(), /Archive\/Rescue/);
    await page.locator('#edit-setup').click();
    await page.locator('#destination').fill('/Volumes/Other');
    await page.waitForTimeout(2200);
    assert.equal(await visible('setup-fields'), true, 'polling preserves editing');
    assert.equal(await page.locator('#destination').inputValue(), '/Volumes/Other');
    await page.locator('#finish-setup').click();
    assert.equal(await visible('setup-fields'), true, 'unverified changes cannot be collapsed');
    await page.locator('#destination').fill(status.destination);
    await page.locator('#finish-setup').click();
    assert.equal(await visible('setup-fields'), false);
    status.volume_error = 'missing_volume';
    await page.waitForTimeout(2200);
    assert.equal(await visible('setup-fields'), true, 'missing disk reveals setup');
    status.volume_error = '';
    await page.reload();
    await page.waitForTimeout(100);
    assert.equal(await visible('setup-fields'), false, 'ready setup starts compact after reload');
    assert.equal(await page.evaluate(() => document.querySelector('#setup').getBoundingClientRect().top < document.querySelector('.metrics').getBoundingClientRect().top), true, 'setup precedes results');
    await page.screenshot({path:'/tmp/disk-rescue-setup-ready.png',fullPage:true});
    status.connected = false;
    await page.waitForTimeout(2200);
    assert.equal(await visible('setup-fields'), true, 'lost authentication reveals setup');
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'mobile layout fits viewport');
    await page.screenshot({path:'/tmp/disk-rescue-setup-mobile.png',fullPage:true});
    assert.deepEqual(errors, []);
    console.log('Setup UI: all checks passed');
  } finally {
    if (browser) await browser.close();
    server.close();
  }
})().catch(e => {console.error(e);process.exitCode=1;});
