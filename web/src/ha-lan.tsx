import {useEffect,useRef,useState} from 'react';
import {api,ApiError,requestKey} from './api';
import {plainError} from './labels';
import {Button,Notice,Status,friendlySentence,timestamp} from './ui';

type HomeObservation={state:string;reason?:string;ipv4?:string[];ipv6?:string[];interface?:string|null;internal_url?:string|null;observed_at?:string|null};
type LanState={contract_version:1;mode:'nat'|'wired';state:string;selected_interface_id:string|null;interface:string|null;guest_mac:string|null;interfaces:{id:string;label:string;interface:string;address:string|null;network:string|null}[];vm_state:string;can_configure:boolean;can_connect?:boolean;core_configuration_pending?:boolean;fallback_complete?:boolean;recovery_pending?:boolean;limitations:string[];home?:HomeObservation;ha_lan_ip?:string|null;message?:string|null};
type Job={id:string;state:'running'|'succeeded'|'failed'|'needs_reconcile';message?:string;request:{id:string;action:'configure_lan'}};
const jobId=/^[a-zA-Z0-9_-]{8,100}$/;
const storageKey=(scope?:string)=>scope&&/^[a-f0-9]{64}$/.test(scope)?`agentic-home-network-job-${scope}`:null;
function checkedJob(value:Job,id:string):Job{
  if(!value||value.id!==id||value.request?.id!==id||value.request.action!=='configure_lan'||
    !['running','succeeded','failed','needs_reconcile'].includes(value.state))throw new Error('家庭網路工作尚未確認。');
  return value;
}
function savedJob(key:string|null):string|null{
  if(!key)return null;
  const value=sessionStorage.getItem(key);
  if(value!==null&&!jobId.test(value))throw new Error('家庭網路工作紀錄需要核對。');
  return value;
}
const unsettled=(job:Job|null)=>job?.state==='running'||job?.state==='needs_reconcile';
function readyForDiscovery(value:LanState|null){
  const observed=Date.parse(value?.home?.observed_at||'');
  return !!value&&value.mode==='wired'&&value.state==='configured'&&value.home?.state==='ready'&&
    Number.isFinite(observed)&&Date.now()-observed>=-5000&&Date.now()-observed<30000&&
    !value.recovery_pending&&!value.fallback_complete;
}

const homeCopy:Record<string,string>={
  needs_home_setup:'已改用有線網路，正在確認家庭區網位址。',
  unavailable:'正在重新核對家庭網路。',
  waiting_for_address:'正在等待路由器分配家庭位址。',
  adapter_selection_required:'正在完成家庭設備探索設定。',
  callback_address_required:'正在完成家庭本機網址設定。',
};

