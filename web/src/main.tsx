import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, ApiError, desktopLogin, consumeDesktopEntry, download, session, configureDeployment, haPath, haTarget, type ConversationSession } from './api';
import {SetupGuide,currentRoute,type View} from './setup';
import './style.css';
import {Status,Notice,Button,Empty,timestamp} from './ui';
import {Provider,RuntimePanel} from './providers';
import {GatewayPanel} from './gateway';
import {Onboarding} from './bootstrap';
import {HomeEntry} from './home-entry';
import {HermesPanel} from './hermes';
import {WorkerPanel} from './workers';
import {TaskDetail,type Task} from './task-detail';
import {BridgeApp} from './bridge';
import {BridgeMaintenance} from './bridge-maintenance';
import {HostLogin,HostPanel} from './host';
import {connection,names,plainError,stateName} from './labels';

type Entity = {entity_id:string; name:string; domain:string; area:string|null; state:string; availability:string; observed_at:string; actions:string[]};
type Permission = {entity_id:string; read:boolean; write:string; purpose:string};
function lostConnection(e:unknown){return !(e instanceof ApiError&&e.status>=400&&e.status<500)}
function entityId(id:string){return id.split(/([._])/).map((part,i)=><span key={i}>{part}{(part==='.'||part==='_')&&<wbr/>}</span>);}

