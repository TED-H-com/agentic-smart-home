import {useEffect,useRef,useState} from 'react';
import {api,ApiError,haPath,haTarget,requestKey} from './api';
import {Button,Confirm,Notice,timestamp} from './ui';
import {isDiscoveryCandidate,isDiscoveryDevice,isDiscoveryIntegration,isDiscoverySource,type DiscoverySnapshot} from './discovery';
import './discovery-setup.css';

type Candidate=DiscoverySnapshot['candidates'][number];
type Integration=DiscoverySnapshot['integrations'][number];
type Setup={contract_version:1;id:string;revision:number;candidate:Candidate;candidate_observed_at:string;created_at:string;updated_at:string;expires_at:string;selected_entry_id:string|null;selected_at:string|null};
type SetupList={contract_version:1;setups:Setup[];limit:20;retention_days:number};
type SetupView={contract_version:1;setup:Setup;observed_at:string;candidate_state:'present'|'not_seen'|'unknown';integration_state:'unselected'|'present'|'not_seen'|'unknown';integration:Integration|null;options:{integration:Integration;known_before:boolean|null}[];options_truncated:boolean;devices:DiscoverySnapshot['devices'];devices_truncated:boolean;device_count:number|null;entity_count:number|null;available_entity_count:number|null;sources:DiscoverySnapshot['sources'];physical_verification:'not_performed';native_path:'/config/integrations'};
type Reference={contract_version:1;id:string;revision:number;forgotten:boolean};
type Mutation={kind:'create'|'select'|'forget';path:string;body:Record<string,string|number>;key:string;id?:string};
const isRecord=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const date=(value:unknown):value is string=>typeof value==='string'&&Number.isFinite(Date.parse(value));
const identifier=(value:unknown):value is string=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(value);
const setupId=(value:unknown):value is string=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
const revision=(value:unknown):value is number=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=1;
const count=(value:unknown):value is number|null=>value===null||typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const unique=(items:{id:string}[])=>new Set(items.map(item=>item.id)).size===items.length;
const invalid=():never=>{throw new Error('接入資料格式已變更，請重新讀取或開啟家庭設定查看。')};
const integrationStates:Record<Integration['state'],string>={loaded:'已載入',not_loaded:'尚未載入',retry:'等待重試',error:'設定異常',disabled:'已停用',ignored:'已忽略',unknown:'狀態未知'};
const sourceNames:Record<string,string>={ha_discovery:'家庭自動探索',ha_devices:'設備登錄',ha_entities:'設備功能清單',ha_states:'功能目前狀態',ha_integrations:'整合設定'};
const sourceStates:Record<string,string>={ok:'已讀取',partial:'部分資料',unavailable:'暫時無法讀取',unsupported:'目前無法支援',not_implemented:'尚未接入'};

