import {Button,Notice,friendlySentence,timestamp} from './ui';
import {isDiscoveryCandidate,isDiscoverySource,type DiscoverySnapshot} from './discovery';
import {TrackingPanel} from './discovery-setup';
import './conversation-discovery.css';

type Candidate=DiscoverySnapshot['candidates'][number]&{integration_name:string;next_step:string};
type Discovery={contract_version:1;state:'consent_required'|'ready'|'unavailable';observed_at:string|null;candidates:Candidate[];sources:DiscoverySnapshot['sources'];candidate_count:number|null;truncated:boolean;native_path:'/config/integrations';limitations:string[];content_digest:string|null};
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const text=(value:unknown):value is string=>typeof value==='string';
const date=(value:unknown)=>value===null||text(value)&&Number.isFinite(Date.parse(value));
const count=(value:unknown)=>value===null||typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const unique=(items:{id:string}[])=>new Set(items.map(item=>item.id)).size===items.length;
const sourceNames:Record<string,string>={ha_discovery:'家庭自動探索',ha_devices:'已連接設備',ha_entities:'設備功能',ha_states:'設備狀態',ha_integrations:'已設定服務',ha_config:'家庭連線',account_inventory:'品牌帳號設備'};
const sourceStates:Record<string,string>={ok:'已讀取',partial:'部分資料',unavailable:'暫時無法讀取',unsupported:'目前無法支援',not_implemented:'尚未接入'};
export const discoveryRequest='請找出家庭裡尚未接好的設備，列出找到的名稱，帶我完成下一步。';

/** Unknown or partial observations must never become a successful empty scan. */
export function parseConversationDiscovery(value:unknown):Discovery|null{
  if(!record(value)||value.contract_version!==1||!['consent_required','ready','unavailable'].includes(String(value.state))||value.native_path!=='/config/integrations'||!date(value.observed_at)||!count(value.candidate_count)||typeof value.truncated!=='boolean'||!(value.content_digest===null||text(value.content_digest)&&/^[a-f0-9]{64}$/.test(value.content_digest)))return null;
  if(!Array.isArray(value.candidates)||value.candidates.length>8||!value.candidates.every(item=>record(item)&&text(item.integration_name)&&item.integration_name.length>0&&item.integration_name.length<=160&&text(item.next_step)&&item.next_step.length>0&&item.next_step.length<=160&&isDiscoveryCandidate(item))||!unique(value.candidates))return null;
  if(!Array.isArray(value.sources)||value.sources.length>7||!value.sources.every(item=>isDiscoverySource(item)&&date(item.observed_at))||!unique(value.sources)||!Array.isArray(value.limitations)||value.limitations.length>16||!value.limitations.every(item=>text(item)&&item.length<=160))return null;
  const discovery=value as unknown as Discovery;
  const source=discovery.sources.find(item=>item.id==='ha_discovery');
  if(discovery.state==='ready'&&(!discovery.observed_at||!source))return null;
  if(discovery.candidate_count!==null&&(source?.state!=='ok'||source.truncated||discovery.candidate_count<discovery.candidates.length))return null;
  if(discovery.state==='consent_required'&&(discovery.candidates.length||discovery.sources.length||discovery.candidate_count!==null||discovery.observed_at!==null||discovery.content_digest!==null||discovery.truncated))return null;
  return discovery;
}

export function DiscoverDevicesAction({disabled,onDiscover}:{disabled:boolean;onDiscover:()=>void}){
  return <div className="conversation-discover-action"><Button secondary type="button" disabled={disabled} onClick={onDiscover}>探索家庭設備</Button><p className="helper-text">將找到的設備名稱與接入提示交給管家協助。</p></div>;
}

export function ConversationDiscovery({value,disabled,onDiscover,onSelect}:{value:unknown;disabled:boolean;onDiscover:()=>void;onSelect:(candidate:Candidate)=>void}){
  const data=parseConversationDiscovery(value);
  if(!data)return <section className="conversation-discovery" aria-label="設備探索結果"><Notice error>暫時無法讀取探索結果，請再試一次。</Notice><Button secondary type="button" disabled={disabled} onClick={onDiscover}>重試探索</Button></section>;
  if(data.state==='consent_required')return <section className="conversation-discovery" aria-label="設備探索授權"><DiscoverDevicesAction disabled={disabled} onDiscover={onDiscover}/></section>;
  const source=data.sources.find(item=>item.id==='ha_discovery');
  const incomplete=source?.state!=='ok'||source.truncated||data.candidate_count===null;
  return <section className="conversation-discovery" aria-label="設備探索結果">
    {data.state==='unavailable'?<><Notice>{friendlySentence(data.limitations[0],'家庭暫時沒有回應，尚無法確認有哪些設備。')}</Notice><Button secondary type="button" disabled={disabled} onClick={onDiscover}>重試探索</Button></>:<>
      <h3>待接入的設備</h3>
      {data.candidates.length?<ul className="conversation-discovery-candidates">{data.candidates.map(candidate=><li key={candidate.id}><div><strong>{candidate.title&&candidate.title!==candidate.integration_domain?candidate.title:candidate.integration_name}</strong>{candidate.title&&candidate.title!==candidate.integration_domain&&candidate.title!==candidate.integration_name&&<span>{candidate.integration_name}</span>}<p>{candidate.next_step}</p></div><Button secondary type="button" disabled={disabled} onClick={()=>onSelect(candidate)}>協助設定</Button></li>)}</ul>:<p>{incomplete?'探索資料尚未完整取得，目前無法確認待接入的設備。':'這次未找到待接入的設備；仍可能有需要登入或配對的設備。'}</p>}
      {data.candidates.length>0&&incomplete&&<p className="helper-text">目前只取得部分結果，尚未確認家中所有設備。</p>}
      {data.truncated&&<p className="helper-text">{data.candidate_count===null?'這次先列出部分結果。':`這次先列出 ${data.candidates.length} 項，共找到 ${data.candidate_count} 項待設定結果。`}</p>}
      <p className="helper-text">找到設備不代表已接入；配對和控制權限仍需你確認。</p>
      {data.candidates.length>0&&<TrackingPanel candidates={data.candidates}/>}
    </>}
    {data.observed_at&&<p className="helper-text">探索於 <time dateTime={data.observed_at}>{timestamp(data.observed_at)}</time>，是這次對話的觀測結果。</p>}
    {data.sources.length>0&&<details><summary>探索範圍</summary><p>已讀取家庭提供的探索資料；尚未掃描整個家庭網路。</p><ul className="conversation-discovery-sources">{data.sources.map(source=><li key={source.id}><span>{sourceNames[source.id]||'其他來源'}</span><span>{sourceStates[source.state]}{source.truncated?' · 部分清單':''}</span></li>)}</ul></details>}
  </section>;
}
