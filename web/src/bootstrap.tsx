import {useCallback,useEffect,useRef,useState} from 'react';
import {api,haPath,requestKey,session} from './api';
import {Button,Notice,Status,timestamp} from './ui';
import {Provider} from './providers';
import {ChannelSettings,ManualOnboarding} from './channels';
import {HermesPanel} from './hermes';
import {HomeEntry} from './home-entry';
import type {View} from './setup';

type ProviderId='codex'|'claude'|'agy'|'grok';
type Step={id:string;state:string;code?:string|null;verified_at?:string|null};
type Bootstrap={contract:number;id:string|null;state:string;provider:ProviderId|null;channel:string;current_step:string|null;steps:Step[];human_action:null|{kind:string;message?:string;provider?:string;channel?:string;code?:string};updated_at:string|null};
const names:Record<string,string>={codex:'Codex',claude:'Claude Code',agy:'Antigravity',grok:'Grok Build'};
const groups=[{title:'網路與主機',short:'網路',ids:['network','runner_pairing']},{title:'登入助手',short:'助手',ids:['cli_auth','cli_assist']},{title:'設定管家',short:'管家',ids:['hermes_install','hermes_auth']},{title:'連接通訊',short:'通訊',ids:['channel']},{title:'啟動家庭',short:'家庭',ids:['ha_provision','ha_native']}];
const stepNames:Record<string,string>={network:'檢查網路',runner_pairing:'連接助手',cli_auth:'登入助手',cli_assist:'準備設定',hermes_install:'正在準備智能管家',hermes_auth:'登入 Hermes',channel:'選擇通訊方式',ha_provision:'正在啟動家庭',ha_native:'設定你的家庭'};
const states:Record<string,string>={idle:'尚未開始',pending:'尚未開始',running:'處理中',waiting_input:'等你操作',blocked:'需要處理',completed:'已完成',skipped:'已略過'};

function SelectedCli({provider,onChange}:{provider:ProviderId;onChange:()=>void}){
  const [runtime,setRuntime]=useState<any>(null),[error,setError]=useState('');
  const refresh=useCallback(async()=>{try{setRuntime((await api('runtimes')).host);setError('');onChange()}catch(e){setError((e as Error).message)}},[onChange]);
  useEffect(()=>{refresh()},[refresh]);
  return <div className="bootstrap-auth">{error&&<Notice error>{error}</Notice>}<Provider key={provider} provider={provider} runtime="host" paired={runtime?.protocol===1} state={runtime?.providers?.find((p:any)=>p.provider===provider)} onChange={refresh} subscriptionFirst/></div>
}

function NativeHome({onChange,ownerRequired=false}:{onChange:()=>void;ownerRequired?:boolean}){
  const [token,setToken]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [authorization,setAuthorization]=useState<{state:string;error_code?:string|null}|null>(null),[statusError,setStatusError]=useState('');
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function refresh(){try{const value=await api('host/authorization');if(active){setAuthorization(value);setStatusError('')}}catch(e){if(active)setStatusError((e as Error).message)}if(active)timer=setTimeout(refresh,2500)}refresh();return()=>{active=false;clearTimeout(timer)}},[]);
  async function connect(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{await api('host/connect',{token:token.trim()});setToken('');await session();onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function authorize(){setBusy(true);setError('');try{const result=await api('host/authorize',{});const url=new URL(result.url);if(url.origin!==new URL(haPath()).origin||url.pathname!=='/auth/authorize')throw new Error('家庭授權位置不正確，請重新檢查主機設定。');location.assign(url.toString())}catch(e){setError((e as Error).message);setBusy(false)}}
  return <section aria-label="Home Assistant 原生設定"><h2>{ownerRequired?'連接你的家庭':'設定你的家庭'}</h2><ol className="native-home-steps"><li><span className="step-number" aria-hidden>1</span><div><h3>建立家庭帳號</h3><p className="helper-text">建立管理者帳號後，回到這裡繼續。</p><a className={`button ${ownerRequired?'secondary':''}`} href={haPath()} target="_blank" rel="noreferrer">開啟 Home Assistant 設定 ↗</a></div></li><li><span className="step-number" aria-hidden>2</span><div><h3>連接智能管家</h3><p className="helper-text">由你在 Home Assistant 確認授權。</p><Button secondary={!ownerRequired} disabled={busy||authorization?.state==='exchanging'} onClick={authorize}>{busy?'準備家庭授權…':'授權連接家庭'}</Button></div></li></ol><p className="helper-text">此主機的授權有效一年，可隨時在 Home Assistant 撤銷。</p>{error&&<Notice error>{error}</Notice>}{statusError&&<Notice error>{statusError}</Notice>}{authorization?.state==='failed'&&<Notice error>家庭授權尚未完成，請用管理者帳號重試。</Notice>}{authorization?.state==='expired'&&<Notice error>家庭授權已過期，請重試。</Notice>}{authorization?.state==='exchanging'&&<p role="status">正在確認授權…</p>}{authorization?.state==='connected'&&<p role="status">已授權，正在確認連線…</p>}
    <details><summary>進階：使用 HA owner 權杖</summary><p>也可以到 Home Assistant 的 <a href={haPath('/profile/security')} target="_blank" rel="noreferrer">個人資料 → 安全性 ↗</a> 建立管理者的長效存取權杖。權杖不會交給 AI。</p><form onSubmit={connect}><label>HA owner 權杖<input type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} required/></label><Button type="submit" disabled={busy||!token.trim()}>{busy?'驗證管理者身份…':'驗證並連接家庭'}</Button></form></details><p className="helper-text">需要協助？<a href="#gateway">和智能管家聊天</a>。</p></section>
}

