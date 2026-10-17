// Browser-memory lifecycle only: no host, Supervisor, Core or household requests.
import type {DemoResponse} from './model';

type Row=Record<string,any>;
export function haLanRequests(data:Row,time:()=>string){
  const lan=data['network/ha-lan'],ready=structuredClone(lan.home),guestMac=lan.guest_mac;
  const receipts=new Map<string,{signature:string;job:Row}>();
  let pending:{job:Row;started:number}|null=null;
  const failure=(status:number,detail:string):DemoResponse=>({status,body:{detail}});
  function observe(){
    const running=data['host/status'].vm==='running';
    lan.vm_state=data['host/status'].vm;lan.can_configure=!running;
    data.status.ha.status=running?'connected':'unavailable';
    data['host/diagnosis'].findings=running?[]:['示範家庭系統已關機。'];
    const empty={...ready,interface:null,ipv4:[],ipv6:[],connected:null,discovery_enabled:null,default_adapter:null,internal_url:null};
    lan.home=lan.mode==='nat'?{...empty,state:'not_configured',reason:'wired_lan_not_configured'}:
      running?structuredClone(ready):{...empty,state:'unavailable',reason:'home_network_observation_unavailable'};
    lan.home.observed_at=time();
  }
  function progress(){
    if(!pending||performance.now()-pending.started<600)return false;
    const job=pending.job,request=job.request;
    if(request.action==='configure_lan'){
      const selected=lan.interfaces.find((item:Row)=>item.id===request.interface_id);
      Object.assign(lan,{mode:selected?'wired':'nat',state:selected?'configured':'disabled',
        selected_interface_id:selected?.id||null,interface:selected?.interface||null,guest_mac:selected?guestMac:null});
    }else data['host/status'].vm=request.action==='start'?'running':'shut off';
    observe();
    Object.assign(job,{state:'succeeded',finished_at:Date.now()/1000,result:request.action==='configure_lan'?{mode:lan.mode,state:lan.state}:{vm:lan.vm_state}});
    pending=null;return true;
  }
  function request(path:string,method:string,body:Row,key?:string):DemoResponse|undefined{
    if(method==='GET'&&path==='network/ha-lan')return {status:200,body:lan};
    if(method!=='POST'||path!=='host/actions')return;
    if(!key||!/^[-_A-Za-z0-9]{8,100}$/.test(key))return failure(400,'host_action_idempotency_required');
    if(!['configure_lan','shutdown','start'].includes(body.action))return failure(409,'示範中無法進行這個操作。');
    const configure=body.action==='configure_lan',allowed=configure?['action','interface_id']:['action','snapshot'];
    if(Object.keys(body).some(field=>!allowed.includes(field))||(!configure&&body.snapshot!=null))return failure(422,'host_action_invalid');
    if(configure&&(!Object.hasOwn(body,'interface_id')||body.interface_id!==null&&(typeof body.interface_id!=='string'||! /^[a-f0-9]{64}$/.test(body.interface_id))))return failure(422,'ha_lan_interface_id_invalid');
    const normalized=configure?{action:body.action,interface_id:body.interface_id}:{action:body.action,snapshot:null};
    const signature=JSON.stringify(normalized),previous=receipts.get(key);
    if(previous)return previous.signature===signature?{status:202,body:previous.job}:failure(409,'host_action_request_conflict');
    if(pending)return failure(409,'host_action_in_progress');
    if(configure&&!lan.can_configure)return failure(409,'ha_lan_shutdown_required');
    if(configure&&body.interface_id!==null&&!lan.interfaces.some((item:Row)=>item.id===body.interface_id))return failure(409,'ha_lan_selected_interface_unavailable');
    const job={id:key,state:'running',request:{id:key,...normalized},at:Date.now()/1000};
    data['host/status'].jobs.push(job);receipts.set(key,{signature,job});pending={job,started:performance.now()};
    return {status:202,body:job};
  }
  return {request,progress};
}
