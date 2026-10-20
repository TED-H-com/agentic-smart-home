import {useEffect,useId,useRef,useState,type FormEvent} from 'react';
import {createPortal} from 'react-dom';
import {api,ApiError,haPath,haTarget,requestKey} from './api';
import {Button,Notice,timestamp} from './ui';
import {isDiscoveryCandidate,isDiscoveryDevice,isDiscoveryIntegration,isDiscoverySource,type DiscoverySnapshot} from './discovery';
import './device-setup.css';

type Candidate=DiscoverySnapshot['candidates'][number];
type SetupInput={step:'link'|'2sa'|'backup_share';error:'invalid_auth'|'cannot_connect'|'unknown'|'missing_data'|'otp_failed'|null;options:{value:string;label:string}[];can_submit:boolean};
export type DeviceSetup={contract_version:1;id:string;revision:number;proposal_digest:string;candidate:Candidate;candidate_aliases?:string[];
  state:'proposed'|'preparing'|'applying'|'needs_input'|'configured'|'needs_reconcile'|'blocked';input:SetupInput|null;
  can_approve:boolean;can_reconcile:boolean;message:string;reason_code:string|null;
  created_at:string;updated_at:string;expires_at:number;backup_id:string|null;
  backup_scope:'home_assistant_configuration';integration:DiscoverySnapshot['integrations'][number]|null;
  devices:DiscoverySnapshot['devices'];devices_truncated:boolean;device_count:number|null;
  entity_count:number|null;available_entity_count:number|null;observed_at:string|null;
  physical_verification:'not_performed';native_path:'/config/integrations'};
type Mutation={path:string;body:Record<string,unknown>;key:string};
type Setup=DeviceSetup;
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const count=(value:unknown)=>value===null||typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const date=(value:unknown)=>typeof value==='string'&&value.length<=40&&Number.isFinite(Date.parse(value));
const identifier=(value:unknown)=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(value);
const label=(value:unknown)=>typeof value==='string'&&value.length<=160;
const shareValue=(value:unknown)=>typeof value==='string'&&/^\/[^\x00-\x1f]{1,255}$/u.exec(value)?.[0]===value;
const optionalLabel=(value:unknown)=>value===null||label(value);
const pendingStates=new Set(['preparing','applying']);
const connectedReasons=new Set(['device_setup_already_configured','device_setup_existing_updated']);
const nativeSetupReasons=new Set(['device_setup_model_unsupported','device_setup_core_unsupported','device_setup_flow_unsupported']);
export const setupStateNames:Record<Setup['state'],string>={proposed:'確認接入',preparing:'正在備份',applying:'正在接入',needs_input:'繼續接入',configured:'接入結果',needs_reconcile:'待確認結果',blocked:'接入暫停'};
const invalid=()=>{throw new Error('接入資料尚未確認，請重新讀取。')};

export function supportsChatSetup(candidate:Candidate){
  return candidate.source_id==='ha_discovery'&&(
    candidate.discovery_method==='zeroconf'&&(
      candidate.integration_domain==='ipp'&&(candidate.confirmation_step==null||candidate.confirmation_step==='zeroconf_confirm')||
      candidate.integration_domain==='shelly'&&candidate.confirmation_step==='confirm_discovery')||
    candidate.integration_domain==='synology_dsm'&&['ssdp','zeroconf'].includes(candidate.discovery_method)&&candidate.confirmation_step==='link');
}

