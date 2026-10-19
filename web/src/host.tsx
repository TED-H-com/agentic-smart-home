import {useCallback,useEffect,useRef,useState} from 'react';
import {api,haPath,requestKey,session} from './api';
import {names,plainError} from './labels';
import {MaintenanceSettings} from './maintenance';
import {ApplianceStatus} from './appliance';
import {Confirm,Notice,timestamp} from './ui';

type Fault={text:string;code?:string};

const actionLabels:Record<string,string>={provision:'建立',start:'啟動',shutdown:'正常關機',snapshot:'建立快照',restore:'還原快照',configure_lan:'設定家庭區網'};
const jobLabels:Record<string,string>={running:'處理中',succeeded:'已完成',failed:'未完成',needs_reconcile:'需要確認'};
const time=(value:number)=>new Date(value*1000).toLocaleString('zh-TW',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});

export function HostLogin({onLogin,expired=false,desktopError=false}:{onLogin:()=>void;expired?:boolean;desktopError?:boolean}){
  const [key,setKey]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[show,setShow]=useState(false);
  return <main className="host-login"><h1>登入主機</h1><p>{expired?'登入已到期，重新登入後會回到剛才的頁面。':'用主機登入金鑰開啟管理中心。'}</p>
    {desktopError&&<p className="notice error" role="alert">桌面登入已過期或未完成。請重新開啟桌面入口，或貼上主機金鑰。</p>}
    <form className="login-form" onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{await api('host/login',{key:key.trim()});setKey('');onLogin()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>
      <label htmlFor="host-key">主機登入金鑰</label><div className="secret-field"><input id="host-key" type={show?'text':'password'} value={key} onChange={e=>setKey(e.target.value)} autoComplete="current-password" autoFocus required/><button className="text-button" type="button" aria-pressed={show} onClick={()=>setShow(!show)}>{show?'隱藏':'顯示'}</button></div>
      {error&&<div role="alert" className="notice error"><p>{error}</p><button className="text-button" type="button" onClick={()=>setError('')}>關閉提示</button></div>}<button disabled={busy||!key.trim()}>{busy?'登入中…':'開啟管理中心'}</button>
    </form><details><summary>找不到金鑰？</summary><p>打開家目錄的 <strong>Agentic-Home.txt</strong>，複製「主機登入金鑰」。</p><p>或在終端執行：</p><pre>sudo agentic-home access-key</pre><p className="helper-text">從另一台電腦使用時，請用 SSH tunnel 同時轉送 8099 與 8123。</p></details></main>;
}

export function HostPanel({onConnected,onAgent,appStatus}:{onConnected:()=>void;onAgent:()=>void;appStatus:any}){
  const [status,setStatus]=useState<any>(null),[token,setToken]=useState(''),[busy,setBusy]=useState('');
  const [error,setError]=useState<Fault|null>(null),[connectionError,setConnectionError]=useState<Fault|null>(null),[notice,setNotice]=useState('');
  // An answer-read failure clears itself once the next read of that answer succeeds.
  const answerFault=useRef(false),cowPauseKey=useRef<string|null>(null);
  const [restore,setRestore]=useState(''),[diagnosis,setDiagnosis]=useState<any>(null),[editToken,setEditToken]=useState(false);
  const [question,setQuestion]=useState(''),[provider,setProvider]=useState('codex'),[providerChosen,setProviderChosen]=useState(false),[answer,setAnswer]=useState<any>(null),[confirmShutdown,setConfirmShutdown]=useState(false);
  const reload=useCallback(async()=>{try{const result=await api('host/status');setStatus(result);setConnectionError(null)}catch(e){setConnectionError(plainError(e))}},[]);
  useEffect(()=>{let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{await reload();if(alive)timer=setTimeout(poll,4000)};poll();return()=>{alive=false;clearTimeout(timer)}},[reload]);
  useEffect(()=>{const id=sessionStorage.getItem('agentic-host-task');if(id)api(`host/tasks/${id}`).then(setAnswer).catch(()=>sessionStorage.removeItem('agentic-host-task'))},[]);
  useEffect(()=>{if(answer?.state!=='planning')return;let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{const result=await api(`host/tasks/${answer.id}`);if(alive){setAnswer(result);if(answerFault.current){answerFault.current=false;setError(null)}}}catch(e){if(alive){answerFault.current=true;setError(plainError(e))}}if(alive)timer=setTimeout(poll,2000)};timer=setTimeout(poll,2000);return()=>{alive=false;clearTimeout(timer)}},[answer?.id,answer?.state]);
  const providers=appStatus?.runner?.providers||[];
  useEffect(()=>{if(!appStatus||providerChosen||!providers.length)return;const first=providers.find((p:any)=>p.status==='connected');if(first)setProvider(first.provider);setProviderChosen(true)},[appStatus,providerChosen,providers.length]);
  const providerReady=providers.some((p:any)=>p.provider===provider&&p.status==='connected');
  async function action(action:string,snapshot?:string,key?:string){setBusy(action);setError(null);setNotice('');try{await api('host/actions',{action,snapshot:snapshot||null},false,key);if(key===cowPauseKey.current)cowPauseKey.current=null;setNotice('操作已交給主機處理，請保持主機電源。');setRestore('');await reload()}catch(e){setError(plainError(e))}finally{setBusy('')}}
  async function connect(e:React.FormEvent){e.preventDefault();setBusy('connect');setError(null);try{await api('host/connect',{token:token.trim()});setToken('');await session();await onConnected();await reload();setEditToken(false);setNotice('家庭已連接。')}catch(e){setError(plainError(e))}finally{setBusy('')}}
  const running=status?.jobs?.some((j:any)=>j.state==='running');
  const unavailable=!status||!!connectionError;
  const cowBlocked=['critical','invalid','unavailable'].includes(status?.root_cow?.state);
  const haConnected=appStatus?.ha?.status==='connected';
  const runnerUnavailable=appStatus?.runner?.status==='unavailable';
  const vmLabels:Record<string,string>={'running':'運作中','shut off':'已關機','not_created':'尚未建立'};
  const vmText=vmLabels[status?.vm]||status?.vm||'讀取中';
  const activeJob=status?.jobs?.find((j:any)=>j.state==='running');
  const tokenForm=<form onSubmit={connect}><label>家庭權杖<input type="password" value={token} onChange={e=>setToken(e.target.value)} autoComplete="off" required/></label><div className="row-actions"><button disabled={!!busy||!token.trim()}>{busy==='connect'?'驗證中…':'驗證並連接'}</button>{editToken&&<button type="button" className="text-button" onClick={()=>{setToken('');setEditToken(false)}}>取消</button>}</div></form>;
  return <div className="host-page">
    {connectionError&&<Notice error reason={connectionError.code}><p>無法讀取主機狀態。</p><p>{connectionError.text}</p><button className="text-button" onClick={reload}>重試連線</button></Notice>}
    {error&&<Notice error reason={error.code}><p>這項操作沒有完成。</p><p>{error.text}</p><button className="text-button" onClick={()=>setError(null)}>關閉提示</button></Notice>}
    {notice&&<div className="notice" role="status">{notice}</div>}
    <section className="host-overview" aria-label="主機概況"><h2>{status?.profile?.label||'正在讀取主機…'}</h2><dl aria-live="polite"><div><dt>家庭連線</dt><dd>{!appStatus?'讀取中':haConnected?'已連接':status?.ha_connected?'等待回應':'尚未連接'}</dd></div><div><dt>可用空間</dt><dd>{typeof status?.storage_free_bytes==='number'?`${(status.storage_free_bytes/1024**3).toFixed(1)} GB`:'尚無資料'}</dd></div></dl></section>
    {status?.root_cow?.message&&<Notice error={["critical","invalid","unavailable"].includes(status.root_cow.state)}><p>{status.root_cow.message}</p>{status.root_cow.state==='critical'&&status.vm==='running'&&<><p>暫停後家庭自動化也會停止；其他系統寫入仍可能用盡還原點空間。</p><button className="secondary" disabled={!!busy||running||unavailable} onClick={()=>{cowPauseKey.current??=requestKey();void action('shutdown',undefined,cowPauseKey.current)}}>暫停 HA 以保護還原點</button></>}</Notice>}
    <section className="settings-section vm-card" aria-label="家庭系統"><div className="section-heading"><h2>家庭系統</h2><span className={`status ${status?.vm==='running'?'s-connected':''}`}>{vmText}</span></div>
      <div className="row-actions vm-actions">{status?.vm!=='running'&&<button disabled={!!busy||running||unavailable} onClick={()=>action(status?.provisioned?'start':'provision')}>{status?.provisioned?'啟動':'建立'}</button>}{status?.vm==='running'&&haConnected&&<a className="button" href={haPath()} target="_blank" rel="noreferrer">開啟家庭 ↗</a>}{status?.vm==='running'&&!confirmShutdown&&<button type="button" className="secondary" disabled={!!busy||running||unavailable} onClick={()=>setConfirmShutdown(true)}>正常關機</button>}<button className="text-button" disabled={!!busy||unavailable} onClick={async()=>{setBusy('diagnose');setError(null);try{setDiagnosis(await api('host/diagnosis'))}catch(e){setError(plainError(e))}finally{setBusy('')}}}>{busy==='diagnose'?'檢查中…':'檢查連線'}</button></div>
      {status?.vm==='running'&&confirmShutdown&&<Confirm sentence="關閉家庭系統？家中自動化會暫停。" confirmLabel="確認關機" busy={busy==='shutdown'} onConfirm={()=>{void action('shutdown').finally(()=>setConfirmShutdown(false))}} onCancel={()=>setConfirmShutdown(false)}/>}
      {status?.vm==='running'&&!haConnected&&<p className="helper-text">家庭系統已啟動，第一次開啟可能還在準備。</p>}
      {activeJob&&<div className="notice operation-progress" role="status"><strong>{actionLabels[activeJob.request.action]||activeJob.request.action}處理中</strong>{activeJob.request.action==='snapshot'&&<p>正在關機並複製磁碟，完成後會恢復原本的開關狀態。</p>}</div>}
      {diagnosis&&<div className="notice" role="status"><p>{diagnosis.findings.length?diagnosis.findings.join(' '):'主機與家庭都有回應。'}</p></div>}
      <details><summary>更多</summary><p className="vm-spec">{status?.profile?`${status.profile.memory_mib/1024} GB 記憶體 · ${status.profile.vcpus} vCPU`:'讀取配置中…'}</p><p>設備可依 IP 加入。自動探索需要連通家庭區網，可在「家庭區網接入」選擇有線網路。</p><p className="helper-text">遠端使用請同時轉送 8099 與 8123。</p>{haConnected&&(editToken?tokenForm:<button className="text-button" type="button" onClick={()=>setEditToken(true)}>更新授權</button>)}</details>
    </section>
    <ApplianceStatus value={status?.appliance} unavailable={unavailable}/>
    {!haConnected&&<section className="settings-section"><h2>連接你的家庭</h2><ol className="connect-steps"><li><a href={haPath()} target="_blank" rel="noreferrer">開啟 Home Assistant</a>，建立家庭帳號。</li><li>到 <a href={haPath('/profile/security')} target="_blank" rel="noreferrer">個人資料 → 安全性</a> 建立權杖。</li><li>貼到下方並連接。</li></ol><p className="helper-text">權杖由主機保存，不傳給 AI。</p>{tokenForm}</section>}
    <section className="settings-section host-agent"><div><h2>問問主機狀況</h2></div><div>{!providerReady||runnerUnavailable?<div className="host-agent-login">{runnerUnavailable?<span>等待 AI 帳號恢復連線…</span>:<><span>尚未登入 AI 帳號。</span><button type="button" className="text-button" onClick={onAgent}>登入 AI 帳號 →</button></>}</div>:<form onSubmit={async e=>{e.preventDefault();setBusy('ask');setError(null);try{const result=await api('host/ask',{message:question,provider});setAnswer(result);sessionStorage.setItem('agentic-host-task',result.id);setQuestion('')}catch(e){setError(plainError(e))}finally{setBusy('')}}}><label>想問什麼？<textarea rows={3} value={question} onChange={e=>setQuestion(e.target.value)} required maxLength={5000} placeholder="例如：家庭為什麼沒有啟動？"/></label><div className="row-actions"><select value={provider} onChange={e=>{setProvider(e.target.value);setProviderChosen(true)}} aria-label="AI 帳號">{providers.filter((p:any)=>p.status==='connected').map((p:any)=><option key={p.provider} value={p.provider}>{names[p.provider]||'AI 帳號'}</option>)}</select><button disabled={!!busy||answer?.state==='planning'||!question.trim()||appStatus?.paused}>{busy==='ask'?'送出中…':'送出'}</button></div></form>}
      {answer?.state==='planning'&&<div className="notice" role="status">正在讀取主機狀況…<button className="text-button" type="button" onClick={()=>api(`host/tasks/${answer.id}/cancel`,{}).catch(e=>setError(plainError(e)))}>取消查詢</button></div>}
      {answer?.answer&&<div className="notice agent-answer" role="status"><p>{answer.answer.message}</p>{answer.answer.suggested_action!=='none'&&<button className="secondary" disabled={!!busy||running||unavailable} onClick={()=>action(answer.answer.suggested_action)}>執行建議：{actionLabels[answer.answer.suggested_action]}</button>}</div>}{answer?.message&&<p role="alert">{answer.message}</p>}{answer?.state==='cancelled'&&<p role="status">查詢已取消，可以重新描述。</p>}
    </div></section>
    <details className="host-fold"><summary>快照 · {status?.snapshots?.length?`${status.snapshots.length} 份`:'尚無'}</summary><div className="maintenance-content"><p>建立快照會關閉家庭系統，期間家庭自動化會暫停。</p><button className="secondary" disabled={!!busy||running||unavailable||cowBlocked||!status?.provisioned} onClick={()=>action('snapshot')}>建立快照</button>
      {status?.snapshots?.length?<><p className="helper-text">還原會取代目前的家庭系統資料，請先正常關機。智能管家的紀錄不在快照內。</p>{status.snapshots.map((s:any)=><div className="service-row snapshot-row" key={s.id} title={s.id}><div className="section-heading"><code>{timestamp(typeof s.created_at==='number'?new Date(s.created_at*1000).toISOString():s.created_at)}</code><button className="secondary" disabled={!!busy||running||unavailable||status.vm!=='shut off'} onClick={()=>setRestore(s.id)}>選擇還原</button></div>{restore===s.id&&<div className="notice"><p>還原會取代目前的家庭系統資料。</p><div className="row-actions"><button className="danger" disabled={!!busy} onClick={()=>action('restore',s.id)}>還原這份快照</button><button className="secondary" type="button" onClick={()=>setRestore('')}>取消</button></div></div>}</div>)}</>:<p className="subtle">{status?'完成家庭設定後，可建立第一份快照。':'正在讀取快照…'}</p>}
    </div></details>
    <MaintenanceSettings version={appStatus?.version}/>
    <details className="host-history host-fold"><summary>操作紀錄 · {status?.jobs?.length?`${status.jobs.length} 筆`:'尚無'}</summary>{status?.jobs?.length?status.jobs.slice().reverse().map((job:any)=><div className="service-row" key={job.id}><div className="section-heading"><strong>{actionLabels[job.request.action]||job.request.action}</strong><span className={`status s-${job.state}`}>{jobLabels[job.state]||job.state}</span></div>{job.message&&<p>{job.message}</p>}<small>{time(job.at)}</small></div>):<p className="subtle">尚無操作紀錄。</p>}</details>
  </div>;
}
