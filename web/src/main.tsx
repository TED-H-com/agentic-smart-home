import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, desktopLogin, consumeDesktopEntry, download, session, configureDeployment, haPath, haTarget, type ConversationSession } from './api';
import {SetupGuide,currentRoute,type View} from './setup';
import './style.css';
import {Status,Notice,Button,Empty,timestamp} from './ui';
import {Provider} from './providers';
import {GatewayPanel,RuntimePanel} from './gateway';
import {Onboarding} from './bootstrap';
import {HomeEntry} from './home-entry';
import {HermesPanel} from './hermes';
import {WorkerPanel} from './workers';
import {TaskDetail,type Task} from './task-detail';
import {BridgeApp} from './bridge';
import {BridgeMaintenance} from './bridge-maintenance';
import {HostLogin,HostPanel} from './host';

type Entity = {entity_id:string; name:string; domain:string; area:string|null; state:string; availability:string; observed_at:string; actions:string[]};
type Permission = {entity_id:string; read:boolean; write:string; purpose:string};

function App({hostMode=false,active=true,desktopEntry=false,onLogout}:{hostMode?:boolean;active?:boolean;desktopEntry?:boolean;onLogout:()=>void}) {
  const [conversationSession,setConversationSession]=useState<ConversationSession>();
  const [dailyEntry]=useState(()=>hostMode&&(!location.hash||(desktopEntry&&location.hash==='#onboarding')));
  const [route,setRoute] = useState(()=>dailyEntry?{view:'entry' as View,task:null}:currentRoute(hostMode));
  const {view,task:selected}=route;
  const [status,setStatus] = useState<any>(null), [tasks,setTasks] = useState<Task[]>([]);
  const [detail,setDetail] = useState<Task|null>(null), [error,setError] = useState('');
  const [ready,setReady] = useState(false), [draft,setDraft] = useState(''), [refreshing,setRefreshing]=useState(false);
  const [dirty,setDirty]=useState(false), [pendingView,setPendingView]=useState<View|null>(null);
  const [menuOpen,setMenuOpen]=useState(false),[gatewayMounted,setGatewayMounted]=useState(view==='gateway');
  useEffect(()=>{if(view==='gateway')setGatewayMounted(true)},[view]);
  useEffect(()=>setMenuOpen(false),[view]);
  const load = useCallback(async () => {const data = await api('tasks'); setTasks(data.tasks);}, []);
  const refreshStatus = useCallback(async () => setStatus(await api('status')), []);
  const refresh = useCallback(async()=>{setRefreshing(true);try{setConversationSession(await session());setReady(true);await Promise.all([load(),refreshStatus()]);setError('')}catch(e){setError((e as Error).message)}finally{setRefreshing(false)}},[load,refreshStatus]);
  useEffect(()=>{const update=()=>{const next=currentRoute(hostMode);if(dirty&&next.view!==view){setPendingView(next.view);history.replaceState(null,'',`#${view}`);return}setRoute(next)};window.addEventListener('hashchange',update);return()=>window.removeEventListener('hashchange',update)},[hostMode,dirty,view]);
  useEffect(()=>{if(!active)return;refresh();let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{await Promise.all([load(),refreshStatus()])}catch(e){if(alive)setError((e as Error).message)}if(alive)timer=setTimeout(poll,10000)};timer=setTimeout(poll,10000);const auth=setInterval(()=>session().catch(()=>{}),1800000);return()=>{alive=false;clearTimeout(timer);clearInterval(auth)}},[active,refresh,load,refreshStatus]);
  useEffect(()=>{if(!selected||!active){setDetail(null);return}let alive=true;let timer:ReturnType<typeof setTimeout>;setDetail(null);const update=async()=>{try{const d=await api(`tasks/${selected}`);if(alive)setDetail(d)}catch(e){if(alive)setError((e as Error).message)}if(alive)timer=setTimeout(update,2500)};update();return()=>{alive=false;clearTimeout(timer)}},[selected,active]);
  function navigate(next:View){setMenuOpen(false);if(dirty&&next!==view){setPendingView(next);return}location.hash=next;setRoute({view:next,task:null})}
  function openTask(task:Task){location.hash=`home/${task.id}`;setRoute({view:'home',task:task.id});load().catch(()=>{})}
  const nav:[View,string,string][]=hostMode
    ? [['entry','家庭','01'],['gateway','智能管家','02'],['inventory','設備','03'],['automations','自動化','04'],['history','紀錄','05'],['settings','設定','06']]
    : [['home','工作台','01'],['inventory','設備與權限','02'],['automations','家庭自動化','03'],['history','任務紀錄','04'],['settings','連線與設定','05']];
  const hiddenTitles:Partial<Record<View,string>>={home:'家庭提案',agents:'助手帳號',host:'主機維護',onboarding:'開始設定',workers:'進階工具'};
  const activeNav=hostMode?(view==='home'?'gateway':['agents','host','onboarding','workers'].includes(view)?'settings':view):view;
  useEffect(()=>{const reveal=()=>{const button=document.querySelector<HTMLElement>('nav button[aria-current="page"]');const nav=button?.parentElement;if(button&&nav)nav.scrollLeft+=button.getBoundingClientRect().left-nav.getBoundingClientRect().left-(nav.clientWidth-button.clientWidth)/2};reveal();window.addEventListener('resize',reveal);return()=>window.removeEventListener('resize',reveal)},[view]);
  const connected=status?.ha?.status==='connected';
  return <div className={`app-shell ${view==='onboarding'?'setup-shell':''}`}><a className="skip-link" href="#main-content" onClick={e=>{e.preventDefault();document.getElementById('main-content')?.focus()}}>跳到主要內容</a><aside className="sidebar"><a className="brand" href={hostMode?'#entry':'#home'} onClick={e=>{e.preventDefault();navigate(hostMode?'entry':'home')}}><span className="brand-mark">a<span>h</span></span><span>Agentic<br/><b>Smart Home</b></span></a><button className="nav-toggle" aria-controls="main-navigation" aria-expanded={menuOpen} onClick={()=>setMenuOpen(!menuOpen)}>{menuOpen?'收起選單':'選單'} <span aria-hidden>{menuOpen?'−':'+'}</span></button><p className="nav-label">家庭控制中心</p><nav id="main-navigation" className={menuOpen?'is-open':''} aria-label="主選單">{nav.map(([id,label,index])=><button className={activeNav===id?'active':''} aria-current={activeNav===id?'page':undefined} onClick={()=>navigate(id)} key={id}><span>{index}</span>{label}</button>)}</nav><div className="sidebar-foot"><small>Agentic Home {status?.version || '讀取中'}</small></div></aside>
  <main id="main-content" tabIndex={-1}><header className="topbar"><div><span className="eyebrow">{status?.ha?.location_name || 'AGENTIC HOME · LINUX'}</span><h1>{nav.find(n=>n[0]===view)?.[1]||hiddenTitles[view]||'智能管家'}</h1></div><div className="top-actions">{!['entry','onboarding'].includes(view)&&<span className="ha-status">HA <Status value={status?status.ha?.status:'loading'}/></span>}{ready&&<Button secondary disabled={!status} onClick={async()=>{try{await api('pause',{paused:!status?.paused});await refreshStatus()}catch(e){setError((e as Error).message)}}}>{status?.paused?'恢復 AI':'暫停 AI'}</Button>}{hostMode&&<button className="text-button" onClick={async()=>{try{await api('host/logout',{});onLogout()}catch(e){setError((e as Error).message)}}}>登出</button>}</div></header>
  {error&&<div className="global-error"><Notice error>{error}<div className="row-actions"><Button secondary disabled={refreshing} onClick={refresh}>{refreshing?'重新連線中…':'重新連線'}</Button><button className="text-button" onClick={()=>setError('')}>關閉提示</button></div></Notice></div>}
  {pendingView&&<div className="global-error"><Notice>設備授權尚未儲存。可以留在本頁儲存，或捨棄這次修改。<div className="row-actions"><Button secondary onClick={()=>setPendingView(null)}>繼續編輯</Button><button className="text-button" onClick={()=>{setDirty(false);location.hash=pendingView;setRoute({view:pendingView,task:null});setPendingView(null)}}>捨棄修改並離開</button></div></Notice></div>}
  {!ready?<div className="content"><Empty title={error?'目前無法開啟管理中心':'正在建立管理連線'}>{hostMode?'請確認本機管理服務正在運作。':'請使用 HA owner 從 App 側欄開啟。'}</Empty></div>:<div className="content">
  {status?.paused&&<Notice>AI 已暫停，不接受新任務。已部署的 Home Assistant 自動化繼續運作。</Notice>}
  {(['home','inventory'].includes(view)||view==='settings'&&!hostMode)&&<SetupGuide status={status} hostMode={hostMode} navigate={navigate} view={view}/>}
  {view==='home'&&<><div className="section-intro"><p>描述需求、查看提案，讓 Home Assistant 接手日常規則。</p><span className="subtle">{!status?'讀取觀測狀態…':status.events_connected?'即時觀測已連線':connected?'正在重連 HA 事件':'連接家庭後即可取得觀測'}</span></div>
    <div className="workspace"><section className="request-column"><Composer status={status} draft={draft} setDraft={setDraft} onCreated={openTask} onSetup={()=>navigate('settings')}/><div className="section-heading"><h2>最近任務</h2><button className="text-button" onClick={()=>navigate('history')}>查看全部</button></div>{tasks.length===0?<Empty title="第一條規則，從一句話開始">完成上方設定後，描述要自動完成的事情。你會先看到規則預覽。</Empty>:<div className="task-list">{tasks.slice(0,8).map(t=><button className={`task-row ${selected===t.id?'selected':''}`} key={t.id} onClick={()=>openTask(t)}><div><Status value={t.state}/><small>{timestamp(t.created_at)}</small></div><h3>{t.request}</h3><p>{t.message}</p></button>)}</div>}</section><section className="detail-column" aria-label="任務詳情">{detail?<TaskDetail key={detail.id} task={detail} onChange={()=>{load().catch(()=>{});api(`tasks/${detail.id}`).then(setDetail).catch(()=>{})}}/>:<Empty title={selected?'正在載入任務':'提案與結果會出現在這裡'}>先確認操作對象與效果，再批准執行；每次變更都有紀錄。</Empty>}</section></div></>}
  {view==='inventory'&&<Inventory onSaved={refreshStatus} onDirty={setDirty}/>}
  {view==='automations'&&<Automations onTask={openTask} onEdit={(id)=>{setDraft(`修改自動化 ${id}：`);navigate('home')}}/>}
  {view==='history'&&<><div className="section-intro"><p>回看每個需求、批准和執行結果。</p><Button secondary onClick={async()=>{try{download(new Blob([JSON.stringify(await api('evidence'),null,2)],{type:'application/json'}),'agentic-home-evidence.json')}catch(e){setError((e as Error).message)}}}>匯出執行證據</Button></div><div className="history-table"><div className="history-head"><span>需求</span><span>Agent</span><span>狀態</span><span>時間</span></div>{tasks.map(t=><button key={t.id} onClick={()=>openTask(t)}><strong>{t.request}</strong><span>{t.provider==='codex'?'Codex':'Claude Code'}</span><Status value={t.state}/><time>{timestamp(t.created_at)}</time></button>)}</div>{!tasks.length&&<Empty title="還沒有任務">送出第一個需求後，這裡會保留處理與操作紀錄。</Empty>}</>}
  {view==='settings'&&<Settings hostMode={hostMode} status={status} onChange={refreshStatus}/>}
  {view==='entry'&&<HomeEntry automatic={dailyEntry&&active} navigate={navigate}/>}
  {(view==='gateway'||gatewayMounted)&&<div hidden={view!=='gateway'}><GatewayPanel key={conversationSession?.storage_scope||'legacy'} visible={view==='gateway'&&active} conversationSession={conversationSession}/></div>}
  {view==='workers'&&<WorkerPanel/>}
  {view==='agents'&&<><HermesPanel/><RuntimePanel/></>}
  {view==='onboarding'&&<Onboarding status={status} onChange={refreshStatus} navigate={navigate}/>}
  {view==='host'&&<HostPanel appStatus={status} onConnected={refreshStatus} onAgent={()=>navigate('settings')}/>}
  </div>}{view!=='gateway'&&<footer>Agentic Smart Home</footer>}</main></div>
}

