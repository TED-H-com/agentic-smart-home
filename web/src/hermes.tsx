import {useCallback,useEffect,useRef,useState} from 'react';
import {api,ApiError,requestKey} from './api';
import {Button,CopyButton,Notice} from './ui';

const labels:Record<string,string>={needs_auth:'尚待登入',configured:'已儲存，待實際回覆驗證',ready:'已完成推論驗證',needs_reauth:'需要重新登入',unavailable:'服務尚未連線'};
const authKey='agentic-auth-hermes-device';
const terminalStates=['failed','expired','interrupted','stop_unconfirmed'];
const loginRecovery:Record<string,{text:string;action:string;kind:'start'|'check'}>={
  access_denied:{text:'登入被拒絕，請用你的帳號重新登入。',action:'重新登入',kind:'start'},
  slow_down:{text:'官方要求稍候，請一分鐘後再試。',action:'再試一次',kind:'start'},
  expired_token:{text:'代碼已過期，請重新開始。',action:'重新開始',kind:'start'},
  stop_unconfirmed:{text:'尚未確認上一個登入已停止，請稍後重新檢查。',action:'重新檢查',kind:'check'},
  oauth_failed:{text:'登入沒有完成，請重新開始。',action:'重新開始',kind:'start'},
};
type DeviceAuth={auth_id:string;state:string;verification_uri:string|null;user_code:string|null;error?:string|null;reason?:string|null;model?:string};
function loginCode(auth?:{reason?:string|null;error?:string|null}|null){const code=auth?.reason||auth?.error;return typeof code==='string'&&loginRecovery[code]?code:''}