function parseSetup(value:unknown):Setup{
  if(!isRecord(value)||value.contract_version!==1||!setupId(value.id)||!revision(value.revision)||!isDiscoveryCandidate(value.candidate)||!identifier(value.candidate.id)||!/^[a-z][a-z0-9_]{0,63}$/.test(value.candidate.integration_domain)||!date(value.candidate_observed_at)||!date(value.created_at)||!date(value.updated_at)||!date(value.expires_at)||(value.selected_entry_id!==null&&!identifier(value.selected_entry_id))||(value.selected_at!==null&&!date(value.selected_at)))return invalid();
  return value as unknown as Setup;
}
export function parseDiscoverySetupList(value:unknown):SetupList{
  if(!isRecord(value)||value.contract_version!==1||value.limit!==20||!revision(value.retention_days)||!Array.isArray(value.setups)||value.setups.length>20)return invalid();
  const setups=value.setups.map(parseSetup);
  if(!unique(setups))return invalid();
  return {...value,setups} as SetupList;
}
export function parseDiscoverySetupView(value:unknown,expectedId:string):SetupView{
  if(!isRecord(value)||value.contract_version!==1||value.native_path!=='/config/integrations'||value.physical_verification!=='not_performed'||!date(value.observed_at)||typeof value.candidate_state!=='string'||!['present','not_seen','unknown'].includes(value.candidate_state)||typeof value.integration_state!=='string'||!['unselected','present','not_seen','unknown'].includes(value.integration_state))return invalid();
  const setup=parseSetup(value.setup);
  if(setup.id!==expectedId||!Array.isArray(value.options)||value.options.length>100||!value.options.every(option=>isRecord(option)&&isDiscoveryIntegration(option.integration)&&identifier(option.integration.id)&&option.integration.domain===setup.candidate.integration_domain&&(option.known_before===null||typeof option.known_before==='boolean'))||!Array.isArray(value.devices)||value.devices.length>100||!value.devices.every(isDiscoveryDevice)||!Array.isArray(value.sources)||value.sources.length>16||!value.sources.every(isDiscoverySource)||typeof value.options_truncated!=='boolean'||typeof value.devices_truncated!=='boolean'||!count(value.device_count)||!count(value.entity_count)||!count(value.available_entity_count))return invalid();
  if(!unique(value.devices)||!unique(value.sources)||!unique(value.options.map(option=>option.integration))||value.entity_count!==null&&value.available_entity_count!==null&&value.available_entity_count>value.entity_count)return invalid();
  const sourceMap=new Map(value.sources.map(source=>[source.id,source]));
  const complete=(id:string)=>{const source=sourceMap.get(id);return source?.state==='ok'&&!source.truncated};
  if(!['ha_discovery','ha_devices','ha_entities','ha_states','ha_integrations'].every(id=>sourceMap.has(id))||value.sources.some(source=>source.observed_at!==null&&!date(source.observed_at)))return invalid();
  if(value.candidate_state==='not_seen'&&!complete('ha_discovery')||value.integration_state==='not_seen'&&!complete('ha_integrations'))return invalid();
  if(value.integration_state==='unselected'?(setup.selected_entry_id!==null||value.integration!==null):setup.selected_entry_id===null)return invalid();
  if(value.integration_state==='present'){
    if(!isDiscoveryIntegration(value.integration)||value.integration.id!==setup.selected_entry_id||value.integration.domain!==setup.candidate.integration_domain)return invalid();
    if(value.devices.some(device=>device.integration_ids.length!==1||device.integration_ids[0]!==setup.selected_entry_id))return invalid();
    if(value.device_count!==null&&(!complete('ha_devices')||value.device_count<value.devices.length)||value.entity_count!==null&&(value.device_count===null||!complete('ha_entities'))||value.available_entity_count!==null&&(value.entity_count===null||!complete('ha_states')))return invalid();
  }else if(value.integration!==null||value.devices.length||value.device_count!==null||value.entity_count!==null||value.available_entity_count!==null)return invalid();
  return {...value,setup} as SetupView;
}
function parseReference(value:unknown,action:Mutation):Reference{
  if(!isRecord(value)||value.contract_version!==1||!setupId(value.id)||!revision(value.revision)||typeof value.forgotten!=='boolean'||value.forgotten!==(action.kind==='forget')||action.id&&value.id!==action.id)return invalid();
  return value as unknown as Reference;
}

