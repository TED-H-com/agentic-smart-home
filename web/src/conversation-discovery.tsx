import {Button,Notice,friendlySentence,timestamp} from './ui';
import {isDiscoveryCandidate,isDiscoveryDevice,isDiscoveryIntegration,isDiscoverySource,type DiscoverySnapshot} from './discovery';
import {TrackingPanel} from './discovery-setup';
import {DeviceSetupPanel,supportsChatSetup} from './device-setup';
import './conversation-discovery.css';

type Candidate=DiscoverySnapshot['candidates'][number]&{integration_name:string;next_step:string};
type Network={state:string;mode:'nat'|'wired'|'unknown';observed_at:string|null;reason_code:string|null;next_step:string};
type Observation={id:string;source_id:string;name:string|null;services:string[];observed_at:string;manufacturer:string|null;model:string|null};
type InventorySource={id:string;state:string;observed_at:string|null;item_count:number|null;truncated:boolean};
type Environment={devices:DiscoverySnapshot['devices'];integrations:DiscoverySnapshot['integrations'];inventory_observations:Observation[];inventory_sources:InventorySource[];device_count:number|null;integration_count:number|null;inventory_observation_count:number|null;inventory_revision:number|null;inventory_expires_at:string|null;truncated:boolean;network:Network|null};
type Discovery={contract_version:1;state:'consent_required'|'ready'|'unavailable';share_scope?:'ha_candidates'|'environment';environment?:Environment|null;observed_at:string|null;candidates:Candidate[];sources:DiscoverySnapshot['sources'];candidate_count:number|null;truncated:boolean;native_path:'/config/integrations';limitations:string[];content_digest:string|null};
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const text=(value:unknown):value is string=>typeof value==='string';
const date=(value:unknown)=>value===null||text(value)&&Number.isFinite(Date.parse(value));
const count=(value:unknown)=>value===null||typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const unique=(items:{id:string}[])=>new Set(items.map(item=>item.id)).size===items.length;
const nullableText=(value:unknown)=>value===null||text(value);
const inventorySourceIds=['lan_neighbors','lan_mdns','lan_ssdp','router_import'];
const networkStates=['unavailable','not_configured','needs_home_setup','waiting_for_address','adapter_selection_required','callback_address_required','ready'];
function isEnvironment(value:unknown):value is Environment{
  if(!record(value)||!Array.isArray(value.devices)||value.devices.length>16||!value.devices.every(isDiscoveryDevice)||!unique(value.devices)||!Array.isArray(value.integrations)||value.integrations.length>16||!value.integrations.every(isDiscoveryIntegration)||!unique(value.integrations))return false;
  if(!Array.isArray(value.inventory_observations)||value.inventory_observations.length>16||!value.inventory_observations.every(item=>record(item)&&text(item.id)&&inventorySourceIds.includes(String(item.source_id))&&nullableText(item.name)&&Array.isArray(item.services)&&item.services.length<=16&&item.services.every(text)&&text(item.observed_at)&&date(item.observed_at)&&nullableText(item.manufacturer)&&nullableText(item.model)))return false;
  if(!Array.isArray(value.inventory_sources)||value.inventory_sources.length>4||!value.inventory_sources.every(item=>record(item)&&inventorySourceIds.includes(String(item.id))&&['ok','partial','unavailable','not_configured','expired'].includes(String(item.state))&&date(item.observed_at)&&count(item.item_count)&&typeof item.truncated==='boolean')||!unique(value.inventory_sources as InventorySource[]))return false;
  if(!count(value.device_count)||!count(value.integration_count)||!count(value.inventory_observation_count)||!count(value.inventory_revision)||!date(value.inventory_expires_at)||typeof value.truncated!=='boolean')return false;
  if(value.network!==null&&(!record(value.network)||!networkStates.includes(String(value.network.state))||!['nat','wired','unknown'].includes(String(value.network.mode))||!date(value.network.observed_at)||!nullableText(value.network.reason_code)||!text(value.network.next_step)||value.network.next_step.length>160))return false;
  return true;
}
const sourceNames:Record<string,string>={ha_discovery:'家庭自動探索',ha_devices:'已連接設備',ha_entities:'設備功能',ha_states:'設備狀態',ha_integrations:'已設定服務',ha_config:'家庭連線',account_inventory:'品牌帳號設備'};
const sourceStates:Record<string,string>={ok:'已讀取',partial:'部分資料',unavailable:'暫時無法讀取',unsupported:'目前無法支援',not_implemented:'尚未接入'};
export const discoveryRequest='請找出家庭裡尚未接好的設備，列出找到的名稱，帶我完成下一步。';

