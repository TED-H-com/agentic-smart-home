import {useEffect,useRef,useState} from 'react';
import {api,ApiError,requestKey} from './api';
import {parseInventory,type InventorySnapshot} from './network-inventory';
import {Button,Notice} from './ui';

type Scope={id:string;label:string;interface:string;address:string;network:string};
type Scopes={contract_version:1;scopes:Scope[];limitations:string[]};
type Attempt={scope:Scope;baseline:InventorySnapshot;key:string;revision:number|null;notified:boolean};
const lanIds=['lan_neighbors','lan_mdns','lan_ssdp'];
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const text=(value:unknown,max:number):value is string=>typeof value==='string'&&value.length>0&&value.length<=max;
const revision=(value:unknown):value is number=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;

function parseScopes(value:unknown):Scopes{
  if(!record(value)||value.contract_version!==1||!Array.isArray(value.scopes)||value.scopes.length>32||
    !Array.isArray(value.limitations)||value.limitations.length>16||!value.limitations.every(item=>typeof item==='string'&&item.length<=160)||
    !value.scopes.every(item=>record(item)&&text(item.id,128)&&/^[A-Za-z0-9_-]+$/.test(item.id)&&
      text(item.label,160)&&text(item.interface,32)&&text(item.address,45)&&text(item.network,49))||
    new Set(value.scopes.map(item=>item.id)).size!==value.scopes.length){
    throw new Error('主機網路資料暫時無法確認，請重新讀取。');
  }
  return value as Scopes;
}

/** A revision change alone could be an import/clear or another network's scan. */
function savedScan(snapshot:InventorySnapshot,attempt:Attempt):boolean{
  if(snapshot.revision<=attempt.baseline.revision||attempt.revision!==null&&snapshot.revision<attempt.revision)return false;
  const sources=snapshot.sources.filter(source=>lanIds.includes(source.id));
  if(sources.length!==3||!sources.every(source=>source.target===attempt.scope.network&&
    ['ok','partial','unavailable'].includes(source.state)&&source.observed_at&&
    source.observed_at!==attempt.baseline.sources.find(previous=>previous.id===source.id)?.observed_at))return false;
  if(new Set(sources.map(source=>source.observed_at)).size!==1)return false;
  return snapshot.observations.every(item=>item.source_id==='router_import'||item.scope_id===attempt.scope.id);
}

/** Render only for the current conversation, keyed by its session and thread.
 * Scan POST is sent once. Uncertain outcomes are resolved only through saved GETs.
 */