export function Onboarding({status,onChange,navigate}:{status:any;onChange:()=>void;navigate:(view:View)=>void}){
  const [data,setData]=useState<Bootstrap|null>(null),[provider,setProvider]=useState<ProviderId>('codex'),[busy,setBusy]=useState(false),[readError,setReadError]=useState(''),[error,setError]=useState(''),[manual,setManual]=useState(false);
  const pending=useRef<{signature:string;key:string}|null>(null);
  const refresh=useCallback(async()=>{const next=await api('bootstrap');if(next.contract!==1||!Array.isArray(next.steps))throw new Error('設定服務尚未提供可用的進度，請重試或開啟手動設定。');setData(next);setReadError('');if(next.id&&pending.current?.signature.startsWith('["start",')){pending.current=null;setError('')}},[]);
  useEffect(()=>{let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{await refresh()}catch(e){if(alive)setReadError((e as Error).message)}if(alive)timer=setTimeout(poll,2500)};poll();return()=>{alive=false;clearTimeout(timer)}},[refresh]);
  const changed=useCallback(()=>{refresh().catch(e=>setReadError(e.message));onChange()},[refresh,onChange]);
  async function mutate(path:string,body:unknown){setBusy(true);setError('');const signature=JSON.stringify([path,body]);if(pending.current?.signature!==signature)pending.current={signature,key:requestKey()};try{await api(`bootstrap/${path}`,body,false,pending.current.key);pending.current=null;await refresh();onChange()}catch(e){setError((e as Error).message);throw e}finally{setBusy(false)}}
  const action=data?.human_action,started=!!data?.id,complete=data?.state==='completed';
  const selected=data?.provider||provider;
  const current=data?.steps.find(s=>s.id===data.current_step);
  const activeGroup=groups.findIndex(group=>group.ids.includes(data?.current_step||''));
  const disconnected=!!readError;
  return <div className="onboarding bootstrap"><div className="onboarding-intro"><span className="eyebrow">{complete?'已就緒':started?`第 ${Math.max(1,activeGroup+1)} / 5 步`:'初次設定'}</span></div>
    <ol className="onboarding-steps bootstrap-steps" aria-label="自動設定進度">{groups.map((group,index)=>{const entries=data?.steps.filter(step=>group.ids.includes(step.id))||[];const done=started&&entries.length===group.ids.length&&entries.every(step=>['completed','skipped'].includes(step.state));const skipped=done&&entries.every(step=>step.state==='skipped');return <li key={group.title} aria-current={activeGroup===index?'step':undefined} className={done?'complete':''}><span className="step-number" aria-hidden>{skipped?'—':done?'✓':index+1}</span><span><strong><span className="step-full">{group.title}</span><span className="step-short" aria-hidden>{group.short}</span></strong><small>{skipped?'已略過':done?'已完成':activeGroup===index?(states[data?.state||'']||'處理中'):'尚未開始'}</small></span></li>})}</ol>
    {readError&&<Notice error>{readError}{data&&<p>進度已保留，請恢復連線後繼續。</p>}<Button secondary disabled={busy} onClick={()=>refresh().catch(e=>setReadError(e.message))}>重試連線</Button></Notice>}{error&&<Notice error>{error}</Notice>}
    <div className="onboarding-body" aria-busy={busy}>{!data?<p role="status">正在讀取設定進度…</p>:!started?<section aria-label="選擇初始化助手"><h2>先選一個 AI 助手</h2><p>登入後，其餘設定會自動接續。</p><form onSubmit={e=>{e.preventDefault();mutate('start',{provider,channel:'line'}).catch(()=>{})}}><label>使用的助手<select value={provider} disabled={busy||disconnected} onChange={e=>setProvider(e.target.value as ProviderId)}><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="grok">Grok Build</option><option value="agy">Antigravity</option></select></label>{provider==='claude'&&<p className="helper-text">Claude Code 目前使用官方 setup-token 登入。</p>}{provider==='agy'&&<Notice>Google 訂閱登入尚未整合。Antigravity 目前需要 Gemini API key。</Notice>}<Button type="submit" disabled={busy||disconnected}>{busy?'正在開始…':'開始設定'}</Button><p className="helper-text">完成後加入 HA 智能管家側欄。</p><details><summary>側欄與自動維護</summary><p className="helper-text">開始設定即允許安裝及維護本產品 App；不接管 HA 系統更新。你可在外層設定停止維護或撤銷連接，手動停止或移除 App 也會被保留。</p></details></form></section>:complete?<HomeEntry completion navigate={navigate}/>:<>
      {!['cli_login','hermes_login','channel_setup','ha_native_onboarding'].includes(action?.kind||'')&&<div className="section-heading bootstrap-stage-heading"><div><span className="eyebrow">{names[selected]}</span><h2>{data.state==='blocked'?'這一步需要重試':stepNames[data.current_step||'']||'繼續完成設定'}</h2></div><Status value={data.state==='blocked'?'needs_reconcile':data.state==='waiting_input'?'waiting_for_user':'running'}/></div>}
      {action?.message&&(data.state==='blocked'||action.kind==='retry')&&<Notice error>{action.message}</Notice>}
      {disconnected?<p className="helper-text">等待恢復連線。</p>:action?.kind==='cli_login'?<SelectedCli provider={selected} onChange={changed}/>:action?.kind==='hermes_login'?<HermesPanel guided/>:action?.kind==='channel_setup'?<ChannelSettings guided initialChannel={data.channel} onChannelChange={channel=>mutate('preferences',{channel})} onChange={changed}/>:action?.kind==='ha_native_onboarding'?<NativeHome onChange={changed} ownerRequired={action.code==='ha_owner_required'}/>:data.state==='blocked'||action?.kind==='retry'?<><Button disabled={busy} onClick={()=>mutate('retry',{}).catch(()=>{})}>{busy?'重新檢查中…':'重試這一步'}</Button></>:<p role="status">{data.current_step==='hermes_install'?'正在準備 Hermes，請稍候。':data.current_step==='ha_provision'?'正在建立家庭環境，請保持電源與網路。':data.current_step==='cli_assist'?'助手正在確認設定…':'正在檢查，完成後會自動繼續。'}</p>}
      <p className="helper-text bootstrap-resume">進度會保留，可稍後繼續。</p>
      <details><summary>查看設定詳情</summary><ol className="bootstrap-log">{data.steps.map(step=><li key={step.id}><strong>{stepNames[step.id]||step.id}</strong><span>{states[step.state]||step.state}</span>{step.code&&<code>{step.code}</code>}{step.verified_at&&<small>{timestamp(step.verified_at)}</small>}</li>)}</ol>{current?.code&&<p className="helper-text">目前狀態：{current.code}</p>}</details>
    </>}</div>
    {!complete&&<div className="onboarding-actions"><button className="text-button" onClick={()=>navigate('gateway')}>稍後設定，先聊天</button><button className="text-button" onClick={()=>navigate('host')}>主機狀態</button></div>}
    <details className="bootstrap-manual" onToggle={e=>setManual(e.currentTarget.open)}><summary>其他設定方式</summary>{manual&&<ManualOnboarding status={status} onChange={onChange} navigate={navigate}/>}</details>
  </div>
}
