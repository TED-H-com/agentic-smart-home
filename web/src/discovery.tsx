import {useEffect,useRef,useState} from 'react';
import {api,haPath,haTarget} from './api';
import {Button,Notice,timestamp} from './ui';
import {TrackingPanel} from './discovery-setup';
import {NetworkInventory} from './network-inventory';
import './discovery.css';

type Source = {id:string;state:'ok'|'partial'|'unavailable'|'unsupported'|'not_implemented';truncated:boolean;item_count:number|null;observed_at:string|null;reason_code:string|null};
type Candidate = {id:string;integration_domain:string;title:string;discovery_method:string;source_id:'ha_discovery'|'network_inventory';native_path:'/config/integrations'};
type Device = {id:string;name:string;manufacturer:string|null;model:string|null;via_device_id:string|null;parent_device_id:string|null;integration_ids:string[];disabled:boolean;entity_count:number|null;available_entity_count:number|null};
type Integration = {id:string;domain:string;title:string;state:'loaded'|'not_loaded'|'retry'|'error'|'disabled'|'ignored'|'unknown'};
export type DiscoverySnapshot = {contract_version:1;observed_at:string;content_digest:string;native_path:'/config/integrations';sources:Source[];candidates:Candidate[];devices:Device[];integrations:Integration[];limitations:string[]};
type Snapshot = DiscoverySnapshot;
const sourceNames:Record<string,string>={ha_discovery:'家庭自動探索',ha_devices:'已登錄設備',ha_entities:'設備功能清單',ha_states:'功能目前狀態',ha_integrations:'已設定整合',ha_config:'家庭連線資料',router_inventory:'路由器設備清單',lan_probe:'內網主動探測',account_inventory:'獨立品牌帳號盤點（另需整合）'};
const sourceStates:Record<Source['state'],string>={ok:'已讀取',partial:'部分資料',unavailable:'暫時無法讀取',unsupported:'目前無法支援',not_implemented:'尚未接入'};
const integrationStates:Record<Integration['state'],string>={loaded:'已載入',not_loaded:'尚未載入',retry:'等待重試',error:'設定異常',disabled:'已停用',ignored:'已忽略',unknown:'狀態未知'};
const discoveryNames:Record<string,string>={zeroconf:'區域網路服務',ssdp:'區域網路服務',dhcp:'網路位址資訊',homekit:'HomeKit',bluetooth:'藍牙',usb:'USB',mqtt:'MQTT',integration_discovery:'家庭整合'};
const isRecord=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const isText=(value:unknown):value is string=>typeof value==='string';
const nullableText=(value:unknown)=>value===null||isText(value);
const count=(value:unknown)=>value===null||typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const unique=(items:Record<string,unknown>[])=>items.every(item=>isText(item.id)&&item.id.length>0)&&new Set(items.map(item=>item.id)).size===items.length;

// Shared field guards keep setup readback and the existing snapshot on the same semantics.
export const isDiscoverySource=(item:unknown):item is Source=>isRecord(item)&&isText(item.id)&&item.id.length>0&&isText(item.state)&&Object.hasOwn(sourceStates,item.state)&&typeof item.truncated==='boolean'&&count(item.item_count)&&nullableText(item.observed_at)&&nullableText(item.reason_code);
export const isDiscoveryCandidate=(item:unknown):item is Candidate=>isRecord(item)&&isText(item.id)&&item.id.length>0&&isText(item.title)&&isText(item.integration_domain)&&isText(item.discovery_method)&&['ha_discovery','network_inventory'].includes(String(item.source_id))&&item.native_path==='/config/integrations';
export const isDiscoveryDevice=(item:unknown):item is Device=>isRecord(item)&&isText(item.id)&&item.id.length>0&&isText(item.name)&&nullableText(item.manufacturer)&&nullableText(item.model)&&nullableText(item.via_device_id)&&nullableText(item.parent_device_id)&&typeof item.disabled==='boolean'&&Array.isArray(item.integration_ids)&&item.integration_ids.every(isText)&&count(item.entity_count)&&count(item.available_entity_count)&&(item.entity_count===null||item.available_entity_count===null||(item.available_entity_count as number)<=(item.entity_count as number));
export const isDiscoveryIntegration=(item:unknown):item is Integration=>isRecord(item)&&isText(item.id)&&item.id.length>0&&isText(item.title)&&isText(item.domain)&&isText(item.state)&&Object.hasOwn(integrationStates,item.state);