export function parseDeviceSetup(value:unknown,candidate?:Candidate,expectedId?:string):Setup{
  if(!record(value)||value.contract_version!==1||typeof value.id!=='string'||!/[a-f0-9]{32}/.test(value.id)||value.id.length!==32||!Number.isSafeInteger(value.revision)||Number(value.revision)<1||typeof value.proposal_digest!=='string'||!/^[a-f0-9]{64}$/.test(value.proposal_digest))return invalid();
  const aliases=value.candidate_aliases??[];
  if(!Array.isArray(aliases)||aliases.length>16||!aliases.every(item=>typeof item==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(item)))return invalid();
  if(!isDiscoveryCandidate(value.candidate)||!/^[A-Za-z0-9_-]{1,128}$/.test(value.candidate.id)||value.candidate.title.length>160||!supportsChatSetup(value.candidate)||typeof value.state!=='string'||!Object.hasOwn(setupStateNames,value.state))return invalid();
  const input=value.input??null;
  if(value.state==='needs_input'){
    if(value.candidate.integration_domain!=='synology_dsm'||!record(input)||typeof input.step!=='string'||!['link','2sa','backup_share'].includes(input.step)||!(input.error===null||typeof input.error==='string'&&['invalid_auth','cannot_connect','unknown','missing_data','otp_failed'].includes(input.error))||typeof input.can_submit!=='boolean')return invalid();
    if(!Array.isArray(input.options)||input.options.length>64||!input.options.every(item=>record(item)&&shareValue(item.value)&&label(item.label))||new Set(input.options.map(item=>item.value)).size!==input.options.length||input.step!=='backup_share'&&input.options.length>0)return invalid();
  }else if(input!==null)return invalid();
  if(candidate&&(value.candidate.integration_domain!==candidate.integration_domain||value.candidate.id!==candidate.id&&(!!expectedId||!aliases.includes(candidate.id))))return invalid();
  if(expectedId&&value.id!==expectedId)return invalid();
  if(typeof value.can_approve!=='boolean'||typeof value.can_reconcile!=='boolean'||typeof value.message!=='string'||value.message.length>1000||!(value.reason_code===null||typeof value.reason_code==='string'&&value.reason_code.length<=160)||!date(value.created_at)||!date(value.updated_at)||typeof value.expires_at!=='number'||!Number.isFinite(value.expires_at))return invalid();
  if(value.backup_scope!=='home_assistant_configuration'||!(value.backup_id===null||typeof value.backup_id==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(value.backup_id))||value.physical_verification!=='not_performed'||value.native_path!=='/config/integrations')return invalid();
  if(!(value.integration===null||isDiscoveryIntegration(value.integration)&&value.integration.domain===value.candidate.integration_domain)||!Array.isArray(value.devices)||value.devices.length>100||!value.devices.every(isDiscoveryDevice)||typeof value.devices_truncated!=='boolean')return invalid();
  if(value.integration!==null&&(!identifier(value.integration.id)||!label(value.integration.title)))return invalid();
  if(!value.devices.every(item=>identifier(item.id)&&label(item.name)&&optionalLabel(item.manufacturer)&&optionalLabel(item.model)&&
    (item.via_device_id===null||identifier(item.via_device_id))&&(item.parent_device_id===null||identifier(item.parent_device_id))&&
    item.integration_ids.length<=100&&item.integration_ids.every(identifier))||new Set(value.devices.map(item=>item.id)).size!==value.devices.length)return invalid();
  if(!count(value.device_count)||!count(value.entity_count)||!count(value.available_entity_count)||!(value.observed_at===null||date(value.observed_at)))return invalid();
  if(!Array.isArray(value.sources)||value.sources.length>16||!value.sources.every(item=>isDiscoverySource(item)&&identifier(item.id)&&optionalLabel(item.reason_code)&&(item.observed_at===null||date(item.observed_at)))||new Set(value.sources.map(item=>item.id)).size!==value.sources.length)return invalid();
  if(value.can_approve&&value.state!=='proposed'||value.can_reconcile&&!['configured','needs_reconcile','needs_input'].includes(value.state))return invalid();
  if(value.entity_count!==null&&value.available_entity_count!==null&&Number(value.available_entity_count)>Number(value.entity_count))return invalid();
  return {...value,input} as unknown as Setup;
}

export function parseDeviceSetupList(value:unknown):Setup[]{
  if(!record(value)||value.contract_version!==1||!Array.isArray(value.setups)||value.setups.length>100)return invalid();
  const setups=value.setups.map(item=>parseDeviceSetup(item));
  if(new Set(setups.map(item=>item.id)).size!==setups.length)return invalid();
  return setups.sort((a,b)=>Date.parse(b.created_at)-Date.parse(a.created_at)||a.id.localeCompare(b.id));
}