function App({hostMode=false,active=true,desktopEntry=false,onLogout}:{hostMode?:boolean;active?:boolean;desktopEntry?:boolean;onLogout:()=>void}) {
  const [conversationSession,setConversationSession]=useState<ConversationSession>();
  const [dailyEntry]=useState(()=>hostMode&&(!location.hash||(desktopEntry&&location.hash==='#onboarding')));
  const [route,setRoute] = useState(()=>dailyEntry?{view:'entry' as View,task:null}:currentRoute(hostMode));
  const {view,task:selected}=route;
  const [status,setStatus] = useState<any>(null), [tasks,setTasks] = useState<Task[]>([]);
  const [detail,setDetail] = useState<Task|null>(null), [error,setError] = useState<{text:string;code?:string}|null>(null);
  const [ready,setReady] = useState(false), [draft,setDraft] = useState(''), [refreshing,setRefreshing]=useState(false);
  const [dirty,setDirty]=useState(false), [pendingView,setPendingView]=useState<View|null>(null);
  const [menuOpen,setMenuOpen]=useState(false),[gatewayMounted,setGatewayMounted]=useState(view==='gateway');
  // Only connection and selected-task errors can be disproved by polling.
  const errorKind=useRef<''|'lost'|'task'>('');
  const menuRef=useRef<HTMLButtonElement>(null);
  useEffect(()=>{if(view==='gateway')setGatewayMounted(true)},[view]);
  useEffect(()=>setMenuOpen(false),[view]);
  useEffect(()=>{if(!menuOpen)return;const onKey=(event:KeyboardEvent)=>{if(event.key!=='Escape')return;setMenuOpen(false);menuRef.current?.focus()};window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey)},[menuOpen]);
  const load = useCallback(async () => {const data = await api('tasks'); setTasks(data.tasks);}, []);
  const refreshStatus = useCallback(async () => setStatus(await api('status')), []);
  const refresh = useCallback(async(recovery=false)=>{setRefreshing(true);try{setConversationSession(await session());setReady(true);await Promise.all([load(),refreshStatus()]);if(errorKind.current==='lost'||!recovery&&errorKind.current===''){errorKind.current='';setError(null)}}catch(e){errorKind.current=lostConnection(e)?'lost':'';setError(plainError(e))}finally{setRefreshing(false)}},[load,refreshStatus]);
  useEffect(()=>{const update=()=>{const next=currentRoute(hostMode);if(dirty&&next.view!==view){setPendingView(next.view);history.replaceState(null,'',`#${view}`);return}setRoute(next)};window.addEventListener('hashchange',update);return()=>window.removeEventListener('hashchange',update)},[hostMode,dirty,view]);
  useEffect(()=>{if(!active)return;refresh();let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{await Promise.all([load(),refreshStatus()]);if(alive&&errorKind.current==='lost')await refresh(true)}catch(e){if(alive){errorKind.current=lostConnection(e)?'lost':'';setError(plainError(e))}}if(alive)timer=setTimeout(poll,10000)};timer=setTimeout(poll,10000);const auth=setInterval(()=>session().catch(()=>{}),1800000);return()=>{alive=false;clearTimeout(timer);clearInterval(auth)}},[active,refresh,load,refreshStatus]);
  useEffect(()=>{if(errorKind.current==='task'){errorKind.current='';setError(null)}if(!selected||!active){setDetail(null);return}let alive=true;let timer:ReturnType<typeof setTimeout>;setDetail(null);const update=async()=>{try{const d=await api(`tasks/${selected}`);if(alive){setDetail(d);if(errorKind.current==='task'){errorKind.current='';setError(null)}}}catch(e){if(alive){errorKind.current=lostConnection(e)?'lost':'task';setError(plainError(e))}}if(alive)timer=setTimeout(update,2500)};update();return()=>{alive=false;clearTimeout(timer)}},[selected,active]);
  function navigate(next:View){setMenuOpen(false);if(dirty&&next!==view){setPendingView(next);return}location.hash=next;setRoute({view:next,task:null})}
  function openTask(task:Task){location.hash=`home/${task.id}`;setRoute({view:'home',task:task.id});load().catch(()=>{})}
  const nav:[View,string][]=hostMode
    ? [['entry','家庭'],['gateway','智能管家'],['inventory','設備'],['automations','自動化'],['history','紀錄'],['settings','設定']]
    : [['home','工作台'],['inventory','設備與權限'],['automations','家庭自動化'],['history','任務紀錄'],['settings','連線與設定']];
  const hiddenTitles:Partial<Record<View,string>>={home:'家庭提案',agents:'AI 帳號',host:'主機維護',onboarding:'初次設定',workers:'開發助手'};
  const activeNav=hostMode?(view==='home'?'gateway':['agents','host','onboarding','workers'].includes(view)?'settings':view):view;
  const focus=view==='onboarding';
  const title=nav.find(item=>item[0]===view)?.[1]||hiddenTitles[view]||'智能管家';
  const haState=status?.ha?.status as string|undefined;
  const familyLoading=!status||haState==='loading';
  const familyConnected=haState==='connected';
  const familyLabel=familyLoading?'正在連線':familyConnected?'家庭已連線':'家庭未連線';
  const showFamily=!['entry','onboarding'].includes(view);
  async function logout(){try{await api('host/logout',{});onLogout()}catch(e){errorKind.current='';setError(plainError(e))}}
  async function resumeAi(){try{await api('pause',{paused:false});await refreshStatus()}catch(e){errorKind.current='';setError(plainError(e))}}
  const brand=<><span className="brand-mark">a<span>h</span></span><span className="brand-name">Agentic<br/><b>Smart Home</b></span></>;
  useEffect(()=>{const reveal=()=>{const button=document.querySelector<HTMLElement>('nav button[aria-current="page"]');const nav=button?.parentElement;if(button&&nav)nav.scrollLeft+=button.getBoundingClientRect().left-nav.getBoundingClientRect().left-(nav.clientWidth-button.clientWidth)/2};reveal();window.addEventListener('resize',reveal);return()=>window.removeEventListener('resize',reveal)},[view]);
  return <div className={`app-shell ${focus?'setup-shell':''}`}><a className="skip-link" href="#main-content" onClick={e=>{e.preventDefault();document.getElementById('main-content')?.focus()}}>跳到主要內容</a><aside className="sidebar"><div className="sidebar-inner">{focus?<div className="brand">{brand}</div>:<a className="brand" href={hostMode?'#entry':'#home'} onClick={e=>{e.preventDefault();navigate(hostMode?'entry':'home')}}>{brand}</a>}{!focus&&<button ref={menuRef} type="button" className="nav-toggle" aria-controls="main-navigation" aria-expanded={menuOpen} aria-label={menuOpen?'收起選單':'選單'} onClick={()=>setMenuOpen(open=>!open)}><span className="nav-bars" aria-hidden="true"/></button>}{!focus&&<nav id="main-navigation" className={menuOpen?'is-open':''} aria-label="主選單">{nav.map(([id,label])=><button type="button" className={activeNav===id?'active':''} aria-current={activeNav===id?'page':undefined} onClick={()=>navigate(id)} key={id}>{label}</button>)}</nav>}{hostMode&&!focus&&<div className="sidebar-foot"><button type="button" className="text-button sidebar-logout" onClick={logout}>登出</button></div>}</div></aside>
  <main id="main-content" tabIndex={-1}>{focus?<h1 className="sr-only">{title}</h1>:<header className="topbar"><h1>{title}</h1><div className="top-actions">{showFamily&&(hostMode&&!familyLoading&&!familyConnected?<button type="button" className="ha-status ha-offline" onClick={()=>navigate('host')}><span className="connection-dot" aria-hidden="true"/>{familyLabel}<span className="sr-only">，前往主機維護</span></button>:<span className={`ha-status ${familyLoading?'ha-loading':familyConnected?'ha-connected':'ha-offline'}`} role="status"><span className="connection-dot" aria-hidden="true"/>{familyLabel}</span>)}{status?.paused&&<button type="button" className="pause-chip" onClick={resumeAi}>AI 已暫停 · 恢復</button>}</div></header>}
  {error&&<div className="global-error"><Notice error reason={error.code}><p>{error.text}</p><div className="row-actions"><Button secondary disabled={refreshing} onClick={()=>refresh()}>{refreshing?'重新連線中…':'重新連線'}</Button><button className="text-button" onClick={()=>{errorKind.current='';setError(null)}}>關閉提示</button></div></Notice></div>}
  {pendingView&&<div className="global-error"><Notice>設備授權尚未儲存。可以留在本頁儲存，或捨棄這次修改。<div className="row-actions"><Button secondary onClick={()=>setPendingView(null)}>繼續編輯</Button><button className="text-button" onClick={()=>{setDirty(false);location.hash=pendingView;setRoute({view:pendingView,task:null});setPendingView(null)}}>捨棄修改並離開</button></div></Notice></div>}
  {!ready?<div className="content"><Empty title={error?'暫時無法開啟':'正在連線'}>{hostMode?'請確認本機服務正在運作。':'請從家庭側欄重新開啟。'}</Empty></div>:<div className="content">
  {(['home','inventory'].includes(view)||view==='settings'&&!hostMode)&&<SetupGuide status={status} hostMode={hostMode} navigate={navigate} view={view}/>}
  {view==='home'&&<><div className="section-intro"><p>描述需求，先看提案，再交給家庭執行。</p></div>
    <div className="workspace"><section className="request-column"><Composer status={status} draft={draft} setDraft={setDraft} onCreated={openTask} onSetup={()=>navigate('settings')}/><div className="section-heading"><h2>最近任務</h2><button className="text-button" onClick={()=>navigate('history')}>查看全部</button></div>{tasks.length===0?<Empty title="還沒有提案" action={<Button secondary onClick={()=>document.getElementById('request')?.focus()}>寫下需求</Button>}>描述想自動完成的事，你會先看到規則。</Empty>:<div className="task-list">{tasks.slice(0,8).map(t=><button className={`task-row ${selected===t.id?'selected':''}`} key={t.id} onClick={()=>openTask(t)}><div><Status value={t.state}/><small>{timestamp(t.created_at)}</small></div><h3>{t.request}</h3><p>{t.message}</p></button>)}</div>}</section><section className="detail-column" aria-label="任務詳情">{detail?<TaskDetail key={detail.id} task={detail} onChange={()=>{load().catch(()=>{});api(`tasks/${detail.id}`).then(setDetail).catch(()=>{})}}/>:<Empty title={selected?'正在載入':'提案會顯示在這'} action={selected?undefined:<Button secondary onClick={()=>document.getElementById('request')?.focus()}>寫下需求</Button>}>{selected?'正在讀取這筆提案。':'先確認對象與效果，再批准執行。'}</Empty>}</section></div></>}
  {view==='inventory'&&<Inventory onSaved={refreshStatus} onDirty={setDirty}/>}
  {view==='automations'&&<Automations onTask={openTask} onCreate={()=>navigate('home')} onEdit={(id)=>{setDraft(`修改自動化 ${id}：`);navigate('home')}}/>}
  {view==='history'&&<><div className="section-intro"><p>回看每個需求、批准與結果。</p><Button secondary onClick={async()=>{try{download(new Blob([JSON.stringify(await api('evidence'),null,2)],{type:'application/json'}),'agentic-home-evidence.json')}catch(e){errorKind.current='';setError(plainError(e))}}}>匯出紀錄</Button></div><div className="history-table"><div className="history-head"><span>需求</span><span>AI</span><span>狀態</span><span>時間</span></div>{tasks.map(t=><button key={t.id} onClick={()=>openTask(t)}><strong>{t.request}</strong><span>{names[t.provider]||t.provider}</span><Status value={t.state}/><time>{timestamp(t.created_at)}</time></button>)}</div>{!tasks.length&&<Empty title="還沒有紀錄" action={<Button secondary onClick={()=>navigate('home')}>建立需求</Button>}>送出需求後，處理紀錄會留在這裡。</Empty>}</>}
  {view==='settings'&&<Settings hostMode={hostMode} status={status} onChange={refreshStatus}/>}
  {view==='entry'&&<HomeEntry automatic={dailyEntry&&active} navigate={navigate}/>}
  {(view==='gateway'||gatewayMounted)&&<div hidden={view!=='gateway'}><GatewayPanel key={conversationSession?.storage_scope||'legacy'} visible={view==='gateway'&&active} conversationSession={conversationSession}/></div>}
  {view==='workers'&&<WorkerPanel/>}
  {view==='agents'&&<><HermesPanel/><RuntimePanel/></>}
  {view==='onboarding'&&<Onboarding status={status} onChange={refreshStatus} navigate={navigate}/>}
  {view==='host'&&<HostPanel appStatus={status} onConnected={refreshStatus} onAgent={()=>navigate('settings')}/>}
  </div>}</main></div>
}