// Additive fields are accepted, but missing or unknown semantics never become an empty scan.
export function parseDiscoverySnapshot(value:unknown):Snapshot{
  const invalid=()=>{throw new Error('探索資料格式已變更，請開啟家庭設定查看。')};
  if(!isRecord(value)||value.contract_version!==1||value.native_path!=='/config/integrations'||!isText(value.observed_at)||!Number.isFinite(Date.parse(value.observed_at))||!isText(value.content_digest)||!(/^[a-f0-9]{64}$/).test(value.content_digest))return invalid();
  for(const key of ['sources','candidates','devices','integrations']){
    const items=value[key];
    if(!Array.isArray(items)||!items.every(isRecord)||!unique(items))return invalid();
  }
  const sources=value.sources as Record<string,unknown>[];
  if(!['ha_discovery','ha_devices','ha_integrations'].every(id=>sources.some(item=>item.id===id))||!sources.every(isDiscoverySource))return invalid();
  if(!(value.candidates as Record<string,unknown>[]).every(isDiscoveryCandidate))return invalid();
  if(!(value.devices as Record<string,unknown>[]).every(isDiscoveryDevice))return invalid();
  if(!(value.integrations as Record<string,unknown>[]).every(isDiscoveryIntegration))return invalid();
  if(!Array.isArray(value.limitations)||!value.limitations.every(isText))return invalid();
  return value as unknown as Snapshot;
}

