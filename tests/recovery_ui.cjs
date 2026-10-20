// NODE_PATH=<directory containing Playwright> node tests/recovery_ui.cjs
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
const {createServer}=require('node:http');
const {join}=require('node:path');
const assert=require('node:assert/strict');

(async()=>{
  const server=createServer((req,res)=>{
    const file=req.url==='/'?'index.html':req.url.slice(1);
    res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(readFileSync(join(__dirname,'../rescue/web',file)));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,channel:'chrome'});
    const page=await browser.newPage();
    const status={transfer_settings:{workers:4,timeout:12,request_interval:0,catalog_auto_retry:false},connected:true,destination:'/Volumes/Archive/Rescue',volume_error:'',free_bytes:240000000000,
      counts:{original:{verified_size:1,pending:1},preview:{}},catalog_files:1000,catalog_bytes:9000,verified_bytes:10,
      active_jobs:0,transferred_bytes:0,speed_bps:0,running:false,scanning:false,retry_in:0,safe_to_disconnect:true,
      previews:false,snapshot:{id:1,complete:true},pause_reason:'user',selection_count:2,folder_selection:{included:['/Фото']}};
    const files=[{id:1,path:'/Фото/a.jpg',size:10,original_status:'verified_size',preview_status:null,attempts:1},
      {id:2,path:'/Фото/Семья/b.jpg',size:20,original_status:'pending',preview_status:null,attempts:0}];
    const summary={total:2,bytes:30,saved:1,saved_bytes:10,pending:1,active:0,errors:0};
    const posts=[],errors=[];
    let supported=true,offline=false;
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/api/**',async route=>{
      const req=route.request(),url=new URL(req.url());
      if(req.method()==='POST'){posts.push(url.pathname);if(url.pathname==='/api/settings')status.transfer_settings=req.postDataJSON();}
      if(offline&&url.pathname==='/api/status'){await route.abort();return;}
      let json=status;
      if(url.pathname==='/api/bootstrap')json={secret:'test'};
      if(url.pathname==='/api/media')json={mime:'image/png',kind:'preview',url:'/media-test',path:'/Фото/a.jpg'};
      if(url.pathname==='/api/recovery'){
        if(!supported){await route.fulfill({status:404,json:{error:'not_found'}});return;}
        const parent=url.searchParams.get('parent');
        const matching=files.filter(f=>(!url.searchParams.get('state')||f.original_status===url.searchParams.get('state'))&&f.path.includes(url.searchParams.get('query')||''));
        json={...summary,total:matching.length,folders:[],files:matching,file_total:matching.length};
        if(parent==='/'){json.folders=matching.length?[{...summary,path:'/Фото'}]:[];json.files=[];json.file_total=0;}
        if(parent==='/Фото'){json.folders=[{...summary,total:1,path:'/Фото/Семья',saved:0}];json.files=matching.filter(f=>f.id===1);json.file_total=json.files.length;}
        if(parent==='/Фото/Семья'){json.folders=[];json.files=matching.filter(f=>f.id===2);json.file_total=json.files.length;}
      }
      if(url.pathname==='/api/folders')json={folders:[{path:'/',files:1000,bytes:9000},{path:'/Фото',files:2,bytes:30}]};
      await route.fulfill({json});
    });
    await page.route('**/media-test',route=>route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=','base64')}));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#recovery-tree summary').first().waitFor();
    await page.locator('#disk-status').waitFor();
    assert.match(await page.locator('#disk-status').innerText(),/Яндекс.Диск подключён/);
    status.running=true;status.active_jobs=4;status.retry_in=38;
    await page.waitForTimeout(2200);
    assert.match(await page.locator('#disk-status').innerText(),/Скачиваем: 4/);
    assert.match(await page.locator('#disk-status').innerText(),/повтор через 38 с/);
    await page.setViewportSize({width:1280,height:500});
    await page.locator('#disk-status').scrollIntoViewIfNeeded();
    await page.evaluate(()=>scrollTo({top:scrollY+document.getElementById('disk-status').getBoundingClientRect().top+100,behavior:'instant'}));
    assert.equal(Math.round(await page.locator('#disk-status').evaluate(el=>el.getBoundingClientRect().top)),8,'status remains pinned during scroll');
    status.running=false;status.active_jobs=0;status.retry_in=0;
    status.connected=false;status.pause_reason='auth';
    await page.waitForTimeout(2200);
    assert.match(await page.locator('#disk-status').innerText(),/обновите токен/);
    offline=true;
    await page.waitForTimeout(2200);
    assert.match(await page.locator('#disk-status').innerText(),/Статус Яндекс.Диска неизвестен/);
    offline=false;status.connected=true;status.pause_reason='user';status.scan_error='timeout';
    await page.waitForTimeout(2200);
    assert.match(await page.locator('#disk-status').innerText(),/Каталог: тайм-аут · нужен повтор/);
    status.scan_error='';
    const choice=page.getByRole('checkbox',{name:'Выбрать папку /Фото',exact:true});
    await choice.waitFor();
    await choice.check();
    assert.match(await page.locator('#folder-total').innerText(),/2 файлов/);
    assert.equal(await page.locator('#folder-start').isEnabled(),true);
    assert.equal(await page.locator('#tree-view').getAttribute('aria-pressed'),'true');
    assert.match(await page.locator('#results-destination').innerText(),/Archive\/Rescue/);
    assert.equal(await page.locator('#files-count').innerText(),'2','results do not count the 1000 cloud files');
    await page.locator('#recovery-tree summary').first().click();
    await page.getByText('a.jpg',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Просмотр /Фото/a.jpg',exact:true}).click();
    await page.locator('#media-image').waitFor();
    assert.match(await page.locator('#media-caption').innerText(),/превью/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#media-dialog').isVisible(),false);
    await page.waitForFunction(()=>document.getElementById('media-image').getAttribute('src')===null);
    await page.locator('#flat-view').click();
    await page.locator('#file-rows tr').first().waitFor();
    assert.equal(await page.locator('#file-rows tr').count(),2);
    assert.match(await page.locator('#file-rows').innerText(),/Фото\/Семья\/b.jpg/);
    await page.locator('#state-filter').selectOption('pending');
    await page.waitForTimeout(400);
    assert.equal(await page.locator('#file-rows tr').count(),1);
    await page.locator('#state-filter').selectOption('');
    await page.locator('#tree-view').click();
    await page.waitForTimeout(2200);
    assert.equal(await page.locator('#recovery-tree details').first().getAttribute('open'),'','polling retains expanded folders');
    assert.deepEqual(posts,[],'view changes never start, pause or change recovery');
    await page.locator('#advanced-settings summary').click();
    await page.locator('#settings-workers').fill('2');
    await page.waitForTimeout(2200);
    assert.equal(await page.locator('#settings-workers').inputValue(),'2','polling preserves unapplied settings');
    await page.locator('#settings-apply').click();
    await page.waitForFunction(()=>document.getElementById('settings-message').textContent.startsWith('Сохранено'));
    assert.equal(status.transfer_settings.workers,2);
    assert.deepEqual(posts,['/api/settings']);
    await page.locator('#settings-defaults').click();
    assert.equal(await page.locator('#settings-workers').inputValue(),'4');
    assert.equal(status.transfer_settings.workers,2,'reset does not silently apply');
    await page.screenshot({path:'/tmp/disk-rescue-results-tree.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    supported=false;
    await page.reload();
    await page.locator('#results-compatibility').waitFor();
    assert.equal(await page.locator('#network-error').isVisible(),false,'old running server remains usable');
    assert.deepEqual(errors,[]);
    console.log('Recovery UI: all checks passed');
  }finally{if(browser)await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
