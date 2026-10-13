import {useCallback,useEffect,useRef,useState} from 'react';
import {api,requestKey,type ConversationSession} from './api';
import {Button,Notice,Status,labels,timestamp} from './ui';
import {WorkerResult} from './workers';
import {ChannelSettings} from './channels';
import {names,type Runtime} from './labels';

const conversationKey='agentic-auth-conversations';
const continuable=new Set(['queued','planning','awaiting_user','awaiting_approval','succeeded']);
const retryable=new Set(['rate_limited','provider_unavailable','timeout','runtime_unavailable','inference_failed']);
const sameThread=(m:any,n:any)=>m.source===n.source&&m.runtime===n.runtime&&m.provider===n.provider&&(m.coordinator||'direct')===(n.coordinator||'direct')&&(m.thread_id||m.id)===(n.thread_id||n.id);
function savedConversations(key=conversationKey,identity?:ConversationSession):Record<string,string>{
  try{const stored=sessionStorage.getItem(key);const value=JSON.parse(stored||'{}');if(!stored&&identity?.resume&&/^[a-f0-9]{64}$/.test(identity.resume.message_id))value['hermes-host-codex']=identity.resume.message_id;return Object.fromEntries(Object.entries(value).filter(([key,id])=>/^(hermes-)?(host|haos)-(codex|claude|agy|grok)$/.test(key)&&typeof id==='string'&&/^[a-f0-9]{64}$/.test(id))) as Record<string,string>}catch{return {}}
}
function speakerOf(m:any){return m.coordinator==='hermes'?'智能管家':names[m.provider]||'開發助手'}
function fitComposer(el:HTMLTextAreaElement){
  el.style.height='auto';
  const style=getComputedStyle(el);
  const line=parseFloat(style.lineHeight)||24;
  const pad=parseFloat(style.paddingTop)+parseFloat(style.paddingBottom);
  const limit=line*6+pad;
  el.style.height=`${Math.min(el.scrollHeight,limit)}px`;
  el.style.overflowY=el.scrollHeight>limit+1?'auto':'hidden';
}
function CaseWork({id,bridge=false,storageScope}:{id:string;bridge?:boolean;storageScope?:string}) {
  const [jobs,setJobs]=useState<{id:string;state:string;worker_id:string;artifact_count:number;evidence_count:number;content_expired:boolean}[]>([]),[expanded,setExpanded]=useState<string|null>(null);
  useEffect(()=>{if(!/^[a-f0-9]{32}$/.test(id))return;let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{const value=await api(`hermes/cases/${id}`);if(active)setJobs(value.jobs||[])}catch{if(active)setJobs([])}if(active)timer=setTimeout(poll,4000)}poll();return()=>{active=false;clearTimeout(timer)}},[id]);
  if(!jobs.length)return null;
  return <div className="conversation-work">{jobs.map(job=>{
    const summary=job.state==='succeeded'?`${job.artifact_count} 份成果 · ${job.evidence_count} 份證據`:`${labels[job.state]||job.state} · ${job.artifact_count} 份成果 · ${job.evidence_count} 份證據`;
    return <div className="assistant-work" key={job.id}>{job.content_expired?<p className="helper-text">成果已超過保存期限。</p>:<div className="case-line"><p>{summary}</p><Button secondary aria-expanded={expanded===job.id} onClick={()=>setExpanded(expanded===job.id?null:job.id)}>{expanded===job.id?'收起成果':'查看成果'}</Button></div>}{expanded===job.id&&!job.content_expired&&<WorkerResult key={job.id} id={job.id} caseId={id} bridge={bridge} storageScope={storageScope}/>}</div>
  })}</div>
}