/** Owner actions stay on the trusted page; the model never submits approval. */
export function DeviceSetupPanel({candidate,disabled,visible=true,exactId,onShare}:{candidate:Candidate;disabled:boolean;visible?:boolean;exactId?:string;onShare?:(id:string)=>void}){
  const [setup,setSetup]=useState<Setup|null>(null),[open,setOpen]=useState(!!exactId),[busy,setBusy]=useState(false),[fresh,setFresh]=useState(false),[error,setError]=useState('');
  const [nativeReason,setNativeReason]=useState<string|null>(null);
  const [username,setUsername]=useState(''),[password,setPassword]=useState(''),[otp,setOtp]=useState(''),[share,setShare]=useState<string|null>(null);
  const formId=useId(),Form=exactId?'div':'form';
  const card=useRef<HTMLElement|null>(null),scrollAfterAction=useRef(false);
  const alive=useRef(true),generation=useRef(0),inFlight=useRef(false),allowed=useRef(!disabled),readable=useRef(visible),revoked=useRef(false);
  const latest=useRef<Setup|null>(null),pending=useRef<Mutation|null>(null);
  readable.current=visible&&!revoked.current;allowed.current=!disabled&&readable.current;
  function clearInput(){setUsername('');setPassword('');setOtp('');setShare(null)}
  function current(ticket:number){return alive.current&&readable.current&&generation.current===ticket}
  function accept(value:Setup,ownerAction=false){
    const previous=latest.current;
    if(ownerAction&&!exactId&&(previous?.id!==value.id||previous?.state!==value.state||previous?.input?.step!==value.input?.step||previous?.input?.can_submit!==value.input?.can_submit))scrollAfterAction.current=true;
    if(latest.current?.revision!==value.revision||latest.current?.id!==value.id)clearInput();latest.current=value;setSetup(value);setOpen(true);setFresh(true);setNativeReason(null)}
  async function read(ticket:number,clearError=true,ownerAction=false){
    let next:Setup|null=null;
    const id=exactId||latest.current?.id;
    if(id){
      next=parseDeviceSetup(await api(`device-setups/${id}`),exactId?candidate:latest.current?.candidate,id);
    }else{
      const matches=parseDeviceSetupList(await api('device-setups')).filter(item=>item.candidate.integration_domain===candidate.integration_domain&&(item.candidate.id===candidate.id||item.candidate_aliases?.includes(candidate.id)));
      if(matches.length){
        next=parseDeviceSetup(await api(`device-setups/${matches[0].id}`),matches[0].candidate,matches[0].id);
      }
    }
    if(current(ticket)){
      if(next)accept(next,ownerAction);
      if(clearError)setError('');
    }
    return next;
  }
  async function refresh(ownerAction=false){
    if(!readable.current||inFlight.current)return;
    inFlight.current=true;const ticket=++generation.current;setBusy(true);setFresh(false);
    try{await read(ticket,true,ownerAction)}catch(e){if(current(ticket)){setError((e as Error).message);setFresh(false)}}
    finally{if(current(ticket)){setBusy(false);inFlight.current=false}}
  }
  async function send(path:string,body:Record<string,unknown>){
    if(!supportsChatSetup(candidate))return;
    if(exactId&&path!==`device-setups/${exactId}/reconcile`&&path!==`device-setups/${exactId}/input`)return;
    if((path.endsWith('/reconcile')?!readable.current:!allowed.current)||inFlight.current)return;
    inFlight.current=true;const ticket=++generation.current;setBusy(true);setFresh(false);setError('');setNativeReason(null);setOpen(true);
    // Secret submissions are single-use and never retained for a same-key retry.
    const singleUse=path.endsWith('/input')||Object.hasOwn(body,'credentials');
    if(singleUse)pending.current=null;
    else if(!pending.current||pending.current.path!==path||JSON.stringify(pending.current.body)!==JSON.stringify(body))pending.current={path,body,key:requestKey()};
    const key=singleUse?requestKey():pending.current!.key;
    try{
      const response=api(path,body,false,key);body={};
      const result=parseDeviceSetup(await response,candidate,exactId);
      if(!current(ticket))return;
      if(latest.current&&result.id!==latest.current.id&&!(path==='device-setups'&&(latest.current.state==='blocked'||latest.current.state==='proposed')))return invalid();
      pending.current=null;accept(result,true);
      await read(ticket,true,true);
    }catch(e){
      if(!current(ticket))return;
      if(e instanceof ApiError&&e.status>0&&e.status<500)pending.current=null;
      if(e instanceof ApiError&&e.status===409&&e.code&&nativeSetupReasons.has(e.code)){
        setNativeReason(e.code);setFresh(false);
        setError(e.code==='device_setup_model_unsupported'?'這個型號尚未支援對話接入，請到家庭設定繼續。':e.message);
        return;
      }
      setError((e as Error).message);setFresh(false);
      // A lost response only triggers readback, never another approval/HA write.
      if(!(e instanceof ApiError)||![401,403].includes(e.status)){
        try{await read(ticket,false,true)}catch{/* Explicit refresh remains available. */}
      }
    }finally{if(current(ticket)){setBusy(false);inFlight.current=false}}
  }
  useEffect(()=>{
    alive.current=true;
    return()=>{clearInput();alive.current=false;generation.current++;inFlight.current=false};
  },[]);
  useEffect(()=>{
    generation.current++;inFlight.current=false;setBusy(false);setFresh(false);
    if(!visible){clearInput();return}
    // The parent only restores visibility after a fresh authenticated session.
    revoked.current=false;readable.current=true;allowed.current=!disabled;
    void refresh();
    const timer=setInterval(()=>{
      if(document.visibilityState==='visible'&&latest.current&&pendingStates.has(latest.current.state))void refresh();
    },3000);
    const expired=()=>{clearInput();revoked.current=true;readable.current=false;allowed.current=false;generation.current++;inFlight.current=false;pending.current=null;latest.current=null;setSetup(null);setNativeReason(null);setOpen(false);setFresh(false);setBusy(false)};
    const events=['agentic-session-expired','agentic-bridge-expired'];
    const unavailable=()=>{clearInput();readable.current=false;allowed.current=false;generation.current++;inFlight.current=false;setBusy(false);setFresh(false)};
    window.addEventListener('agentic-bridge-unavailable',unavailable);
    for(const event of events)window.addEventListener(event,expired);
    return()=>{clearInterval(timer);generation.current++;inFlight.current=false;window.removeEventListener('agentic-bridge-unavailable',unavailable);for(const event of events)window.removeEventListener(event,expired)};
    // Candidate changes remount this component using its source/id key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[visible]);
  useEffect(()=>{
    if(!busy&&scrollAfterAction.current){
      scrollAfterAction.current=false;
      // Keep the active controls inside the thread when the card is taller than it.
      if(visible&&!exactId)(card.current?.querySelector('.device-setup-input')||card.current)?.scrollIntoView({block:'nearest'});
    }
  },[setup,busy,visible,exactId]);
  if(!open&&!exactId)return <div className="device-setup-action"><Button secondary type="button" disabled={disabled||busy} onClick={()=>void send('device-setups',{candidate_id:candidate.id,integration_domain:candidate.integration_domain})}>{candidate.integration_domain==='shelly'?'核對型號':'協助設定'}</Button>{error&&<Notice error>{error}</Notice>}</div>;
  const working=setup&&pendingStates.has(setup.state);
  const needsNativeSetup=!!nativeReason||!!setup?.reason_code&&nativeSetupReasons.has(setup.reason_code);
  const canApprove=!!setup&&!needsNativeSetup&&fresh&&setup.can_approve&&setup.expires_at>Date.now()/1000;
  const input=setup?.input;
  const unavailableInput=setup?.state==='needs_input'&&!input?.can_submit;
  const step=setup?.state==='proposed'&&candidate.integration_domain==='synology_dsm'&&!exactId?'link':setup?.state==='needs_input'&&input?.can_submit?input.step:null;
  const inputReady=step==='link'?!!username&&!!password:step==='2sa'?/^[0-9]{6,8}$/.test(otp):!!input?.options.some(option=>option.value===share);
  const canSubmit=!!setup&&!!step&&!needsNativeSetup&&fresh&&!disabled&&!busy&&inputReady&&(setup.state==='proposed'?canApprove:!!input?.can_submit);
  function submit(event:FormEvent){
    event.preventDefault();event.stopPropagation();
    if(!setup||!step||!canSubmit||!allowed.current||inFlight.current)return;
    const body=setup.state==='proposed'?{expected_revision:setup.revision,proposal_digest:setup.proposal_digest,credentials:{username,password}}:
      {expected_revision:setup.revision,step,...(step==='link'?{username,password}:step==='2sa'?{otp_code:otp}:{backup_share:share})};
    clearInput();
    void send(`device-setups/${setup.id}/${setup.state==='proposed'?'approve':'input'}`,body);
  }
  return <section ref={card} className="device-setup" aria-label={`接入 ${candidate.title}`} aria-busy={busy||!!working}>
    <h4>{setup?setupStateNames[setup.state]:needsNativeSetup?'需要到家庭設定':'正在確認設備'}</h4>
    {error&&error!==setup?.message&&<Notice error>{error}</Notice>}
    {setup&&<>
      <p><strong>{setup.candidate.title||candidate.title||'設備'}</strong></p>
      {setup.state==='needs_input'?<Notice error={!!input?.error}>{setup.message}</Notice>:<p role={working?'status':undefined}>{setup.message}</p>}
      {!fresh&&!working&&<p className="helper-text">正在顯示保存的紀錄，請重新讀取目前狀態。</p>}
      {setup.state==='configured'&&<div className="device-setup-readback">
        <p>{setup.integration?.state==='loaded'?'家庭已載入這項設備設定。':setup.integration?'家庭已保存設定，仍在確認設備是否可用。':'尚未取得這次設定的最新狀態。'}</p>
        {setup.device_count!==null&&<p>{setup.device_count} 筆設備登錄{setup.available_entity_count!==null&&setup.entity_count!==null?` · ${setup.available_entity_count} / ${setup.entity_count} 個功能目前可讀取`:''}</p>}
        <p className="helper-text">尚未驗證實體功能；可以先確認設備已開機。</p>
      </div>}
      {step&&<Form className="device-setup-input" id={exactId?undefined:formId} onSubmit={submit}>
        {step==='link'&&<>
          <label>NAS 帳號<input form={formId} autoComplete="username" maxLength={128} required disabled={disabled||busy||!fresh} value={username} onChange={event=>setUsername(event.target.value)}/></label>
          <label>NAS 密碼<input form={formId} type="password" autoComplete="current-password" maxLength={256} required disabled={disabled||busy||!fresh} value={password} onChange={event=>setPassword(event.target.value)}/></label>
        </>}
        {step==='2sa'&&<label>驗證碼<input form={formId} inputMode="numeric" autoComplete="one-time-code" maxLength={8} pattern="[0-9]{6,8}" required disabled={disabled||busy||!fresh} value={otp} onChange={event=>setOtp(event.target.value)}/></label>}
        {step==='backup_share'&&<fieldset disabled={disabled||busy||!fresh}><legend>備份資料夾</legend>{input?.options.map(option=><label className="device-setup-option" key={option.value}><input form={formId} type="radio" name={`backup-share-${setup.id}`} value={option.value} checked={share===option.value} onChange={()=>setShare(option.value)}/><span>{option.label}</span></label>)}</fieldset>}
        <Button form={formId} type="submit" disabled={!canSubmit}>{step==='link'?setup.state==='proposed'?'登入並接入':'重新登入':step==='2sa'?'送出驗證碼':'使用這個資料夾'}</Button>
      </Form>}
      {/* History lives inside the composer form; associate its controls with a separate form. */}
      {step&&exactId&&createPortal(<form hidden id={formId} onSubmit={submit}/>,document.body)}
      {!exactId&&setup.state==='proposed'&&candidate.integration_domain!=='synology_dsm'&&<Button type="button" disabled={disabled||busy||!canApprove} onClick={()=>void send(`device-setups/${setup.id}/approve`,{expected_revision:setup.revision,proposal_digest:setup.proposal_digest})}>確認接入</Button>}
      {unavailableInput&&<Button type="button" disabled={!visible||busy||revoked.current||!setup.can_reconcile} onClick={()=>void(fresh?send(`device-setups/${setup.id}/reconcile`,{expected_revision:setup.revision}):refresh(true))}>重新讀取</Button>}
      {setup.state==='needs_reconcile'&&<Button type="button" disabled={!visible||busy||!fresh||revoked.current||!setup.can_reconcile} onClick={()=>void send(`device-setups/${setup.id}/reconcile`,{expected_revision:setup.revision})}>確認接入結果</Button>}
      {!exactId&&!needsNativeSetup&&(setup.state==='blocked'&&!connectedReasons.has(setup.reason_code||'')||setup.state==='proposed'&&(setup.expires_at<=Date.now()/1000||setup.reason_code==='device_setup_credentials_changed'))&&<Button type="button" disabled={disabled||busy||!fresh} onClick={()=>void send('device-setups',{candidate_id:candidate.id,integration_domain:candidate.integration_domain})}>重新準備接入</Button>}
      {exactId&&['proposed','blocked'].includes(setup.state)&&<p className="helper-text">需要重新接入時，請先探索目前的設備。</p>}
      {setup.backup_id&&<p className="helper-text">已保存家庭設定備份。</p>}
      {setup.observed_at&&<p className="helper-text">讀回於 {timestamp(setup.observed_at)}</p>}
      {setup.reason_code&&<details><summary>診斷資訊</summary><code>{setup.reason_code}</code></details>}
    </>}
    {needsNativeSetup&&visible&&!revoked.current&&<a className="button secondary" href={haPath('/config/integrations')} target={haTarget()} rel="noreferrer">開啟家庭設定 ↗</a>}
    {(!working||error)&&!unavailableInput&&<Button secondary type="button" disabled={!visible||busy||revoked.current} onClick={()=>void refresh(true)}>重新讀取</Button>}
    {!exactId&&!setup&&!busy&&!needsNativeSetup&&<Button secondary type="button" disabled={disabled} onClick={()=>void send('device-setups',{candidate_id:candidate.id,integration_domain:candidate.integration_domain})}>重試設定</Button>}
    {onShare&&setup&&!unavailableInput&&<div className="device-setup-share"><p className="helper-text">將這筆接入進度與設備狀態交給管家，用於這則訊息。</p><Button secondary type="button" disabled={disabled||busy||!fresh||!visible||revoked.current} onClick={()=>{if(allowed.current&&fresh&&!inFlight.current)onShare(setup.id)}}>請管家解讀結果</Button></div>}
  </section>;
}