function Composer({status,draft,setDraft,onCreated,onSetup}:{status:any;draft:string;setDraft:(v:string)=>void;onCreated:(t:Task)=>void;onSetup:()=>void}) {
  const [provider,setProvider]=useState('codex'),[model,setModel]=useState(''),[providerChosen,setProviderChosen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{if(!status||providerChosen)return;const first=status.runner?.providers?.find((p:any)=>p.status==='connected');if(first)setProvider(first.provider);setProviderChosen(true)},[status,providerChosen]);
  const connected=status?.runner?.providers?.find((p:any)=>p.provider===provider)?.status==='connected';
  async function submit(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{const task=await api('tasks',{message:draft,provider,model});onCreated(task);setDraft('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const providerName={codex:'Codex',claude:'Claude Code',agy:'Antigravity',grok:'Grok Build'}[provider]||'AI';
  const paused=!!status?.paused,familyOff=status?.ha?.status!=='connected';
  const reason=paused?'AI 已暫停。':familyOff?'家庭尚未連線。':!connected?'login':'';
  return <form className="composer" onSubmit={submit}><div className="section-heading"><h2>你想讓家做什麼？</h2></div><label className="sr-only" htmlFor="request">家庭需求</label><textarea id="request" value={draft} onChange={e=>setDraft(e.target.value)} maxLength={5000} placeholder="例如：有人經過玄關時打開燈，先讓我看規則。" rows={5} required/><div className="composer-controls"><Button disabled={busy||!connected||familyOff||paused||!draft.trim()} type="submit">{busy?'送出中…':'送出需求'} <span aria-hidden>↑</span></Button></div>{reason==='login'?<p className="helper-text">先完成 {providerName} 登入。<button type="button" className="text-button" onClick={onSetup}>前往連線設定 →</button></p>:reason?<p className="helper-text">{reason}</p>:null}<details><summary>進階</summary><label>使用的 AI<select value={provider} onChange={e=>{setProvider(e.target.value);setProviderChosen(true)}}><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="agy">Antigravity</option><option value="grok">Grok Build</option></select></label><label className="model-field">模型（可留空）<input value={model} onChange={e=>setModel(e.target.value)} placeholder={provider==='codex'?'gpt-6-astra':'使用官方預設'} maxLength={100}/></label></details>{error&&<Notice error>{error}</Notice>}</form>
}


function Inventory({onSaved,onDirty}:{onSaved:()=>void;onDirty:(dirty:boolean)=>void}){
  const [entities,setEntities]=useState<Entity[]>([]),[permissions,setPermissions]=useState<Record<string,Permission>>({}),[filter,setFilter]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false),[loading,setLoading]=useState(true),[dirty,setDirty]=useState(false),[scope,setScope]=useState('all');
  async function load(){setLoading(true);setError('');try{const d=await api('inventory');setEntities(d.entities);setPermissions(Object.fromEntries(d.policy.entities.map((p:Permission)=>[p.entity_id,p])))}catch(e){setError((e as Error).message)}finally{setLoading(false)}}
  useEffect(()=>{load()},[]);
  function change(e:Entity,patch:Partial<Permission>){setSaved(false);setDirty(true);setPermissions(prev=>({...prev,[e.entity_id]:{...(prev[e.entity_id]||{entity_id:e.entity_id,read:false,write:'none',purpose:''}),...patch}}))}
  useEffect(()=>{onDirty(dirty);const unload=(event:BeforeUnloadEvent)=>{if(dirty){event.preventDefault();event.returnValue=''}};window.addEventListener('beforeunload',unload);return()=>window.removeEventListener('beforeunload',unload)},[dirty,onDirty]);
  async function save(){setBusy(true);setError('');try{await api('policy',{entities:Object.values(permissions)});setSaved(true);setDirty(false);onSaved()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const visible=entities.filter(e=>`${e.entity_id} ${e.name} ${e.area||''}`.toLowerCase().includes(filter.toLowerCase())&&(scope==='all'||(scope==='shared'?permissions[e.entity_id]?.read:e.availability!=='available')));
  return <><div className="section-intro"><p>選擇要分享給 AI 的設備；新設備預設不分享。</p></div>{error&&<Notice error>{error}</Notice>}{saved&&<Notice>已儲存。受影響的提案需要重新產生。</Notice>}<div className="toolbar"><label className="search"><span className="sr-only">搜尋設備</span><input type="search" placeholder="搜尋設備或房間" value={filter} onChange={e=>setFilter(e.target.value)}/></label><label className="inventory-filter"><span className="sr-only">設備篩選</span><select value={scope} onChange={e=>setScope(e.target.value)}><option value="all">全部設備</option><option value="shared">已分享</option><option value="unavailable">離線或狀態未知</option></select></label><span className="subtle">{visible.length} / {entities.length} 個設備</span><Button secondary onClick={load} disabled={loading||dirty}>{loading?'盤點中…':'重新盤點'}</Button></div><div className="inventory-table"><div className="inventory-head"><span>設備 / 房間</span><span>狀態</span><span>分享給 AI</span><span>操作範圍與用途</span></div>{visible.map(e=>{const p=permissions[e.entity_id];const helper=['input_boolean.agentic_motion_test','input_boolean.agentic_lamp_test'].includes(e.entity_id);return <div className="entity-row" key={e.entity_id}><div><strong>{e.name}</strong><code>{entityId(e.entity_id)}</code><small>{e.area||'未分配房間'}</small></div><div><span className={`entity-state ${e.availability!=='available'?'muted':''}`}>{stateName(e.state)}</span><small>{timestamp(e.observed_at)}</small></div><label className="check"><input type="checkbox" checked={p?.read||false} aria-label={`分享 ${e.entity_id}`} onChange={event=>change(e,{read:event.target.checked,...(!event.target.checked?{write:'none'}:{})})}/>允許讀取</label><div className="permission-controls"><select aria-label={`${e.entity_id} 操作權限`} value={p?.write||'none'} disabled={!p?.read} onChange={event=>change(e,{write:event.target.value,purpose:event.target.value==='dedicated_helper'?'測試用開關，不連接其他規則':p?.purpose||''})}><option value="none">唯讀</option>{helper&&<option value="dedicated_helper">測試用開關</option>}{e.domain==='light'&&<option value="lighting">一般照明</option>}{e.domain==='switch'&&<option value="low_risk_switch">已確認用途的低風險開關</option>}</select>{p?.write&&p.write!=='none'&&<input aria-label={`${e.entity_id} 用途`} placeholder="用途，例如：玄關燈" value={p.purpose} onChange={event=>change(e,{purpose:event.target.value})} maxLength={200}/>}</div></div>})}</div>{dirty&&<div className="save-bar" role="status"><span>設備授權有未儲存的修改</span><Button disabled={busy} onClick={save}>{busy?'儲存中…':'儲存修改'}</Button></div>}{loading&&<p className="subtle">正在讀取設備…</p>}{!loading&&!error&&!visible.length&&<Empty title="沒有符合的設備" action={<Button secondary disabled={dirty} onClick={()=>{if(filter||scope!=='all'){setFilter('');setScope('all')}else load()}}>{filter||scope!=='all'?'清除篩選':'重新盤點'}</Button>}>請確認家庭已連線，或調整搜尋。</Empty>}</>
}

function Automations({onTask,onEdit,onCreate}:{onTask:(t:Task)=>void;onEdit:(id:string)=>void;onCreate:()=>void}){
  const [items,setItems]=useState<any[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState('');
  useEffect(()=>{api('automations').then(d=>setItems(d.automations)).catch(e=>setError(e.message))},[]);
  async function propose(id:string,action:string){setBusy(id);try{onTask(await api(`automations/${id}/propose`,{action}))}catch(e){setError((e as Error).message)}finally{setBusy('')}}
  return <><div className="section-intro"><p>只列出這裡建立、並留有紀錄的規則。</p><a className="external-link" href={haPath("/config/automation/dashboard")} target={haTarget()} rel="noreferrer">開啟家庭自動化 ↗</a></div>{error&&<Notice error>{error}</Notice>}{items.map(a=><article className="automation-row" key={a.id}><div><span className="eyebrow">{a.enabled?'已啟用':'已停用'}</span><h2>{a.intent.title}</h2><p><code>{a.intent.trigger.entity_id}</code> {stateName(a.intent.trigger.from_state)} → {stateName(a.intent.trigger.to_state)}</p><code className="subtle">{a.id}</code></div><div className="row-actions"><Button secondary onClick={()=>onEdit(a.id)}>修改</Button><Button secondary disabled={busy===a.id} onClick={()=>propose(a.id,a.enabled?'disable':'enable')}>{a.enabled?'預覽停用':'預覽啟用'}</Button><details className="more-actions"><summary>更多</summary><Button secondary disabled={busy===a.id} onClick={()=>propose(a.id,'rollback')}>預覽回復</Button></details></div></article>)}{!items.length&&<Empty title="還沒有自動化" action={<Button secondary onClick={onCreate}>建立需求</Button>}>描述需求並批准後，規則會出現在這裡。</Empty>}</>
}

function codingDetail(value:any){
  const list=Array.isArray(value?.workers)?value.workers:null;
  if(!list) return '';
  const worker=list.find((item:any)=>item&&item.worker_id==='host-coder');
  if(!worker) return '';
  // Same reading as the 開發助手 page chip: a signed-in account cannot run jobs without the sandbox.
  if(worker.available!==true) return '尚未連接';
  const provider=Array.isArray(worker.providers)?worker.providers.find((item:any)=>item&&item.id==='codex'):null;
  const state=provider?.credential_state;
  if(typeof state!=='string'||!state) return '';
  return ['configured','inference_verified','connected'].includes(state)?'已就緒':'需要登入';
}
function channelDetail(status:any){
  const gateway=status?.gateway;
  if(!gateway||gateway.selected!==true) return '';
  const primary=gateway.preferences?.primary;
  if(typeof primary!=='string'||!names[primary]) return '';
  let state='';
  if(primary==='web') state=typeof gateway.web?.status==='string'?gateway.web.status:'';
  else if(Array.isArray(gateway.channels)){const current=gateway.channels.find((item:any)=>item&&item.id===primary);state=typeof current?.status==='string'?current.status:'';}
  const label=connection[state];
  return label?`${names[primary]} · ${label}`:'';
}
function Settings({status,onChange,hostMode}:{status:any;onChange:()=>void;hostMode:boolean}){
  const [sidebarOpen,setSidebarOpen]=useState(false);
  const [pairing,setPairing]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[services,setServices]=useState<any[]>([]),[passphrase,setPassphrase]=useState(''),[coding,setCoding]=useState('');
  async function loadServices(){setServices((await api('services')).services)}
  useEffect(()=>{loadServices().catch(e=>setError(e.message))},[]);
  useEffect(()=>{if(!hostMode)return;let alive=true;api('workers').then(value=>{if(alive)setCoding(codingDetail(value))}).catch(()=>{});return()=>{alive=false}},[hostMode]);
  async function pair(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{await api('pair',JSON.parse(pairing));setPairing('');setNotice('執行環境已配對');onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function service(id:string,action:string){setError('');try{await api(`services/${id}`,{action});await loadServices()}catch(e){setError((e as Error).message)}}
  const providers=status?.runner?.providers;
  const accountState=Array.isArray(providers)?(providers.some((item:any)=>item.status==='connected')?'已登入':'尚未登入'):'';
  const hub:[string,string,string][]=[[ '#agents','AI 帳號',accountState],[ '#gateway/channels','通訊',channelDetail(status)],[ '#workers','開發助手',coding],[ '#host','主機維護',''],[ '#home','家庭提案',''],[ '#onboarding','初次設定','']];
  if(hostMode)return <div className="settings-layout settings-simple">
    {error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    <section className="settings-ai" aria-labelledby="settings-ai-title"><div><h2 id="settings-ai-title">AI 狀態</h2><p className="settings-ai-state">{status?.paused?'已暫停':'運作中'}</p></div><Button secondary={!status?.paused} disabled={!status} onClick={async()=>{try{await api('pause',{paused:!status?.paused});await onChange()}catch(e){setError((e as Error).message)}}}>{status?.paused?'恢復 AI':'暫停 AI'}</Button><p className="helper-text">暫停時不接新需求，家中自動化照常運作。</p></section>
    <nav className="settings-hub row-actions" aria-label="設定入口">{hub.map(([href,label,detail])=><a className="button secondary" href={href} key={href}><span className="settings-link-text"><span>{label}</span>{detail&&<small>{detail}</small>}</span></a>)}</nav>
    <details className="settings-fold" onToggle={event=>setSidebarOpen(event.currentTarget.open)}><summary>智能管家側欄</summary>{sidebarOpen&&<BridgeMaintenance/>}</details>
    <details className="settings-fold"><summary>家庭服務</summary>
      {services.map(s=><article className="service-row" key={s.id}>
        <div className="section-heading"><h3>{s.title}</h3><Status value={s.state}/></div>
        <div className="row-actions"><a className="button secondary" href={haPath(s.path)} target={haTarget()} rel="noreferrer" onClick={()=>service(s.id,'start')}>開啟設定 ↗</a><Button secondary onClick={()=>service(s.id,'verify')}>檢查連線</Button><button className="text-button" onClick={()=>service(s.id,'skip')}>稍後設定</button></div>
        <details><summary>設定說明</summary><ol>{s.steps.map((step:string)=><li key={step}>{step}</li>)}</ol><a className="external-link" href={s.docs} target="_blank" rel="noreferrer">官方說明 ↗</a>{s.id==='alexa_control'&&<button className="text-button" onClick={()=>service(s.id,'confirm_voice_test')}>我已實際用 Alexa 指令控制設備</button>}{s.observed?.at&&<pre>{JSON.stringify(s.observed,null,2)}</pre>}</details>
      </article>)}
    </details>
    <details className="settings-fold"><summary>備份與復原</summary>
      <p className="helper-text">下載加密備份，家庭資料請另外保存。</p>
      <form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{download(await api('backups/export',{passphrase},true),'agentic-home-backup.enc');setPassphrase('');setNotice('已下載加密備份，請另外保管密碼。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>
        <label>備份加密密碼（至少 16 個字元）<input type="password" value={passphrase} onChange={e=>setPassphrase(e.target.value)} minLength={16} required autoComplete="new-password"/></label>
        <div className="row-actions"><Button secondary type="submit" disabled={busy}>下載加密備份</Button><a className="external-link" href={haPath('/config/backup/overview')} target={haTarget()} rel="noreferrer">家庭備份 ↗</a></div>
      </form>
      <details><summary>還原說明</summary><p className="helper-text">先停止服務，用 <code>restore_controller.py</code> 解密還原後再啟動；登入資料不在這份備份。</p></details>
    </details>
    <details className="settings-fold"><summary>紀錄保存</summary><Retention compact/></details>
    <details className="settings-fold"><summary>主機連線 <Status value={status?.runner?.protocol===1?'connected':status?.runner?.status||'unpaired'}/></summary>
      {status?.runner?.protocol!==1&&<Button secondary disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api('host/pair',{});await onChange();setNotice('主機已連接。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>{busy?'連線中…':'連接主機'}</Button>}
    </details>
    <p className="product-version">版本 {status?.version||''}</p>
  </div>;
  return <div className="settings-layout">{error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}<section className="settings-section"><div className="section-heading"><h2>執行環境</h2><Status value={status?.runner?.protocol===1?'connected':status?.runner?.status||'unpaired'}/></div>{status?.runner?.protocol===1?<p>執行環境已連線，家庭操作由這裡管理。</p>:<form onSubmit={pair}><p>貼上執行環境的一次性配對資料，10 分鐘內有效。</p><label>一次性配對資料<textarea value={pairing} onChange={e=>setPairing(e.target.value)} rows={3} autoComplete="off" spellCheck={false} required placeholder='{"host":"…","port":8443,"fingerprint":"…","code":"…"}'/></label><Button disabled={busy} type="submit">{busy?'連線中…':'配對執行環境'}</Button></form>}</section>
  <section className="settings-section"><h2>登入 AI 帳號</h2><p>家庭資料只送給你選擇的 AI。用量與計費由該官方帳號決定。</p><div className="provider-grid">{['codex','claude','agy','grok'].map(provider=><Provider key={provider} provider={provider} state={status?.runner?.providers?.find((p:any)=>p.provider===provider)} paired={status?.runner?.protocol===1} onChange={onChange}/>)}</div></section>
  <section className="settings-section"><h2>家庭服務</h2><p>帳號在家庭原本的流程完成，回來後再檢查連線。</p>{services.map(s=><article className="service-row" key={s.id}><div className="section-heading"><h3>{s.title}</h3><Status value={s.state}/></div><ol>{s.steps.map((step:string)=><li key={step}>{step}</li>)}</ol><div className="row-actions"><a className="button secondary" href={haPath(s.path)} target={haTarget()} rel="noreferrer" onClick={()=>service(s.id,'start')}>開啟原生設定 ↗</a><Button secondary onClick={()=>service(s.id,'verify')}>回來驗證</Button><button className="text-button" onClick={()=>service(s.id,'skip')}>稍後設定</button><a className="text-button" href={s.docs} target="_blank" rel="noreferrer">官方說明</a></div>{s.id==='alexa_control'&&<button className="text-button" onClick={()=>service(s.id,'confirm_voice_test')}>我已實際用 Alexa 指令控制設備</button>}{s.observed?.at&&<details><summary>查看連線觀測 · {timestamp(s.observed.at)}</summary><pre>{JSON.stringify(s.observed,null,2)}</pre></details>}</article>)}</section>
  <section className="settings-section"><h2>備份與復原</h2><p>下載加密備份後請另外保管，密碼不要放在一起。</p><form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{download(await api('backups/export',{passphrase},true),'agentic-home-backup.enc');setPassphrase('');setNotice('已下載加密備份，請另外保管密碼。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}><label>備份加密密碼（至少 16 個字元）<input type="password" value={passphrase} onChange={e=>setPassphrase(e.target.value)} minLength={16} required autoComplete="new-password"/></label><div className="row-actions"><Button secondary type="submit" disabled={busy}>下載加密備份</Button><a className="external-link" href={haPath("/config/backup/overview")} target={haTarget()} rel="noreferrer">家庭備份 ↗</a><a className="external-link" href={haPath("/config/updates")} target={haTarget()} rel="noreferrer">家庭更新 ↗</a></div></form><details><summary>還原說明</summary><p className="helper-text">先停止服務，用 <code>restore_controller.py</code> 解密還原後再啟動；登入資料不在這份備份。</p></details></section><Retention/></div>
}

function Retention({compact=false}:{compact?:boolean}){
  const [days,setDays]=useState({audit_days:30,context_days:7}),[message,setMessage]=useState(''),[error,setError]=useState('');
  useEffect(()=>{api('retention').then(setDays).catch(e=>setError(e.message))},[]);
  return <section className={compact?'retention-fields':'settings-section'}>{!compact&&<h2>紀錄保存</h2>}<p className={compact?'helper-text':undefined}>{compact?'縮短天數會立即清理到期資料；規則需要的紀錄會保留。':'已結束的任務依天數清理；管理規則所需的紀錄會保留，縮短天數會立即清理。'}</p><form onSubmit={async e=>{e.preventDefault();try{await api('retention',days);setMessage('保存設定已更新');setError('')}catch(e){setError((e as Error).message)}}}><label>任務紀錄（1–30 天）<input type="number" min={1} max={30} value={days.audit_days} onChange={e=>setDays({...days,audit_days:Number(e.target.value)})}/></label><label>AI 觀測資料（1–7 天）<input type="number" min={1} max={Math.min(7,days.audit_days)} value={days.context_days} onChange={e=>setDays({...days,context_days:Number(e.target.value)})}/></label><Button secondary type="submit">儲存保存天數</Button></form>{message&&<Notice>{message}</Notice>}{error&&<Notice error>{error}</Notice>}</section>
}


function Root(){
  const [deployment,setDeployment]=useState<any>(null),[error,setError]=useState<{text:string;code?:string}|null>(null),[mounted,setMounted]=useState(false),[desktopError,setDesktopError]=useState(false),[desktopEntry,setDesktopEntry]=useState(false);
  const load=useCallback(async()=>{setError(null);try{let result=await api('deployment');configureDeployment(result.mode);if(result.mode==='host'){setDesktopError(await desktopLogin()==='failed');if(consumeDesktopEntry()){setDesktopEntry(true);result=await api('deployment')}}setDeployment(result);if(result.mode==='ingress'||result.mode==='host'&&result.authenticated)setMounted(true)}catch(e){setError(plainError(e))}},[]);
  useEffect(()=>{load();const expire=()=>setDeployment((d:any)=>({...d,authenticated:false}));window.addEventListener('agentic-session-expired',expire);return()=>window.removeEventListener('agentic-session-expired',expire)},[load]);
  if(!deployment)return <main className="host-login"><h1>{error?'服務尚未回應':'正在開啟主機'}</h1>{error?<><p>請確認主機後再試一次。</p><p className="helper-text" role="alert" data-reason={error.code}>{error.text}</p><Button onClick={load}>重試連線</Button></>:<p role="status">正在確認這台主機。</p>}</main>;
  if(deployment.mode==='bridge')return <BridgeApp deployment={deployment} onRetry={load}/>;
  if(!['host','ingress'].includes(deployment.mode))return <main className="host-login"><h1>無法確認入口</h1><p>請重新連線後再試。</p><Button secondary onClick={load}>重新連線</Button></main>;
  const loggedIn=deployment.mode==='ingress'||deployment.authenticated;
  return <>{!loggedIn&&<HostLogin expired={mounted} desktopError={desktopError} onLogin={()=>{setDeployment({...deployment,authenticated:true});setMounted(true)}}/>}{mounted&&<div hidden={!loggedIn}><App hostMode={deployment.mode==='host'} active={loggedIn} desktopEntry={desktopEntry} onLogout={()=>{setDesktopEntry(false);Object.keys(sessionStorage).filter(key=>key.startsWith('agentic-auth-')||key==='agentic-host-task').forEach(key=>sessionStorage.removeItem(key));setMounted(false);setDeployment({...deployment,authenticated:false})}}/></div>}</>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><Root/></StrictMode>);
