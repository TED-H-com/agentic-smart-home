// Fictional browser-memory inventory. No host, LAN, router or cloud requests occur.
import {sha256} from '@noble/hashes/sha2.js';
import type {DemoResponse} from './model';
type Row=Record<string,any>;
const scope={id:'demo-home-lan',label:'示範家庭網路',interface:'demo0',address:'192.168.50.10',network:'192.168.50.0/24'};
const hash=(value:unknown)=>Array.from(sha256(new TextEncoder().encode(JSON.stringify(value))),byte=>byte.toString(16).padStart(2,'0')).join('');
const source=(id:string,method:string):Row=>({id,adapter_version:1,state:'not_configured',method,target:'尚未選擇來源',observed_at:null,item_count:null,truncated:false,reason_code:null,content_digest:null,limitations:['fictional_browser_memory_only']});
export function inventoryRequests(data:Row,time:()=>string){
  const receipts=new Map<string,{body:string;response:DemoResponse}>();
  const initial=()=>({contract_version:1,revision:0,observed_at:time(),expires_at:null,
    sources:[source('lan_neighbors','neighbor_table'),source('lan_mdns','mdns'),source('lan_ssdp','ssdp'),source('router_import','json_import')],
    observations:[] as Row[],recommendations:[] as Row[],limitations:['fictional_browser_memory_only','not_physical_verification','multiple_sources_not_identity_proof'],content_digest:'0'.repeat(64)});
  data.networkInventory=initial();
  const failure=(status:number,code:string):DemoResponse=>({status,body:{detail:`inventory_${code}`}});
  function recommendations(rows:Row[]):Row[]{
    return rows.flatMap(item=>{
      if(item.source_updated_at&&Date.parse(item.source_updated_at)<Date.parse(time())-86400000)return [];
      const services=item.services.join(' ').toLowerCase(),name=`${item.name||''} ${Object.values(item.metadata).join(' ')}`.toLowerCase();
      let domain='',title='',reason='',confidence=item.source_id==='lan_mdns'?'service_match':'hint',next='';
      if(services.includes('_shelly._tcp')){domain='shelly';title='Shelly 接入建議';reason='服務公告含 Shelly 線索';next='在家庭新增 Shelly 整合，確認設備位址與實際型號。'}
      else if(services.includes('_googlecast._tcp')){domain='cast';title='Google Cast 接入建議';reason='服務公告含 Google Cast 線索';next='在家庭新增 Google Cast 整合，核對播放設備。'}
      else if(services.includes('hue')){domain='hue';title='Philips Hue 接入建議';reason='服務公告含 Hue 橋接器線索';next='在家庭新增 Philips Hue 整合，依指示按橋接器配對鍵。'}
      else if(/tuya|smart life/.test(name)){domain='tuya';title='Tuya 帳號接入建議';reason='匯入名稱含品牌提示；尚未確認設備身分';confidence='hint';next='在家庭新增 Tuya 整合，使用本人品牌帳號完成官方授權。'}
      else if(/shelly/.test(name)){domain='shelly';title='Shelly 接入建議';reason='匯入名稱含品牌提示；尚未確認設備身分';confidence='hint';next='先核對型號，再於家庭新增 Shelly 整合。'}
      if(!domain)return [];
      if(item.source_id==='router_import'&&services)reason='匯入清單的服務欄位，尚未驗證設備回應';
      return [{id:`demo-net-${hash([item.id,domain]).slice(0,24)}`,observation_id:item.id,integration_domain:domain,title,confidence,reason,next_step:next,documentation_url:`https://www.home-assistant.io/integrations/${domain}/`,native_path:'/config/integrations'}];
    });
  }
  function save(observations:Row[],sources:Row[]){
    const previous=data.networkInventory;
    const snapshot={...previous,revision:previous.revision+1,observed_at:time(),expires_at:new Date(Date.parse(time())+86400000).toISOString(),observations,recommendations:recommendations(observations),sources};
    snapshot.content_digest=hash(snapshot);data.networkInventory=snapshot;
    return {status:200,body:{contract_version:1,revision:snapshot.revision}};
  }
  function csv(content:string):Row[]{
    const table:string[][]=[],row:string[]=[];let field='',quoted=false;
    for(let index=0;index<=content.length;index++){
      const char=content[index];
      if(char==='"'){if(quoted&&content[index+1]==='"'){field+='"';index++}else quoted=!quoted}
      else if((char===','||char==='\n'||char===undefined)&&!quoted){row.push(field.replace(/\r$/,''));field='';if(char!==','){if(row.some(Boolean))table.push([...row]);row.length=0}}
      else if(char!==undefined)field+=char;
    }
    if(quoted||table.length<2)throw new Error('invalid');
    const columns=table.shift()!;
    return table.map(row=>Object.fromEntries(columns.map((key,index)=>[key.trim(),row[index]||''])));
  }
  return (path:string,method:string,body:Row,key?:string):DemoResponse|undefined=>{
    if(!path.startsWith('discovery/inventory'))return;
    if(method==='GET'&&path==='discovery/inventory')return {status:200,body:data.networkInventory};
    if(method==='GET'&&path==='discovery/inventory/scopes')return {status:200,body:{contract_version:1,scopes:[scope],limitations:['fictional_browser_memory_only','no_network_requests']}};
    if(method!=='POST'||!['scan','import','clear'].includes(path.split('/')[2]))return failure(404,'not_found');
    if(!key)return failure(400,'idempotency_required');
    const receiptKey=`${path}:${key}`,prior=receipts.get(receiptKey),encoded=JSON.stringify(body);
    if(prior)return prior.body===encoded?prior.response:failure(409,'request_conflict');
    const snapshot=data.networkInventory;
    let response:DemoResponse;
    if(path.endsWith('/scan')){
      if(body.scope_id!==scope.id)return failure(409,'scope_changed');
      const rows=[
        {id:'demo-net-neighbor',source_id:'lan_neighbors',name:'未識別的網路端點',services:[],address:'192.168.50.24',mac:'02:00:00:00:00:24',reachability:'unknown'},
        {id:'demo-net-shelly',source_id:'lan_mdns',name:'書房 Shelly 插座',services:['_shelly._tcp.local.'],address:'192.168.50.32',mac:null,reachability:'responded'},
        {id:'demo-net-cast',source_id:'lan_mdns',name:'客廳媒體播放器',services:['_googlecast._tcp.local.'],address:'192.168.50.40',mac:null,reachability:'responded'},
        {id:'demo-net-hue',source_id:'lan_ssdp',name:'走廊照明橋接器',services:['urn:schemas-upnp-org:device:basic:1','hue bridge'],address:'192.168.50.42',mac:null,reachability:'responded'},
      ].map(item=>({...item,scope_id:scope.id,observed_at:time(),source_updated_at:null,metadata:{}}));
      const sources=snapshot.sources.map((item:Row)=>item.id==='router_import'?item:{...item,state:'ok',target:`${scope.interface} ${scope.network}`,observed_at:time(),item_count:rows.filter(row=>row.source_id===item.id).length,content_digest:hash(rows.filter(row=>row.source_id===item.id))});
      response=save([...rows,...snapshot.observations.filter((item:Row)=>item.source_id==='router_import')],sources);
    }else if(path.endsWith('/import')){
      if(typeof body.content!=='string'||new TextEncoder().encode(body.content).length>65536)return failure(422,'import_too_large');
      if(typeof body.label!=='string'||!body.label.trim()||body.label.length>80)return failure(422,'import_invalid');
      let rows:Row[];
      try{
        if(body.format==='json'){const parsed=JSON.parse(body.content);rows=Array.isArray(parsed)?parsed:parsed.devices||parsed.clients||parsed.leases}
        else if(body.format==='csv')rows=csv(body.content);
        else if(body.format==='dnsmasq')rows=body.content.trim().split(/\r?\n/).map((line:string)=>{const parts=line.trim().split(/\s+/);if(parts.length!==5)throw new Error('invalid');return {ip:parts[2],mac:parts[1],hostname:parts[3]==='*'?null:parts[3]}});
        else throw new Error('format');
        if(!Array.isArray(rows)||!rows.length||rows.length>256)throw new Error('rows');
        rows=rows.map((item,index)=>{
          if(!item||typeof item!=='object')throw new Error('row');
          const address=item.ip||item.ip_address||item.address||null,mac=(item.mac||item.mac_address||'').toLowerCase()||null,name=item.hostname||item.name||null;
          if(!address&&!mac&&!name||address&&(typeof address!=='string'||address.length>45)||mac&&!/^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/.test(mac)||name&&(typeof name!=='string'||name.length>160))throw new Error('row');
          const services=Array.isArray(item.services)?item.services:typeof item.services==='string'?item.services.split(';').filter(Boolean):[];
          if(services.length>16||services.some((value:unknown)=>typeof value!=='string'||value.length>160))throw new Error('services');
          let sourceUpdated:string|null=null;if(item.last_seen!==undefined&&item.last_seen!==null&&item.last_seen!==''){const parsed=typeof item.last_seen==='number'?item.last_seen*1000:Date.parse(item.last_seen);if(!Number.isFinite(parsed))throw new Error('last_seen');sourceUpdated=new Date(parsed).toISOString()}
          const metadata:Row={};for(const key of ['manufacturer','model'])if(typeof item[key]==='string')metadata[key]=item[key].slice(0,160);
          return {id:`demo-import-${hash([index,address,mac,name]).slice(0,24)}`,source_id:'router_import',scope_id:'demo-import',address,mac,name,services,observed_at:time(),source_updated_at:sourceUpdated,reachability:'unknown',metadata};
        });
      }catch{return failure(422,'import_invalid')}
      response=save([...snapshot.observations.filter((item:Row)=>item.source_id!=='router_import'),...rows],snapshot.sources.map((item:Row)=>item.id!=='router_import'?item:{...item,state:'ok',method:`${body.format}_import`,target:body.label,observed_at:time(),item_count:rows.length,content_digest:hash(rows)}));
    }else{
      if(body.expected_revision!==snapshot.revision)return failure(409,'revision_changed');
      const empty=initial();response=save([],empty.sources);
    }
    receipts.set(receiptKey,{body:encoded,response});return response;
  };
}
