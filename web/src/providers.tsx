import {useCallback,useEffect,useRef,useState} from 'react';
import {api,ApiError,haPath,haTarget} from './api';
import {Status,Notice,Button,CopyButton,friendlySentence} from './ui';
import {names,type Runtime} from './labels';
const runtimeNames:Record<Runtime,string>={host:'主機',haos:'家庭現場'};
const official=/^https:\/\/(auth\.openai\.com|accounts\.x\.ai|auth\.x\.ai)\//;
const repoUrl='https://github.com/TED-H-com/agentic-smart-home#stable';
const liveAuth=new Set(['pending','verifying']),endedAuth=new Set(['failed','expired','unverified','stop_unconfirmed']);
const completedAuth=new Set(['connected','configured','succeeded','failed','expired','cancelled','unverified','disconnected','interrupted']);
function savedProviderAuth(key:string){const id=sessionStorage.getItem(key);return id&&/^[a-zA-Z0-9_-]{1,100}$/.test(id)?{id,status:'restoring'}:null}
const accountCopy:Record<string,string>={needs_reauth:'登入已失效，請重新登入。',rate_limited:'已達用量上限，請稍後再試。',invalid_credentials:'登入資訊無效，請重新輸入。',network:'無法連線到 AI 服務，請檢查網路後再試。',cli_output_unrecognized:'無法確認登入結果，請稍後再試。',timeout:'AI 帳號回應逾時，請稍後再試。'};
const quietReasons=new Set(['rate_limited','network','timeout']);
function methodsFor(provider:string){return provider==='claude'?['oauth_token','api_key']:provider==='agy'?['api_key']:['device_code','api_key']}
export function Provider({provider,state,paired,onChange,runtime,subscriptionFirst=false,unavailable=false}:{provider:string;state:any;paired:boolean;onChange:()=>void;runtime?:'host'|'haos';subscriptionFirst?:boolean;unavailable?:boolean}){
  const prefix=runtime?`runtimes/${runtime}/`:'';
  const authKey=`agentic-auth-${runtime||'default'}-${provider}`;
  const name=provider==='codex'?'Codex':provider==='agy'?'Antigravity':provider==='grok'?'Grok Build':'Claude Code';
  const methods=methodsFor(provider);
  const [method,setMethod]=useState(['codex','grok'].includes(provider)?'device_code':subscriptionFirst&&provider==='claude'?'oauth_token':'api_key'),[secret,setSecret]=useState(''),[auth,setAuth]=useState<any>(()=>savedProviderAuth(authKey)),[busy,setBusy]=useState(false),[error,setError]=useState(''),[readError,setReadError]=useState('');
  const authRef=useRef(auth),authEpoch=useRef(0),suppressResume=useRef(false);
  function adopt(value:any){authRef.current=value;setAuth(value);if(!value||completedAuth.has(value.status))sessionStorage.removeItem(authKey);else if(value.id)sessionStorage.setItem(authKey,value.id)}
  useEffect(()=>()=>{authEpoch.current++},[provider,prefix,authKey]);
  // Another tab may own the login. The Runner reports it, so this card resumes it.
  const resume=state?.pending_session;
  useEffect(()=>{const current=authRef.current;if(!suppressResume.current&&resume&&liveAuth.has(resume.status)&&current?.id!==resume.id&&(!current||completedAuth.has(current.status)))adopt(resume)},[resume?.id,resume?.status,auth?.id,auth?.status]);
  useEffect(()=>{if(!auth?.id||!liveAuth.has(auth.status)&&auth.status!=='restoring')return;let alive=true,failures=0;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{const epoch=authEpoch.current;try{const result=await api(`${prefix}auth/${auth.id}`);if(!alive||epoch!==authEpoch.current)return;failures=0;adopt(result);setReadError('');if(!liveAuth.has(result.status)){onChange();return}}catch(e){if(!alive||epoch!==authEpoch.current)return;if(e instanceof ApiError&&e.status===404){adopt({...auth,status:'expired'});setReadError('');return}failures++;setReadError((e as Error).message)}if(alive)timer=setTimeout(poll,failures>=3?10000:auth.status==='verifying'?1500:3000)};timer=setTimeout(poll,auth.status==='restoring'?0:auth.status==='verifying'?1000:2000);return()=>{alive=false;clearTimeout(timer)}},[auth?.id,auth?.status,onChange,provider,prefix,authKey]);
  async function login(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{const result=await api(`${prefix}providers/${provider}/auth`,{method,secret});authEpoch.current++;suppressResume.current=false;adopt(result);setReadError('');setSecret('');onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function cancel(){setBusy(true);setError('');try{const result=await api(`${prefix}providers/${provider}/auth/${auth.id}/cancel`,{});authEpoch.current++;suppressResume.current=true;adopt(['cancelled','unverified'].includes(result.status)&&!result.reason?null:result);setReadError('');onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function logout(){setBusy(true);setError('');try{await api(`${prefix}providers/${provider}/logout`,{});authEpoch.current++;suppressResume.current=true;adopt(null);setReadError('');onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const restoring=auth?.status==='restoring',pending=auth?.status==='pending',checking=auth?.status==='verifying',terminal=!!auth&&endedAuth.has(auth.status),connected=state?.status==='connected'&&!pending&&!checking;
  const reason=terminal?String(auth?.reason||''):!auth&&!pending&&!checking?String(state?.reason||''):'';
  const quiet=quietReasons.has(reason);
  const hint=method==='api_key'?provider==='agy'?'請用 Gemini API 金鑰；尚不支援 Google 訂閱。':'使用 API 金鑰，可能另外計費。':method==='oauth_token'?'在自己的電腦執行 claude setup-token，再貼上。':provider==='codex'?'必要時請在 ChatGPT 開啟裝置碼登入。':'在官方頁面輸入代碼完成登入。';
  const ended=accountCopy[reason]?friendlySentence(auth?.message,accountCopy[reason]):auth?.status==='expired'?'登入已過期，請重新開始。':auth?.status==='stop_unconfirmed'?'登入程序還沒停止，請稍後再試。':auth?.message||'登入未完成，請重新開始。';
  const stateSentence=!auth&&reason&&accountCopy[reason]?friendlySentence(state?.message,accountCopy[reason]):!auth&&state?.message?state.message:'';
  return <article className="provider"><div className="section-heading"><h3>{name}</h3><Status value={unavailable||restoring||readError?'loading':checking?'checking':pending?'pending':quiet&&!connected?'retry_later':state?.status||'needs_reauth'} label={unavailable||readError?'等待連線…':restoring?'讀取狀態…':undefined}/></div>{error&&<Notice error>{error}</Notice>}{readError&&<Notice error>{readError}</Notice>}{connected?<Button secondary disabled={busy} onClick={logout}>登出</Button>:<>
    {terminal&&<Notice error>{ended}</Notice>}
    {stateSentence&&<Notice error>{stateSentence}</Notice>}
    <form onSubmit={login} hidden={restoring||pending||checking}>{methods.length>1&&<details><summary>其他登入方式</summary><label>登入方式<select value={method} disabled={busy} onChange={e=>setMethod(e.target.value)}>{methods.map(item=><option key={item} value={item}>{item==='device_code'?'官方訂閱':item==='oauth_token'?'登入權杖':'API 金鑰'}</option>)}</select></label></details>}{method!=='device_code'&&<label>{method==='api_key'?'API 金鑰':'登入權杖'}<input type="password" value={secret} onChange={e=>setSecret(e.target.value)} autoComplete="off" required/></label>}<p className="helper-text">{hint}</p><Button type="submit" disabled={!paired||unavailable||busy}>{busy?'啟動中…':terminal?quiet?'再試一次':'重新開始':'開始登入'}</Button></form>
    {restoring&&<p role="status">正在恢復登入狀態…</p>}
    {pending&&<div className="auth-code" role="status">{auth.code?<div className="code-row"><strong>{auth.code}</strong><CopyButton text={auth.code}>複製</CopyButton></div>:<span>等待官方提供一次性代碼…</span>}{auth.url&&official.test(auth.url)&&<a className="button auth-link" href={auth.url} target="_blank" rel="noreferrer">開啟登入頁 ↗</a>}<p>{provider==='codex'?'必要時請在 ChatGPT 開啟裝置碼登入。':'在官方頁面輸入代碼並確認。'}</p><Button secondary disabled={busy} onClick={cancel}>取消登入</Button></div>}
    {checking&&<div className="auth-code" role="status"><span>正在向官方確認登入資訊…</span><Button secondary disabled={busy} onClick={cancel}>取消</Button></div>}
</>}</article>
}

export function RuntimePanel({only,onChange}:{only?:Runtime;onChange?:()=>void}){
  const [runtime,setRuntime]=useState<Runtime>(only||'host'),[states,setStates]=useState<any>(null),[pair,setPair]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [readError,setReadError]=useState('');
  const ticket=useRef(0);
  const refresh=useCallback(async()=>{const current=++ticket.current;try{const value=await api('runtimes');if(current===ticket.current){setStates(value);setReadError('')}}catch(e){if(current===ticket.current)setReadError((e as Error).message)}},[]);
  const changed=useCallback(()=>{void refresh();onChange?.()},[refresh,onChange]);
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){await refresh();if(active)timer=setTimeout(poll,10000)}poll();return()=>{active=false;ticket.current++;clearTimeout(timer)}},[refresh]);
  useEffect(()=>{if(only)setRuntime(only)},[only]);
  const state=states?.[runtime],paired=state?.protocol===1,unavailable=!!readError||state?.status==='unavailable';
  const versions=state?.providers?.filter((item:any)=>item.version)||[];
  async function connect(){setBusy(true);setError('');try{if(runtime==='host')await api('host/pair',{});else{const data=JSON.parse(pair);await api('runtimes/haos/pair',{fingerprint:data.fingerprint,code:data.code});setPair('')}await refresh();onChange?.()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const pairing=<div className="runtime-pair">{runtime==='haos'?<><p>在開發助手選「家庭現場」並啟用，主機會自動安裝和配對。</p><a className="button" href="#workers">前往開發助手</a><details><summary>手動安裝與配對</summary><ol className="connect-steps"><li>在 App 商店加入這個網址。<code>{repoUrl}</code> <CopyButton text={repoUrl}>複製網址</CopyButton></li><li>到 <a href={haPath('/hassio/store')} target={haTarget()} rel="noreferrer">App 商店 ↗</a> 重新載入後安裝並啟動，網路保留 8443。</li><li>貼上一次性配對資料。這份資料只給這台主機使用。</li></ol><label>配對資料<textarea rows={3} autoComplete="off" spellCheck={false} value={pair} onChange={e=>setPair(e.target.value)} placeholder='{"fingerprint":"…","code":"…"}'/></label><Button disabled={busy||!pair.trim()||!!readError} onClick={connect}>{busy?'配對中…':'配對家庭現場'}</Button></details></>:<><p>{unavailable?'主機仍無法連線時，可重新配對。':'先連接這台主機上的開發助手。'}</p><Button secondary={unavailable} disabled={busy||!!readError} onClick={connect}>{busy?'配對中…':unavailable?'重新配對主機':'連接主機'}</Button></>}</div>;
  return <section className="runtime-panel" aria-label="開發助手帳號">{!only&&<div className="runtime-tabs" role="group" aria-label="開發助手位置">{(['host','haos'] as Runtime[]).map(id=><Button secondary aria-pressed={runtime===id} key={id} onClick={()=>setRuntime(id)}>{runtimeNames[id]}<small>{readError?'等待連線…':states?.[id]?.protocol===1?'已配對':states?.[id]?.status==='unavailable'?'無法連線':'待連接'}</small></Button>)}</div>}
    <div className="section-heading"><h2>{runtime==='host'?'主機的 AI 帳號':'家庭現場的 AI 帳號'}</h2><Status value={unavailable?'loading':paired?'connected':state?.status||'loading'} label={unavailable?'等待連線…':undefined}/></div>
    <p>{runtime==='host'?'登入這台主機上的 AI，家庭關機時仍可用。':'在家庭裡執行，登入資料與主機分開保存。'}</p>
    {error&&<Notice error>{error}</Notice>}{readError&&<Notice error>{readError}</Notice>}
    {state?.status==='unpaired'&&!readError&&pairing}
    {unavailable&&<div className="runtime-pair"><p>暫時連不上，會自動重試。</p><details><summary>重新連接</summary>{pairing}</details></div>}
    {paired&&<div className="provider-grid runtime-providers">{['codex','claude','agy','grok'].map(provider=><Provider key={`${runtime}-${provider}`} runtime={runtime} provider={provider} paired={paired} unavailable={unavailable} state={state?.providers?.find((p:any)=>p.provider===provider)} onChange={changed}/>)}</div>}
    {!!versions.length&&<details><summary>技術資料</summary>{versions.map((item:any)=><p key={item.provider}>{names[item.provider]||item.provider} {item.version} · {item.auth_method||'尚未登入'}</p>)}</details>}
  </section>
}
