import {useEffect,useRef,useState} from 'react';
import {api,requestKey} from './api';
import {stateName} from './labels';
import {Button,Notice,Status,labels,timestamp} from './ui';

export type Task = {id:string; request:string; provider:string; state:string; message:string; created_at:string; updated_at:string; proposal_id?:string; proposal?:any; evidence?:any; events?:any[]; sources?:any[]};

type Named={name:string;area:string|null};
let inventoryNames:Record<string,Named>|null=null;
let inventoryLoad:Promise<void>|null=null;
function loadNames(){
  if(inventoryNames)return Promise.resolve();
  if(!inventoryLoad)inventoryLoad=api('inventory').then(data=>{
    const map:Record<string,Named>={};
    for(const entity of data?.entities||[])if(entity&&typeof entity.entity_id==='string'&&typeof entity.name==='string'&&entity.name.trim())map[entity.entity_id]={name:entity.name.trim(),area:typeof entity.area==='string'&&entity.area.trim()?entity.area.trim():null};
    inventoryNames=map;
  }).catch(()=>{inventoryNames={}});
  return inventoryLoad;
}
function deviceName(id:string,allowNames:boolean){
  if(allowNames){const known=inventoryNames?.[id];if(known?.name)return known.area?`${known.name}（${known.area}）`:known.name}
  return id==='input_boolean.agentic_lamp_test'?'測試燈':id==='input_boolean.agentic_motion_test'?'測試動作':id;
}
function happen(p:any,allowNames:boolean){
  const effect=p.mode==='disable'?'批准後停用，重啟後仍保持停用':p.mode==='rollback'?'批准後回復前一份設定':p.mode==='action'?'批准後執行一次並確認狀態':'批准後啟用，之後由家庭自行執行';
  if(p.intent){
    const trigger=p.intent.trigger;
    const window=p.intent.time_window?`在 ${p.intent.time_window.after}–${p.intent.time_window.before} `:'' ;
    const then=p.intent.actions.map((a:any)=>`${a.action==='turn_on'?'開啟':'關閉'}${deviceName(a.entity_id,allowNames)}`).join('、');
    return `當 ${deviceName(trigger.entity_id,allowNames)} ${window}從 ${stateName(trigger.from_state)} 變成 ${stateName(trigger.to_state)}，則${then}；${effect}。`;
  }
  if(p.action)return `批准後${p.action.action==='turn_on'?'開啟':'關閉'}${deviceName(p.action.entity_id,allowNames)}一次，並確認狀態。`;
  return `${effect}。`;
}