export function HermesPanel({guided=false}:{guided?:boolean}){
  const [data,setData]=useState<any>(null),[model,setModel]=useState(''),[key,setKey]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[failure,setFailure]=useState('');
  const [method,setMethod]=useState(guided?'openai-codex':'openrouter'),[auth,setAuth]=useState<DeviceAuth|null>(null);
  const pending=useRef<{body:string;key:string}|null>(null);
  const refresh=useCallback(async()=>{const value=await api('hermes');setData(value);return value},[]);
  function adopt(value:DeviceAuth){setAuth(value);const code=loginCode(value);if(code&&value.state!=='pending'&&value.state!=='configured'){setFailure(code);setMethod('openai-codex');if(value.model)setModel(value.model)}else if(value.state==='pending'||value.state==='configured')setFailure('')}
  useEffect(()=>{let alive=true;refresh().then(value=>{if(!alive)return;setModel(value.runtime?.model||(guided?value.runtime?.default_models?.['openai-codex']||'':''));const pendingAuth=value.runtime?.pending_auth;if(pendingAuth?.state==='pending'&&pendingAuth.auth_id){setAuth(pendingAuth);sessionStorage.setItem(authKey,pendingAuth.auth_id);setFailure('')}else{const id=sessionStorage.getItem(authKey);if(id)api(`hermes/auth/device/${encodeURIComponent(id)}`).then(result=>{if(alive)adopt(result)}).catch(()=>sessionStorage.removeItem(authKey))}}).catch(e=>{if(alive)setError(e.message)});return()=>{alive=false}},[refresh,guided]);
  useEffect(()=>{if(auth?.state!=='pending')return;let alive=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{const value=await api(`hermes/auth/device/${encodeURIComponent(auth.auth_id)}`);if(alive){adopt(value);setError('');if(value.state==='configured'){sessionStorage.removeItem(authKey);await refresh()}}}catch(e){if(alive)setError((e as Error).message)}if(alive)timer=setTimeout(poll,2000)};timer=setTimeout(poll,1000);return()=>{alive=false;clearTimeout(timer)}},[auth?.auth_id,auth?.state,refresh]);
  function pick(next:string){setMethod(next);setModel((data?.runtime?.provider===next?data.runtime.model:'')||(next==='openai-codex'?data?.runtime?.default_models?.['openai-codex']||'':''));setKey('');setError('');setNotice('')}
  async function save(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');setNotice('');try{await api('hermes/auth',{provider:'openrouter',model,api_key:key});setKey('');await refresh();setNotice(guided?'已儲存，正在驗證連線。':'已儲存。到對話提出問題後才會驗證連線。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function deviceLogin(e?:React.FormEvent){e?.preventDefault();setBusy(true);setError('');setNotice('');const body={provider:'openai-codex',model:model.trim()},serialized=JSON.stringify(body);if(pending.current?.body!==serialized)pending.current={body:serialized,key:requestKey()};try{const result=await api('hermes/auth/device',body,false,pending.current.key);if(result.state==='pending')sessionStorage.setItem(authKey,result.auth_id);pending.current=null;adopt(result);await refresh()}catch(e){if(e instanceof ApiError&&e.code&&loginRecovery[e.code]){setFailure(e.code);setError('')}else setError((e as Error).message)}finally{setBusy(false)}}
  async function recheck(){setBusy(true);setError('');try{if(auth?.auth_id)adopt(await api(`hermes/auth/device/${encodeURIComponent(auth.auth_id)}`));else await refresh()}catch(e){if(e instanceof ApiError&&e.code&&loginRecovery[e.code]){setFailure(e.code);setError('')}else setError((e as Error).message)}finally{setBusy(false)}}
  const authenticating=auth?.state==='pending',terminal=!!auth&&terminalStates.includes(auth.state),recovery=loginRecovery[failure];
  return <section className="runtime-panel hermes-panel" aria-label="智能管家登入"><div className="section-heading"><h2>智能管家登入</h2><span className="subtle" role="status">{data?.runtime?.execution_state==='stop_unconfirmed'?'工作尚未確認停止':data?labels[data.runtime?.state]||'服務尚未連線':'讀取狀態…'}</span></div>
    <p>{guided?'請再完成官方授權；不會沿用 AI 帳號的登入。':'登入後可查詢主機，其餘可以稍後再設。'}</p>
    {error&&<Notice error><span>請再試一次。</span><small>{error}</small></Notice>}{notice&&<Notice>{notice}</Notice>}
    {recovery&&<div className="recovery-line" role="status"><p>{recovery.text}</p><small className="reason-code">{failure}</small><Button type="button" disabled={busy||(recovery.kind==='start'&&!model.trim())} onClick={()=>recovery.kind==='check'?recheck():deviceLogin()}>{recovery.action}</Button></div>}
    <div hidden={authenticating||!!recovery} className="hermes-login-form"><div className="radio-cards" role="radiogroup" aria-label="登入方式">{[['openai-codex','官方訂閱登入'],['openrouter','OpenRouter API key']].map(([id,label])=><label key={id} className={`radio-card ${method===id?'chosen':''}`}><input type="radio" name="hermes-method" value={id} checked={method===id} disabled={busy||authenticating} onChange={()=>pick(id)}/><strong>{label}</strong></label>)}</div>
    {method==='openrouter'?<form onSubmit={save}><div className="channel-fields"><label>OpenRouter 模型 ID<input required maxLength={200} value={model} onChange={e=>setModel(e.target.value)} placeholder="供應商／模型 ID"/></label><label>OpenRouter API key<input required type="password" autoComplete="off" maxLength={4096} value={key} onChange={e=>setKey(e.target.value)}/></label></div>
      <p className="helper-text">{guided?'登入與 AI 帳號分開；系統會呼叫模型一次來驗證連線，供應商可能計費。':'儲存金鑰不會呼叫模型；提出問題時，供應商可能計費。'}</p>
      <Button type="submit" disabled={busy||!model.trim()||!key.trim()}>{busy?'儲存中…':'儲存登入'}</Button>
    </form>:<form onSubmit={deviceLogin}><details className="model-options" open={!guided||!model.trim()}><summary>進階：模型</summary><label>模型 ID<input required maxLength={200} value={model} disabled={authenticating} onChange={e=>setModel(e.target.value)} placeholder="使用此帳號可存取的模型"/></label></details><p className="helper-text">{guided?'授權後只保存這裡的憑證，接著驗證一次連線。':'實際回覆成功後才會顯示已完成驗證。'}</p><Button type="submit" disabled={busy||authenticating||!model.trim()}>{busy?'取得授權中…':terminal?'重新開始':'開始登入'}</Button></form>}</div>
    {authenticating&&<div className="auth-code" role="status">{auth.user_code?<div className="code-row"><strong>{auth.user_code}</strong><CopyButton text={auth.user_code}>複製</CopyButton></div>:<span>等待官方提供一次性代碼…</span>}{auth.verification_uri==='https://auth.openai.com/codex/device'&&<a className="button auth-link" href={auth.verification_uri} target="_blank" rel="noreferrer">開啟登入頁 ↗</a>}<p>在官方頁面輸入代碼並確認。</p><Button secondary disabled={busy} onClick={async()=>{setBusy(true);try{setAuth(await api(`hermes/auth/device/${encodeURIComponent(auth.auth_id)}/cancel`,{}));sessionStorage.removeItem(authKey)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>取消登入</Button></div>}
    {terminal&&!recovery&&<Notice error>{auth?.state==='expired'?'官方授權已過期，請重新開始。':auth?.state==='stop_unconfirmed'?'尚未確認前一次登入已停止，請重新開始。':'官方登入未完成，請重新開始。'}</Notice>}
    {!authenticating&&!recovery&&<div className="row-actions"><Button secondary type="button" disabled={busy} onClick={()=>refresh().catch(e=>setError(e.message))}>重新檢查</Button>{data?.runtime?.state&&['configured','ready','needs_reauth'].includes(data.runtime.state)&&<Button secondary type="button" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api('hermes/auth/logout',{});setKey('');setAuth(null);sessionStorage.removeItem(authKey);setNotice('已登出。');await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>登出</Button>}</div>}
    {!guided&&<details><summary>技術資料</summary><p>主機查詢可獨立使用。尚未就緒的能力不會派工。</p>{data?.workers?.map((worker:any)=><p key={worker.worker_id}>{worker.worker_id==='host-coder'?'主機的開發助手':'家庭現場的開發助手'} · {worker.available?'可用':'尚未啟用'}</p>)}{data?.runtime?.version&&<p>版本 {data.runtime.version}</p>}</details>}
  </section>
}
