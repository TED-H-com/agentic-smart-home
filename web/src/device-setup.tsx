import {useEffect,useRef,useState} from 'react';
import {api,ApiError,requestKey} from './api';
import {Button,Notice,timestamp} from './ui';
import {isDiscoveryCandidate,isDiscoveryDevice,isDiscoveryIntegration,type DiscoverySnapshot} from './discovery';
import './device-setup.css';

type Candidate=DiscoverySnapshot['candidates'][number];
type Setup={contract_version:1;id:string;revision:number;proposal_digest:string;candidate:Candidate;candidate_aliases?:string[];
  state:'proposed'|'preparing'|'applying'|'configured'|'needs_reconcile'|'blocked';
  can_approve:boolean;can_reconcile:boolean;message:string;reason_code:string|null;
  created_at:string;updated_at:string;expires_at:number;backup_id:string|null;
  backup_scope:'home_assistant_configuration';integration:DiscoverySnapshot['integrations'][number]|null;
  devices:DiscoverySnapshot['devices'];devices_truncated:boolean;device_count:number|null;
  entity_count:number|null;available_entity_count:number|null;observed_at:string|null;
  physical_verification:'not_performed';native_path:'/config/integrations'};
type Mutation={path:string;body:Record<string,unknown>;key:string};
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const count=(value:unknown)=>value===null||typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const date=(value:unknown)=>typeof value==='string'&&Number.isFinite(Date.parse(value));
const pendingStates=new Set(['preparing','applying']);
const stateNames:Record<Setup['state'],string>={proposed:'確認接入',preparing:'正在備份',applying:'正在接入',configured:'接入結果',needs_reconcile:'正在確認結果',blocked:'接入暫停'};
const invalid=()=>{throw new Error('接入資料尚未確認，請重新讀取。')};

export function supportsChatSetup(candidate:Candidate){
  return candidate.source_id==='ha_discovery'&&candidate.integration_domain==='ipp'&&candidate.discovery_method==='zeroconf';
}

export function parseDeviceSetup(value:unknown,candidate:Candidate):Setup{
  if(!record(value)||value.contract_version!==1||typeof value.id!=='string'||!/[a-f0-9]{32}/.test(value.id)||value.id.length!==32||!Number.isSafeInteger(value.revision)||Number(value.revision)<1||typeof value.proposal_digest!=='string'||!/^[a-f0-9]{64}$/.test(value.proposal_digest))return invalid();
  const aliases=value.candidate_aliases??[];
  if(!Array.isArray(aliases)||aliases.length>16||!aliases.every(item=>typeof item==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(item)))return invalid();
  if(!isDiscoveryCandidate(value.candidate)||(value.candidate.id!==candidate.id&&!aliases.includes(candidate.id))||!supportsChatSetup(value.candidate)||value.candidate.integration_domain!==candidate.integration_domain||!Object.hasOwn(stateNames,String(value.state)))return invalid();
  if(typeof value.can_approve!=='boolean'||typeof value.can_reconcile!=='boolean'||typeof value.message!=='string'||value.message.length>1000||!(value.reason_code===null||typeof value.reason_code==='string')||!date(value.created_at)||!date(value.updated_at)||typeof value.expires_at!=='number'||!Number.isFinite(value.expires_at))return invalid();
  if(value.backup_scope!=='home_assistant_configuration'||!(value.backup_id===null||typeof value.backup_id==='string')||value.physical_verification!=='not_performed'||value.native_path!=='/config/integrations')return invalid();
  if(!(value.integration===null||isDiscoveryIntegration(value.integration)&&value.integration.domain==='ipp')||!Array.isArray(value.devices)||value.devices.length>100||!value.devices.every(isDiscoveryDevice)||typeof value.devices_truncated!=='boolean')return invalid();
  if(!count(value.device_count)||!count(value.entity_count)||!count(value.available_entity_count)||!(value.observed_at===null||date(value.observed_at)))return invalid();
  if(value.can_approve&&value.state!=='proposed'||value.can_reconcile&&!['configured','needs_reconcile'].includes(String(value.state)))return invalid();
  if(value.entity_count!==null&&value.available_entity_count!==null&&Number(value.available_entity_count)>Number(value.entity_count))return invalid();
  return value as unknown as Setup;
}

