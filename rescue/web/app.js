'use strict';
const $ = id => document.getElementById(id);
let secret = '', current = null, rows = [], offset = 0, selected = new Set(), fetching = false, actionBusy = false;
let loadedSnapshot = null, searchTimer, refreshTimer;
const labels = {pending:'В очереди',active:'Скачивается',deferred:'Повторим',verified_md5:'Проверен · MD5',verified_size:'Проверен · размер',preview_saved:'Сохранено',preview_absent:'Не предоставлено',conflict:'Локальный конфликт'};
const errors = {auth:'Доступ истёк или отклонён. Приостановите работу и подключите актуальный токен.',auth_required:'Сначала подключите Яндекс.Диск.',invalid_response:'Сервис вернул неполный ответ. Повторите получение каталога позже.',unavailable:'Яндекс пока не отдаёт данные. Повторите попытку позже.',timeout:'Сервер не ответил вовремя. Можно повторить попытку.',throttled:'Яндекс ограничил запросы. Приложение выдержит паузу.',missing_volume:'Накопитель не найден. Подключите выбранный диск.',wrong_volume:'По этому пути обнаружен другой том. Верните выбранный накопитель.',unsafe_path:'Небезопасный путь назначения. Проверьте папку и символические ссылки.',no_space:'Не хватает свободного места с резервом 1 ГиБ. Освободите место на диске.',disk_io:'Ошибка записи. Проверьте диск перед продолжением.',state_error:'Не удалось сохранить состояние. Остановите процесс и проверьте каталог данных.',different_account:'Этот каталог восстановления связан с другим аккаунтом. Используйте отдельный --data-dir.',different_destination:'Очередь связана с другим назначением. Выберите прежнюю папку или отдельный --data-dir.',destination_busy:'Этот диск уже используется другим процессом восстановления.',pause_required:'Сначала приостановите восстановление и дождитесь завершения записей.',writers_active:'Завершаются активные передачи. Дождитесь подтверждения паузы.',confirmation_required:'Подтвердите подключение прежнего диска кнопкой «Продолжить».',destination_required:'Сначала выберите и проверьте папку назначения.',invalid_selection:'Выбранные файлы не принадлежат этому снимку. Обновите список.',scan_active:'Каталог уже загружается.',operation_failed:'Не удалось выполнить действие. Проверьте подключение и повторите.',network_io:'Передача оборвалась. Файл будет запрошен повторно.',checksum_mismatch:'Контрольная сумма не совпала. Копия не принята.',size_mismatch:'Размер не совпал. Копия не принята.',cancelled:'Передача приостановлена.',local_conflict:'Локальный файл сохранён, требуется другая копия.',internal_error:'Восстановление остановлено из-за внутренней ошибки.',invalid_input:'Проверьте введённые данные.'};
const explain = code => errors[code] || 'Действие пока недоступно. Обновите состояние и повторите.';
const count = (kind, state) => current?.counts[kind]?.[state] || 0;
function bytes(n) { if (!Number.isFinite(n)) return '—'; const u=['Б','КБ','МБ','ГБ','ТБ']; let i=0; while(n>=1000&&i<4){n/=1000;i++;} return new Intl.NumberFormat('ru-RU',{maximumFractionDigits:i?1:0}).format(n)+' '+u[i]; }
function text(id,value){ $(id).textContent=value; }
function message(value,isError=false){text('operation-message',value);$('operation-message').hidden=!value;$('operation-message').classList.toggle('error',isError);}
async function api(path, data) {
  const headers={'X-Session-Secret':secret}; const options={headers,cache:'no-store'};
  if(data!==undefined){headers['Content-Type']='application/json';options.method='POST';options.body=JSON.stringify(data);}
  const response=await fetch(path,options);
  if(!response.ok){let error={};try{error=await response.json();}catch{}throw new Error(explain(error.error));}
  return response.json();
}
async function command(path,data={}){
  if(actionBusy)return;
  actionBusy=true;render();
  try{await api(path,data);message('Действие принято. Обновляем состояние…');await refresh();}
  catch(e){message(e.message,true);}
  finally{actionBusy=false;render();}
}
function render(){
  if(!current)return;
  const s=current, busy=actionBusy||s.operation?.state==='running', writing=s.active_jobs>0;
  text('connection-state',s.connected?'Подключён':'Не подключён');$('connection-state').classList.toggle('ready',s.connected);
  text('destination-state',s.destination&&!s.volume_error?'Проверен':'Ожидает диск');$('destination-state').classList.toggle('ready',!!s.destination&&!s.volume_error);
  if(s.destination&&document.activeElement!==$('destination'))$('destination').value=s.destination;
  text('free-space',s.free_bytes!==null?'Свободно '+bytes(s.free_bytes):'Выберите существующую папку на диске');
  text('catalog-bytes',bytes(s.catalog_bytes));text('catalog-count',s.catalog_files.toLocaleString('ru-RU')+' файлов / версий');text('files-count',s.catalog_files.toLocaleString('ru-RU'));
  text('verified-bytes',bytes(s.verified_bytes));text('verified-count',(count('original','verified_md5')+count('original','verified_size')).toLocaleString('ru-RU')+' проверенных файлов');
  text('preview-count',count('preview','preview_saved').toLocaleString('ru-RU'));text('deferred-count',(count('original','deferred')+count('preview','deferred')).toLocaleString('ru-RU'));
  const percent=s.catalog_bytes>0?Math.min(100,100*s.verified_bytes/s.catalog_bytes):(s.catalog_files&&count('original','verified_md5')+count('original','verified_size')===s.catalog_files?100:0);
  $('progress').value=percent;text('progress-percent',Math.floor(percent)+'%');
  text('progress-text',s.catalog_files?'Проверено '+bytes(s.verified_bytes)+' из '+bytes(s.catalog_bytes)+' известных оригиналов':'Подключите Диск и получите каталог');
  text('speed',s.running&&writing?bytes(s.speed_bps)+'/с':'—');text('transferred','Передано за сеанс: '+bytes(s.transferred_bytes));
  const complete=s.pause_reason==='complete', pausing=!s.running&&writing;
  text('run-badge',s.scanning?'Получаем каталог':pausing?'Завершаем записи':s.running?'Восстановление':complete?'Готово':s.pause_reason&&s.pause_reason!=='user'?'Требуется действие':'Подготовка');
  $('run-badge').classList.toggle('active',s.running||complete);
  text('recovery-heading',s.running?'Сохраняем доступные файлы':complete?'Все задания очереди обработаны':pausing?'Дожидаемся остановки записей':'Готовим безопасную копию');
  text('activity-text',s.retry_in>0?'Пауза запросов: '+Math.ceil(s.retry_in)+' с':writing?'Активных передач: '+s.active_jobs:s.running?'Ожидаем заданий и повторных попыток':complete?'Оригиналы и превью учтены раздельно':'Ожидает запуска');
  text('disk-safety',s.safe_to_disconnect?'Активных записей нет':pausing?'Не отключайте диск до остановки записей':'Перед отключением диска нажмите «Приостановить»');
  text('snapshot-info',s.snapshot?(s.snapshot.complete?'Последний каталог получен.':'Каталог неполный — можно сохранять уже известные файлы.')+' Предыдущие наблюдения сохраняются.':'Сначала получим список того, что доступно в облаке.');
  $('connect-button').disabled=$('configured-token').disabled=busy||s.running||writing||s.scanning;
  $('destination-button').disabled=busy||s.running||writing;
  $('scan-button').disabled=busy||!s.connected||s.scanning;
  $('scan-button').textContent=s.scanning?'Получаем каталог…':s.snapshot&&!s.snapshot.complete?'Продолжить каталог':'Получить каталог';
  const queued=Object.values(s.counts.original).reduce((a,b)=>a+b,0)+Object.values(s.counts.preview).reduce((a,b)=>a+b,0);
  const resumable=!s.running&&!pausing&&!!s.destination&&queued>0&&!complete;
  $('start-button').hidden=s.running||pausing||resumable;
  $('start-button').disabled=busy||!s.connected||!s.destination||!!s.volume_error||!s.catalog_files;
  $('pause-button').hidden=!s.running&&!s.scanning&&!pausing;$('pause-button').disabled=pausing;
  $('resume-button').hidden=!resumable;
  $('resume-button').disabled=busy||!s.connected;
  $('previews').disabled=s.running||writing||resumable;
  if(s.running||resumable)$('previews').checked=s.previews;
  $('export-button').disabled=!s.catalog_files;
  $('selection-button').disabled=busy||s.running||writing||!s.connected||!s.destination||!!s.volume_error||!selected.size;
  text('selection-button','Восстановить выбранные ('+selected.size+')');
  $('fixture-label').hidden=!s.fixture;
  if(s.operation?.state==='failed')message(explain(s.operation.error),true);
  else if(s.pause_reason&&!['user','complete'].includes(s.pause_reason))message(explain(s.pause_reason),true);
  else if(s.scan_error)message(explain(s.scan_error),true);
  else if(s.operation?.state==='running')message('Выполняем: '+({connect:'проверка доступа',destination:'проверка диска',catalog:'получение каталога',start:'запуск',resume:'продолжение'}[s.operation.kind]||'действие')+'…');
  else if(s.operation?.state==='done')message('');
}
function badge(state){const span=document.createElement('span');span.className='status-tag'+(['verified_md5','verified_size','preview_saved'].includes(state)?' saved':state==='deferred'?' waiting':'');span.textContent=labels[state]||'—';return span;}
function renderRows(){
  const body=$('file-rows');body.replaceChildren();
  for(const row of rows){
    const tr=document.createElement('tr'),check=document.createElement('td');check.className='check-cell';
    const input=document.createElement('input');input.type='checkbox';input.checked=selected.has(row.id);input.setAttribute('aria-label','Выбрать '+row.path);
    input.addEventListener('change',()=>{if(input.checked)selected.add(row.id);else selected.delete(row.id);render();});check.append(input);tr.append(check);
    const name=document.createElement('td');name.className='file-name';name.title=row.path+(row.error_code?' · '+explain(row.error_code):'')+(row.preview_error?' · Превью: '+explain(row.preview_error):'');
    const icon=document.createElement('span');icon.className='file-icon';icon.textContent='▧';name.append(icon,document.createTextNode(row.path.split('/').pop()||row.path));
    const path=document.createElement('span');path.className='file-path';path.textContent=row.path;name.append(path);
    if(row.local_original||row.local_preview){const local=document.createElement('span');local.className='file-path';local.textContent=([row.original_status,row.preview_status].some(s=>['verified_md5','verified_size','preview_saved'].includes(s))?'Копия: ':'Назначение: ')+(row.local_original||row.local_preview);local.title=local.textContent;name.append(local);}
    tr.append(name);
    const size=document.createElement('td');size.textContent=bytes(row.size);tr.append(size);
    for(const state of [row.original_status,row.preview_status]){const td=document.createElement('td');td.append(badge(state));tr.append(td);}
    const attempts=document.createElement('td');attempts.textContent=row.attempts||'—';tr.append(attempts);body.append(tr);
  }
  $('empty-state').hidden=rows.length>0;
  $('previous-page').disabled=offset===0;$('next-page').disabled=rows.length<100;
  text('page-info',rows.length?'Показаны '+(offset+1)+'–'+(offset+rows.length)+' · последний снимок':'Нет файлов по выбранным условиям');
  $('select-page').checked=rows.length>0&&rows.every(r=>selected.has(r.id));render();
}
async function loadRows(){
  if(!current?.snapshot){rows=[];renderRows();return;}
  const sid=current.snapshot.id;
  if(loadedSnapshot!==sid){selected.clear();offset=0;loadedSnapshot=sid;}
  const params=new URLSearchParams({offset,limit:100,query:$('search').value,state:$('state-filter').value,snapshot_id:sid});
  const data=await api('/api/files?'+params);rows=data.files;renderRows();
}
async function refresh(){
  if(fetching)return;
  fetching=true;
  try{current=await api('/api/status');$('network-error').hidden=true;render();await loadRows();}
  catch(e){$('network-error').hidden=false;}
  finally{fetching=false;}
}
$('connect-form').addEventListener('submit',e=>{e.preventDefault();const token=$('token').value;$('token').value='';command('/api/connect',{token});});
$('configured-token').addEventListener('click',()=>command('/api/connect',{}));
$('destination-form').addEventListener('submit',e=>{e.preventDefault();command('/api/destination',{path:$('destination').value.trim()});});
$('scan-button').addEventListener('click',()=>command('/api/catalog'));
$('start-button').addEventListener('click',()=>command('/api/start',{previews:$('previews').checked}));
$('pause-button').addEventListener('click',()=>command('/api/pause'));
$('resume-button').addEventListener('click',()=>command('/api/resume',{confirm_destination:true}));
$('selection-button').addEventListener('click',()=>command('/api/start',{previews:$('previews').checked,file_ids:[...selected],snapshot_id:loadedSnapshot}));
$('select-page').addEventListener('change',()=>{for(const r of rows){if($('select-page').checked)selected.add(r.id);else selected.delete(r.id);}renderRows();});
$('previous-page').addEventListener('click',()=>{offset=Math.max(0,offset-100);refresh();});$('next-page').addEventListener('click',()=>{offset+=100;refresh();});
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{offset=0;refresh();},250);});
$('state-filter').addEventListener('change',()=>{offset=0;refresh();});
$('export-button').addEventListener('click',async()=>{try{const response=await fetch('/api/export.csv',{headers:{'X-Session-Secret':secret}});if(!response.ok)throw new Error('Не удалось экспортировать отчёт.');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='disk-rescue-report.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){message(e.message,true);}});
async function boot(){try{const response=await fetch('/api/bootstrap',{cache:'no-store'});if(!response.ok)throw new Error();secret=(await response.json()).secret;await refresh();refreshTimer=setTimeout(tick,2000);}catch{$('network-error').hidden=false;setTimeout(boot,2000);}}
async function tick(){await refresh();refreshTimer=setTimeout(tick,2000);}
boot();
