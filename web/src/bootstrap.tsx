import {HomeLanFold} from './ha-lan';
import {useCallback,useEffect,useRef,useState} from 'react';
import {api,haPath,requestKey,session} from './api';
import {plainError} from './labels';
import {Button,Notice,friendlySentence,timestamp} from './ui';
import {Provider} from './providers';
import {ChannelSettings,ManualOnboarding} from './channels';
import {HermesPanel} from './hermes';
import {HomeEntry} from './home-entry';
import type {View} from './setup';

type ProviderId='codex'|'claude'|'agy'|'grok';
type Step={id:string;state:string;code?:string|null;verified_at?:string|null};
type Bootstrap={contract:number;id:string|null;state:string;provider:ProviderId|null;channel:string;current_step:string|null;steps:Step[];human_action:null|{kind:string;message?:string;provider?:string;channel?:string;code?:string};updated_at:string|null};
const legacyGroups=[{title:'主機',ids:['network','runner_pairing']},{title:'AI 帳號',ids:['cli_auth','cli_assist']},{title:'智能管家',ids:['hermes_install','hermes_auth']},{title:'通訊',ids:['channel']},{title:'家庭',ids:['ha_provision','ha_native']}];
const butlerGroups=[{title:'準備家庭',ids:['network','runner_pairing','ha_provision']},{title:'智能管家',ids:['hermes_install','hermes_auth']},{title:'連接家庭',ids:['ha_native']}];
const stepNames:Record<string,string>={network:'檢查網路',runner_pairing:'連接主機',cli_auth:'登入 AI 帳號',cli_assist:'確認設定',hermes_install:'準備智能管家',hermes_auth:'登入智能管家',channel:'選擇通訊',ha_provision:'啟動家庭',ha_native:'設定家庭'};
const states:Record<string,string>={idle:'尚未開始',pending:'尚未開始',running:'處理中',waiting_input:'等你操作',blocked:'需要處理',completed:'已完成',skipped:'已略過'};
const human=['cli_login','hermes_login','channel_setup','ha_native_onboarding'];
const retryCopy:Record<string,string>={
  hermes_rate_limited:'供應商暫時限制請求，請稍後再重試驗證。',
  hermes_provider_unavailable:'模型服務暫時無法連線，請稍後再重試驗證。',
  hermes_verification_timeout:'這次模型驗證逾時，請稍後再重試。',
  hermes_verification_failed:'智能管家的連線驗證沒有完成，請檢查登入後重試。',
  hermes_install_failed:'智能管家還沒準備好，請查看主機後重試。',
  ha_provision_failed:'家庭環境尚未就緒，請查看主機後重試。',
  cli_assist_failed:'設定確認沒有完成，請檢查登入後重試。',
  cli_assist_expired:'這次設定確認已過期，請重試。',
  needs_reauth:'登入已失效，請重新登入。',
  rate_limited:'已達用量上限，請稍後再試。',
  invalid_credentials:'登入資訊無效，請重新輸入。',
  network:'無法連線到 AI 服務，請檢查網路後再試。',
  cli_output_unrecognized:'無法確認登入結果，請稍後再試。',
  timeout:'AI 帳號回應逾時，請稍後再試。',
};

function SelectedCli({provider,onChange}:{provider:ProviderId;onChange:()=>void}){
  const [runtime,setRuntime]=useState<any>(null),[error,setError]=useState('');
  const ticket=useRef(0);
  const refresh=useCallback(async()=>{const current=++ticket.current;try{const value=(await api('runtimes')).host;if(current===ticket.current){setRuntime(value);setError('')}}catch(e){if(current===ticket.current)setError((e as Error).message)}},[]);
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){await refresh();if(active)timer=setTimeout(poll,5000)}poll();return()=>{active=false;ticket.current++;clearTimeout(timer)}},[refresh]);
  const changed=useCallback(()=>{void refresh();onChange()},[refresh,onChange]);
  return <div className="bootstrap-auth">{error&&<Notice error>{error}</Notice>}<Provider key={provider} provider={provider} runtime="host" paired={runtime?.protocol===1} unavailable={!!error||runtime?.status==='unavailable'} state={runtime?.providers?.find((p:any)=>p.provider===provider)} onChange={changed} subscriptionFirst/></div>
}