/** Tracking is deliberately lazy: neither opening chat nor reading discovery starts it. */
export function TrackingPanel({candidates,onOpenChange}:{candidates:Candidate[];onOpenChange?:(open:boolean)=>void}){
  const [open,setOpen]=useState(false),[list,setList]=useState<SetupList|null>(null),[view,setView]=useState<SetupView|null>(null),[id,setId]=useState<string|null>(null);
  const [choice,setChoice]=useState(''),[filter,setFilter]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(''),[fresh,setFresh]=useState(false),[uncertain,setUncertain]=useState(false),[forget,setForget]=useState(false);
  const alive=useRef(true),expanded=useRef(false),generation=useRef(0),selected=useRef<string|null>(null),inFlight=useRef(false),pending=useRef<Mutation|null>(null),panel=useRef<HTMLDetailsElement>(null);
  const isCurrent=(request:number)=>alive.current&&expanded.current&&generation.current===request;
  function clearView(){setView(null);setChoice('');setFresh(false);setForget(false)}
  function toggle(next:boolean){
    expanded.current=next;generation.current++;inFlight.current=false;setBusy('');setOpen(next);setError('');setFilter('');clearView();setId(null);selected.current=null;setList(null);
    if(!next)setUncertain(!!pending.current);
    onOpenChange?.(next);
    if(next)void read(null);
    else panel.current?.querySelector('summary')?.focus();
  }
  async function read(target=selected.current){
    if(inFlight.current||!expanded.current)return;
    const request=++generation.current;inFlight.current=true;setBusy('read');setError('');setFresh(false);setChoice('');setForget(false);
    try{
      const [nextList,nextView]=await Promise.all([target?Promise.resolve(null):api('discovery/setups').then(parseDiscoverySetupList),target?api(`discovery/setups/${target}`).then(value=>parseDiscoverySetupView(value,target)):Promise.resolve(null)]);
      if(isCurrent(request)){if(nextList)setList(nextList);setView(nextView);setFresh(!!nextView);setUncertain(!!pending.current)}
    }catch(e){if(isCurrent(request)){setError(e instanceof Error?e.message:'接入進度暫時無法讀取，請稍後重試。');if(e instanceof ApiError&&[401,403,404,410].includes(e.status)){setList(null);clearView()}}}
    finally{if(isCurrent(request)){inFlight.current=false;setBusy('')}}
  }
  async function mutate(action:Mutation){
    if(inFlight.current||!expanded.current)return;
    pending.current=action;const request=++generation.current;inFlight.current=true;setBusy(action.kind);setError('');setUncertain(false);setFresh(false);setForget(false);
    let acknowledged=false;
    try{
      const reference=parseReference(await api(action.path,action.body,false,action.key),action);
      if(!isCurrent(request))return;
      acknowledged=true;pending.current=null;setChoice('');setFilter('');clearView();
      const target=reference.forgotten?null:reference.id;selected.current=target;setId(target);
      const [nextList,nextView]=await Promise.all([api('discovery/setups').then(parseDiscoverySetupList),target?api(`discovery/setups/${target}`).then(value=>parseDiscoverySetupView(value,target)):Promise.resolve(null)]);
      if(nextView&&(nextView.setup.revision<reference.revision||action.kind==='create'&&(nextView.setup.candidate.id!==action.body.candidate_id||nextView.setup.candidate.integration_domain!==action.body.integration_domain)||action.kind==='select'&&nextView.setup.revision===reference.revision&&nextView.setup.selected_entry_id!==action.body.entry_id))return invalid();
      if(isCurrent(request)){setList(nextList);setView(nextView);setFresh(!!nextView)}
    }catch(e){
      if(!isCurrent(request))return;
      const retry= !acknowledged&&(!(e instanceof ApiError)||e.status===0||e.status>=500);
      if(!retry)pending.current=null;
      setUncertain(retry);setError(acknowledged?'進度已保存，但暫時無法讀回；請重新讀取接入。':retry?'尚未確認這次操作的結果；重試會使用同一筆請求。':e instanceof Error?e.message:'這次沒有完成，請重新讀取後再試。');
      if(e instanceof ApiError&&[401,403,404,409,410].includes(e.status))clearView();
      // A revision conflict is a readback boundary, never an automatic re-submit.
      if(e instanceof ApiError&&e.status===409&&action.id){
        try{
          const nextView=parseDiscoverySetupView(await api(`discovery/setups/${action.id}`),action.id);
          if(isCurrent(request)){setView(nextView);setFresh(true);setChoice('')}
        }catch{/* The explicit read action remains available if conflict readback fails. */}
      }
    }finally{if(isCurrent(request)){inFlight.current=false;setBusy('')}}
  }
  function create(candidate:Candidate){
    if(pending.current)return;
    void mutate({kind:'create',path:'discovery/setups',body:{candidate_id:candidate.id,integration_domain:candidate.integration_domain},key:requestKey()});
  }
  function selectEntry(){
    if(!view||!fresh||!choice||pending.current)return;
    void mutate({kind:'select',path:`discovery/setups/${view.setup.id}/select`,body:{expected_revision:view.setup.revision,entry_id:choice},key:requestKey(),id:view.setup.id});
  }
  function forgetSetup(){
    if(!view||!fresh||pending.current)return;
    void mutate({kind:'forget',path:`discovery/setups/${view.setup.id}/forget`,body:{expected_revision:view.setup.revision},key:requestKey(),id:view.setup.id});
  }
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;generation.current++}},[]);
  useEffect(()=>{
    const refresh=()=>{if(expanded.current&&document.visibilityState==='visible'&&!inFlight.current)void read()};
    const expire=()=>{pending.current=null;setUncertain(false);toggle(false)};
    window.addEventListener('focus',refresh);document.addEventListener('visibilitychange',refresh);
    const events=['agentic-session-expired','agentic-bridge-expired','agentic-bridge-unavailable'];events.forEach(event=>window.addEventListener(event,expire));
    return()=>{window.removeEventListener('focus',refresh);document.removeEventListener('visibilitychange',refresh);events.forEach(event=>window.removeEventListener(event,expire))};
  },[]);
  const locked=!!busy||uncertain;
  const matchingCandidates=candidates.filter(candidate=>`${candidate.title} ${candidate.integration_domain}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).slice(0,20);
  const options=view?.options.filter(option=>`${option.integration.title} ${option.integration.domain} ${option.integration.id}`.toLocaleLowerCase().includes(filter.toLocaleLowerCase()))||[];
  const completeSource=(sourceId:string)=>{const source=view?.sources.find(item=>item.id===sourceId);return source?.state==='ok'&&!source.truncated};
  const deviceKnown=view?.integration_state==='present'&&completeSource('ha_devices');
  const countsKnown=deviceKnown&&completeSource('ha_entities')&&completeSource('ha_states');
  return <details className="discovery-setup" ref={panel} open={open}>
    <summary onClick={event=>{event.preventDefault();toggle(!open)}}>接入進度</summary>
    {open&&<div className="discovery-setup-content" aria-busy={!!busy}>
      <div className="discovery-setup-tools"><p>保存設定進度；登入與配對仍在家庭完成。</p><Button secondary type="button" disabled={!!busy} onClick={()=>void read()}>重新讀取接入</Button></div>
      {busy&&<p role="status">{busy==='read'?'正在讀取接入進度…':'正在保存接入進度…'}</p>}
      {error&&!uncertain&&<Notice error><p>{error}</p></Notice>}
      {uncertain&&<Notice error><p>尚未確認這次操作的結果；重試會使用同一筆請求。</p><Button secondary type="button" disabled={!!busy} onClick={()=>pending.current&&void mutate(pending.current)}>重試本次操作</Button></Notice>}
      {list&&<p className="helper-text">追蹤保存 {list.retention_days} 天，最多 {list.limit} 項；不會自動授予管家讀取或控制權限。</p>}
      {!id&&<>
        <label className="discovery-setup-filter">篩選接入項目<input type="search" aria-label="篩選接入項目" value={filter} onChange={event=>setFilter(event.target.value)}/></label>
        {list&&<section aria-label="已保存的接入"><h3>已保存的接入</h3>{list.setups.length?<ul className="discovery-setup-list">{list.setups.map(setup=><li key={setup.id}><div><strong>{setup.candidate.title||setup.candidate.integration_domain}</strong><span>{setup.selected_entry_id?'已選擇整合':'等待選擇整合'} · 保存至 {timestamp(setup.expires_at)}</span></div><Button secondary type="button" disabled={locked} aria-label={`查看接入 ${setup.candidate.title||setup.candidate.integration_domain}`} onClick={()=>{selected.current=setup.id;setId(setup.id);clearView();setFilter('');void read(setup.id)}}>查看進度</Button></li>)}</ul>:<p className="discovery-setup-empty">還沒有保存的接入進度。</p>}</section>}
        <section aria-label="可追蹤的探索候選"><h3>待設定的探索結果</h3>{matchingCandidates.length?<ul className="discovery-setup-list">{matchingCandidates.map(candidate=><li key={candidate.id}><div><strong>{candidate.title||candidate.integration_domain}</strong><span>{candidate.integration_domain} · 待設定</span></div><Button secondary type="button" disabled={locked||!list} aria-label={`追蹤接入 ${candidate.title||candidate.integration_domain}`} onClick={()=>create(candidate)}>追蹤接入</Button></li>)}</ul>:<p className="discovery-setup-empty">目前沒有符合條件的候選；已保存的進度仍可接續。</p>}{candidates.length>20&&<p className="helper-text">一次最多顯示 20 項，請輸入名稱篩選。</p>}</section>
      </>}
      {id&&<>
        <Button secondary type="button" disabled={locked} onClick={()=>{selected.current=null;setId(null);clearView();setFilter('');void read(null)}}>返回接入清單</Button>
        {!view&&<a className="discovery-setup-fallback" href={haPath('/config/integrations')} target={haTarget()} rel="noreferrer">開啟家庭設定 ↗</a>}
        {view&&<article className="discovery-setup-detail" role="region" aria-label="接入讀回">
          <div className="discovery-setup-heading"><div><h3>{view.setup.candidate.title||view.setup.candidate.integration_domain}</h3><p>{view.setup.selected_entry_id?'已選擇整合；請查看本次讀回':'等待家庭設定與選擇整合'}</p></div><a className="button secondary" href={haPath('/config/integrations')} target={haTarget()} rel="noreferrer">開啟家庭設定 ↗</a></div>
          {!fresh&&<Notice>以下是上次讀回資料；目前狀態尚未重新確認。</Notice>}
          <p className="helper-text">讀回於 {timestamp(view.observed_at)}。{view.candidate_state==='not_seen'?'原候選目前未見，仍不能據此判定設定完成。':view.candidate_state==='unknown'?'原候選目前無法確認。':'原候選仍出現在家庭探索中。'}</p>
          <section className="discovery-setup-readback" aria-label="目前讀回狀態">
            <dl><div><dt>整合設定</dt><dd>{view.integration_state==='present'&&view.integration?<>{view.integration.title||view.integration.domain} · {integrationStates[view.integration.state]}</>:view.integration_state==='unselected'?'尚未選擇':view.integration_state==='not_seen'?'所選整合目前未見':'尚未確認'}{view.setup.selected_entry_id&&<code className="discovery-setup-selected-id">{view.setup.selected_entry_id}</code>}</dd></div><div><dt>設備登錄</dt><dd>{deviceKnown&&view.device_count!==null?`${view.device_count} 筆設備紀錄`:'尚未完整確認'}</dd></div><div><dt>狀態讀取</dt><dd>{countsKnown&&view.available_entity_count!==null&&view.entity_count!==null?`${view.available_entity_count} / ${view.entity_count} 個功能顯示可用狀態`:'尚未完整確認'}</dd></div></dl>
            <p>尚未進行實體操作驗證。</p><p className="helper-text">功能數量涵蓋相關設備紀錄的功能，不等於整合的全部功能。</p>
          </section>
          {view.setup.selected_entry_id&&<p className="helper-text">整合關聯由你選擇，不代表已證明是同一台實體設備。</p>}
          <section className="discovery-setup-choice" aria-label="選擇設定結果"><h3>{view.setup.selected_entry_id?'變更整合選擇':'選擇設定結果'}</h3><p>選擇你在家庭設定的整合，保存後再讀回。</p><label>篩選整合<input type="search" aria-label="篩選整合" value={filter} disabled={locked||!fresh} onChange={event=>setFilter(event.target.value)}/></label>
            {options.length?<fieldset disabled={locked||!fresh}><legend className="sr-only">選擇整合</legend>{options.map(option=><label key={option.integration.id} className="discovery-setup-option"><input type="radio" name={`setup-entry-${view.setup.id}`} value={option.integration.id} aria-label={`選擇整合 ${option.integration.title||option.integration.domain} ${option.integration.id}`} checked={choice===option.integration.id} onChange={()=>setChoice(option.integration.id)}/><span><strong>{option.integration.title||option.integration.domain}</strong><small>{integrationStates[option.integration.state]} · <code>{option.integration.id}</code></small><small>{option.known_before===true?'追蹤開始前已讀到':option.known_before===false?'完整的開始前清單未見；不代表由這次設定建立':'開始前資料不足，無法比較'}</small></span></label>)}</fieldset>:<p className="discovery-setup-empty">{view.options_truncated?'清單不完整，請到家庭確認設定結果。':filter?'沒有符合篩選條件的整合。':'目前沒有可選整合；請先在家庭完成設定。'}</p>}
            {view.options_truncated&&<Notice>整合清單已截短，目前可選結果不完整。</Notice>}
            <Button type="button" disabled={locked||!fresh||!choice} onClick={selectEntry}>保存整合選擇</Button>
          </section>
          <details><summary>讀回資料與來源</summary>{view.devices.length>0&&<ul className="discovery-setup-devices">{view.devices.map(device=><li key={device.id}><strong>{device.name}</strong><span>{[device.manufacturer,device.model,device.disabled?'已停用':''].filter(Boolean).join(' · ')||'品牌與型號尚無資料'}</span></li>)}</ul>}{view.devices_truncated&&<p>設備清單已截短，僅顯示部分紀錄。</p>}<ul className="discovery-setup-sources">{view.sources.filter(source=>Object.hasOwn(sourceNames,source.id)).map(source=><li key={source.id}><span>{sourceNames[source.id]}<small>{source.observed_at||'尚無觀測時間'}</small></span><span>{sourceStates[source.state]}{source.truncated?' · 已截短':''}</span></li>)}</ul><p className="helper-text">保存至 {timestamp(view.setup.expires_at)}</p></details>
          <div className="discovery-setup-forget">{forget?<Confirm sentence="停止追蹤只刪除此進度；家庭整合與設備會保留。" confirmLabel="確認停止追蹤" onConfirm={forgetSetup} onCancel={()=>setForget(false)} busy={locked}/>:<Button secondary type="button" disabled={locked||!fresh} onClick={()=>setForget(true)}>停止追蹤</Button>}</div>
        </article>}
      </>}
      {onOpenChange&&<div className="discovery-setup-return"><Button secondary type="button" onClick={()=>toggle(false)}>返回分享</Button></div>}
    </div>}
  </details>;
}
