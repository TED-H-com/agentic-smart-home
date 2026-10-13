import {useCallback,useEffect,useRef,useState} from 'react';
import {api,requestKey,type ConversationSession} from './api';
import {Button,Notice,Status,timestamp} from './ui';
import {WorkerResult} from './workers';
import {ChannelSettings} from './channels';
import {names,type Runtime} from './labels';

const conversationKey='agentic-auth-conversations';
const continuable=new Set(['queued','planning','awaiting_user','awaiting_approval','succeeded']);
const sameThread=(m:any,n:any)=>m.source===n.source&&m.runtime===n.runtime&&m.provider===n.provider&&(m.coordinator||'direct')===(n.coordinator||'direct')&&(m.thread_id||m.id)===(n.thread_id||n.id);
function savedConversations(key=conversationKey,identity?:ConversationSession):Record<string,string>{
  try{const stored=sessionStorage.getItem(key);const value=JSON.parse(stored||'{}');if(!stored&&identity?.resume&&/^[a-f0-9]{64}$/.test(identity.resume.message_id))value['hermes-host-codex']=identity.resume.message_id;return Object.fromEntries(Object.entries(value).filter(([key,id])=>/^(hermes-)?(host|haos)-(codex|claude|agy|grok)$/.test(key)&&typeof id==='string'&&/^[a-f0-9]{64}$/.test(id))) as Record<string,string>}catch{return {}}
}
function CaseWork({id,bridge=false,storageScope}:{id:string;bridge?:boolean;storageScope?:string}) {
  const [jobs,setJobs]=useState<{id:string;state:string;worker_id:string;artifact_count:number;evidence_count:number;content_expired:boolean}[]>([]),[expanded,setExpanded]=useState<string|null>(null);
  useEffect(()=>{if(!/^[a-f0-9]{32}$/.test(id))return;let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{const value=await api(`hermes/cases/${id}`);if(active)setJobs(value.jobs||[])}catch{if(active)setJobs([])}if(active)timer=setTimeout(poll,4000)}poll();return()=>{active=false;clearTimeout(timer)}},[id]);
  if(!jobs.length)return null;
  return <div className="conversation-work">{jobs.map(job=><div className="assistant-work" key={job.id}><div className="section-heading"><h4>{job.state==='succeeded'?'處理結果':'處理進度'}</h4><Status value={job.state}/></div>{job.content_expired?<p className="helper-text">成果已超過保存期限。</p>:<><div className="section-heading"><p className="helper-text">{job.artifact_count} 份成果 · {job.evidence_count} 份證據</p><Button secondary aria-expanded={expanded===job.id} onClick={()=>setExpanded(expanded===job.id?null:job.id)}>{expanded===job.id?'收起成果':'查看成果'}</Button></div>{expanded===job.id&&<WorkerResult key={job.id} id={job.id} caseId={id} bridge={bridge} storageScope={storageScope}/>}</>}</div>)}</div>
}