export function GatewayPanel({visible=true,bridge=false,conversationSession}:{visible?:boolean;bridge?:boolean;conversationSession?:ConversationSession}){
  const storage=conversationSession?.storage_scope?`${conversationKey}-${conversationSession.storage_scope}`:conversationKey;
  const [data,setData]=useState<any>(null),[messages,setMessages]=useState<any[]>([]),[draft,setDraft]=useState(''),[runtime,setRuntime]=useState<Runtime>('host'),[provider,setProvider]=useState('codex'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[settings,setSettings]=useState(()=>!bridge&&location.hash==='#gateway/channels'),[history,setHistory]=useState(false),[viewedId,setViewedId]=useState<string|null>(null);
  const [replies,setReplies]=useState(()=>savedConversations(storage,conversationSession)),[loaded,setLoaded]=useState(false),[coordinator,setCoordinator]=useState('hermes'),[away,setAway]=useState(false);
  const pending=useRef<{body:string;key:string;thread:string|null}|null>(null),initialized=useRef(false),handledLink=useRef(''),thread=useRef<HTMLElement>(null),pin=useRef(true),box=useRef<HTMLTextAreaElement>(null),restoreFocus=useRef(false);
  const scope=coordinator==='hermes'?'hermes-host-codex':`${runtime}-${provider}`,replyId=replies[scope];
  const cursor=messages.find(m=>m.id===replyId&&m.source==='web'&&m.thread_id&&(coordinator==='hermes'||(m.runtime===runtime&&m.provider===provider))&&(m.coordinator||'direct')===coordinator);
  // A second browser surface may have continued this same thread since our last send.
  const latest=cursor?messages.filter(m=>sameThread(m,cursor)).slice().sort((a,b)=>Date.parse(b.created_at)-Date.parse(a.created_at))[0]:null;
  const active=latest&&continuable.has(latest.state)?latest:undefined;
  const refresh=useCallback(async()=>{const [g,m]=await Promise.all([bridge?Promise.resolve(null):api('gateway'),api('gateway/messages')]);setData(g);setMessages(m.messages);setLoaded(true);setError('')},[bridge]);
  useEffect(()=>{if(!visible)return;if(!bridge&&!initialized.current){initialized.current=true;api('gateway').then(g=>{setRuntime(g.preferences.runtime);setProvider(g.preferences.provider);setCoordinator(g.preferences.coordinator||'direct')}).catch(e=>setError(e.message))}refresh().catch(e=>setError(e.message));const timer=setInterval(()=>refresh().catch(e=>setError(e.message)),3000);return()=>clearInterval(timer)},[refresh,visible,bridge]);
  useEffect(()=>{try{sessionStorage.setItem(storage,JSON.stringify(replies))}catch{/* Conversation works in memory when storage is unavailable. */}},[replies,storage]);
  function startNew(){if(/^#gateway\//.test(location.hash))window.history.replaceState(null,'',`${location.pathname}${location.search}#gateway`);setViewedId(null);setHistory(false);setReplies(previous=>{const next={...previous};delete next[scope];return next});setError('');pending.current=null}
  function refill(message:any){const text=typeof message.message==='string'?message.message:'';startNew();setDraft(text);restoreFocus.current=true}
  function replyAction(message:any){if(message.state==='needs_reconcile'||message.error_code==='stop_unconfirmed'||message.reason==='stop_unconfirmed')return null;const code=message.error_code||message.reason;if(code==='needs_reauth')return bridge?null:<div className="row-actions"><a className="button" href="#agents">重新登入智能管家</a></div>;if(typeof code==='string'&&retryable.has(code))return <div className="row-actions"><Button type="button" onClick={()=>refill(message)}>重試</Button></div>;return null}
  function continueFrom(message:any){setViewedId(null);setHistory(false);setRuntime(message.runtime);setProvider(message.provider);setCoordinator(message.coordinator||'direct');setReplies(previous=>({...previous,[`${message.coordinator==='hermes'?'hermes-':''}${message.runtime}-${message.provider}`]:message.id}));setError('');document.getElementById('gateway-message')?.focus()}
  async function submit(e:React.FormEvent){
    e.preventDefault();if(busy||!loaded||!draft.trim()||(replyId&&!active))return;
    pin.current=true;setBusy(true);setError('');
    let body={message:draft,runtime:coordinator==='hermes'?'host':runtime,provider:coordinator==='hermes'?'codex':provider,reply_to:active?.id||null,...(coordinator==='hermes'?{coordinator}: {})};
    const previous=pending.current?JSON.parse(pending.current.body):null;
    // A lost reply must retry the original parent and key even after polling sees its result.
    if(previous&&pending.current?.thread===(active?.thread_id||null)&&previous.message===body.message&&previous.runtime===body.runtime&&previous.provider===body.provider&&previous.coordinator===body.coordinator)body=previous;
    const serialized=JSON.stringify(body);
    if(pending.current?.body!==serialized)pending.current={body:serialized,key:requestKey(),thread:active?.thread_id||null};
    try{const result=await api('gateway/messages',body,false,pending.current!.key);setReplies(previous=>({...previous,[scope]:result.id}));setMessages(previous=>[result,...previous.filter(m=>m.id!==result.id)]);restoreFocus.current=true;setDraft('');setViewedId(null);pending.current=null;await refresh()}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  function onComposerKey(e:React.KeyboardEvent<HTMLTextAreaElement>){
    const native=e.nativeEvent;
    if(native.isComposing||native.keyCode===229)return;
    if(e.key!=='Enter'||e.shiftKey)return;
    e.preventDefault();
    if(busy||!loaded||!draft.trim()||!!(replyId&&!active))return;
    e.currentTarget.form?.requestSubmit();
  }
  useEffect(()=>{const changed=()=>setSettings(!bridge&&location.hash==='#gateway/channels');window.addEventListener('hashchange',changed);return()=>window.removeEventListener('hashchange',changed)},[bridge]);
  const selected=messages.find(m=>m.id===(viewedId||replyId));
  const conversation=selected?messages.filter(m=>sameThread(m,selected)).slice().reverse().sort((a,b)=>Date.parse(a.created_at)-Date.parse(b.created_at)):[];
  const recent=messages.filter((m,index)=>!messages.slice(0,index).some(n=>sameThread(m,n)));
  const threadKey=`${scope}:${viewedId||replyId||''}`;
  const seenThread=useRef(threadKey);
  if(seenThread.current!==threadKey){seenThread.current=threadKey;pin.current=true}
  const tail=conversation.map(m=>`${m.id}:${m.state}:${m.answer||''}`).join('|');
  const inlineTyping=!!conversation.length&&!conversation[conversation.length-1].answer&&['queued','planning'].includes(conversation[conversation.length-1].state);
  function onThreadScroll(){const el=thread.current;if(!el)return;const near=el.scrollHeight-el.scrollTop-el.clientHeight<=48;pin.current=near;setAway(!near&&el.scrollHeight>el.clientHeight+8)}
  function jumpToLatest(){const el=thread.current;if(!el)return;pin.current=true;setAway(false);el.scrollTo({top:el.scrollHeight,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'})}
  useEffect(()=>{const el=thread.current;if(!el)return;if(pin.current){el.scrollTop=el.scrollHeight;setAway(false)}else setAway(el.scrollHeight>el.clientHeight+8)},[tail,busy,loaded]);
  useEffect(()=>{if(box.current)fitComposer(box.current)},[draft,viewedId]);
  useEffect(()=>{if(!busy&&restoreFocus.current){restoreFocus.current=false;document.getElementById('gateway-message')?.focus()}},[busy,draft]);
  useEffect(()=>{if(loaded&&!conversation.length&&!viewedId&&!settings&&!history)document.getElementById('gateway-message')?.focus()},[loaded,conversation.length,viewedId,settings,history]);
  function openConversation(message:any){if(busy)return;if(message.source==='web'&&message.thread_id&&continuable.has(message.state))continueFrom(message);else{setHistory(false);setViewedId(message.id);setError('')}}
  useEffect(()=>{if(!loaded)return;const select=()=>{const id=/^#gateway\/([a-f0-9]{64})$/.exec(location.hash)?.[1];if(!id){handledLink.current='';return}if(handledLink.current===id)return;const message=messages.find(m=>m.id===id);if(message){handledLink.current=id;openConversation(message);}else setError('找不到這段對話；可能已超過保存期限。')};select();window.addEventListener('hashchange',select);return()=>window.removeEventListener('hashchange',select)},[loaded,messages]);
  return <div className="assistant-page">
    <div className="assistant-toolbar"><Button secondary disabled={busy} aria-expanded={history} onClick={()=>setHistory(!history)}>對話紀錄{messages.length?` · ${recent.length}`:''}</Button>{!viewedId&&<Button secondary disabled={busy||!replyId} onClick={startNew}>新對話</Button>}{!bridge&&<a href="#gateway/channels">通訊設定</a>}</div>
    {history&&<section className="assistant-history" aria-label="對話紀錄">{!recent.length?<p className="subtle">還沒有對話。</p>:recent.map(m=><button key={m.id} className="assistant-history-item secondary" disabled={busy} onClick={()=>openConversation(m)}><span>{m.message}</span><small><span>{labels[m.state]||m.state}</span>{m.source!=='web'&&<span>{names[m.source]||m.source}</span>}<time dateTime={m.created_at}>{timestamp(m.created_at)}</time></small></button>)}</section>}
    {!bridge&&settings&&<section className="assistant-channel-settings"><div className="section-heading"><h2>通訊設定</h2><Button secondary onClick={()=>{setSettings(false);location.hash='gateway'}}>回到對話</Button></div><ChannelSettings onChange={refresh}/></section>}
    {error&&<Notice error>{error}</Notice>}
    {!bridge&&<details className="assistant-options"><summary>進階</summary><div className="composer-controls"><label>對話方式<select aria-label="對話方式" value={coordinator} disabled={busy} onChange={e=>{setViewedId(null);setCoordinator(e.target.value)}}><option value="hermes">智能管家（建議）</option><option value="direct">直接交給開發助手</option></select></label>{coordinator==='direct'&&<><label>執行位置<select aria-label="執行位置" disabled={busy} value={runtime} onChange={e=>{setViewedId(null);setRuntime(e.target.value as Runtime)}}><option value="host">主機</option><option value="haos">家庭現場</option></select></label><label>AI<select aria-label="AI" disabled={busy} value={provider} onChange={e=>{setViewedId(null);setProvider(e.target.value)}}>{['codex','claude','agy','grok'].map(id=><option key={id} value={id}>{names[id]}</option>)}</select></label></>}</div>{data&&<Button secondary onClick={async()=>{try{await api('gateway/preferences',{...data.preferences,runtime,provider,coordinator});await refresh()}catch(e){setError((e as Error).message)}}}>設為預設</Button>}<div className="row-actions"><a href="#agents">AI 帳號</a><a href="#workers">開發助手</a></div></details>}
    <section className="assistant-conversation" aria-label="目前對話" ref={thread} onScroll={onThreadScroll}>
      {!loaded?<p role="status">正在讀取對話…</p>:<>{!conversation.length&&!busy&&<p className="subtle chat-empty">需要協助時，直接告訴管家。</p>}{(!!conversation.length||(busy&&!inlineTyping))&&<div className="inbox assistant-messages">{conversation.map((m,index)=>{
        const name=speakerOf(m);
        const typing=!m.answer&&['queued','planning'].includes(m.state);
        const place=m.coordinator==='direct'?`${names[m.runtime]||m.runtime} · ${names[m.provider]||m.provider}`:undefined;
        const caseHere=m.coordinator==='hermes'&&m.case_id&&!conversation.slice(index+1).some(next=>next.case_id===m.case_id);
        return <article key={m.id} className="inbox-message assistant-message chat-turn"><div className="assistant-question"><h3>{m.message}</h3></div><p className="chat-meta">{m.source!=='web'&&<span>{names[m.source]||m.source}</span>}<time dateTime={m.created_at}>{timestamp(m.created_at)}</time></p><div className="chat-reply"><span className="assistant-speaker" title={place}>{name}</span>{typing?<div className="assistant-answer chat-typing" role="status" aria-label="智能管家正在回覆"><span className="sr-only">智能管家正在回覆</span><span className="chat-dots" aria-hidden="true"><i/><i/><i/></span></div>:<div className="assistant-answer">{m.answer&&<p>{m.answer}</p>}{replyAction(m)}{m.state!=='succeeded'&&<Status value={m.state}/>}<div className="row-actions">{m.task_id&&<a className="button secondary" href={`#home/${m.task_id}`}>檢查家庭操作 →</a>}{m.delivery==='unconfirmed'&&<small>通道送達尚未確認。</small>}</div>{m.context_truncated&&<p className="helper-text">較早的前文已省略；重要條件請再補充。</p>}</div>}{caseHere&&<CaseWork id={m.case_id} bridge={bridge} storageScope={conversationSession?.storage_scope}/>}</div></article>
      })}{busy&&!inlineTyping&&<div className="chat-reply chat-pending"><span className="assistant-speaker">{coordinator==='hermes'?'智能管家':names[provider]||'開發助手'}</span><div className="assistant-answer chat-typing" role="status" aria-label="智能管家正在回覆"><span className="sr-only">智能管家正在回覆</span><span className="chat-dots" aria-hidden="true"><i/><i/><i/></span></div></div>}</div>}</>}
    </section>
    <div className="chat-dock">
      {away&&<button type="button" className="chat-jump" onClick={jumpToLatest}>最新訊息 ↓</button>}
      {viewedId?<div className="assistant-readonly"><p>{selected?.source==='web'?'這段對話已結束。':'請回原來的聊天室繼續。'}</p><Button onClick={startNew}>開始新對話</Button></div>:<form className="assistant-composer" onSubmit={submit}>
        {replyId&&loaded&&!active&&!(selected&&(selected.error_code==='needs_reauth'||selected.reason==='needs_reauth'||retryable.has(selected.error_code)||retryable.has(selected.reason)))&&<Notice>這段對話暫時無法接續，請等候完成或開始新對話。</Notice>}
        <div className="chat-compose"><label className="sr-only" htmlFor="gateway-message">對話訊息</label><textarea id="gateway-message" ref={box} disabled={busy} maxLength={5000} rows={1} value={draft} onChange={e=>{setDraft(e.target.value);fitComposer(e.target)}} onKeyDown={onComposerKey} placeholder={active?'繼續說…':'輸入訊息…'}/><Button type="submit" className="chat-send" aria-label="送出訊息" disabled={busy||!loaded||!draft.trim()||!!(replyId&&!active)}>{busy?'送出中…':<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M5 12h12M13 6l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>}</Button></div>
      </form>}
    </div>
  </div>
}
