'use strict';
// Apply before the first paint; browser storage may be unavailable in private contexts.
let savedTheme = 'sage';
try { if (localStorage.getItem('disk-rescue-theme') === 'dark') savedTheme = 'dark'; } catch {}
document.documentElement.dataset.theme = savedTheme;
document.addEventListener('DOMContentLoaded', () => {
const themeSelect = document.getElementById('theme-select');
themeSelect.value = savedTheme;
themeSelect.addEventListener('change', () => {
  document.documentElement.dataset.theme = themeSelect.value;
  try { localStorage.setItem('disk-rescue-theme', themeSelect.value); } catch {}
});

const $ = id => document.getElementById(id);
let secret = '', current = null, rows = [], offset = 0, fetching = false, actionBusy = false;
let searchTimer, refreshTimer,serviceOnline=false;
let resultsView='tree',recoverySupported=true,resultsGeneration=0,resultsScope='',resultsTotal=0,recoverySummary=null;
const openRecoveryFolders=new Set(),recoveryOffsets=new Map();
let settingsDirty=false,settingsSaving=false;
const transferDefaults={workers:4,timeout:12,request_interval:0,catalog_auto_retry:false};
let setupEditing=false,connectionEditing=false,lastConnectionOperation=null;
let destinationDirty=false,pendingDestination=null,destinationBaseline=null;
const labels = {pending:'В очереди',active:'Скачивается',deferred:'Повторим',verified_md5:'Проверен · MD5',verified_size:'Проверен · размер',preview_saved:'Сохранено',preview_absent:'Не предоставлено',conflict:'Локальный конфликт'};
const errors = {catalog_changing:'Каталог меняется во время обхода. Наблюдения сохранены; продолжите получение каталога позже.',auth:'Доступ истёк или отклонён. Приостановите работу и подключите актуальный токен.',auth_required:'Сначала подключите Яндекс.Диск.',invalid_response:'Сервис вернул неполный ответ. Повторите получение каталога позже.',unavailable:'Яндекс пока не отдаёт данные. Повторите попытку позже.',timeout:'Сервер не ответил вовремя. Можно повторить попытку.',throttled:'Яндекс ограничил запросы. Приложение выдержит паузу.',missing_volume:'Накопитель не найден. Подключите выбранный диск.',wrong_volume:'По этому пути обнаружен другой том. Верните выбранный накопитель.',unsafe_path:'Небезопасный путь назначения. Проверьте папку и символические ссылки.',no_space:'Не хватает свободного места с резервом 1 ГиБ. Освободите место на диске.',disk_io:'Ошибка записи. Проверьте диск перед продолжением.',state_error:'Не удалось сохранить состояние. Остановите процесс и проверьте каталог данных.',different_account:'Этот каталог восстановления связан с другим аккаунтом. Используйте отдельный --data-dir.',different_destination:'Подтвердите смену папки назначения: прежние файлы и результаты останутся сохранены.',destination_busy:'Эта папка уже используется другим экземпляром Disk Rescue. Вернитесь в его вкладку или завершите тот экземпляр приложения.',pause_required:'Сначала приостановите восстановление и дождитесь завершения записей.',writers_active:'Завершаются активные передачи. Дождитесь подтверждения паузы.',confirmation_required:'Подтвердите подключение прежнего диска кнопкой «Продолжить».',destination_required:'Сначала выберите и проверьте папку назначения.',invalid_selection:'Выбранные файлы не принадлежат этому снимку. Обновите список.',scan_active:'Каталог уже загружается.',operation_failed:'Не удалось выполнить действие. Проверьте подключение и повторите.',network_io:'Передача оборвалась. Файл будет запрошен повторно.',checksum_mismatch:'Контрольная сумма не совпала. Копия не принята.',size_mismatch:'Размер не совпал. Копия не принята.',cancelled:'Передача приостановлена.',local_conflict:'Локальный файл сохранён, требуется другая копия.',internal_error:'Восстановление остановлено из-за внутренней ошибки.',invalid_input:'Проверьте введённые данные.'};
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
function fillSettings(value){
  $('settings-workers').value=value.workers;$('settings-timeout').value=value.timeout;
  $('settings-interval').value=value.request_interval;$('settings-catalog-retry').checked=value.catalog_auto_retry;
}
function renderSettings(){
  const value=current?.transfer_settings;
  $('settings-apply').disabled=!value||settingsSaving;
  if(!value){text('settings-summary','Потребуется обновление сервера');return;}
  if(!settingsDirty)fillSettings(value);
  text('settings-summary','Параллельно: '+value.workers+' · тайм-аут '+value.timeout+' с · '+(value.request_interval?'API: '+value.request_interval+' с':'API без паузы')+' · каталог: '+(value.catalog_auto_retry?'автоповтор':'вручную'));
}
$('settings-form').addEventListener('input',()=>{settingsDirty=true;text('settings-message','Есть неприменённые изменения');});
$('settings-defaults').addEventListener('click',()=>{fillSettings(transferDefaults);settingsDirty=true;text('settings-message','Значения сброшены. Нажмите «Применить».');});
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();if(settingsSaving)return;
  const chosen={workers:Number($('settings-workers').value),timeout:Number($('settings-timeout').value),request_interval:Number($('settings-interval').value),catalog_auto_retry:$('settings-catalog-retry').checked};
  settingsSaving=true;renderSettings();
  try{await api('/api/settings',chosen);settingsDirty=false;current.transfer_settings=chosen;text('settings-message','Сохранено. Текущие передачи продолжаются.');renderSettings();}
  catch(error){text('settings-message',error.message);}
  finally{settingsSaving=false;renderSettings();}
});
function renderDiskStatus(){
  const bar=$('disk-status'),s=current;
  let access='Проверяем состояние Яндекс.Диска…',action='',feedback='',state='neutral';
  if(!serviceOnline){
    access='Нет связи с локальным сервисом';action='Статус Яндекс.Диска неизвестен';state='error';
  }else if(s){
    const checking=s.operation?.state==='running'&&['connect','restore'].includes(s.operation.kind);
    access=s.connected?'Яндекс.Диск подключён':checking?'Проверяем доступ к Яндекс.Диску':s.pause_reason==='auth'?'Яндекс.Диск: обновите токен':'Яндекс.Диск не подключён';
    state=s.connected?'ready':checking?'neutral':'error';
    action=s.active_jobs>0?(s.running?'Скачиваем: '+s.active_jobs:'Завершаем передачи: '+s.active_jobs):s.running?'Ожидаем заданий или повторов':s.pause_reason==='complete'?'Очередь завершена':Object.values(s.counts.original).some(n=>n>0)?'Загрузка на паузе':'Ожидаем запуска';
    if(s.scanning)action=(s.active_jobs>0?action+' · ':'')+'Получаем каталог';
    if(s.catalog_retry_in>0){feedback='Каталог: повтор автоматически через '+Math.ceil(s.catalog_retry_in)+' с';state='waiting';}
    else if(s.retry_in>0){feedback='Пауза запросов · повтор через '+Math.ceil(s.retry_in)+' с';state='waiting';}
    else if(s.scan_error&&!s.scanning){feedback=s.scan_error==='timeout'?'Каталог: тайм-аут · нужен повтор':s.scan_error==='throttled'?'Каталог: ограничение запросов · нужен повтор':'Каталог остановлен · требуется действие';state='waiting';}
    else if(s.volume_error){feedback='Накопитель недоступен';state='error';}
    else if(s.operation?.state==='failed'){feedback=s.operation.error==='timeout'?'Последнее действие: тайм-аут · нужен повтор':'Последнее действие не выполнено';state='waiting';}
    if(!s.connected&&!checking)state='error';
  }
  text('disk-access',access);text('disk-action',action);text('disk-feedback',feedback);
  $('disk-action').hidden=!action;$('disk-feedback').hidden=!feedback;bar.dataset.state=state;
}
function render(){
  if(!current)return;
  renderDiskStatus();renderSettings();
  const s=current, busy=actionBusy||s.operation?.state==='running', writing=s.active_jobs>0;
  text('connection-state',s.connected?'Подключён':'Не подключён');$('connection-state').classList.toggle('ready',s.connected);
  text('destination-state',s.destination&&!s.volume_error?'Проверен':'Ожидает диск');$('destination-state').classList.toggle('ready',!!s.destination&&!s.volume_error);
  if(!destinationDirty&&document.activeElement!==$('destination')&&(s.destination||s.expected_destination?.root))$('destination').value=s.destination||s.expected_destination.root;
  $('forget-token').hidden=!s.token_saved;
  $('forget-token').disabled=busy;
  text('free-space',s.free_bytes!==null?'Свободно '+bytes(s.free_bytes):'Выберите существующую папку на диске');
  text('catalog-bytes',bytes(s.catalog_bytes));text('catalog-count',s.catalog_files.toLocaleString('ru-RU')+' файлов / версий');
  const totalBytes=recoverySummary?.bytes??s.catalog_bytes, savedBytes=recoverySummary?.saved_bytes??s.verified_bytes;
  text('verified-bytes',bytes(savedBytes));text('verified-count',(recoverySummary?.saved??(count('original','verified_md5')+count('original','verified_size'))).toLocaleString('ru-RU')+' проверенных файлов');
  text('preview-count',(recoverySummary?.previews_saved??count('preview','preview_saved')).toLocaleString('ru-RU'));text('deferred-count',(recoverySummary?.errors??(count('original','deferred')+count('preview','deferred'))).toLocaleString('ru-RU'));
  const percent=totalBytes>0?Math.min(100,100*savedBytes/totalBytes):(recoverySummary?.total&&recoverySummary.saved===recoverySummary.total?100:0);
  $('progress').value=percent;text('progress-percent',Math.floor(percent)+'%');
  text('progress-text',recoverySummary?.total===0?'Выберите содержимое для восстановления':s.catalog_files?'Проверено '+bytes(savedBytes)+' из '+bytes(totalBytes)+' выбранных оригиналов':'Подключите Диск и получите каталог');
  text('speed',s.running&&writing?bytes(s.speed_bps)+'/с':'—');text('transferred','Передано за сеанс: '+bytes(s.transferred_bytes));
  const complete=s.pause_reason==='complete', pausing=!s.running&&writing;
  text('run-badge',s.scanning?'Получаем каталог':pausing?'Завершаем записи':s.running?'Восстановление':complete?'Готово':s.pause_reason&&s.pause_reason!=='user'?'Требуется действие':'Подготовка');
  $('run-badge').classList.toggle('active',s.running||complete);
  text('recovery-heading',s.running?'Сохраняем доступные файлы':complete?'Все задания очереди обработаны':pausing?'Дожидаемся остановки записей':'Готовим безопасную копию');
  text('activity-text',s.retry_in>0?'Пауза запросов: '+Math.ceil(s.retry_in)+' с':writing?'Активных передач: '+s.active_jobs:s.running?'Ожидаем заданий и повторных попыток':complete?'Оригиналы и превью учтены раздельно':'Ожидает запуска');
  text('disk-safety',s.safe_to_disconnect?'Активных записей нет':pausing?'Не отключайте диск до остановки записей':'Перед отключением диска нажмите «Приостановить»');
  text('results-destination',s.destination?'Сохраняем в: '+s.destination:'Папка назначения ещё не выбрана');
  $('connect-button').disabled=$('configured-token').disabled=busy||s.running||writing||s.scanning;
  $('destination-button').disabled=busy||s.running||writing;
  $('scan-button').disabled=busy||!s.connected||s.scanning;
  $('scan-button').textContent=s.scanning?'Получаем каталог…':s.snapshot&&!s.snapshot.complete?'Продолжить каталог':'Получить каталог';
  const queued=Object.values(s.counts.original).reduce((a,b)=>a+b,0)+Object.values(s.counts.preview).reduce((a,b)=>a+b,0);
  const resumable=!s.running&&!pausing&&!!s.destination&&queued>0&&!complete;
  $('start-button').hidden=false;
  $('start-button').disabled=busy||s.running||writing||!s.connected||!s.destination||!!s.volume_error||!s.catalog_files;
  $('pause-button').hidden=!s.running&&!s.scanning&&!pausing;$('pause-button').disabled=pausing;
  $('resume-button').hidden=!resumable;
  $('resume-button').disabled=busy||!s.connected;
  $('previews').disabled=s.running||writing||resumable;
  if(s.running||resumable)$('previews').checked=s.previews;
  $('export-button').disabled=!recoverySupported||!recoverySummary?.total;
  $('fixture-label').hidden=!s.fixture;
  updateFolderControls();
  if(pendingDestination&&s.operation?.id!==destinationBaseline&&s.operation?.kind==='destination'&&s.operation.state==='done'){destinationDirty=false;pendingDestination=null;}
  if(s.operation?.id!==destinationBaseline&&s.operation?.kind==='destination'&&s.operation.state==='failed'&&s.operation.error==='different_destination'&&pendingDestination&&!$('destination-dialog').open){
    text('old-destination-path',s.expected_destination?.root||s.destination||'—');text('new-destination-path',pendingDestination);$('destination-dialog').showModal();
  }
  if(s.operation?.state==='failed'&&s.operation.error==='different_destination'&&!pendingDestination)message('');
  else if(s.operation?.state==='failed')message(explain(s.operation.error),true);
  else if(s.pause_reason&&!['user','complete'].includes(s.pause_reason))message(explain(s.pause_reason),true);
  else if(s.restoration_error)message(explain(s.restoration_error),true);
  else if(s.catalog_retry_in>0)message('Получение каталога временно приостановлено. Повторим автоматически через '+Math.ceil(s.catalog_retry_in)+' с.');
  else if(s.scan_error&&!s.scanning)message(explain(s.scan_error),true);
  else if(s.operation?.state==='running')message('Выполняем: '+({connect:'проверка доступа',destination:'проверка диска',catalog:'получение каталога',start:'запуск',resume:'продолжение'}[s.operation.kind]||'действие')+'…');
  else if(s.operation?.state==='done')message('');
  renderSetup();
}
function renderSetup(){
  const op=current.operation;
  if(current.connected&&op?.state==='done'&&['connect','restore'].includes(op.kind)&&op.id!==lastConnectionOperation){
    lastConnectionOperation=op.id;
    connectionEditing=false;
  }
  const connectionExpanded=!current.connected||connectionEditing;
  const connectionFields=$('connection-fields');
  const connectionFocus=!connectionExpanded&&connectionFields.contains(document.activeElement);
  connectionFields.hidden=!connectionExpanded;
  $('connection-summary').hidden=connectionExpanded;
  $('edit-connection').setAttribute('aria-expanded',String(connectionExpanded));
  text('connection-description',current.token_saved?'Доступ проверен. Токен сохранён на этом Mac.':'Доступ проверен. Подключение активно.');
  const ready=current.connected&&!!current.destination&&!current.volume_error;
  const expanded=!ready||setupEditing;
  const fields=$('setup-fields');
  const moveFocus=!expanded&&fields.contains(document.activeElement);
  fields.hidden=!expanded;
  $('setup-summary').hidden=expanded;
  $('edit-setup').hidden=expanded;
  $('edit-setup').setAttribute('aria-expanded',String(expanded));
  $('finish-setup').hidden=!ready;
  $('finish-setup').disabled=actionBusy||current.operation?.state==='running';
  text('summary-destination',current.destination||'');
  text('summary-space',current.free_bytes!==null?'Свободно '+bytes(current.free_bytes):'');
  text('summary-previews',$('previews').checked?'Превью сохраняются':'Без превью');
  if(moveFocus)$('edit-setup').focus();
  else if(connectionFocus)$('edit-connection').focus();
}
$('edit-connection').addEventListener('click',()=>{connectionEditing=true;renderSetup();$('token').focus();});
$('edit-setup').addEventListener('click',()=>{setupEditing=true;renderSetup();$('destination').focus();});
$('finish-setup').addEventListener('click',()=>{
  if($('token').value||$('destination').value.trim()!==current.destination){message('Сначала подключите введённый токен и проверьте выбранную папку.',true);return;}
  setupEditing=false;destinationDirty=false;renderSetup();
});
document.querySelector('nav a[href="#setup"]').addEventListener('click',()=>{setupEditing=true;if(current)renderSetup();});
$('previews').addEventListener('change',()=>{if(current)renderSetup();});
function badge(state){const span=document.createElement('span');span.className='status-tag'+(['verified_md5','verified_size','preview_saved'].includes(state)?' saved':state==='deferred'?' waiting':state==='conflict'?' conflict':state==='active'?' active':'');span.textContent=labels[state]||'—';return span;}
let mediaRequest=0;
function fileLabel(row,className){
  const original=['verified_md5','verified_size'].includes(row.original_status)&&/\.(jpe?g|png|gif|webp|bmp|mp4|m4v|webm|mov|ogv)$/i.test(row.path);
  const available=original||row.preview_status==='preview_saved';
  const label=document.createElement(available?'button':'span');label.className=className+(available?' media-link':'');
  label.textContent=row.path.split('/').pop()||row.path;
  if(available){label.type='button';label.setAttribute('aria-label','Просмотр '+row.path);label.addEventListener('click',()=>openPreview(row));}
  return label;
}
function clearMedia(){
  mediaRequest++;
  $('media-video').pause();$('media-video').removeAttribute('src');$('media-video').load();
  $('media-image').removeAttribute('src');
  $('media-image').hidden=$('media-video').hidden=true;
}
async function openPreview(row){
  clearMedia();const request=mediaRequest;
  text('media-title',row.path.split('/').pop());text('media-caption',row.path);text('media-message','Открываем сохранённую копию…');
  $('media-dialog').showModal();
  try{
    const response=await fetch('/api/media?id='+row.id,{headers:{'X-Session-Secret':secret},cache:'no-store'});
    if(!response.ok)throw new Error('Сохранённая копия недоступна. Проверьте подключение накопителя. Если сервер ещё не обновлён, просмотр появится после его следующего запуска.');
    const info=await response.json();
    if(request!==mediaRequest)return;
    text('media-caption',info.kind==='preview'?'Сохранённое превью · уменьшенная копия':'Сохранённый оригинал');
    const media=info.mime.startsWith('video/')?$('media-video'):$('media-image');
    media.onerror=()=>{if(request===mediaRequest)text('media-message','Браузер не смог показать этот формат или кодек. Сохранённый файл остаётся на диске.');};
    media.hidden=false;if(media.tagName==='IMG')media.alt=row.path;
    media.src=info.url;text('media-message','');
  }catch(e){if(request===mediaRequest)text('media-message',e.message);}
}
$('close-media').addEventListener('click',()=>$('media-dialog').close());
$('media-dialog').addEventListener('close',clearMedia);
function renderRows(){
  const body=$('file-rows');body.replaceChildren();
  for(const row of rows){
    const tr=document.createElement('tr');
    const name=document.createElement('td');name.className='file-name';name.title=row.path+(row.error_code?' · '+explain(row.error_code):'')+(row.preview_error?' · Превью: '+explain(row.preview_error):'');
    const icon=document.createElement('span');icon.className='file-icon';icon.textContent='▧';name.append(icon,fileLabel(row,'file-label'));
    const path=document.createElement('span');path.className='file-path';path.textContent=row.path;name.append(path);
    if(row.local_original||row.local_preview){const local=document.createElement('span');local.className='file-path';local.textContent=([row.original_status,row.preview_status].some(s=>['verified_md5','verified_size','preview_saved'].includes(s))?'Копия: ':'Назначение: ')+(row.local_original||row.local_preview);local.title=local.textContent;name.append(local);}
    tr.append(name);
    const size=document.createElement('td');size.textContent=bytes(row.size);tr.append(size);
    for(const state of [row.original_status,row.preview_status]){const td=document.createElement('td');td.append(badge(state));tr.append(td);}
    const attempts=document.createElement('td');attempts.textContent=row.attempts||'—';tr.append(attempts);body.append(tr);
  }
  $('empty-state').hidden=resultsTotal>0||!recoverySupported;
  text('results-empty-title',recoverySummary?.total?'Нет файлов по выбранным условиям':'Восстановление ещё не выбрано');
  text('results-empty-description',recoverySummary?.total?'Измените поиск или фильтр статуса.':'Выберите весь Диск или папки в блоке «Что восстановить». Здесь появятся только файлы выбранного восстановления.');
  $('results-pagination').hidden=resultsView!=='flat'||!resultsTotal;
  $('previous-page').disabled=offset===0;$('next-page').disabled=offset+rows.length>=resultsTotal;
  text('page-info',rows.length?'Показаны '+(offset+1)+'–'+(offset+rows.length)+' из '+resultsTotal:'Нет файлов по выбранным условиям');
  render();
}
async function readRecovery(parent, pageOffset=0, unfiltered=false){
  const params=new URLSearchParams({offset:pageOffset,limit:100,query:unfiltered?'':$('search').value,state:unfiltered?'':$('state-filter').value});
  if(parent!==null)params.set('parent',parent);
  const response=await fetch('/api/recovery?'+params,{headers:{'X-Session-Secret':secret},cache:'no-store'});
  if(response.status===404){recoverySupported=false;return null;}
  if(!response.ok)throw new Error('Не удалось прочитать результаты восстановления.');
  return response.json();
}
function folderProgress(data){
  return data.saved+' / '+data.total+' сохранено · '+data.pending+' ожидает · '+data.active+' загружается · '+data.errors+' с ошибками';
}
function syncRecoveryTree(container,data,parent){
  const existing=new Map([...container.children].filter(el=>el.matches('details')).map(el=>[el.dataset.path,el]));
  const keep=new Set();
  for(const folder of data.folders){
    keep.add(folder.path);
    let details=existing.get(folder.path);
    if(!details){
      details=document.createElement('details');details.className='folder-node recovery-node';details.dataset.path=folder.path;
      const summary=document.createElement('summary'),name=document.createElement('span'),stats=document.createElement('span');
      name.className='folder-name';name.textContent=folder.path.split('/').pop();stats.className='folder-size';
      summary.append(name,stats);details.append(summary);
      const children=document.createElement('div');children.className='folder-children';details.append(children);
      details.addEventListener('toggle',()=>{
        if(details.open){openRecoveryFolders.add(folder.path);loadRecoveryBranch(folder.path,children).catch(e=>message(e.message,true));}
        else openRecoveryFolders.delete(folder.path);
      });
      container.append(details);
      details.open=openRecoveryFolders.has(folder.path);
    }
    details.querySelector('.folder-size').textContent=folderProgress(folder);
    details.querySelector('summary').title=folder.path+' · '+bytes(folder.saved_bytes)+' из '+bytes(folder.bytes);
  }
  for(const [path,el] of existing)if(!keep.has(path))el.remove();
  let files=container.querySelector(':scope > .recovery-leaves');
  if(!files){files=document.createElement('div');files.className='recovery-leaves';container.append(files);}
  files.replaceChildren();
  for(const row of data.files){
    const line=document.createElement('div');line.className='recovery-file';
    const name=fileLabel(row,'folder-name');name.title=row.path;
    const size=document.createElement('span');size.className='muted';size.textContent=bytes(row.size);
    line.append(name,size,badge(row.original_status));
    if(row.preview_status)line.append(badge(row.preview_status));
    files.append(line);
  }
  const start=recoveryOffsets.get(parent)||0;
  if(data.file_total>100){
    const controls=document.createElement('div');controls.className='branch-pagination';
    const info=document.createElement('span');info.textContent='Файлы '+(start+1)+'–'+(start+data.files.length)+' из '+data.file_total;
    controls.append(info);
    for(const [label,delta] of [['←',-100],['→',100]]){
      const button=document.createElement('button');button.className='button small secondary';button.textContent=label;button.setAttribute('aria-label',delta<0?'Предыдущие файлы папки':'Следующие файлы папки');
      button.disabled=delta<0?start===0:start+100>=data.file_total;
      button.addEventListener('click',()=>{recoveryOffsets.set(parent,Math.max(0,start+delta));loadRecoveryBranch(parent,container).catch(e=>message(e.message,true));});controls.append(button);
    }
    files.append(controls);
  }
}
async function loadRecoveryBranch(path,container){
  const generation=resultsGeneration;
  const data=await readRecovery(path,recoveryOffsets.get(path)||0);
  if(data&&generation===resultsGeneration&&container.isConnected)syncRecoveryTree(container,data,path);
}
async function loadRows(){
  const scope=JSON.stringify([current?.destination,current?.selection_count,current?.folder_selection,current?.operation?.kind==='start'?current.operation.id:null]);
  if(resultsScope!==scope){if($('media-dialog').open)$('media-dialog').close();resultsScope=scope;recoverySummary=null;resultsGeneration++;offset=0;openRecoveryFolders.clear();recoveryOffsets.clear();$('recovery-tree').replaceChildren();}
  $('results-compatibility').hidden=recoverySupported;
  $('results-controls').hidden=!recoverySupported;
  if(!current?.snapshot||!recoverySupported){rows=[];resultsTotal=0;renderRows();return;}
  const generation=resultsGeneration;
  const data=await readRecovery(resultsView==='tree'?'/':null,resultsView==='tree'?(recoveryOffsets.get('/')||0):offset);
  if(!data){$('results-compatibility').hidden=false;$('results-controls').hidden=true;rows=[];resultsTotal=0;renderRows();return;}
  if(generation!==resultsGeneration)return;
  recoverySummary=($('search').value||$('state-filter').value)?await readRecovery(null,0,true):data;
  if(generation!==resultsGeneration)return;
  resultsTotal=data.total;
  text('files-count',data.total.toLocaleString('ru-RU'));
  const chosen=current.selection_count===null?'Весь Диск':current.folder_selection?.included?.join(', ')||'Выбрано файлов: '+current.selection_count;
  text('snapshot-info',data.total?chosen+' · '+folderProgress(data)+' · '+bytes(data.saved_bytes)+' из '+bytes(data.bytes):recoverySummary?.total?'Измените поиск или фильтр статуса.':'Выберите содержимое и запустите восстановление.');
  if(resultsView==='tree'){
    syncRecoveryTree($('recovery-tree'),data,'/');
    for(const el of $('recovery-tree').querySelectorAll('details[open]')){
      if(!el.parentElement.closest('details:not([open])'))await loadRecoveryBranch(el.dataset.path,el.querySelector(':scope > .folder-children'));
    }
  }
  rows=data.files;renderRows();
}
function setResultsView(view){
  resultsView=view;offset=0;resultsGeneration++;
  $('tree-view').setAttribute('aria-pressed',String(view==='tree'));
  $('flat-view').setAttribute('aria-pressed',String(view==='flat'));
  $('recovery-tree').hidden=view!=='tree';$('flat-results').hidden=view!=='flat';
  loadRows().catch(e=>message(e.message,true));
}
$('tree-view').addEventListener('click',()=>setResultsView('tree'));
$('flat-view').addEventListener('click',()=>setResultsView('flat'));
async function refresh(){
  if(fetching)return;
  fetching=true;let statusLoaded=false;
  try{current=await api('/api/status');statusLoaded=true;serviceOnline=true;$('network-error').hidden=true;render();await loadRows();if($('folder-panel').open)await loadFolders();}
  catch(e){$('network-error').hidden=false;if(!statusLoaded)serviceOnline=false;renderDiskStatus();}
  finally{fetching=false;}
}
$('token').addEventListener('input',()=>{connectionEditing=true;});
$('connect-form').addEventListener('submit',e=>{e.preventDefault();const token=$('token').value;$('token').value='';command('/api/connect',{token,remember:$('remember-token').checked});});
$('configured-token').addEventListener('click',()=>command('/api/connect',{remember:$('remember-token').checked}));
$('forget-token').addEventListener('click',()=>command('/api/forget-token'));
$('destination').addEventListener('input',()=>{destinationDirty=true;setupEditing=true;});
$('destination-form').addEventListener('submit',e=>{e.preventDefault();pendingDestination=$('destination').value.trim();destinationBaseline=current?.operation?.id;command('/api/destination',{path:pendingDestination});});
$('cancel-destination-change').addEventListener('click',()=>{pendingDestination=null;destinationDirty=false;$('destination-dialog').close();if(current)$('destination').value=current.destination||current.expected_destination?.root||'';});
$('destination-dialog').addEventListener('cancel',()=>{pendingDestination=null;});
$('confirm-destination-change').addEventListener('click',()=>{const path=pendingDestination;destinationBaseline=current?.operation?.id;$('destination-dialog').close();command('/api/destination',{path,confirm_change:true});});
$('scan-button').addEventListener('click',()=>command('/api/catalog'));
$('start-button').addEventListener('click',()=>command('/api/start',{previews:$('previews').checked}));
$('pause-button').addEventListener('click',()=>command('/api/pause'));
$('resume-button').addEventListener('click',()=>command('/api/resume',{confirm_destination:true}));
$('previous-page').addEventListener('click',()=>{offset=Math.max(0,offset-100);refresh();});$('next-page').addEventListener('click',()=>{offset+=100;refresh();});
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{offset=0;resultsGeneration++;recoveryOffsets.clear();refresh();},250);});
$('state-filter').addEventListener('change',()=>{offset=0;resultsGeneration++;recoveryOffsets.clear();refresh();});
$('export-button').addEventListener('click',async()=>{try{const response=await fetch('/api/export.csv?recovery=1',{headers:{'X-Session-Secret':secret}});if(!response.ok)throw new Error('Не удалось экспортировать отчёт.');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='disk-rescue-report.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){message(e.message,true);}});
let folderRows=[],folderRules=new Map(),folderSnapshot=null,folderCacheKey='';
function folderUnder(path,parent){return path!==parent&&(parent==='/'||path.startsWith(parent+'/'));}
function folderState(path,strict=false){
  let match='',chosen=false;
  for(const [p,value] of folderRules){if((!strict&&path===p)||folderUnder(path,p)){if(p.length>match.length){match=p;chosen=value;}}}
  return chosen;
}
function folderTotals(){
  const byPath=new Map(folderRows.map(r=>[r.path,r]));let files=0,size=0;
  for(const [path,chosen] of folderRules){const r=byPath.get(path);if(r&&chosen!==folderState(path,true)){const sign=chosen?1:-1;files+=sign*r.files;size+=sign*r.bytes;}}
  return {files,size};
}
function updateFolderControls(){
  if(!current)return;
  const totals=folderTotals();
  text('folder-total',totals.files.toLocaleString('ru-RU')+' файлов · '+bytes(totals.size)+' выбрано');
  $('folder-start').disabled=actionBusy||current.operation?.state==='running'||current.running||current.active_jobs>0||!current.connected||!current.destination||!!current.volume_error||totals.files===0;
  for(const input of document.querySelectorAll('#folder-tree input')){
    const path=input.dataset.path;input.checked=folderState(path);
    input.indeterminate=[...folderRules].some(([p,v])=>folderUnder(p,path)&&p!==path&&v!==input.checked);
  }
}
function drawFolders(){
  const children=new Map();
  for(const row of folderRows){if(row.path==='/')continue;const parent=row.path.slice(0,row.path.lastIndexOf('/'))||'/';if(!children.has(parent))children.set(parent,[]);children.get(parent).push(row);}
  function node(row){
    const details=document.createElement('details');details.className='folder-node';
    const summary=document.createElement('summary');
    const input=document.createElement('input');input.type='checkbox';input.dataset.path=row.path;input.setAttribute('aria-label','Выбрать папку '+row.path);
    input.addEventListener('click',e=>e.stopPropagation());
    input.addEventListener('change',()=>{for(const p of [...folderRules.keys()])if(folderUnder(p,row.path))folderRules.delete(p);folderRules.set(row.path,input.checked);updateFolderControls();});
    const name=document.createElement('span');name.className='folder-name';name.textContent=row.path==='/'?'Весь Яндекс.Диск':row.path.split('/').pop();
    const size=document.createElement('span');size.className='folder-size';size.textContent=row.files.toLocaleString('ru-RU')+' · '+bytes(row.bytes);
    summary.append(input,name,size);details.append(summary);
    let built=false;
    details.addEventListener('toggle',()=>{if(details.open&&!built){built=true;const group=document.createElement('div');group.className='folder-children';for(const child of children.get(row.path)||[])group.append(node(child));details.append(group);updateFolderControls();}});
    return details;
  }
  $('folder-tree').replaceChildren();const root=folderRows.find(r=>r.path==='/');if(root){const el=node(root);$('folder-tree').append(el);el.open=true;}
  updateFolderControls();
}
async function loadFolders(){
  if(!current?.snapshot)return;
  const sid=current.snapshot.id,key=sid+':'+current.catalog_files+':'+current.snapshot.cursor+':'+current.snapshot.complete;
  if(folderCacheKey===key)return;
  const data=await api('/api/folders?snapshot_id='+sid);
  if(folderSnapshot!==sid){folderRules.clear();folderSnapshot=sid;const saved=current.folder_selection;if(saved?.snapshot_id===sid){for(const p of saved.included||[])folderRules.set(p,true);for(const p of saved.excluded||[])folderRules.set(p,false);}}
  folderRows=data.folders;folderCacheKey=key;drawFolders();
}
$('folder-panel').addEventListener('toggle',()=>{if($('folder-panel').open)loadFolders().catch(e=>message(e.message,true));});
$('folder-start').addEventListener('click',()=>command('/api/start',{previews:$('previews').checked,folders:[...folderRules].filter(([,v])=>v).map(([p])=>p),excluded_folders:[...folderRules].filter(([,v])=>!v).map(([p])=>p),snapshot_id:folderSnapshot}));
async function boot(){try{const response=await fetch('/api/bootstrap',{cache:'no-store'});if(!response.ok)throw new Error();secret=(await response.json()).secret;await refresh();refreshTimer=setTimeout(tick,2000);}catch{$('network-error').hidden=false;serviceOnline=false;renderDiskStatus();setTimeout(boot,2000);}}
async function tick(){await refresh();refreshTimer=setTimeout(tick,2000);}
boot();

});
