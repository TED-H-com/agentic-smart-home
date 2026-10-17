// Fictional, browser-memory counterpart of the Controller's persistent tracking.
// Resetting/reloading the demo resets these records with the rest of its household.
import type {DemoResponse} from './model';

type Row=Record<string,any>;
export function setupRequests(data:Row,nextId:(length:number)=>string,time:()=>string){
  const records=new Map<string,Row>(),receipts=new Map<string,{digest:string;response:DemoResponse}>();
  const days=7,limit=20;
  const failure=(status:number,code:string):DemoResponse=>({status,body:{detail:`discovery_setup_${code}`}});
  const publicRecord=(record:Row)=>{
    const {baseline:_,baseline_complete:__,...setup}=record;
    return setup;
  };
  return (path:string,method:string,body:Row,key?:string):DemoResponse|undefined=>{
    if(path!=='discovery/setups'&&!path.startsWith('discovery/setups/'))return;
    const parts=path.split('/'),id=parts[2],action=parts[3],record=records.get(id);
    const observed=data.discovery,sources=new Map<string,Row>(observed.sources.map((item:Row)=>[item.id,item]));
    const network=data.networkInventory;
    const networkCandidates=network.recommendations.map((item:Row)=>({id:item.id,title:item.title,integration_domain:item.integration_domain,source_id:'network_inventory',discovery_method:'network_inventory',native_path:'/config/integrations'}));
    const networkSource={id:'network_inventory',state:'ok',truncated:false,item_count:network.observations.length,observed_at:network.observed_at,reason_code:null,location:'network_inventory',method:'local_inventory',target:'示範網路盤點',adapter_version:1};
    const complete=(source:string)=>source==='network_inventory'||sources.get(source)?.state==='ok';
    if(method==='GET'){
      if(parts.length===2)return {status:200,body:{contract_version:1,setups:[...records.values()].map(publicRecord),limit,retention_days:days}};
      if(parts.length!==3||!record)return failure(404,'not_found');
      const allOptions=observed.integrations.filter((item:Row)=>item.domain===record.candidate.integration_domain);
      const selected=allOptions.find((item:Row)=>item.id===record.selected_entry_id)||null;
      const devices=selected?observed.devices.filter((item:Row)=>item.integration_ids.includes(selected.id)):[];
      const devicesComplete=!!selected&&complete('ha_devices');
      return {status:200,body:{contract_version:1,setup:publicRecord(record),observed_at:observed.observed_at,
        candidate_state:(record.candidate.source_id==='network_inventory'?networkCandidates:observed.candidates).some((item:Row)=>item.id===record.candidate.id&&item.integration_domain===record.candidate.integration_domain)?'present':complete(record.candidate.source_id)?'not_seen':'unknown',
        integration_state:!record.selected_entry_id?'unselected':selected?'present':complete('ha_integrations')?'not_seen':'unknown',
        integration:selected,options:allOptions.slice(0,100).map((integration:Row)=>({integration,known_before:record.baseline.includes(integration.id)?true:record.baseline_complete?false:null})),
        options_truncated:allOptions.length>100,devices:devices.slice(0,100).map((item:Row)=>({...item,integration_ids:[selected.id]})),devices_truncated:devices.length>100,
        device_count:devicesComplete?devices.length:null,
        entity_count:devicesComplete&&complete('ha_entities')?devices.reduce((n:number,item:Row)=>n+item.entity_count,0):null,
        available_entity_count:devicesComplete&&complete('ha_entities')&&complete('ha_states')?devices.reduce((n:number,item:Row)=>n+item.available_entity_count,0):null,
        sources:record.candidate.source_id==='network_inventory'?[...observed.sources,networkSource]:observed.sources,physical_verification:'not_performed',native_path:'/config/integrations'}};
    }
    if(method!=='POST'||!(parts.length===2||parts.length===4&&['select','forget'].includes(action)))return failure(404,'not_found');
    if(!key)return failure(400,'idempotency_required');
    const receiptKey=`${path}:${key}`,digest=JSON.stringify(body),previous=receipts.get(receiptKey);
    if(previous){
      if(previous.digest!==digest)return failure(409,'request_conflict');
      const reference=previous.response.body as Row;
      return !reference.forgotten&&!records.has(reference.id)?failure(410,'expired'):previous.response;
    }
    let changed:Row,forgotten=false;
    if(parts.length===2){
      const candidate=(body.source_id==='network_inventory'?networkCandidates:observed.candidates).find((item:Row)=>item.id===body.candidate_id&&item.integration_domain===body.integration_domain);
      if(!candidate)return failure(complete('ha_discovery')?409:503,complete('ha_discovery')?'candidate_changed':'source_unavailable');
      const existing=[...records.values()].find(item=>item.candidate.id===candidate.id&&item.candidate.integration_domain===candidate.integration_domain);
      if(!existing&&records.size>=limit)return failure(409,'limit_reached');
      changed=existing||{contract_version:1,id:nextId(32),revision:1,candidate:structuredClone(candidate),candidate_observed_at:candidate.source_id==='network_inventory'?network.observed_at:observed.observed_at,
        created_at:time(),updated_at:time(),expires_at:new Date(Date.parse(time())+days*86400000).toISOString(),selected_entry_id:null,selected_at:null,
        baseline:observed.integrations.filter((item:Row)=>item.domain===candidate.integration_domain).map((item:Row)=>item.id),baseline_complete:complete('ha_integrations')};
      records.set(changed.id,changed);
    }else{
      if(!record)return failure(404,'not_found');
      if(record.revision!==body.expected_revision)return failure(409,'revision_changed');
      changed={...record,revision:record.revision+1,updated_at:time()};
      if(action==='select'){
        if(!observed.integrations.some((item:Row)=>item.id===body.entry_id&&item.domain===record.candidate.integration_domain))return failure(complete('ha_integrations')?409:503,complete('ha_integrations')?'entry_changed':'source_unavailable');
        changed.selected_entry_id=body.entry_id;changed.selected_at=time();records.set(id,changed);
      }else{forgotten=true;records.delete(id)}
    }
    const response={status:200,body:{contract_version:1,id:changed.id,revision:changed.revision,forgotten}};
    receipts.set(receiptKey,{digest,response});return response;
  };
}