export function TaskDetail({task,onChange,bridge=false}:{task:Task;onChange:()=>void;bridge?:boolean}){
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[named,setNamed]=useState(0);
  const pending=useRef<{signature:string;key:string}|null>(null);
  useEffect(()=>{if(bridge)return;let alive=true;loadNames().then(()=>{if(alive)setNamed(value=>value+1)});return()=>{alive=false}},[bridge]);
  const allowNames=!bridge&&(!!inventoryNames||named>0);
  const p=task.proposal; const [reply,setReply]=useState('');
  async function action(path:string,data:unknown={}){setBusy(true);setError('');setNotice('');const signature=JSON.stringify([path,data]);if(pending.current?.signature!==signature)pending.current={signature,key:requestKey()};try{const result=await api(path,data,false,pending.current.key);pending.current=null;if(result.message)setNotice(result.message);onChange();return true}catch(e){setError((e as Error).message);return false}finally{setBusy(false)}}
  const helper=p?.intent&&new Set([p.intent.trigger.entity_id,...p.intent.actions.map((a:any)=>a.entity_id)]).size===2&&[p.intent.trigger.entity_id,...p.intent.actions.map((a:any)=>a.entity_id)].every((s:string)=>['input_boolean.agentic_motion_test','input_boolean.agentic_lamp_test'].includes(s));
  return <div className="task-detail">
    <div className="section-heading"><h2>{task.request}</h2><Status value={task.state}/></div>
    {task.message&&<p className="task-message">{task.message}</p>}
    {error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    {p&&<>
      <p className="proposal-summary">{happen(p,allowNames)}</p>
      {task.state==='awaiting_approval'&&<div className="approval"><p>有效至 {new Date(p.expires*1000).toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'})} · 只有家庭擁有者可以批准</p><Button disabled={busy||Date.now()>p.expires*1000} onClick={()=>action(`proposals/${p.id}/approve`,{digest:p.digest})}>{busy?'處理中…':'批准並執行'}</Button><Button secondary disabled={busy} onClick={()=>action(`tasks/${task.id}/cancel`)}>取消提案</Button></div>}
      <details className="proposal-diff"><summary>查看設定差異</summary>{p.intent&&<><div className="rule-step"><span>當</span><div><p className="device-label">{deviceName(p.intent.trigger.entity_id,allowNames)}</p><p>{stateName(p.intent.trigger.from_state)} <span aria-hidden>→</span> {stateName(p.intent.trigger.to_state)}</p><code>{p.intent.trigger.entity_id}</code></div></div>{p.intent.time_window&&<div className="rule-step"><span>於</span><p>{p.intent.time_window.after}–{p.intent.time_window.before}</p></div>}<div className="rule-step"><span>則</span><div>{p.intent.actions.map((a:any)=><p key={a.entity_id}><span className="device-label">{deviceName(a.entity_id,allowNames)}</span><b>{a.action==='turn_on'?'開啟':'關閉'}</b><code>{a.entity_id}</code></p>)}</div></div></>}{p.action&&<p><span className="device-label">{deviceName(p.action.entity_id,allowNames)}</span> → {p.action.action==='turn_on'?'開啟':'關閉'}<code>{p.action.entity_id}</code></p>}{p.prior&&<><h4>目前設定</h4><pre>{JSON.stringify(p.prior,null,2)}</pre></>}<h4>批准後的設定</h4><pre>{JSON.stringify(p.final||p.action,null,2)}</pre></details>
      {task.state==='needs_reconcile'&&<Button secondary disabled={busy} onClick={()=>action(`proposals/${p.id}/reconcile`)}>確認現況</Button>}
      {['verifying','succeeded'].includes(task.state)&&p.deployed_at&&helper&&<div className="verification-control"><p>會重設兩個測試開關，再產生規則要求的狀態變化。</p><Button secondary disabled={busy} onClick={()=>action(`proposals/${p.id}/test`)}>試一次這條規則</Button></div>}
    </>}
    {task.evidence&&<div className="evidence"><h3>觀測結果</h3>{task.evidence.kind==='real_state_transition'&&<><p>來源狀態已改變，同一條規則的目標也跟著改變。</p><p>{deviceName(task.evidence.source,allowNames)} · {timestamp(task.evidence.source_at)}</p>{task.evidence.targets?.map((target:any)=><p key={target.entity_id}>{deviceName(target.entity_id,allowNames)} → <strong>{stateName(target.state)}</strong></p>)}</>}{task.evidence.kind==='state_readback'&&<p>{deviceName(task.evidence.entity_id,allowNames)} → <strong>{stateName(task.evidence.state)}</strong>。已確認狀態，實際效果仍待觀察。</p>}</div>}
    {task.state==='awaiting_user'&&<form onSubmit={async e=>{e.preventDefault();if(await action(`tasks/${task.id}/reply`,{message:reply}))setReply('')}}><label>補充需求<textarea value={reply} onChange={e=>setReply(e.target.value)} rows={3} maxLength={5000} required/></label><Button type="submit" disabled={busy}>送出補充</Button></form>}
    {['queued','planning','awaiting_user'].includes(task.state)&&<Button secondary disabled={busy} onClick={()=>action(`tasks/${task.id}/cancel`)}>取消任務</Button>}
    <details><summary>技術資料</summary><p>工作 <code>{task.id}</code></p>{p?.digest&&<p>提案摘要 <code>{p.digest}</code></p>}{!!task.sources?.length&&<pre>{JSON.stringify(task.sources,null,2)}</pre>}{task.evidence&&<pre>{JSON.stringify(task.evidence,null,2)}</pre>}</details>
    {!!task.events?.length&&<details className="timeline"><summary>處理紀錄</summary><ol>{task.events.map(e=><li key={e.seq}><time>{timestamp(e.at)}</time><span>{labels[e.kind]||e.kind}</span></li>)}</ol></details>}
  </div>
}