export function Discovery(){
  const [data,setData]=useState<Snapshot|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(true),[attempt,setAttempt]=useState(0),[limit,setLimit]=useState(20),[networkOpen,setNetworkOpen]=useState(false);
  const initial=useRef<Promise<Snapshot>|null>(null);
  useEffect(()=>{
    let alive=true,inFlight=false;
    const read=async(first=false)=>{
      if(inFlight)return;
      inFlight=true;setBusy(true);
      try{
        const value=await (first?(initial.current ||= api('discovery').then(parseDiscoverySnapshot)):api('discovery').then(parseDiscoverySnapshot));
        if(alive){setData(value);setError('')}
      }catch(e){if(alive){setData(null);setError(e instanceof Error?e.message:'探索暫時無法讀取，請稍後重試。')}}
      finally{inFlight=false;if(alive)setBusy(false)}
    };
    read(attempt===0);
    const focus=()=>{if(document.visibilityState==='visible')read()};
    window.addEventListener('focus',focus);
    return()=>{alive=false;window.removeEventListener('focus',focus)};
  },[attempt]);
  const discovered=data?.sources.find(source=>source.id==='ha_discovery');
  const registered=data?.sources.find(source=>source.id==='ha_devices');
  const partial=data?.sources.some(source=>['partial','unavailable','unsupported'].includes(source.state));
  const devices=new Map(data?.devices.map(device=>[device.id,device]));
  const integrations=new Map(data?.integrations.map(integration=>[integration.id,integration]));
  return <section className="discovery" aria-labelledby="discovery-title" aria-busy={busy}>
    <div className="discovery-heading"><div><h2 id="discovery-title">探索設備</h2><p>接續家庭找到的設備，完成連接設定。</p></div><a className="button secondary" href={haPath('/config/integrations')} target={haTarget()} rel="noreferrer">開啟家庭設定 ↗</a></div>
    {busy&&!data&&!error&&<p className="discovery-loading" role="status">正在讀取家庭探索結果…</p>}
    {error&&<Notice error><p>{error}</p><Button secondary disabled={busy} onClick={()=>setAttempt(value=>value+1)}>{busy?'讀取中…':'重試探索'}</Button></Notice>}
    {data&&<>
      {partial&&<p className="discovery-warning" role="status">部分來源尚未完整讀取，請查看盤點範圍。</p>}
      {data.candidates.length>0?<div className="discovery-candidates"><h3>待設定的探索結果</h3><ul>{data.candidates.slice(0,3).map(candidate=><li key={candidate.id}><strong>{candidate.title||candidate.integration_domain}</strong><span>{discoveryNames[candidate.discovery_method]||'家庭自動探索'} · 待設定</span></li>)}</ul>{data.candidates.length>3&&<p className="helper-text">另有 {data.candidates.length-3} 項，請開啟家庭設定查看。</p>}</div>:<p className="discovery-empty">{discovered?.state==='ok'&&!discovered.truncated?'目前沒有待設定的探索結果；可到家庭設定新增整合。':'探索清單尚未完整取得，請開啟家庭設定確認。'}</p>}
      <div className="discovery-details">
        <details><summary>已登錄設備{registered?.state==='ok'&&!registered.truncated?` · ${data.devices.length}`:' · 部分或未知'}</summary><p className="helper-text">登錄與狀態資訊不代表已通過實際控制測試。</p>
          {data.devices.length===0?<p>{registered?.state==='ok'&&!registered.truncated?'家庭尚未登錄設備。':'設備清單暫時無法完整取得。'}</p>:<><ul className="discovery-devices">{data.devices.slice(0,limit).map(device=>{
            const parent=devices.get(device.via_device_id||device.parent_device_id||'');
            return <li key={device.id}><strong>{device.name}</strong><p>{[device.manufacturer,device.model].filter(Boolean).join(' · ')||'品牌與型號尚無資料'}{device.disabled?' · 已停用':''}</p>{(device.via_device_id||device.parent_device_id)&&<p>經由 {parent?.name||'其他設備'} 連接</p>}<p>{device.entity_count===null?'功能數量尚無資料':`${device.entity_count} 個功能`}{device.available_entity_count!==null?` · ${device.available_entity_count} 個顯示可用狀態`:' · 可用狀態尚未取得'}</p>{device.integration_ids.length>0&&<p>整合：{device.integration_ids.map(id=>{const integration=integrations.get(id);return integration?`${integration.title||integration.domain}（${integrationStates[integration.state]}）`:'尚無資料'}).join('、')}</p>}</li>;
          })}</ul>{data.devices.length>limit&&<Button secondary onClick={()=>setLimit(value=>value+20)}>再顯示 20 台</Button>}</>}
        </details>
        <details><summary>盤點範圍與來源</summary><p>目前依據家庭提供的資料；探索範圍取決於網路與已設定整合。</p><ul className="discovery-sources">{data.sources.filter(source=>!['router_inventory','lan_probe'].includes(source.id)).map(source=><li key={source.id}><span>{sourceNames[source.id]||'其他來源'}</span><span>{sourceStates[source.state]}{source.truncated?' · 清單已截短':''}</span></li>)}</ul><p className="helper-text">更新於 {timestamp(data.observed_at)}；返回此頁時自動讀取。</p><details><summary>技術資料</summary><p>觀測識別：<code>{data.content_digest}</code></p>{data.limitations.length>0&&<ul>{data.limitations.map((reason,index)=><li key={index}><code>{reason}</code></li>)}</ul>}{data.sources.filter(source=>source.reason_code).map(source=><p key={source.id}>{sourceNames[source.id]||source.id}：<code>{source.reason_code}</code></p>)}</details></details>
      </div>
    </>}
    <NetworkInventory onOpenChange={setNetworkOpen}/>
    {!networkOpen&&<TrackingPanel candidates={data?.candidates||[]}/>}
  </section>;
}