function NativeHome({onChange,ownerRequired=false}:{onChange:()=>void;ownerRequired?:boolean}){
  const [token,setToken]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [authorization,setAuthorization]=useState<{state:string;error_code?:string|null}|null>(null),[statusError,setStatusError]=useState('');
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function refresh(){try{const value=await api('host/authorization');if(active){setAuthorization(value);setStatusError('')}}catch(e){if(active)setStatusError((e as Error).message)}if(active)timer=setTimeout(refresh,2500)}refresh();return()=>{active=false;clearTimeout(timer)}},[]);
  async function connect(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{await api('host/connect',{token:token.trim()});setToken('');await session();onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function authorize(){setBusy(true);setError('');try{const result=await api('host/authorize',{});const url=new URL(result.url);if(url.origin!==new URL(haPath()).origin||url.pathname!=='/auth/authorize')throw new Error('家庭授權位置不正確，請重新檢查主機設定。');location.assign(url.toString())}catch(e){setError((e as Error).message);setBusy(false)}}
  return <section aria-label="設定家庭"><h2>{ownerRequired?'連接你的家庭':'設定你的家庭'}</h2><ol className="native-home-steps"><li><span className="step-number" aria-hidden>1</span><div><h3>建立家庭帳號</h3><p className="helper-text">建立家庭擁有者帳號後回來。</p><a className="button secondary" href={haPath()} target="_blank" rel="noreferrer">開啟家庭設定 ↗</a></div></li><li><span className="step-number" aria-hidden>2</span><div><h3>連接智能管家</h3><p className="helper-text">在家庭確認授權。</p><Button disabled={busy||authorization?.state==='exchanging'} onClick={authorize}>{busy?'準備家庭授權…':'授權連接家庭'}</Button></div></li></ol><p className="helper-text">授權有效一年，可隨時在家庭撤銷。</p>{error&&<Notice error>{error}</Notice>}{statusError&&<Notice error>{statusError}</Notice>}{authorization?.state==='failed'&&<Notice error>家庭授權尚未完成，請用家庭擁有者帳號重試。</Notice>}{authorization?.state==='expired'&&<Notice error>家庭授權已過期，請重試。</Notice>}{authorization?.state==='exchanging'&&<p role="status">正在確認授權…</p>}{authorization?.state==='connected'&&<p role="status">已授權，正在確認連線…</p>}
    <details><summary>進階：使用權杖</summary><p>到 <a href={haPath('/profile/security')} target="_blank" rel="noreferrer">個人資料 → 安全性 ↗</a> 建立長效權杖。權杖不會交給 AI。</p><form onSubmit={connect}><label>家庭權杖<input type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} required/></label><Button type="submit" disabled={busy||!token.trim()}>{busy?'驗證中…':'驗證並連接家庭'}</Button></form></details></section>
}