/** Unknown or partial observations must never become a successful empty scan. */
export function parseConversationDiscovery(value:unknown):Discovery|null{
  if(!record(value)||value.contract_version!==1||!['consent_required','ready','unavailable'].includes(String(value.state))||value.native_path!=='/config/integrations'||!date(value.observed_at)||!count(value.candidate_count)||typeof value.truncated!=='boolean'||!(value.content_digest===null||text(value.content_digest)&&/^[a-f0-9]{64}$/.test(value.content_digest)))return null;
  if(!Array.isArray(value.candidates)||value.candidates.length>8||!value.candidates.every(item=>record(item)&&text(item.integration_name)&&item.integration_name.length>0&&item.integration_name.length<=160&&text(item.next_step)&&item.next_step.length>0&&item.next_step.length<=160&&isDiscoveryCandidate(item))||!unique(value.candidates))return null;
  if(!Array.isArray(value.sources)||value.sources.length>7||!value.sources.every(item=>isDiscoverySource(item)&&date(item.observed_at))||!unique(value.sources)||!Array.isArray(value.limitations)||value.limitations.length>16||!value.limitations.every(item=>text(item)&&item.length<=160))return null;
  const discovery=value as unknown as Discovery;
  if(discovery.share_scope!==undefined&&!['ha_candidates','environment'].includes(discovery.share_scope))return null;
  if(discovery.environment!=null&&(discovery.share_scope!=='environment'||!isEnvironment(discovery.environment)))return null;
  const source=discovery.sources.find(item=>item.id==='ha_discovery');
  if(discovery.state==='ready'&&(!discovery.observed_at||!source))return null;
  if(discovery.candidate_count!==null&&((discovery.share_scope!=='environment'&&(source?.state!=='ok'||source.truncated))||discovery.candidate_count<discovery.candidates.length))return null;
  if(discovery.state==='consent_required'&&(discovery.candidates.length||discovery.sources.length||discovery.candidate_count!==null||discovery.observed_at!==null||discovery.content_digest!==null||discovery.truncated||discovery.environment!=null))return null;
  return discovery;
}

export function DiscoverDevicesAction({disabled,onDiscover}:{disabled:boolean;onDiscover:()=>void}){
  return <div className="conversation-discover-action"><Button secondary type="button" disabled={disabled} onClick={onDiscover}>探索家庭設備</Button><p className="helper-text">分享設備名稱、已保存的網路盤點與家庭連線狀態，讓管家協助接入。</p></div>;
}

function EnvironmentContext({value}:{value:Environment}){
  const network=value.network;
  return <section className="conversation-environment" aria-label="家庭環境">
    {network&&network.state!=='ready'&&<Notice>{network.next_step}</Notice>}
    {network?.state==='ready'&&<p className="helper-text">{network.next_step}</p>}
    {value.devices.length>0&&<details><summary>已登錄設備（{value.device_count??`${value.devices.length}+`}）</summary><ul>{value.devices.map(device=><li key={device.id}><strong>{device.name}</strong>{device.disabled?' · 已停用':device.available_entity_count===null?' · 狀態尚未確認':` · ${device.available_entity_count} 個功能目前可讀取`}</li>)}</ul></details>}
    {value.inventory_observations.length>0&&<details><summary>網路探索紀錄（{value.inventory_observation_count??`${value.inventory_observations.length}+`}）</summary><p className="helper-text">保存的服務紀錄可能來自同一台設備；接入前會重新確認。</p><ul>{value.inventory_observations.map(item=><li key={`${item.source_id}:${item.id}`}><strong>{item.name||item.manufacturer||'待確認的網路服務'}</strong><span> · {timestamp(item.observed_at)}</span></li>)}</ul></details>}
    {value.truncated&&<p className="helper-text">這次只分享部分環境資訊。</p>}
  </section>;
}

