import {useCallback,useEffect,useRef,useState} from 'react';
import {api,session,type ConversationSession} from './api';
import {GatewayPanel} from './gateway';
import {TaskDetail,type Task} from './task-detail';
import {Button,Notice} from './ui';

function taskFromHash() {
  const match=/^#home\/([a-zA-Z0-9_-]{1,100})$/.exec(location.hash);
  return match?.[1]||null;
}
function BridgeTask({id,visible}:{id:string;visible:boolean}) {
  const [task,setTask]=useState<Task|null>(null),[error,setError]=useState('');
  const refresh=useCallback(async()=>{const value=await api(`tasks/${id}`);if(value.id!==id)throw new Error('提案與目前連結不符，請重新讀取。');setTask(value);setError('')},[id]);
  useEffect(()=>{if(!visible)return;let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{await refresh()}catch(e){if(active)setError((e as Error).message)}if(active)timer=setTimeout(poll,2500)}poll();return()=>{active=false;clearTimeout(timer)}},[visible,refresh]);
  return <section hidden={!visible} aria-label="家庭提案"><div className="bridge-back"><a className="button secondary" href="#gateway">← 回到對話</a></div>{error&&<Notice error>{error}<Button secondary onClick={()=>refresh().catch(e=>setError(e.message))}>重新讀取提案</Button></Notice>}{task&&<div hidden={!!error}><TaskDetail task={task} onChange={()=>refresh().catch(e=>setError(e.message))}/></div>}{!task&&!error&&<p role="status">正在讀取提案…</p>}</section>
}
function BridgeConversation({identity,active}:{identity:ConversationSession;active:boolean}) {
  const [selected,setSelected]=useState(taskFromHash),[visited,setVisited]=useState<string[]>(()=>{const id=taskFromHash();return id?[id]:[]});
  useEffect(()=>{const changed=()=>{const id=taskFromHash();setSelected(id);if(id)setVisited(previous=>previous.includes(id)?previous:[...previous,id]);else if(location.hash&&!/^#gateway(?:\/[a-f0-9]{64})?$/.test(location.hash))history.replaceState(null,'',`${location.pathname}${location.search}#gateway`)};changed();window.addEventListener('hashchange',changed);return()=>window.removeEventListener('hashchange',changed)},[]);
  return <div hidden={!active}><div hidden={!!selected}><GatewayPanel visible={active&&!selected} bridge conversationSession={identity}/></div>{visited.map(id=><BridgeTask key={id} id={id} visible={active&&selected===id}/>)}</div>
}
export function BridgeApp({deployment,onRetry}:{deployment:any;onRetry:()=>Promise<void>}) {
  const [identity,setIdentity]=useState<ConversationSession|null>(null),[active,setActive]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const generation=useRef(0);
  const ready=deployment.authenticated&&deployment.bridge_state==='ready';
  const refresh=useCallback(async()=>{const current=++generation.current;setBusy(true);try{const value=await session();if(current!==generation.current)return;if(value.mode!=='bridge'||!value.storage_scope||!/^[a-f0-9]{64}$/.test(value.storage_scope))throw new Error('無法確認家庭身分，請重新開啟側欄。');setIdentity(previous=>previous?.storage_scope===value.storage_scope?previous:value);setActive(true);setError('')}catch(e){if(current===generation.current){setActive(false);setError((e as Error).message)}}finally{if(current===generation.current)setBusy(false)}},[]);
  useEffect(()=>{if(!ready){generation.current++;setActive(false);return}refresh();const timer=setInterval(refresh,15000);return()=>clearInterval(timer)},[ready,refresh]);
  useEffect(()=>{const expired=()=>{generation.current++;setBusy(false);setActive(false);setError('家庭授權已失效，請確認身分後重新連線。')};const unavailable=()=>{generation.current++;setBusy(false);setActive(false);setError('暫時連不上智能管家。草稿保留在此頁，重新連線後可繼續。')};window.addEventListener('agentic-bridge-expired',expired);window.addEventListener('agentic-bridge-unavailable',unavailable);return()=>{window.removeEventListener('agentic-bridge-expired',expired);window.removeEventListener('agentic-bridge-unavailable',unavailable)}},[]);
  const blocked=!ready||!active;
  const heading=deployment.bridge_state==='unpaired'?'尚未連接智能管家':deployment.bridge_state==='owner_required'?'請使用家庭擁有者帳號':deployment.bridge_state==='unavailable'?'智能管家暫時無法連線':error?'需要重新確認連線':'正在開啟智能管家…';
  return <main className="bridge-shell" id="main-content" tabIndex={-1}><header className="bridge-heading"><h1>智能管家</h1><a href="/" target="_top" className="text-button">回到家庭</a></header>{blocked&&<section className="bridge-unavailable" aria-label="管家連線"><h2>{heading}</h2>{error?<Notice error>{error}</Notice>:<p>{deployment.bridge_state==='unpaired'?'請在外層設定啟用 HA 智能管家側欄。':deployment.bridge_state==='owner_required'?'只有這個家庭的擁有者可以開啟對話與批准操作。':deployment.bridge_state==='unavailable'?'家庭仍可照常使用；請確認外層主機已啟動。':'正在確認家庭授權。'}</p>}<Button secondary disabled={busy} onClick={async()=>{await onRetry();if(ready)await refresh()}}>{busy?'確認中…':'重新連線'}</Button></section>}{identity&&<BridgeConversation key={identity.storage_scope} identity={identity} active={!blocked}/>}</main>
}