export function GatewayPanel({visible=true,bridge=false,conversationSession}:{visible?:boolean;bridge?:boolean;conversationSession?:ConversationSession}){
  const storage=conversationSession?.storage_scope?`${conversationKey}-${conversationSession.storage_scope}`:conversationKey;
  const [data,setData]=useState<any>(null),[messages,setMessages]=useState<any[]>([]),[draft,setDraft]=useState(''),[runtime,setRuntime]=useState<Runtime>('host'),[provider,setProvider]=useState('codex'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[settings,setSettings]=useState(()=>!bridge&&location.hash==='#gateway/channels'),[history,setHistory]=useState(false),[viewedId,setViewedId]=useState<string|null>(null);
  const [replies,setReplies]=useState(()=>savedConversations(storage,conversationSession)),[loaded,setLoaded]=useState(false),[coordinator,setCoordinator]=useState('hermes');
  const pending=useRef<{body:string;key:string;thread:string|null}|null>(null),initialized=useRef(false),handledLink=useRef('');
  const scope=coordinator==='hermes'?'hermes-host-codex':`${runtime}-${provider}`,replyId=replies[scope];
  const cursor=messages.find(m=>m.id===replyId&&m.source==='web'&&m.thread_id&&(coordinator==='hermes'||(m.runtime===runtime&&m.provider===provider))&&(m.coordinator||'direct')===coordinator);
  // A second browser surface may have continued this same thread since our last send.
  const latest=cursor?messages.filter(m=>sameThread(m,cursor)).slice().sort((a,b)=>Date.parse(b.created_at)-Date.parse(a.created_at))[0]:null;
  const active=latest&&continuable.has(latest.state)?latest:undefined;
  const refresh=useCallback(async()=>{const [g,m]=await Promise.all([bridge?Promise.resolve(null):api('gateway'),api('gateway/messages')]);setData(g);setMessages(m.messages);setLoaded(true);setError('')},[bridge]);
  useEffect(()=>{if(!visible)return;if(!bridge&&!initialized.current){initialized.current=true;api('gateway').then(g=>{setRuntime(g.preferences.runtime);setProvider(g.preferences.provider);setCoordinator(g.preferences.coordinator||'direct')}).catch(e=>setError(e.message))}refresh().catch(e=>setError(e.message));const timer=setInterval(()=>refresh().catch(e=>setError(e.message)),3000);return()=>clearInterval(timer)},[refresh,visible,bridge]);
  useEffect(()=>{try{sessionStorage.setItem(storage,JSON.stringify(replies))}catch{/* Conversation works in memory when storage is unavailable. */}},[replies,storage]);
  function startNew(){if(/^#gateway\//.test(location.hash))window.history.replaceState(null,'',`${location.pathname}${location.search}#gateway`);setViewedId(null);setHistory(false);setReplies(previous=>{const next={...previous};delete next[scope];return next});setError('');pending.current=null}
  function continueFrom(message:any){setViewedId(null);setHistory(false);setRuntime(message.runtime);setProvider(message.provider);setCoordinator(message.coordinator||'direct');setReplies(previous=>({...previous,[`${message.coordinator==='hermes'?'hermes-':''}${message.runtime}-${message.provider}`]:message.id}));setError('');document.getElementById('gateway-message')?.focus()}
  async function submit(e:React.FormEvent){
    e.preventDefault();if(busy||!loaded||(replyId&&!active))return;
    setBusy(true);setError('');
    let body={message:draft,runtime:coordinator==='hermes'?'host':runtime,provider:coordinator==='hermes'?'codex':provider,reply_to:active?.id||null,...(coordinator==='hermes'?{coordinator}: {})};
    const previous=pending.current?JSON.parse(pending.current.body):null;
    // A lost reply must retry the original parent and key even after polling sees its result.
    if(previous&&pending.current?.thread===(active?.thread_id||null)&&previous.message===body.message&&previous.runtime===body.runtime&&previous.provider===body.provider&&previous.coordinator===body.coordinator)body=previous;
    const serialized=JSON.stringify(body);
    if(pending.current?.body!==serialized)pending.current={body:serialized,key:requestKey(),thread:active?.thread_id||null};
    try{const result=await api('gateway/messages',body,false,pending.current!.key);setReplies(previous=>({...previous,[scope]:result.id}));setMessages(previous=>[result,...previous.filter(m=>m.id!==result.id)]);setDraft('');setViewedId(null);pending.current=null;await refresh()}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  useEffect(()=>{const changed=()=>setSettings(!bridge&&location.hash==='#gateway/channels');window.addEventListener('hashchange',changed);return()=>window.removeEventListener('hashchange',changed)},[bridge]);
  const selected=messages.find(m=>m.id===(viewedId||replyId));
  const conversation=selected?messages.filter(m=>sameThread(m,selected)).slice().reverse().sort((a,b)=>Date.parse(a.created_at)-Date.parse(b.created_at)):[];
  const recent=messages.filter((m,index)=>!messages.slice(0,index).some(n=>sameThread(m,n)));
  function openConversation(message:any){if(busy)return;if(message.source==='web'&&message.thread_id&&continuable.has(message.state))continueFrom(message);else{setHistory(false);setViewedId(message.id);setError('')}}
  useEffect(()=>{if(!loaded)return;const select=()=>{const id=/^#gateway\/([a-f0-9]{64})$/.exec(location.hash)?.[1];if(!id){handledLink.current='';return}if(handledLink.current===id)return;const message=messages.find(m=>m.id===id);if(message){handledLink.current=id;openConversation(message);}else setError('找不到這段對話；可能已超過保存期限。')};select();window.addEventListener('hashchange',select);return()=>window.removeEventListener('hashchange',select)},[loaded,messages]);
  return <div className="assistant-page">
    <div className="assistant-toolbar"><Button secondary disabled={busy} aria-expanded={history} onClick={()=>setHistory(!history)}>對話紀錄{messages.length?` · ${recent.length}`:''}</Button><Button secondary disabled={busy||(!replyId&&!viewedId)} onClick={startNew}>開始新對話</Button>{!bridge&&<a href="#settings">設定</a>}</div>
    {history&&<section className="assistant-history" aria-label="對話紀錄"><h2>對話紀錄</h2>{!recent.length?<p className="subtle">還沒有對話。</p>:recent.map(m=><button key={m.id} className="assistant-history-item secondary" disabled={busy} onClick={()=>openConversation(m)}><span>{m.message}</span><small>{names[m.source]} · {timestamp(m.created_at)}</small></button>)}</section>}
    {!bridge&&settings&&<section className="assistant-channel-settings"><div className="section-heading"><h2>通訊入口</h2><Button secondary onClick={()=>{setSettings(false);location.hash='gateway'}}>回到對話</Button></div><ChannelSettings onChange={refresh}/></section>}
    {error&&<Notice error>{error}</Notice>}
    <section className="assistant-conversation" aria-label="目前對話">
      {!loaded?<p role="status">正在讀取對話…</p>:!conversation.length?<p className="subtle">需要協助時，直接告訴管家。</p>:<div className="inbox assistant-messages">{conversation.map((m,index)=><article key={m.id} className="inbox-message assistant-message"><div className="assistant-question"><h3>{m.message}</h3></div><div className="assistant-answer"><span className="assistant-speaker">{m.coordinator==='hermes'?'智能管家':names[m.provider]}</span><p>{m.answer||'正在處理…'}</p>{m.state!=='succeeded'&&<Status value={m.state}/>}<div className="row-actions">{m.task_id&&<a className="button secondary" href={`#home/${m.task_id}`}>檢查家庭操作 →</a>}{m.delivery==='unconfirmed'&&<small>通道送達尚未確認。</small>}</div>{m.coordinator==='hermes'&&m.case_id&&!conversation.slice(index+1).some(next=>next.case_id===m.case_id)&&<CaseWork id={m.case_id} bridge={bridge} storageScope={conversationSession?.storage_scope}/>}{m.context_truncated&&<p className="helper-text">較早的前文已省略；重要條件請再補充。</p>}</div></article>)}</div>}
    </section>
    {viewedId?<div className="assistant-readonly"><p>{selected?.source==='web'?'這段對話已結束。':'外部通訊請回原通道接續。'}</p><Button secondary onClick={startNew}>開啟新對話</Button></div>:<form className="assistant-composer" onSubmit={submit}>
      {replyId&&loaded&&!active&&<Notice>這段對話暫時無法接續，請等候完成或開始新對話。</Notice>}
      <label className="sr-only" htmlFor="gateway-message">對話訊息</label><textarea id="gateway-message" required disabled={busy} maxLength={5000} rows={2} value={draft} onChange={e=>setDraft(e.target.value)} placeholder={active?'繼續說…':'輸入你的需求…'}/>
      <div className="assistant-composer-actions"><span className="subtle">{active?'接續這段對話':coordinator==='hermes'?'智能管家':`${names[runtime]} · ${names[provider]}`}</span><Button type="submit" disabled={busy||!loaded||!draft.trim()||!!(replyId&&!active)}>{busy?'送出中…':'送出訊息'}</Button></div>
    </form>}
    {!bridge&&<details className="assistant-options"><summary>進階對話設定</summary><div className="composer-controls"><label>對話方式<select aria-label="對話方式" value={coordinator} disabled={busy} onChange={e=>{setViewedId(null);setCoordinator(e.target.value)}}><option value="hermes">Hermes 協調助手</option><option value="direct">受控 Agent（進階）</option></select></label>{coordinator==='direct'&&<><label>執行環境<select aria-label="執行環境" disabled={busy} value={runtime} onChange={e=>{setViewedId(null);setRuntime(e.target.value as Runtime)}}><option value="host">外層 Linux</option><option value="haos">HAOS 內層</option></select></label><label>Agent<select aria-label="Agent" disabled={busy} value={provider} onChange={e=>{setViewedId(null);setProvider(e.target.value)}}>{['codex','claude','agy','grok'].map(id=><option key={id} value={id}>{names[id]}</option>)}</select></label></>}</div>{data&&<Button secondary onClick={async()=>{try{await api('gateway/preferences',{...data.preferences,runtime,provider,coordinator});await refresh()}catch(e){setError((e as Error).message)}}}>將上方選擇設為預設</Button>}<div className="row-actions"><a href="#agents">助手帳號</a><a href="#workers">進階工具</a><a href="#gateway/channels">通訊入口</a></div></details>}
  </div>
}