/** Owner actions stay on the trusted page; the model never submits approval. */
export function DeviceSetupPanel({candidate,disabled,visible=true}:{candidate:Candidate;disabled:boolean;visible?:boolean}){
  const [setup,setSetup]=useState<Setup|null>(null),[open,setOpen]=useState(false),[busy,setBusy]=useState(false),[fresh,setFresh]=useState(false),[error,setError]=useState('');
  const alive=useRef(true),generation=useRef(0),inFlight=useRef(false),allowed=useRef(!disabled),readable=useRef(visible),revoked=useRef(false);
  const latest=useRef<Setup|null>(null),pending=useRef<Mutation|null>(null);
  readable.current=visible&&!revoked.current;allowed.current=!disabled&&readable.current;
  function current(ticket:number){return alive.current&&readable.current&&generation.current===ticket}
  function accept(value:Setup){latest.current=value;setSetup(value);setOpen(true);setFresh(true)}
  async function read(ticket:number){
    let next:Setup|null=null;
    if(latest.current){
      next=parseDeviceSetup(await api(`device-setups/${latest.current.id}`),candidate);
    }else{
      const list=await api('device-setups');
      if(!record(list)||list.contract_version!==1||!Array.isArray(list.setups)||list.setups.length>100)return invalid();
      const matches=list.setups.filter(item=>record(item)&&record(item.candidate)&&(item.candidate.id===candidate.id||Array.isArray(item.candidate_aliases)&&item.candidate_aliases.includes(candidate.id)));
      if(matches.length){
        const values=matches.map(item=>parseDeviceSetup(item,candidate)).sort((a,b)=>Date.parse(b.created_at)-Date.parse(a.created_at));
        next=parseDeviceSetup(await api(`device-setups/${values[0].id}`),candidate);
      }
    }
    if(current(ticket)){
      if(next)accept(next);
      setError('');
    }
    return next;
  }
  async function refresh(){
    if(!readable.current||inFlight.current)return;
    inFlight.current=true;const ticket=++generation.current;setBusy(true);setFresh(false);
    try{await read(ticket)}catch(e){if(current(ticket)){setError((e as Error).message);setFresh(false)}}
    finally{if(current(ticket)){setBusy(false);inFlight.current=false}}
  }
  async function send(path:string,body:Record<string,unknown>){
    if((path.endsWith('/reconcile')?!readable.current:!allowed.current)||inFlight.current)return;
    inFlight.current=true;const ticket=++generation.current;setBusy(true);setFresh(false);setError('');setOpen(true);
    if(!pending.current||pending.current.path!==path||JSON.stringify(pending.current.body)!==JSON.stringify(body))pending.current={path,body,key:requestKey()};
    const action=pending.current;
    try{
      const result=parseDeviceSetup(await api(action.path,action.body,false,action.key),candidate);
      if(!current(ticket))return;
      if(latest.current&&result.id!==latest.current.id&&!(path==='device-setups'&&(latest.current.state==='blocked'||latest.current.state==='proposed')))return invalid();
      pending.current=null;accept(result);
      await read(ticket);
    }catch(e){
      if(!current(ticket))return;
      if(e instanceof ApiError&&e.status>0&&e.status<500)pending.current=null;
      setError((e as Error).message);setFresh(false);
      // A lost response only triggers readback, never another approval/HA write.
      if(!(e instanceof ApiError)||![401,403].includes(e.status)){
        try{await read(ticket)}catch{/* Explicit refresh remains available. */}
      }
    }finally{if(current(ticket)){setBusy(false);inFlight.current=false}}
  }
  useEffect(()=>{
    alive.current=true;
    return()=>{alive.current=false;generation.current++;inFlight.current=false};
  },[]);
  useEffect(()=>{
    generation.current++;inFlight.current=false;setBusy(false);setFresh(false);
    if(!visible)return;
    // The parent only restores visibility after a fresh authenticated session.
    revoked.current=false;readable.current=true;allowed.current=!disabled;
    void refresh();
    const timer=setInterval(()=>{
      if(document.visibilityState==='visible'&&latest.current&&pendingStates.has(latest.current.state))void refresh();
    },3000);
    const expired=()=>{revoked.current=true;readable.current=false;allowed.current=false;generation.current++;inFlight.current=false;pending.current=null;latest.current=null;setSetup(null);setOpen(false);setFresh(false);setBusy(false)};
    const events=['agentic-session-expired','agentic-bridge-expired'];
    const unavailable=()=>{readable.current=false;allowed.current=false;generation.current++;inFlight.current=false;setBusy(false);setFresh(false)};
    window.addEventListener('agentic-bridge-unavailable',unavailable);
    for(const event of events)window.addEventListener(event,expired);
    return()=>{clearInterval(timer);generation.current++;inFlight.current=false;window.removeEventListener('agentic-bridge-unavailable',unavailable);for(const event of events)window.removeEventListener(event,expired)};
    // Candidate changes remount this component using its source/id key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[visible]);
  if(!open)return <div className="device-setup-action"><Button secondary type="button" disabled={disabled||busy} onClick={()=>void send('device-setups',{candidate_id:candidate.id,integration_domain:'ipp'})}>協助設定</Button>{error&&<Notice error>{error}</Notice>}</div>;
  const working=setup&&pendingStates.has(setup.state);
  const canApprove=!!setup&&fresh&&setup.can_approve&&setup.expires_at>Date.now()/1000;
  return <section className="device-setup" aria-label={`接入 ${candidate.title}`} aria-busy={busy||!!working}>
    <h4>{setup?stateNames[setup.state]:'正在確認設備'}</h4>
    {error&&<Notice error>{error}</Notice>}
    {setup&&<>
      <p><strong>{setup.candidate.title||candidate.title||'網路印表機'}</strong></p>
      <p role={working?'status':undefined}>{setup.message}</p>
      {!fresh&&!working&&<p className="helper-text">正在顯示保存的紀錄，請重新讀取目前狀態。</p>}
      {setup.state==='configured'&&<div className="device-setup-readback">
        <p>{setup.integration?.state==='loaded'?'家庭已載入這台印表機。':setup.integration?'家庭已保存設定，仍在確認設備是否可用。':'尚未取得這次設定的最新狀態。'}</p>
        {setup.device_count!==null&&<p>{setup.device_count} 筆設備登錄{setup.available_entity_count!==null&&setup.entity_count!==null?` · ${setup.available_entity_count} / ${setup.entity_count} 個功能目前可讀取`:''}</p>}
        <p className="helper-text">尚未驗證實體功能；可以先確認印表機已開機。</p>
      </div>}
      {setup.state==='proposed'&&<Button type="button" disabled={disabled||busy||!canApprove} onClick={()=>void send(`device-setups/${setup.id}/approve`,{expected_revision:setup.revision,proposal_digest:setup.proposal_digest})}>確認接入</Button>}
      {setup.state==='needs_reconcile'&&<Button type="button" disabled={!visible||busy||!fresh||revoked.current||!setup.can_reconcile} onClick={()=>void send(`device-setups/${setup.id}/reconcile`,{expected_revision:setup.revision})}>確認接入結果</Button>}
      {(setup.state==='blocked'||setup.state==='proposed'&&(setup.expires_at<=Date.now()/1000||setup.reason_code==='device_setup_credentials_changed'))&&<Button type="button" disabled={disabled||busy||!fresh} onClick={()=>void send('device-setups',{candidate_id:candidate.id,integration_domain:'ipp'})}>重新準備接入</Button>}
      {setup.backup_id&&<p className="helper-text">已保存家庭設定備份。</p>}
      {setup.observed_at&&<p className="helper-text">讀回於 {timestamp(setup.observed_at)}</p>}
      {setup.reason_code&&<details><summary>診斷資訊</summary><code>{setup.reason_code}</code></details>}
    </>}
    {(!working||error)&&<Button secondary type="button" disabled={!visible||busy||revoked.current} onClick={()=>void refresh()}>重新讀取</Button>}
    {!setup&&!busy&&<Button secondary type="button" disabled={disabled} onClick={()=>void send('device-setups',{candidate_id:candidate.id,integration_domain:'ipp'})}>重試設定</Button>}
  </section>;
}
