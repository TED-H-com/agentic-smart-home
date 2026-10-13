import {useCallback,useEffect,useState} from 'react';
import {api,ApiError,haPath,haTarget} from './api';
import {Status,Notice,Button,CopyButton,friendlySentence} from './ui';
import {names,type Runtime} from './labels';
const runtimeNames:Record<Runtime,string>={host:'主機',haos:'家庭現場'};
const official=/^https:\/\/(auth\.openai\.com|accounts\.x\.ai|auth\.x\.ai)\//;
const repoUrl='https://github.com/teddashh/agentic-smart-home';
const liveAuth=new Set(['pending','verifying']),endedAuth=new Set(['failed','expired','unverified','stop_unconfirmed']);
const accountCopy:Record<string,string>={needs_reauth:'登入已失效，請重新登入。',rate_limited:'已達用量上限，請稍後再試。',invalid_credentials:'登入資訊無效，請重新輸入。',network:'無法連線到 AI 服務，請檢查網路後再試。',cli_output_unrecognized:'無法確認登入結果，請稍後再試。',timeout:'AI 帳號回應逾時，請稍後再試。'};
const quietReasons=new Set(['rate_limited','network','timeout']);
function methodsFor(provider:string){return provider==='claude'?['oauth_token','api_key']:provider==='agy'?['api_key']:['device_code','api_key']}
export function Provider({provider,state,paired,onChange,runtime,subscriptionFirst=false}:{provider:string;state:any;paired:boolean;onChange:()=>void;runtime?:'host'|'haos';subscriptionFirst?:boolean}){
  const prefix=runtime?`runtimes/${runtime}/`:'';
  const authKey=`agentic-auth-${runtime||'default'}-${provider}`;
  const name=provider==='codex'?'Codex':provider==='agy'?'Antigravity':provider==='grok'?'Grok Build':'Claude Code';
  const methods=methodsFor(provider);
  const [method,setMethod]=useState(['codex','grok'].includes(provider)?'device_code':subscriptionFirst&&provider==='claude'?'oauth_token':'api_key'),[secret,setSecret]=useState(''),[auth,setAuth]=useState<any>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{const id=sessionStorage.getItem(authKey);if(id)api(`${prefix}auth/${id}`).then(setAuth).catch(()=>sessionStorage.removeItem(authKey))},[provider,prefix,authKey]);
  // Another tab may own the login. The Runner reports it, so this card resumes it.
  const resume=state?.pending_session;
  useEffect(()=>{if(resume&&liveAuth.has(resume.status))setAuth((current:any)=>current?.id===resume.id||liveAuth.has(current?.status)?current:resume)},[resume?.id,resume?.status]);
  useEffect(()=>{if(!auth?.id||!liveAuth.has(auth.status))return;let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{const result=await api(`${prefix}auth/${auth.id}`);if(alive){setAuth(result);setError('');if(!liveAuth.has(result.status)){sessionStorage.removeItem(authKey);onChange()}}}catch(e){if(alive){setError((e as Error).message);if(e instanceof ApiError&&e.status===404){setAuth({...auth,status:'expired'});sessionStorage.removeItem(authKey)}}}if(alive)timer=setTimeout(poll,auth.status==='verifying'?1500:3000)};timer=setTimeout(poll,auth.status==='verifying'?1000:2000);return()=>{alive=false;clearTimeout(timer)}},[auth?.id,auth?.status,onChange,provider,prefix,authKey]);
  async function login(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{const result=await api(`${prefix}providers/${provider}/auth`,{method,secret});setAuth(result);if(result.id)sessionStorage.setItem(authKey,result.id);setSecret('');onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function cancel(){setBusy(true);setError('');try{const result=await api(`${prefix}providers/${provider}/auth/${auth.id}/cancel`,{});sessionStorage.removeItem(authKey);setAuth(['cancelled','unverified'].includes(result.status)&&!result.reason?null:result);onChange()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function logout(){try{await api(`${prefix}providers/${provider}/logout`,{});sessionStorage.removeItem(authKey);setAuth(null);onChange()}catch(e){setError((e as Error).message)}}
  const pending=auth?.status==='pending',checking=auth?.status==='verifying',terminal=!!auth&&endedAuth.has(auth.status),connected=state?.status==='connected'&&!pending&&!checking;
  const reason=terminal?String(auth?.reason||''):!auth&&!pending&&!checking?String(state?.reason||''):'';
  const quiet=quietReasons.has(reason);
  const hint=method==='api_key'?provider==='agy'?'請用 Gemini API key；Google 訂閱尚未整合。':'使用 API key，供應商可能另行計費。':method==='oauth_token'?'在自己的電腦執行 claude setup-token，完成後貼上。':provider==='codex'?'必要時請在 ChatGPT 開啟裝置碼登入。':'在官方頁面輸入代碼完成登入。';
  const ended=accountCopy[reason]?friendlySentence(auth?.message,accountCopy[reason]):auth?.status==='expired'?'登入已過期，請重新開始。':auth?.status==='stop_unconfirmed'?'尚未確認登入程序停止，請先核對執行環境。':auth?.message||'登入未完成，請重新開始。';
  const stateSentence=!auth&&reason&&accountCopy[reason]?friendlySentence(state?.message,accountCopy[reason]):!auth&&state?.message?state.message:'';
  return <article className="provider"><div className="section-heading"><h3>{name}</h3><Status value={checking?'checking':pending?'pending':state?.status||'needs_reauth'}/></div>{error&&<Notice error>{error}</Notice>}{connected?<Button onClick={logout}>登出</Button>:<>
    <form onSubmit={login} hidden={pending||checking||(terminal&&quiet)}>{methods.length>1&&<details><summary>其他登入方式</summary><label>登入方式<select value={method} disabled={busy} onChange={e=>setMethod(e.target.value)}>{methods.map(item=><option key={item} value={item}>{item==='device_code'?'官方訂閱':item==='oauth_token'?'setup-token':'API key'}</option>)}</select></label></details>}{method!=='device_code'&&<label>{method==='api_key'?'API key':'setup-token'}<input type="password" value={secret} onChange={e=>setSecret(e.target.value)} autoComplete="off" required/></label>}<p className="helper-text">{hint}</p>{!(terminal&&quiet)&&<Button type="submit" disabled={!paired||busy}>{busy?'啟動中…':terminal?'重新開始':'開始登入'}</Button>}</form>
    {pending&&<div className="auth-code" role="status">{auth.code?<div className="code-row"><strong>{auth.code}</strong><CopyButton text={auth.code}>複製</CopyButton></div>:<span>等待官方提供一次性代碼…</span>}{auth.url&&official.test(auth.url)&&<a className="button auth-link" href={auth.url} target="_blank" rel="noreferrer">開啟登入頁 ↗</a>}<p>{provider==='codex'?'必要時請在 ChatGPT 開啟裝置碼登入。':'在官方頁面輸入代碼並確認。'}</p><Button secondary disabled={busy} onClick={cancel}>取消登入</Button></div>}
    {checking&&<div className="auth-code" role="status"><span>正在向官方確認登入資訊…</span><Button secondary disabled={busy} onClick={cancel}>取消</Button></div>}
    {terminal&&<Notice error>{ended}</Notice>}
    {stateSentence&&<Notice error>{stateSentence}</Notice>}</>}</article>
}

export function RuntimePanel({only,onChange}:{only?:Runtime;onChange?:()=>void}){
  const [runtime,setRuntime]=useState<Runtime>(only||'host'),[states,setStates]=useState<any>(null),[pair,setPair]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const refresh=useCallback(async()=>{try{setStates(await api('runtimes'));setError('');onChange?.()}catch(e){setError((e as Error).message)}},[onChange]);
  useEffect(()=>{refresh();const timer=setInterval(refresh,10000);return()=>clearInterval(timer)},[refresh]);
  useEffect(()=>{if(only)setRuntime(only)},[only]);
  const state=states?.[runtime],paired=state?.protocol===1;
  const versions=state?.providers?.filter((item:any)=>item.version)||[];
  async function connect(){setBusy(true);setError('');try{if(runtime==='host')await api('host/pair',{});else{const data=JSON.parse(pair);await api('runtimes/haos/pair',{fingerprint:data.fingerprint,code:data.code});setPair('')}await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <section className="runtime-panel" aria-label="開發助手帳號">{!only&&<div className="runtime-tabs" role="group" aria-label="開發助手位置">{(['host','haos'] as Runtime[]).map(id=><Button secondary aria-pressed={runtime===id} key={id} onClick={()=>setRuntime(id)}>{runtimeNames[id]}<small>{states?.[id]?.protocol===1?'已配對':'待連接'}</small></Button>)}</div>}
    <div className="section-heading"><h2>{runtime==='host'?'主機的 AI 帳號':'家庭現場的 AI 帳號'}</h2><Status value={paired?'connected':state?.status||'loading'}/></div>
    <p>{runtime==='host'?'登入這台主機上的 AI，家庭關機時仍可用。':'在家庭裡執行，登入資料與主機分開保存。'}</p>
    {error&&<Notice error>{error}</Notice>}
    {!paired&&<div className="runtime-pair">{runtime==='haos'?<><ol className="connect-steps"><li>在 App 商店加入這個網址。<code>{repoUrl}</code> <CopyButton text={repoUrl}>複製網址</CopyButton> 或下載 <a href="./downloads/agentic-runner.zip" download>安裝檔</a>。</li><li>到 <a href={haPath('/hassio/store')} target={haTarget()} rel="noreferrer">App 商店 ↗</a> 重新載入後安裝並啟動，網路保留 8443。</li><li>貼上一次性配對資料。這份資料只給這台主機使用。</li></ol><label>配對資料<textarea rows={3} autoComplete="off" spellCheck={false} value={pair} onChange={e=>setPair(e.target.value)} placeholder='{"fingerprint":"…","code":"…"}'/></label></>:<p>先連接這台主機上的開發助手。</p>}<Button disabled={busy||(runtime==='haos'&&!pair.trim())} onClick={connect}>{busy?'配對中…':runtime==='host'?'連接主機':'配對家庭現場'}</Button></div>}
    <div className="provider-grid runtime-providers">{['codex','claude','agy','grok'].map(provider=><Provider key={`${runtime}-${provider}`} runtime={runtime} provider={provider} paired={paired} state={state?.providers?.find((p:any)=>p.provider===provider)} onChange={refresh}/>)}</div>
    {!!versions.length&&<details><summary>技術資料</summary>{versions.map((item:any)=><p key={item.provider}>{names[item.provider]||item.provider} {item.version} · {item.auth_method||'尚未登入'}</p>)}</details>}
  </section>
}