export function ConversationNetworkDiscovery({disabled,onDiscovered}:{disabled:boolean;onDiscovered:()=>void}){
  const [open,setOpen]=useState(false),[scopes,setScopes]=useState<Scopes|null>(null),[scope,setScope]=useState('');
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[uncertain,setUncertain]=useState(false),[done,setDone]=useState(false);
  const mounted=useRef(true),enabled=useRef(!disabled),expanded=useRef(false),generation=useRef(0),inFlight=useRef(false);
  const baseline=useRef<InventorySnapshot|null>(null),pending=useRef<Attempt|null>(null),notify=useRef(onDiscovered);
  enabled.current=!disabled;notify.current=onDiscovered;
  const current=(ticket:number)=>mounted.current&&enabled.current&&expanded.current&&
    generation.current===ticket&&document.visibilityState!=='hidden';

  function close(clear=false){
    generation.current++;expanded.current=false;inFlight.current=false;
    if(clear)pending.current=null;
    baseline.current=null;setOpen(false);setScopes(null);setScope('');setBusy('');setError('');setDone(false);
    setUncertain(!!pending.current);
  }
  function finish(snapshot:InventorySnapshot,attempt:Attempt,ticket:number):boolean{
    if(!current(ticket)||pending.current!==attempt||attempt.notified||!savedScan(snapshot,attempt))return false;
    attempt.notified=true;pending.current=null;baseline.current=snapshot;
    setUncertain(false);setDone(true);setError('');setBusy('');inFlight.current=false;
    notify.current();
    return true;
  }
  function denied(error:unknown):boolean{
    if(error instanceof ApiError&&[401,403].includes(error.status)){close(true);return true}
    return false;
  }
  async function load(){
    if(!enabled.current||inFlight.current||!expanded.current)return;
    const ticket=++generation.current;inFlight.current=true;setBusy('loading');setError('');
    try{
      const [networks,snapshot]=await Promise.all([
        api('discovery/inventory/scopes').then(parseScopes),api('discovery/inventory').then(parseInventory),
      ]);
      if(!current(ticket))return;
      setScopes(networks);setScope(networks.scopes.length===1?networks.scopes[0].id:'');baseline.current=snapshot;
      if(pending.current){
        if(finish(snapshot,pending.current,ticket))return;
        setUncertain(true);setError('尚未確認這次探索已保存；請重新確認結果。');
      }
    }catch(e){
      if(current(ticket)&&!denied(e))setError(e instanceof Error?e.message:'暫時無法讀取主機網路。');
    }finally{if(current(ticket)){inFlight.current=false;setBusy('')}}
  }
  function toggle(){
    if(disabled)return;
    if(expanded.current){close();return}
    expanded.current=true;setOpen(true);setDone(false);void load();
  }
  async function readback(attempt:Attempt,ticket:number){
    // Host collection is bounded to 6 s; its Controller transport has a 10 s
    // deadline. api() already allows 45 s. A lost reply never retries the POST.
    setBusy('readback');setUncertain(true);
    for(const delay of [0,1500,3000]){
      if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
      if(!current(ticket)||pending.current!==attempt)return;
      const snapshot=parseInventory(await api('discovery/inventory'));
      if(!current(ticket))return;
      if(finish(snapshot,attempt,ticket))return;
    }
    if(current(ticket))setError('尚未確認這次探索已保存；請重新確認結果。');
  }
  async function check(){
    const attempt=pending.current;
    if(!attempt||inFlight.current||!enabled.current||!expanded.current)return;
    const ticket=++generation.current;inFlight.current=true;setError('');
    try{await readback(attempt,ticket)}
    catch(e){if(current(ticket)&&!denied(e))setError('暫時無法讀回探索結果，請稍後重新確認。')}
    finally{if(current(ticket)){inFlight.current=false;setBusy('')}}
  }
  async function scan(){
    const selected=scopes?.scopes.find(item=>item.id===scope);
    if(!selected||!baseline.current||pending.current||inFlight.current||!enabled.current||!expanded.current)return;
    const attempt:Attempt={scope:selected,baseline:baseline.current,key:requestKey(),revision:null,notified:false};
    pending.current=attempt;const ticket=++generation.current;inFlight.current=true;
    setBusy('scan');setError('');setDone(false);setUncertain(false);
    try{
      try{
        const result:unknown=await api('discovery/inventory/scan',{scope_id:selected.id},false,attempt.key);
        if(!record(result)||result.contract_version!==1||!revision(result.revision)||result.revision<=attempt.baseline.revision){
          throw new Error('探索回應尚未確認。');
        }
        if(pending.current===attempt)attempt.revision=result.revision;
      }catch(e){
        if(!current(ticket))return;
        if(denied(e))return;
        if(e instanceof ApiError&&e.status>=400&&e.status<500){pending.current=null;throw e}
        // Timeout, lost reply or malformed receipt: inspect persisted data only.
      }
      if(current(ticket))await readback(attempt,ticket);
    }catch(e){
      if(current(ticket)&&!denied(e)){
        setUncertain(!!pending.current);
        setError(pending.current?'暫時無法讀回探索結果，請稍後重新確認。':e instanceof Error?e.message:'探索尚未完成，請重新確認網路。');
      }
    }finally{if(current(ticket)){inFlight.current=false;setBusy('')}}
  }

  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;generation.current++;pending.current=null}},[]);
  useEffect(()=>{if(disabled)close()},[disabled]);
  useEffect(()=>{
    const expire=()=>close(true),unavailable=()=>close(),visibility=()=>{if(document.visibilityState==='hidden')close()};
    const events=['agentic-session-expired','agentic-bridge-expired'];
    events.forEach(event=>window.addEventListener(event,expire));
    window.addEventListener('agentic-bridge-unavailable',unavailable);document.addEventListener('visibilitychange',visibility);
    return()=>{events.forEach(event=>window.removeEventListener(event,expire));
      window.removeEventListener('agentic-bridge-unavailable',unavailable);document.removeEventListener('visibilitychange',visibility)};
  },[]);

  const selected=scopes?.scopes.find(item=>item.id===scope);
  return <div className="conversation-network-discovery">
    <Button secondary type="button" disabled={disabled} aria-expanded={open} onClick={toggle}>探索更多設備</Button>
    {open&&!disabled&&<section className="conversation-network-panel" aria-label="探索家庭網路" aria-busy={!!busy}>
      <h3>探索家庭網路</h3>
      {busy&&<p role="status">{busy==='loading'?'正在讀取可探索的主機網路…':busy==='scan'?'正在探索所選網路…':'正在確認已保存的探索結果…'}</p>}
      {error&&<Notice error>{error}</Notice>}
      {done?<p role="status">探索結果已保存，正在交給管家協助。</p>:uncertain?<>
        <p>這次探索的結果仍待確認，會保留原請求並讀取已保存的觀測。</p>
        <Button secondary type="button" disabled={!!busy} onClick={()=>void check()}>重新確認探索結果</Button>
        <Button secondary type="button" disabled={!!busy} onClick={()=>{pending.current=null;setUncertain(false);setError('');void load()}}>重新選擇網路</Button>
      </>:<>
        {scopes?.scopes.length===1?<p>找到一個可探索的主機網路：<strong>{scopes.scopes[0].label}</strong>。</p>:scopes&&scopes.scopes.length>1?<label>探索的家庭網路<select aria-label="探索的家庭網路" value={scope} disabled={!!busy} onChange={event=>setScope(event.target.value)}><option value="">請選擇家中設備所在的網路</option>{scopes.scopes.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select></label>:null}
        {scopes&&!scopes.scopes.length&&<p>目前沒有可探索的主機實體網路。請確認主機已接上家庭網路後，重新讀取。</p>}
        {selected&&<><p>確認後，會在「{selected.label}」讀取鄰居紀錄及設備服務公告，將結果與已保存的家庭設備觀測交給管家協助。這次探索不會操作或部署設備。</p>
          <p className="helper-text">只涵蓋所選網路；離線、隔離網路、藍牙及需要品牌帳號的設備可能看不到。</p>
          <Button type="button" disabled={!!busy||!baseline.current} onClick={()=>void scan()}>確認探索並交給管家</Button></>}
        {(!scopes||!scopes.scopes.length||error)&&<Button secondary type="button" disabled={!!busy} onClick={()=>void load()}>重新讀取主機網路</Button>}
      </>}
      <Button secondary type="button" onClick={()=>close()}>收起探索</Button>
    </section>}
  </div>;
}
