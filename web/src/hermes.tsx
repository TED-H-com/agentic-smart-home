import {useCallback,useEffect,useRef,useState} from 'react';
import {api,requestKey} from './api';
import {Button,Notice} from './ui';

const labels:Record<string,string>={needs_auth:'尚待登入',configured:'已儲存，待實際回覆驗證',ready:'已完成推論驗證',needs_reauth:'需要重新登入',unavailable:'服務尚未連線'};
const authKey='agentic-auth-hermes-device';
type DeviceAuth={auth_id:string;state:string;verification_uri:string|null;user_code:string|null;error?:string|null};

export function HermesPanel({guided=false}:{guided?:boolean}){
  const [data,setData]=useState<any>(null),[model,setModel]=useState(''),[key,setKey]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [method,setMethod]=useState(guided?'openai-codex':'openrouter'),[auth,setAuth]=useState<DeviceAuth|null>(null);
  const pending=useRef<{body:string;key:string}|null>(null);
  const refresh=useCallback(async()=>{const value=await api('hermes');setData(value);return value},[]);
  useEffect(()=>{refresh().then(value=>setModel(value.runtime?.model||(guided?value.runtime?.default_models?.['openai-codex']||'':''))).catch(e=>setError(e.message));const id=sessionStorage.getItem(authKey);if(id)api(`hermes/auth/device/${encodeURIComponent(id)}`).then(setAuth).catch(()=>sessionStorage.removeItem(authKey))},[refresh,guided]);
  useEffect(()=>{if(auth?.state!=='pending')return;let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{const value=await api(`hermes/auth/device/${encodeURIComponent(auth.auth_id)}`);if(alive){setAuth(value);setError('');if(value.state==='configured'){sessionStorage.removeItem(authKey);await refresh()}}}catch(e){if(alive)setError((e as Error).message)}if(alive)timer=setTimeout(poll,2000)};timer=setTimeout(poll,1000);return()=>{alive=false;clearTimeout(timer)}},[auth?.auth_id,auth?.state,refresh]);
  async function save(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');setNotice('');try{await api('hermes/auth',{provider:'openrouter',model,api_key:key});setKey('');await refresh();setNotice(guided?'登入資料已儲存。系統將驗證模型連線，完成後自動繼續。':'登入資料已儲存。到對話窗口提出第一個問題，即可驗證模型連線。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function deviceLogin(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');setNotice('');const body={provider:'openai-codex',model:model.trim()},serialized=JSON.stringify(body);if(pending.current?.body!==serialized)pending.current={body:serialized,key:requestKey()};try{const result=await api('hermes/auth/device',body,false,pending.current.key);setAuth(result);if(result.state==='pending')sessionStorage.setItem(authKey,result.auth_id);pending.current=null;await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const authenticating=auth?.state==='pending';
  const ready=data?.runtime?.state==='ready';
  return <section className="runtime-panel" aria-label="Hermes 協調服務"><div className="section-heading"><div><span className="eyebrow">HERMES</span><h2>登入你的協調助手</h2></div><span className="subtle" role="status">{data?.runtime?.execution_state==='stop_unconfirmed'?'工作尚未確認停止':data?labels[data.runtime?.state]||'服務尚未連線':'讀取狀態…'}</span></div>
    <p>{guided?'Hermes 使用自己的登入資料。請再完成一次官方授權；初始化助手的 CLI 憑證不會複製過來。':'由 Hermes 接收需求、查詢主機並彙整結果。Home Assistant、LINE 與內層 worker 都可以稍後設定。'}</p>
    {error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    <label>Hermes 登入方式<select value={method} disabled={busy||authenticating} onChange={e=>{setMethod(e.target.value);setModel((data?.runtime?.provider===e.target.value?data.runtime.model:'')||(e.target.value==='openai-codex'?data?.runtime?.default_models?.['openai-codex']||'':''));setKey('');setError('');setNotice('')}}><option value="openai-codex">官方 OpenAI Codex 訂閱授權</option><option value="openrouter">OpenRouter API key（API 計費）</option></select></label>
    {method==='openrouter'?<form onSubmit={save}><div className="channel-fields"><label>OpenRouter 模型 ID<input required maxLength={200} value={model} onChange={e=>setModel(e.target.value)} placeholder="供應商／模型 ID"/></label><label>OpenRouter API key<input required type="password" autoComplete="off" maxLength={4096} value={key} onChange={e=>setKey(e.target.value)}/></label></div>
      <p className="helper-text">Hermes 與 coding CLI 分別登入。{guided?'儲存金鑰後，系統會呼叫模型一次來驗證連線，供應商可能計費。':'儲存金鑰不會呼叫模型；提出問題時，模型供應商可能計費。'}</p>
      <div className="row-actions"><Button type="submit" disabled={busy||!model.trim()||!key.trim()}>{busy?'儲存中…':'儲存 Hermes 登入'}</Button></div>
    </form>:<form onSubmit={deviceLogin}><label>Hermes 模型 ID<input required maxLength={200} value={model} disabled={authenticating} onChange={e=>setModel(e.target.value)} placeholder="使用此帳號可存取的模型"/></label><p className="helper-text">授權後只會保存 Hermes 自己的憑證。{guided?'系統接著以一次實際回覆驗證連線，再繼續設定。':'實際回覆成功後才會顯示已完成推論驗證。'}</p><Button type="submit" disabled={busy||authenticating||!model.trim()}>{busy?'取得授權中…':'開始 Hermes 官方登入'}</Button></form>}
    {authenticating&&<div className="auth-code" role="status"><p>等待你在官方頁面完成 Hermes 授權</p>{auth.verification_uri==='https://auth.openai.com/codex/device'&&<a href={auth.verification_uri} target="_blank" rel="noreferrer">開啟 Hermes 官方授權頁 ↗</a>}{auth.user_code?<strong>{auth.user_code}</strong>:<span>等待官方提供一次性代碼…</span>}<Button secondary disabled={busy} onClick={async()=>{setBusy(true);try{setAuth(await api(`hermes/auth/device/${encodeURIComponent(auth.auth_id)}/cancel`,{}));sessionStorage.removeItem(authKey)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>取消 Hermes 登入</Button></div>}
    {auth&&['failed','expired','interrupted','stop_unconfirmed'].includes(auth.state)&&<Notice error>{auth.state==='expired'?'官方授權已過期，請重新開始。':auth.state==='stop_unconfirmed'?'尚未確認前一次登入程序已停止，請重新檢查狀態。':'官方登入未完成，請重新檢查後再試。'}</Notice>}
    {auth?.state==='configured'&&!ready&&<Notice>官方憑證已儲存，尚待實際回覆驗證。</Notice>}
    <div className="row-actions"><Button secondary type="button" disabled={busy} onClick={()=>refresh().catch(e=>setError(e.message))}>重新檢查</Button>{data?.runtime?.state&&['configured','ready','needs_reauth'].includes(data.runtime.state)&&<Button secondary type="button" disabled={busy||authenticating} onClick={async()=>{setBusy(true);setError('');try{await api('hermes/auth/logout',{});setKey('');setAuth(null);sessionStorage.removeItem(authKey);setNotice('Hermes 已登出。');await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>登出 Hermes</Button>}</div>
    <details><summary>目前可使用的能力</summary><p>主機狀態查詢可獨立於 HA 使用。自動寫程式與派工仍待完成隔離驗證，目前不會執行。</p>{data?.workers?.map((worker:any)=><p key={worker.worker_id}>{worker.worker_id==='host-coder'?'外層 Coding Worker':'HAOS Coding Worker'} · {worker.available?'可用':'尚未啟用'}</p>)}</details>
  </section>
}