export function HomeLanSettings({storageScope,disabled=false,active=true,onContinue}:{storageScope?:string;disabled?:boolean;active?:boolean;onContinue?:()=>void}){
  const [value,setValue]=useState<LanState|null>(null),[choice,setChoice]=useState(''),[job,setJob]=useState<Job|null>(null);
  const [busy,setBusy]=useState(false),[readError,setReadError]=useState(false),[error,setError]=useState<{text:string;code?:string}|null>(null);
  const [unconfirmed,setUnconfirmed]=useState(false),[storageError,setStorageError]=useState(false),[fenced,setFenced]=useState(false);
  const jobRef=useRef<Job|null>(null),verifiedId=useRef<string|null>(null),selectionDirty=useRef(false);
  const alive=useRef(false),serial=useRef(0),mutationSerial=useRef(0),revoked=useRef(false),inFlight=useRef(false),observedAt=useRef(0);
  const enabled=useRef(false),key=storageKey(storageScope);
  enabled.current=active&&!disabled&&!fenced;
  const current=(ticket:number)=>alive.current&&enabled.current&&!revoked.current&&ticket===serial.current&&document.visibilityState!=='hidden';
  const currentMutation=(ticket:number)=>alive.current&&enabled.current&&!revoked.current&&ticket===mutationSerial.current&&document.visibilityState!=='hidden';
  async function refresh(){
    if(!alive.current||!enabled.current||revoked.current||document.visibilityState==='hidden')return null;
    const request=++serial.current;
    let identifier:string|null;
    try{identifier=savedJob(key);if(!key)setStorageError(true)}
    catch{setStorageError(true);setUnconfirmed(true);return null}
    const [network,operation]=await Promise.allSettled([
      api('network/ha-lan'),identifier?api(`host/jobs/${identifier}`).then(result=>checkedJob(result,identifier!)):Promise.resolve(null),
    ]);
    if(!current(request))return null;
    let next:LanState|null=null;
    if(network.status==='fulfilled'&&network.value?.contract_version===1&&Array.isArray(network.value.interfaces)&&['nat','wired'].includes(network.value.mode)){
      next=network.value;setValue(next);setReadError(false);observedAt.current=Date.now();
      if(!selectionDirty.current)setChoice(next!.selected_interface_id||'');
    }else{setReadError(true);observedAt.current=0}
    if(operation.status==='fulfilled'){
      verifiedId.current=identifier;jobRef.current=operation.value;setJob(operation.value);setUnconfirmed(false);if(operation.value)setError(null);
    }else{setUnconfirmed(true);jobRef.current=null;setJob(null)}
    return next&&operation.status==='fulfilled'?next:null;
  }
  useEffect(()=>{
    alive.current=true;revoked.current=false;setFenced(false);
    const expire=()=>{revoked.current=true;++serial.current;++mutationSerial.current;setFenced(true);setValue(null);observedAt.current=0};
    window.addEventListener('agentic-session-expired',expire);
    return()=>{alive.current=false;++serial.current;++mutationSerial.current;window.removeEventListener('agentic-session-expired',expire)};
  },[storageScope]);
  useEffect(()=>{
    let stopped=false,timer:ReturnType<typeof setTimeout>;
    async function poll(){if(stopped)return;await refresh();if(!stopped)timer=setTimeout(poll,5000)}
    const visibility=()=>{++serial.current;observedAt.current=0;if(document.visibilityState!=='hidden'){clearTimeout(timer);void poll()}};
    if(active&&!disabled&&!fenced)void poll();else{++serial.current;observedAt.current=0}
    document.addEventListener('visibilitychange',visibility);
    return()=>{stopped=true;++serial.current;clearTimeout(timer);document.removeEventListener('visibilitychange',visibility)};
  },[active,disabled,fenced,storageScope]);
  async function save(disable=false){
    if(!enabled.current||revoked.current||document.visibilityState==='hidden'||inFlight.current||blocked||journalRecovery||unconfirmed||
      (!disable&&(choiceMissing||((selectionDirty.current||value?.interfaces.length!==1)&&!choiceAvailable))))return;
    // Another surface in this tab may have submitted while this panel was
    // hidden. Never overwrite an operation this panel has not read back.
    try{if(savedJob(key)!==verifiedId.current){void refresh();return}}
    catch{setStorageError(true);return}
    const id=requestKey();
    // Persist only the opaque identity before submission. Unknown outcomes are
    // resolved through exact GETs, including after reload, never by a new POST.
    try{if(!key)throw new Error();sessionStorage.setItem(key,id);if(savedJob(key)!==id)throw new Error()}
    catch{setStorageError(true);return}
    inFlight.current=true;setBusy(true);setError(null);setUnconfirmed(true);
    const ticket=++mutationSerial.current;++serial.current;
    const body=disable?{action:'configure_lan',interface_id:null}:!reauthorization&&!selectionDirty.current&&value?.interfaces.length===1?{action:'configure_lan'}:{action:'configure_lan',interface_id:choice};
    try{
      const result=checkedJob(await api('host/actions',body,false,id),id);
      if(currentMutation(ticket)&&savedJob(key)===id){jobRef.current=result;setJob(result);setUnconfirmed(false)}
    }catch(e){
      // A definite rejection can resolve its own persisted identity even after
      // this card closes. Polling cannot discard this receipt, and a newer
      // surface's identity is never cleared. Unknown outcomes remain fenced.
      let owned=false,rejected=false;
      try{
        owned=savedJob(key)===id;
        if(owned&&key&&e instanceof ApiError&&[400,409,422].includes(e.status)){
          sessionStorage.removeItem(key);rejected=true;
        }
      }catch{if(currentMutation(ticket))setStorageError(true)}
      if(currentMutation(ticket)&&owned){if(rejected)setUnconfirmed(false);setError(plainError(e))}
    }finally{inFlight.current=false;if(alive.current){setBusy(false);if(enabled.current&&!revoked.current)await refresh()}}
  }
  async function continueDiscovery(){
    if(!onContinue||!enabled.current||revoked.current||busy||unconfirmed||storageError||readError||
      Date.now()-observedAt.current>=15000||!readyForDiscovery(value)||unsettled(jobRef.current))return;
    setBusy(true);const next=await refresh();
    if(alive.current)setBusy(false);
    if(next&&enabled.current&&!revoked.current&&document.visibilityState!=='hidden'&&readyForDiscovery(next)&&
      !unsettled(jobRef.current))onContinue();
  }
  const running=job?.state==='running',readBlocked=disabled||!active||fenced||busy||running||readError||storageError||!key||unconfirmed||job?.state==='needs_reconcile';
  const blocked=readBlocked||!(value?.can_connect??value?.can_configure);
  const choiceAvailable=!!value?.interfaces.some(item=>item.id===choice),choiceMissing=!!choice&&!choiceAvailable;
  const wired=value?.mode==='wired'&&value.state==='configured';
  const home=value?.home,ip=(home?.state==='ready'?home.ipv4?.[0]||home.ipv6?.[0]:null)||value?.ha_lan_ip;
  const reauthorization=home?.state==='unavailable'&&home.reason==='home_lan_reauthorization_required';
  const journalRecovery=home?.state==='unavailable'&&home.reason==='home_lan_journal_recovery_required';
  const explain=reauthorization?'家庭網路設定已暫停；請再按一次「連上家庭網路」確認。':ip?`HA 已連上家庭網路：${ip}`:homeCopy[home?.state||'']||'已改用有線網路，正在確認家庭區網位址。';
  const connectionComplete=!reauthorization&&wired&&(home?.state==='ready'||value?.core_configuration_pending===true);
  const fallbackComplete=value?.fallback_complete===true&&value.mode==='nat'&&value.state==='disabled'&&value.vm_state==='running';
  const failed=job&&['failed','needs_reconcile'].includes(job.state);
  const recoveryPending=value?.recovery_pending===true||value?.state==='needs_reconcile'||job?.state==='needs_reconcile';
  return <section className="maintenance-content" aria-label="家庭區網接入" aria-busy={busy}>
    <p>讓家庭連上家中設備；系統會自動備份、重新啟動並確認位址。</p>
    {storageError&&<Notice error>無法保存接入進度，請確認瀏覽器可保存這個網站的工作紀錄後重新開啟。</Notice>}
    {fenced&&<Notice error>登入已變更，請重新登入後查看家庭網路。</Notice>}
    {unconfirmed&&!fenced&&<Notice>正在核對原本的接入工作；尚未確認前不會重新送出。<Button secondary type="button" disabled={busy||!active||disabled} onClick={()=>void refresh()}>重新確認接入進度</Button></Notice>}
    {readError&&<Notice error><p>暫時無法讀取家庭網路，恢復連線後會自動重試。</p></Notice>}
    {journalRecovery&&<Notice error>家庭網路設定需要核對；請聯絡支援，保留目前設定。</Notice>}
    {error&&<Notice error reason={error.code}>{error.text}</Notice>}
    {fallbackComplete&&value?.message&&!failed&&<Notice>{value.message}</Notice>}
    {failed&&<Notice error reason={job.message}>{fallbackComplete?'家庭網路未能連上，已恢復基本網路，可稍後重試。':recoveryPending?'自動恢復尚未完成；資料已保留，可稍後重試或聯絡支援。':friendlySentence(job.message,'家庭網路未能連上，網路設定未變更，可稍後重試。')}</Notice>}
    {!value?<p role="status">正在檢查可用的有線網路…</p>:<>
      <div className="section-heading"><h3>家庭網路</h3><Status value={running?'running':reauthorization?'blocked':journalRecovery?'needs_reconcile':value.state==='needs_reconcile'?'needs_reconcile':wired&&ip?'succeeded':wired?'running':'not_started'} label={running?'正在連接':reauthorization?'已暫停':journalRecovery?'需要核對':value.state==='needs_reconcile'?'需要核對':wired&&ip?'已連接':wired?'正在確認':'尚未接入'}/></div>
      {wired&&!journalRecovery&&<p role="status">{explain}</p>}
      {wired&&home?.state==='needs_home_setup'&&ip&&<p className="helper-text">家庭已取得區網位址；完成家庭授權後會自動核對探索功能。</p>}
      {!wired&&<p className="helper-text">連接期間家庭自動化會短暫暫停；未能連上時會自動恢復基本網路。</p>}
      {choiceMissing&&<p className="helper-text">原先選擇的網路卡目前無法使用，請重新選擇。</p>}
      {(value.interfaces.length>1||selectionDirty.current||choiceMissing)&&<label>接到家庭路由器的網路線<select value={choice} disabled={!!blocked} onChange={event=>{selectionDirty.current=true;setChoice(event.target.value);setError(null)}}>
        <option value="">選擇有線網路</option>
        {choiceMissing&&<option value={choice} disabled>原先選擇的網路卡無法使用</option>}
        {value.interfaces.map(item=><option key={item.id} value={item.id}>{item.interface}{item.address?` · ${item.address}`:''}</option>)}
      </select></label>}
      {!value.interfaces.length&&<p className="helper-text">請插上網路線，或接上 USB 有線網路卡。</p>}
      <Button type="button" onClick={()=>void save()} disabled={!!blocked||journalRecovery||!!connectionComplete||choiceMissing||!value.interfaces.length||((value.interfaces.length>1||selectionDirty.current)&&!choiceAvailable)}>{busy||running?'正在連接…':'連上家庭網路'}</Button>
      {wired&&<Button secondary type="button" disabled={!!blocked} onClick={()=>void save(true)}>使用基本網路</Button>}
      {onContinue&&<div className="home-lan-continue"><p className="helper-text">繼續探索會分享設備名稱、已保存盤點及家庭連線狀態。</p><Button type="button" disabled={!!readBlocked||!readyForDiscovery(value)||Date.now()-observedAt.current>=15000||journalRecovery||reauthorization} onClick={()=>void continueDiscovery()}>繼續探索家庭設備</Button></div>}
      {wired&&<div className="service-row">
        {home?.state==='ready'&&<p className="helper-text">可到「裝置與服務」新增設備，再實測控制。</p>}
        <details><summary>連線資料</summary><dl className="worker-evidence"><dt>主機網路卡</dt><dd>{value.interface||'尚無'}</dd><dt>家庭網路卡</dt><dd>{home?.interface||'尚未讀回'}</dd><dt>家庭區網位址</dt><dd>{[...(home?.ipv4||[]),...(home?.ipv6||[])].join('、')||value.ha_lan_ip||'尚未讀回'}</dd><dt>家庭本機網址</dt><dd>{home?.internal_url||'尚未核對'}</dd><dt>讀回時間</dt><dd>{timestamp(home?.observed_at||undefined)}</dd></dl><p className="helper-text">網路隔離會讓主機無法直接連到家庭區網位址，這是正常情況；系統透過專用管理連線核對。這項檢查不代表所有設備已接通。</p></details>
      </div>}
    </>}
  </section>
}

export function HomeLanFold({setup=false,storageScope}:{setup?:boolean;storageScope?:string}){
  const [open,setOpen]=useState(false);
  return <details className={setup?'bootstrap-manual':'host-fold'} onToggle={event=>setOpen(event.currentTarget.open)}><summary>{setup?'家庭網路（有線設備探索）':'家庭區網接入'}</summary>{open&&<HomeLanSettings key={storageScope} storageScope={storageScope}/>}</details>
}