export function ConversationDiscovery({value,disabled,visible=true,onDiscover,onSelect}:{value:unknown;disabled:boolean;visible?:boolean;onDiscover:()=>void;onSelect:(candidate:Candidate)=>void}){
  const data=parseConversationDiscovery(value);
  if(!data)return <section className="conversation-discovery" aria-label="設備探索結果"><Notice error>暫時無法讀取探索結果，請再試一次。</Notice><Button secondary type="button" disabled={disabled} onClick={onDiscover}>重試探索</Button></section>;
  if(data.state==='consent_required')return <section className="conversation-discovery" aria-label="設備探索授權"><DiscoverDevicesAction disabled={disabled} onDiscover={onDiscover}/></section>;
  const source=data.sources.find(item=>item.id==='ha_discovery');
  const incomplete=source?.state!=='ok'||source.truncated||data.candidate_count===null;
  return <section className="conversation-discovery" aria-label="設備探索結果">
    {data.environment&&<EnvironmentContext value={data.environment}/>}
    {data.state==='unavailable'?<><Notice>{friendlySentence(data.limitations[0],'家庭暫時沒有回應，尚無法確認有哪些設備。')}</Notice><Button secondary type="button" disabled={disabled} onClick={onDiscover}>重試探索</Button></>:<>
      <h3>設備接入線索</h3>
      {data.candidates.length?<ul className="conversation-discovery-candidates">{data.candidates.map(candidate=><li key={`${candidate.source_id}:${candidate.id}`}><div><strong>{candidate.title&&candidate.title!==candidate.integration_domain?candidate.title:candidate.integration_name}</strong>{candidate.title&&candidate.title!==candidate.integration_domain&&candidate.title!==candidate.integration_name&&<span>{candidate.integration_name}</span>}<span>{candidate.source_id==='network_inventory'?'保存的網路線索 · 尚未核對接入':'家庭發現 · 待確認接入'}</span><p>{candidate.next_step}</p></div>{supportsChatSetup(candidate)?<DeviceSetupPanel candidate={candidate} disabled={disabled} visible={visible}/>:<Button secondary type="button" disabled={disabled} onClick={()=>onSelect(candidate)}>{candidate.source_id==='network_inventory'?'請管家核對':'協助設定'}</Button>}</li>)}</ul>:<p>{incomplete?'探索資料尚未完整取得，目前無法確認待接入的設備。':'這次未找到待接入的設備；仍可能有需要登入或配對的設備。'}</p>}
      {data.candidates.length>0&&incomplete&&<p className="helper-text">目前只取得部分結果，尚未確認家中所有設備。</p>}
      {data.truncated&&<p className="helper-text">{data.candidate_count===null?'這次先列出部分結果。':`這次先列出 ${data.candidates.length} 項，共找到 ${data.candidate_count} 項待設定結果。`}</p>}
      {data.candidates.some(candidate=>candidate.source_id==='network_inventory')&&<p className="helper-text">不同來源可能來自同一設備，尚未合併身分。</p>}
      <p className="helper-text">接入與控制權限仍需你確認。</p>
      {data.candidates.some(candidate=>!supportsChatSetup(candidate))&&!disabled&&<TrackingPanel candidates={data.candidates.filter(candidate=>!supportsChatSetup(candidate))}/>}
    </>}
    {data.observed_at&&<p className="helper-text">探索於 <time dateTime={data.observed_at}>{timestamp(data.observed_at)}</time>，是這次對話的觀測結果。</p>}
    {data.sources.length>0&&<details><summary>探索範圍</summary><p>{data.share_scope==='environment'?'已讀取家庭設備及保存的網路探索結果；覆蓋範圍依各來源顯示。':'已讀取家庭提供的探索資料；尚未掃描整個家庭網路。'}</p><ul className="conversation-discovery-sources">{data.sources.map(source=><li key={source.id}><span>{sourceNames[source.id]||'其他來源'}</span><span>{sourceStates[source.state]}{source.truncated?' · 部分清單':''}</span></li>)}{data.environment?.inventory_sources.map(source=><li key={source.id}><span>{source.id==='router_import'?'路由器設備紀錄':'家庭網路探索紀錄'}</span><span>{({ok:'已讀取',partial:'部分資料',unavailable:'暫時無法讀取',not_configured:'尚未取得',expired:'紀錄已過期'} as Record<string,string>)[source.state]}{source.truncated?' · 部分清單':''}</span></li>)}</ul></details>}
  </section>;
}