function Composer({status,draft,setDraft,onCreated,onSetup}:{status:any;draft:string;setDraft:(v:string)=>void;onCreated:(t:Task)=>void;onSetup:()=>void}) {
  const [provider,setProvider]=useState('codex'),[model,setModel]=useState(''),[providerChosen,setProviderChosen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{if(!status||providerChosen)return;const first=status.runner?.providers?.find((p:any)=>p.status==='connected');if(first)setProvider(first.provider);setProviderChosen(true)},[status,providerChosen]);
  const connected=status?.runner?.providers?.find((p:any)=>p.provider===provider)?.status==='connected';
  async function submit(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{const task=await api('tasks',{message:draft,provider,model});onCreated(task);setDraft('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <form className="composer" onSubmit={submit}><div className="section-heading"><h2>你想讓家做什麼？</h2><span className="eyebrow">NEW REQUEST</span></div><label className="sr-only" htmlFor="request">家庭需求</label><textarea id="request" value={draft} onChange={e=>setDraft(e.target.value)} maxLength={5000} placeholder="例如：當測試動作感應器由 off 變 on，打開測試燈。先讓我看規則。" rows={5} required/><div className="composer-controls"><label>使用 Agent<select value={provider} onChange={e=>{setProvider(e.target.value);setProviderChosen(true)}}><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="agy">Antigravity</option><option value="grok">Grok Build</option></select></label><label className="model-field">模型（可留空）<input value={model} onChange={e=>setModel(e.target.value)} placeholder={provider==='codex'?'gpt-6-astra':'使用官方預設'} maxLength={100}/></label><Button disabled={busy||!connected||status?.ha?.status!=='connected'||status?.paused||!draft.trim()} type="submit">{busy?'送出中…':'送出需求'} <span aria-hidden>↑</span></Button></div>{!connected&&<p className="helper-text">先完成 {provider==='codex'?'Codex':'Claude Code'} 登入。<button type="button" className="text-button" onClick={onSetup}>前往連線設定 →</button></p>}{error&&<Notice error>{error}</Notice>}<button type="button" className="suggestion" onClick={()=>setDraft('當 input_boolean.agentic_motion_test 從 off 變 on 時，把 input_boolean.agentic_lamp_test 打開。先給我看要建立的規則，不要先執行。')}>填入 test helpers 範例</button></form>
}


function Inventory({onSaved,onDirty}:{onSaved:()=>void;onDirty:(dirty:boolean)=>void}){
  const [entities,setEntities]=useState<Entity[]>([]),[permissions,setPermissions]=useState<Record<string,Permission>>({}),[filter,setFilter]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false),[loading,setLoading]=useState(true),[dirty,setDirty]=useState(false),[scope,setScope]=useState('all');
  async function load(){setLoading(true);setError('');try{const d=await api('inventory');setEntities(d.entities);setPermissions(Object.fromEntries(d.policy.entities.map((p:Permission)=>[p.entity_id,p])))}catch(e){setError((e as Error).message)}finally{setLoading(false)}}
  useEffect(()=>{load()},[]);
  function change(e:Entity,patch:Partial<Permission>){setSaved(false);setDirty(true);setPermissions(prev=>({...prev,[e.entity_id]:{...(prev[e.entity_id]||{entity_id:e.entity_id,read:false,write:'none',purpose:''}),...patch}}))}
  useEffect(()=>{onDirty(dirty);const unload=(event:BeforeUnloadEvent)=>{if(dirty){event.preventDefault();event.returnValue=''}};window.addEventListener('beforeunload',unload);return()=>window.removeEventListener('beforeunload',unload)},[dirty,onDirty]);
  async function save(){setBusy(true);setError('');try{await api('policy',{entities:Object.values(permissions)});setSaved(true);setDirty(false);onSaved()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const visible=entities.filter(e=>`${e.entity_id} ${e.name} ${e.area||''}`.toLowerCase().includes(filter.toLowerCase())&&(scope==='all'||(scope==='shared'?permissions[e.entity_id]?.read:e.availability!=='available')));
  return <><div className="section-intro"><p>明確選擇 Agent 可讀取的設備，再逐台允許操作。新設備預設不分享。</p><Button onClick={save} disabled={busy||loading||!dirty}>{busy?'儲存中…':'儲存設備授權'}</Button></div>{error&&<Notice error>{error}</Notice>}{saved&&<Notice>設備授權已儲存。先前的提案如受影響，需要重新產生。</Notice>}<div className="toolbar"><label className="search"><span className="sr-only">搜尋設備</span><input type="search" placeholder="搜尋設備、房間或 entity ID" value={filter} onChange={e=>setFilter(e.target.value)}/></label><label className="inventory-filter"><span className="sr-only">設備篩選</span><select value={scope} onChange={e=>setScope(e.target.value)}><option value="all">全部設備</option><option value="shared">已分享</option><option value="unavailable">離線或狀態未知</option></select></label><span className="subtle">{visible.length} / {entities.length} 個設備</span><Button secondary onClick={load} disabled={loading||dirty}>{loading?'盤點中…':'重新盤點'}</Button></div><div className="inventory-table"><div className="inventory-head"><span>設備 / 房間</span><span>狀態</span><span>分享給 Agent</span><span>操作範圍與用途</span></div>{visible.map(e=>{const p=permissions[e.entity_id];const helper=['input_boolean.agentic_motion_test','input_boolean.agentic_lamp_test'].includes(e.entity_id);return <div className="entity-row" key={e.entity_id}><div><strong>{e.name}</strong><code>{e.entity_id}</code><small>{e.area||'未分配房間'}</small></div><div><span className={`entity-state ${e.availability!=='available'?'muted':''}`}>{e.state}</span><small>{timestamp(e.observed_at)}</small></div><label className="check"><input type="checkbox" checked={p?.read||false} aria-label={`分享 ${e.entity_id}`} onChange={event=>change(e,{read:event.target.checked,...(!event.target.checked?{write:'none'}:{})})}/>允許讀取</label><div className="permission-controls"><select aria-label={`${e.entity_id} 操作權限`} value={p?.write||'none'} disabled={!p?.read} onChange={event=>change(e,{write:event.target.value,purpose:event.target.value==='dedicated_helper'?'專用測試 helper，不連接其他規則':p?.purpose||''})}><option value="none">唯讀</option>{helper&&<option value="dedicated_helper">專用測試 helper</option>}{e.domain==='light'&&<option value="lighting">一般照明</option>}{e.domain==='switch'&&<option value="low_risk_switch">已確認用途的低風險開關</option>}</select>{p?.write&&p.write!=='none'&&<input aria-label={`${e.entity_id} 用途`} placeholder="記錄用途與已知下游影響" value={p.purpose} onChange={event=>change(e,{purpose:event.target.value})} maxLength={200}/>}</div></div>})}</div>{dirty&&<div className="save-bar" role="status"><span>設備授權有未儲存的修改</span><Button disabled={busy} onClick={save}>{busy?'儲存中…':'儲存修改'}</Button></div>}{loading&&<p className="subtle">正在讀取 Home Assistant 盤點…</p>}{!loading&&!error&&!visible.length&&<Empty title="沒有符合的設備">請確認 HA 連線或調整搜尋條件。</Empty>}</>
}

function Automations({onTask,onEdit}:{onTask:(t:Task)=>void;onEdit:(id:string)=>void}){
  const [items,setItems]=useState<any[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState('');
  useEffect(()=>{api('automations').then(d=>setItems(d.automations)).catch(e=>setError(e.message))},[]);
  async function propose(id:string,action:string){setBusy(id);try{onTask(await api(`automations/${id}/propose`,{action}))}catch(e){setError((e as Error).message)}finally{setBusy('')}}
  return <><div className="section-intro"><p>這裡只管理 Agentic Home 建立並保有紀錄的規則。</p><a className="external-link" href={haPath("/config/automation/dashboard")} target={haTarget()} rel="noreferrer">開啟 HA 原生自動化 ↗</a></div>{error&&<Notice error>{error}</Notice>}{items.map(a=><article className="automation-row" key={a.id}><div><span className="eyebrow">{a.enabled?'已部署・啟用':'已部署・停用'}</span><h2>{a.intent.title}</h2><p><code>{a.intent.trigger.entity_id}</code> {a.intent.trigger.from_state} → {a.intent.trigger.to_state}</p><code className="subtle">{a.id}</code></div><div className="row-actions"><Button secondary onClick={()=>onEdit(a.id)}>修改</Button><Button secondary disabled={busy===a.id} onClick={()=>propose(a.id,a.enabled?'disable':'enable')}>{a.enabled?'預覽停用':'預覽啟用'}</Button><Button secondary disabled={busy===a.id} onClick={()=>propose(a.id,'rollback')}>預覽回復</Button></div></article>)}{!items.length&&<Empty title="尚未建立家庭自動化">在工作台描述你的需求；批准後，真正的 HA 規則會出現在這裡。</Empty>}</>
}

function Settings({status,onChange,hostMode}:{status:any;onChange:()=>void;hostMode:boolean}){
  const [sidebarOpen,setSidebarOpen]=useState(false);
  const [pairing,setPairing]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[services,setServices]=useState<any[]>([]),[passphrase,setPassphrase]=useState('');
  async function loadServices(){setServices((await api('services')).services)}
  useEffect(()=>{loadServices().catch(e=>setError(e.message))},[]);
  async function pair(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{await api('pair',JSON.parse(pairing));setPairing('');setNotice('Runner 配對完成');onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function service(id:string,action:string){setError('');try{await api(`services/${id}`,{action});await loadServices()}catch(e){setError((e as Error).message)}}
  if(hostMode)return <div className="settings-layout settings-simple">
    {error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    <nav className="settings-hub row-actions" aria-label="設定入口">
      <a className="button secondary" href="#gateway/channels">通訊入口</a>
      <a className="button secondary" href="#agents">助手帳號</a>
      <a className="button secondary" href="#onboarding">開始設定</a>
      <a className="button secondary" href="#host">主機維護</a>
      <a className="button secondary" href="#workers">進階工具</a>
      <a className="button secondary" href="#home">家庭提案</a>
    </nav>
    <details className="settings-fold" onToggle={event=>setSidebarOpen(event.currentTarget.open)}><summary>HA 智能管家側欄</summary>{sidebarOpen&&<BridgeMaintenance/>}</details>
    <details className="settings-fold"><summary>家庭服務</summary>
      {services.map(s=><article className="service-row" key={s.id}>
        <div className="section-heading"><h3>{s.title}</h3><Status value={s.state}/></div>
        <div className="row-actions"><a className="button secondary" href={haPath(s.path)} target={haTarget()} rel="noreferrer" onClick={()=>service(s.id,'start')}>開啟設定 ↗</a><Button secondary onClick={()=>service(s.id,'verify')}>檢查連線</Button><button className="text-button" onClick={()=>service(s.id,'skip')}>稍後設定</button></div>
        <details><summary>設定說明</summary><ol>{s.steps.map((step:string)=><li key={step}>{step}</li>)}</ol><a className="external-link" href={s.docs} target="_blank" rel="noreferrer">官方說明 ↗</a>{s.id==='alexa_control'&&<button className="text-button" onClick={()=>service(s.id,'confirm_voice_test')}>我已實際用 Alexa 指令控制設備</button>}{s.observed?.at&&<pre>{JSON.stringify(s.observed,null,2)}</pre>}</details>
      </article>)}
    </details>
    <details className="settings-fold"><summary>備份與復原</summary>
      <p className="helper-text">下載加密的管理中心備份；家庭系統另用 HA 備份。</p>
      <form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{download(await api('backups/export',{passphrase},true),'agentic-home-backup.enc');setPassphrase('');setNotice('已下載加密備份，請另外保管密碼。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>
        <label>備份加密密碼（至少 16 個字元）<input type="password" value={passphrase} onChange={e=>setPassphrase(e.target.value)} minLength={16} required autoComplete="new-password"/></label>
        <div className="row-actions"><Button secondary type="submit" disabled={busy}>下載加密備份</Button><a className="external-link" href={haPath('/config/backup/overview')} target={haTarget()} rel="noreferrer">HA 備份 ↗</a></div>
      </form>
      <details><summary>還原說明</summary><p className="helper-text">停止 Controller，以 restore_controller.py 解密還原後再啟動。Runner 登入資料不包含在此備份。</p></details>
    </details>
    <details className="settings-fold"><summary>紀錄保存</summary><Retention compact/></details>
    <details className="settings-fold"><summary>執行環境連線 <Status value={status?.runner?.protocol===1?'connected':status?.runner?.status||'unpaired'}/></summary>
      {status?.runner?.protocol===1?<p className="helper-text">本機執行環境已連線。</p>:<Button secondary disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api('host/pair',{});await onChange();setNotice('本機執行環境已連接')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>{busy?'連線中…':'連接本機 Runner'}</Button>}
    </details>
  </div>;
  return <div className="settings-layout">{error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}<section className="settings-section"><div className="section-heading"><div><span className="eyebrow">01 / CONNECTION</span><h2>Controller 與 Runner</h2></div><Status value={status?.runner?.protocol===1?'connected':status?.runner?.status||'unpaired'}/></div>{status?.runner?.protocol===1?<p>管理中心與 Agent 執行環境已連線。Runner 負責 Agent 執行；Controller 負責家庭操作。</p>:hostMode?<><p>安裝時會自動連接本機 Runner。若連線尚未完成，可在這裡重試。</p><Button secondary disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api('host/pair',{});await onChange();setNotice('本機 Runner 已連接')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>{busy?'連線中…':'連接本機 Runner'}</Button></>:<form onSubmit={pair}><p>在 Runner App 記錄複製一次性配對 JSON。配對資料包含內部位址、憑證指紋與 10 分鐘有效的配對碼。</p><label>一次性配對資料<textarea value={pairing} onChange={e=>setPairing(e.target.value)} rows={3} autoComplete="off" spellCheck={false} required placeholder='{"host":"…","port":8443,"fingerprint":"…","code":"…"}'/></label><Button disabled={busy} type="submit">{busy?'連線中…':'配對 Runner'}</Button></form>}</section>
  <section className="settings-section"><span className="eyebrow">02 / YOUR AGENTS</span><h2>登入你的 Agent</h2><p>家庭資料只送給你在任務中選擇的 provider。訂閱與 API key 的用量、額度與計費由官方帳號決定。</p><div className="provider-grid">{['codex','claude','agy','grok'].map(provider=><Provider key={provider} provider={provider} state={status?.runner?.providers?.find((p:any)=>p.provider===provider)} paired={status?.runner?.protocol===1} onChange={onChange}/>)}</div></section>
  <section className="settings-section"><span className="eyebrow">03 / HOME SERVICES</span><h2>接入家庭服務</h2><p>帳號與 MFA 在 Home Assistant 原生流程完成。回來後以實際 config entry 與 entity 驗證。</p>{services.map(s=><article className="service-row" key={s.id}><div className="section-heading"><h3>{s.title}</h3><Status value={s.state}/></div><ol>{s.steps.map((step:string)=><li key={step}>{step}</li>)}</ol><div className="row-actions"><a className="button secondary" href={haPath(s.path)} target={haTarget()} rel="noreferrer" onClick={()=>service(s.id,'start')}>開啟原生設定 ↗</a><Button secondary onClick={()=>service(s.id,'verify')}>回來驗證</Button><button className="text-button" onClick={()=>service(s.id,'skip')}>稍後設定</button><a className="text-button" href={s.docs} target="_blank" rel="noreferrer">官方說明</a></div>{s.id==='alexa_control'&&<button className="text-button" onClick={()=>service(s.id,'confirm_voice_test')}>我已實際用 Alexa 指令控制設備</button>}{s.observed?.at&&<details><summary>查看連線觀測 · {timestamp(s.observed.at)}</summary><pre>{JSON.stringify(s.observed,null,2)}</pre></details>}</article>)}</section>
  <section className="settings-section"><span className="eyebrow">04 / RECOVERY</span><h2>備份與復原</h2><p>每次規則部署前會建立 HA data 備份與 Controller 一致快照。下載加密 Controller 備份後，請放到另一台裝置，並另外保管密碼。</p><form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{download(await api('backups/export',{passphrase},true),'agentic-home-backup.enc');setPassphrase('');setNotice('已下載加密 Controller 備份；HA 與 Runner 請使用原生備份管理。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}><label>備份加密密碼（至少 16 個字元）<input type="password" value={passphrase} onChange={e=>setPassphrase(e.target.value)} minLength={16} required autoComplete="new-password"/></label><div className="row-actions"><Button secondary type="submit" disabled={busy}>下載加密備份</Button><a className="external-link" href={haPath("/config/backup/overview")} target={haTarget()} rel="noreferrer">HA 備份管理 ↗</a><a className="external-link" href={haPath("/config/updates")} target={haTarget()} rel="noreferrer">HA 更新 ↗</a></div></form><p className="helper-text">還原方式：停止 Controller，以隨附 restore_controller.py 解密並還原其資料庫，再啟動 App。Runner 登入資料不包含在此檔案。</p></section><Retention/></div>
}

function Retention({compact=false}:{compact?:boolean}){
  const [days,setDays]=useState({audit_days:30,context_days:7}),[message,setMessage]=useState(''),[error,setError]=useState('');
  useEffect(()=>{api('retention').then(setDays).catch(e=>setError(e.message))},[]);
  return <section className={compact?'retention-fields':'settings-section'}>{!compact&&<><span className="eyebrow">05 / PRIVACY</span><h2>紀錄保存</h2></>}<p className={compact?'helper-text':undefined}>{compact?'縮短天數會立即清理到期資料；現有規則的必要紀錄會保留。':'已結束的任務與觀測依天數清理。管理現有規則所需的設定、批准與去重紀錄會保留；縮短天數會立即清理到期資料。'}</p><form onSubmit={async e=>{e.preventDefault();try{await api('retention',days);setMessage('保存設定已更新');setError('')}catch(e){setError((e as Error).message)}}}><label>任務紀錄（1–30 天）<input type="number" min={1} max={30} value={days.audit_days} onChange={e=>setDays({...days,audit_days:Number(e.target.value)})}/></label><label>Agent 觀測資料（1–7 天）<input type="number" min={1} max={Math.min(7,days.audit_days)} value={days.context_days} onChange={e=>setDays({...days,context_days:Number(e.target.value)})}/></label><Button secondary type="submit">儲存保存天數</Button></form>{message&&<Notice>{message}</Notice>}{error&&<Notice error>{error}</Notice>}</section>
}


function Root(){
  const [deployment,setDeployment]=useState<any>(null),[error,setError]=useState(''),[mounted,setMounted]=useState(false),[desktopError,setDesktopError]=useState(false),[desktopEntry,setDesktopEntry]=useState(false);
  const load=useCallback(async()=>{setError('');try{let result=await api('deployment');configureDeployment(result.mode);if(result.mode==='host'){setDesktopError(await desktopLogin()==='failed');if(consumeDesktopEntry()){setDesktopEntry(true);result=await api('deployment')}}setDeployment(result);if(result.mode==='ingress'||result.mode==='host'&&result.authenticated)setMounted(true)}catch(e){setError((e as Error).message)}},[]);
  useEffect(()=>{load();const expire=()=>setDeployment((d:any)=>({...d,authenticated:false}));window.addEventListener('agentic-session-expired',expire);return()=>window.removeEventListener('agentic-session-expired',expire)},[load]);
  if(!deployment)return <main className="host-login"><span className="eyebrow">AGENTIC HOME</span><h1>{error?'管理中心尚未回應':'正在開啟你的家庭主機…'}</h1>{error&&<><p role="alert">{error}</p><Button onClick={load}>重試連線</Button></>}</main>;
  if(deployment.mode==='bridge')return <BridgeApp deployment={deployment} onRetry={load}/>;
  if(!['host','ingress'].includes(deployment.mode))return <main className="host-login"><h1>無法確認此入口</h1><Button secondary onClick={load}>重新連線</Button></main>;
  const loggedIn=deployment.mode==='ingress'||deployment.authenticated;
  return <>{!loggedIn&&<HostLogin expired={mounted} desktopError={desktopError} onLogin={()=>{setDeployment({...deployment,authenticated:true});setMounted(true)}}/>}{mounted&&<div hidden={!loggedIn}><App hostMode={deployment.mode==='host'} active={loggedIn} desktopEntry={desktopEntry} onLogout={()=>{setDesktopEntry(false);Object.keys(sessionStorage).filter(key=>key.startsWith('agentic-auth-')||key==='agentic-host-task').forEach(key=>sessionStorage.removeItem(key));setMounted(false);setDeployment({...deployment,authenticated:false})}}/></div>}</>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><Root/></StrictMode>);