export function Onboarding({status,onChange,navigate}:{status:any;onChange:()=>void;navigate:(view:View)=>void}){
  const [data,setData]=useState<Bootstrap|null>(null),[busy,setBusy]=useState(false),[readError,setReadError]=useState<{text:string;code?:string}|null>(null),[error,setError]=useState(''),[manual,setManual]=useState(false);
  const pending=useRef<{signature:string;key:string}|null>(null),refreshTicket=useRef(0);
  const refresh=useCallback(async()=>{const ticket=++refreshTicket.current;try{const next=await api('bootstrap');if(ticket!==refreshTicket.current)return;if(next.contract!==1||!Array.isArray(next.steps))throw new Error('設定服務尚未提供可用的進度，請重試或開啟手動設定。');setData(next);setReadError(null);if(next.id&&pending.current?.signature.startsWith('["start",')){pending.current=null;setError('')}}catch(e){if(ticket===refreshTicket.current)throw e}},[]);
  useEffect(()=>{let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{await refresh()}catch(e){if(alive)setReadError(plainError(e))}if(alive)timer=setTimeout(poll,2500)};poll();return()=>{alive=false;refreshTicket.current++;clearTimeout(timer)}},[refresh]);
  const changed=useCallback(()=>{refresh().catch(e=>setReadError(plainError(e)));onChange()},[refresh,onChange]);
  async function mutate(path:string,body:unknown){setBusy(true);setError('');const signature=JSON.stringify([path,body]);if(pending.current?.signature!==signature)pending.current={signature,key:requestKey()};try{await api(`bootstrap/${path}`,body,false,pending.current.key);pending.current=null;await refresh();onChange()}catch(e){setError((e as Error).message);throw e}finally{setBusy(false)}}
  const action=data?.human_action,started=!!data?.id,complete=data?.state==='completed';
  const selected=data?.provider||'codex';
  const butler=!data?.id||data.steps.some(step=>step.id==='cli_auth'&&step.code==='coding_setup_deferred');
  const groups=butler?[...butlerGroups,...(data?.id&&data.channel!=='web'?[{title:'通訊',ids:['channel']}]:[])]:legacyGroups;
  const current=data?.steps.find(s=>s.id===data.current_step);
  const activeGroup=groups.findIndex(group=>group.ids.includes(data?.current_step||''));
  const disconnected=!!readError;
  const blocked=!!data&&(data.state==='blocked'||action?.kind==='retry');
  const retryCode=action?.code||current?.code||'';
  const retrySentence=blocked?friendlySentence(action?.message,retryCopy[retryCode]||(typeof action?.message==='string'?action.message:'')):'';
  const cue=data?.current_step==='hermes_install'?'正在準備智能管家。':data?.current_step==='ha_provision'?'正在建立家庭環境，請保持電源與網路。':data?.current_step==='cli_assist'?'助手正在確認設定…':'正在檢查這個步驟。';
  return <div className="onboarding bootstrap">
    <ol className="onboarding-steps bootstrap-steps" aria-label="自動設定進度">{groups.map((group,index)=>{const entries=data?.steps.filter(step=>group.ids.includes(step.id))||[];const done=started&&entries.length===group.ids.length&&entries.every(step=>['completed','skipped'].includes(step.state));const skipped=done&&entries.every(step=>step.state==='skipped');const here=activeGroup===index;return <li key={group.title} aria-current={here?'step':undefined} className={done?'complete':''}><span className="step-dot" aria-hidden>{skipped?'—':done?'✓':index+1}</span>{done&&!skipped&&<span className="sr-only">已完成</span>}<span className={here||!started?'step-label':'step-label step-label-quiet'}>{group.title}</span>{skipped&&<small>已略過</small>}</li>})}</ol>
    {readError&&<Notice error reason={readError.code}><span>請重試連線。</span><p>{readError.text}</p><Button secondary disabled={busy} onClick={()=>refresh().catch(e=>setReadError(plainError(e)))}>重試連線</Button></Notice>}{error&&<Notice error>{error}</Notice>}
    {!started&&data&&<HomeLanFold setup/>}
    <div className="onboarding-body" aria-busy={busy}>{!data?<p role="status">正在讀取設定進度…</p>:!started?<section aria-label="準備家庭與智能管家"><h2>開始準備你的家庭</h2><p>先準備家庭環境，再登入智能管家。開發助手和通訊可稍後啟用。</p><form onSubmit={e=>{e.preventDefault();mutate('start',{provider:'codex',channel:'web'}).catch(()=>{})}}><Button type="submit" disabled={busy||disconnected}>{busy?'正在開始…':'開始設定'}</Button><details><summary>詳細說明</summary><p>開始即會安裝並維護智能管家側欄；不改家庭的系統更新，你停止或移除後不會自動裝回，可隨時在設定撤銷。</p></details></form></section>:complete?<HomeEntry completion navigate={navigate}/>:<>
      {retrySentence&&<Notice error>{retrySentence}</Notice>}
      {disconnected?<p className="helper-text">等待恢復連線。</p>:action?.kind==='cli_login'?<SelectedCli provider={selected} onChange={changed}/>:action?.kind==='hermes_login'?<HermesPanel guided/>:action?.kind==='channel_setup'?<ChannelSettings guided initialChannel={data.channel} onChannelChange={channel=>mutate('preferences',{channel})} onChange={changed}/>:action?.kind==='ha_native_onboarding'?<NativeHome onChange={changed} ownerRequired={action.code==='ha_owner_required'}/>:data.state==='blocked'||action?.kind==='retry'?<Button disabled={busy} onClick={()=>mutate('retry',{}).catch(()=>{})}>{busy?'重新檢查中…':'重試這一步'}</Button>:<p role="status">{cue}</p>}
      <details><summary>技術資料</summary><ol className="bootstrap-log">{data.steps.map(step=><li key={step.id}><strong>{stepNames[step.id]||step.id}</strong><span>{states[step.state]||step.state}</span>{step.code&&<code>{step.code}</code>}{step.verified_at&&<small>{timestamp(step.verified_at)}</small>}</li>)}</ol>{current?.code&&<p className="helper-text">目前狀態：{current.code}</p>}</details>
    </>}</div>
    {!complete&&<div className="onboarding-actions"><button className="text-button" type="button" onClick={()=>navigate('gateway')}>稍後再設定</button></div>}
    <details className="bootstrap-manual" onToggle={e=>setManual(e.currentTarget.open)}><summary>其他設定方式</summary>{manual&&<ManualOnboarding status={status} onChange={onChange} navigate={navigate}/>}</details>
  </div>
}
