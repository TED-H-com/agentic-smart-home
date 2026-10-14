import fixtures from '../preview/fixtures.json';
import {sha256} from '@noble/hashes/sha2.js';

export const AGENTIC_DEMO_BUILD='fictional-browser-only-v1';
export const fixtureClock='2026-10-13T09:00:00+08:00';
type Row=Record<string,any>;
declare global {interface Window {__agenticDemo:{unhandled:string[];marker:string}}}
const clone=<T>(value:T):T=>structuredClone(value);
const hash=(bytes:Uint8Array)=>Array.from(sha256(bytes),v=>v.toString(16).padStart(2,'0')).join('');
const patch='diff --git a/docs/demo-light.md b/docs/demo-light.md\n--- a/docs/demo-light.md\n+++ b/docs/demo-light.md\n@@ -1 +1,2 @@\n-感應器開燈。\n+玄關感應器偵測到動作時，打開客廳燈。\n+這份說明使用虛構家庭，不會部署。\n';

export function installDemo(){
  const screenshot=new URLSearchParams(location.search).get('screenshot')==='1';
  const bridge=new URLSearchParams(location.search).get('surface')==='bridge';
  const clock=screenshot?Date.parse(fixtureClock):Date.now();
  if(screenshot)Date.now=()=>clock;
  const time=(offset=0)=>new Date(clock+offset).toISOString();
  const data:Row=clone(fixtures);
  // Preview fixtures are the base; timestamps and identifiers here are exclusively fictional.
  function redate(value:any):void{if(!value||typeof value!=='object')return;for(const key of Object.keys(value)){if(typeof value[key]==='string'&&/^2026-10-13T/.test(value[key]))value[key]=time();else redate(value[key])}}
  redate(data);
  data.session.principal='demo-owner';data.session.csrf='demo-csrf';
  data.session.resume.message_id='3'.repeat(64);data.session.resume.thread_id='4'.repeat(64);data.session.resume.case_id='5'.repeat(32);
  const initialMessage=data['gateway/messages'].messages[0];Object.assign(initialMessage,{id:'3'.repeat(64),thread_id:'4'.repeat(64),case_id:'5'.repeat(32)});
  data['hermes/cases/'+'5'.repeat(32)]=data['hermes/cases/cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd'];
  data.inventory.entities[0].area='客廳';
  data.inventory.policy.entities=data.inventory.entities.map((e:Row)=>({entity_id:e.entity_id,read:true,write:e.domain==='light'?'lighting':'none',purpose:e.domain==='light'?'客廳一般照明':''}));
  data.status.policy_entities=3;data.status.policy_readable=3;
  data.services.services=[];
  const intent=clone(data['tasks/demo-proposal'].proposal.intent);
  intent.actions=[{entity_id:'light.demo_living_room',action:'turn_on'}];
  intent.time_window={after:'18:00:00',before:'23:00:00'};
  data['tasks/demo-proposal'].proposal.intent=clone(intent);
  data.automations.automations[0].intent=clone(intent);
  const compile=(value:Row,id:string):Row=>({id,alias:value.title,description:'Managed by Agentic Home · compiler 1',mode:'single',triggers:[{platform:'state',entity_id:value.trigger.entity_id,from:value.trigger.from_state,to:value.trigger.to_state}],conditions:value.time_window?[{condition:'time',after:value.time_window.after,before:value.time_window.before}]:[],actions:value.actions.map((a:Row)=>({action:`${a.entity_id.split('.')[0]}.${a.action}`,target:{entity_id:a.entity_id}}))});
  data['tasks/demo-proposal'].proposal.final=compile(intent,'agentic_demo_evening');
  data['tasks/demo-proposal'].proposal.expires=Date.now()/1000+3600;
  data.tasks.tasks=[data['tasks/demo-proposal']];
  data['host/diagnosis']={findings:[],synthetic_data:true};
  data['host/authorization']={state:'connected'};
  data['host/status'].snapshots=[{id:'6'.repeat(64),created_at:time(-86400000)}];
  data['host/status'].jobs=[{id:'demo-snapshot',state:'succeeded',request:{action:'snapshot'},at:(clock-86400000)/1000,message:'示範快照已建立。'}];
  data.updates={trusted_publisher_configured:true,records:[]};
  if(bridge){data.deployment={mode:'bridge',authenticated:true,bridge_state:'ready'};data.session.mode='bridge';data.session.storage_scope='7'.repeat(64)}
  const bytes=new TextEncoder().encode(patch),digest=hash(bytes);
  const artifact={id:'8'.repeat(32),name:'household-description.patch',kind:'patch',media_type:'text/plain',size:bytes.length,sha256:digest};
  const initialJob=data['worker/jobs'].jobs[0];
  Object.assign(initialJob,{id:'9'.repeat(32),case_id:'5'.repeat(32),facts:['已釐清感應器與客廳燈的關係，補上說明。成果是示範，不會部署。'],artifacts:[artifact],runtime_location:'demo-sandbox',verification:{state:'passed',observed_from:'independent_host_verifier',patch_sha256:digest,tests_executed:1}});
  data['worker/jobs/'+initialJob.id]=initialJob;
  data['hermes/cases/'+'5'.repeat(32)].jobs=[{id:initialJob.id,state:'succeeded',worker_id:'host-coder',artifact_count:1,evidence_count:0,content_expired:false}];
  const tasks:Row[]=data.tasks.tasks,jobs:Row[]=data['worker/jobs'].jobs;
  const starts=new Map<string,number>(),jobStarts=new Map<string,number>();
  const messages:Row[]=data['gateway/messages'].messages;
  let serial=10;
  const nextId=(length:number)=>(serial++).toString(16).padStart(length,'0');
  function proposal(title:string,mode='upsert',automation?:Row):Row{
    const id='demo-task-'+serial++,rule=clone(automation?.intent||intent);rule.title=title;
    const final=compile(rule,automation?.id||'agentic_'+id);
    if(mode==='disable')final.initial_state=false;
    else if(mode==='upsert')final.initial_state=true;
    const p={id,mode,intent:rule,prior:automation?compile(automation.intent,automation.id):null,final,digest:hash(new TextEncoder().encode(JSON.stringify(final))),expires:Date.now()/1000+3600,action:null};
    const task={id,provider:'codex',state:'awaiting_approval',request:title,message:'請先看規則；批准前不會改動示範家庭。',created_at:time(serial*1000),updated_at:time(),proposal_id:id,proposal:p,events:[],automation_id:automation?.id};tasks.unshift(task);return task;
  }
  function progress(){
    for(const task of tasks){const start=starts.get(task.id);if(start===undefined)continue;
      const elapsed=performance.now()-start,step=Math.min(3,Math.floor(elapsed/1800));
      // Same end states as engine.record_deployed: an enabled rule waits for a real trigger; nothing is observed here.
      const done=step===3,disabled=task.proposal.final.initial_state===false;
      task.state=done?(disabled?'succeeded':'verifying'):'executing';
      task.message=done?(disabled?'規則已停用並讀回':'已部署並啟用，正在等待真實狀態觸發'):['正在備份示範設定。','正在寫入示範規則。','正在讀回確認示範設定。'][step];
      task.events=Array.from({length:step+1},(_,i)=>({seq:i+1,at:time(i*1800),kind:['backup_completed','write_completed','readback_completed','deployment_confirmed'][i]}));
      if(done){starts.delete(task.id);task.proposal.deployed_at=time();
        const rules=data.automations.automations,existing=rules.find((r:Row)=>r.id===task.automation_id);
        if(existing){existing.enabled=task.proposal.mode!=='disable';existing.intent=clone(task.proposal.intent)}else rules.push({id:task.proposal.final.id,enabled:true,intent:clone(task.proposal.intent)});
      }
    }
    for(const job of jobs){const start=jobStarts.get(job.id);if(start===undefined)continue;const step=Math.min(4,Math.floor((performance.now()-start)/1400));job.phase=['queued','launching','running','verifying','collecting'][step];job.state=step===4?'succeeded':'running';if(step===4){Object.assign(job,{stop_confirmed:true,facts:clone(initialJob.facts),artifacts:job.mode==='development'?[artifact]:[],verification:clone(initialJob.verification)});jobStarts.delete(job.id)}}
    data['worker/jobs'].active_job_id=jobStarts.keys().next().value||null;data.workers.active_job_id=data['worker/jobs'].active_job_id;
  }
  window.__agenticDemo={unhandled:[],marker:AGENTIC_DEMO_BUILD};
  const original=window.fetch.bind(window);
  window.fetch=async(input,options)=>{
    const url=new URL(input instanceof Request?input.url:String(input),location.href);
    if(url.origin!==location.origin)throw new TypeError('Demo blocks off-origin requests');
    const match=/\/api\/(.+)$/.exec(url.pathname);if(!match)return original(input,options);
    const path=decodeURIComponent(match[1]),method=(options?.method||(input instanceof Request?input.method:'GET')).toUpperCase();
    if(!screenshot)await new Promise(resolve=>setTimeout(resolve,150+Math.random()*250));
    progress();let result:any,status=200;
    if(method==='GET'){
      if(path==='tasks')result={tasks};
      else if(path.startsWith('tasks/'))result=tasks.find(t=>t.id===path.slice(6));
      // Like the Controller's Gateway.present(): a saved proposal reply shows what its task says now.
      else if(path==='gateway/messages')result={...data[path],messages:messages.map(m=>{const task=m.state==='awaiting_approval'&&tasks.find(t=>t.id===m.task_id);return task&&task.state!=='awaiting_approval'&&task.message?{...m,progress:task.state==='verifying'?'succeeded':task.state,answer:task.message}:m})};
      else if(path==='worker/jobs')result=data[path];
      else if(/^worker\/jobs\/[^/]+\/artifacts\//.test(path)){const job=jobs.find(j=>j.id===path.split('/')[2]);if(job?.artifacts.some((a:Row)=>a.id===path.split('/')[4]))result={content_base64:btoa(String.fromCharCode(...bytes)),sha256:digest}}
      else if(/^worker\/jobs\/[^/]+$/.test(path))result=jobs.find(j=>j.id===path.slice(12));
      else result=data[path];
      if(result===undefined){status=404;result={detail:'這份資料已過期或不存在。'};window.__agenticDemo.unhandled.push(path)}
    }else if(method==='POST'){
      // Only read fields used by supported operations. Bodies are never reflected wholesale.
      const body=JSON.parse(typeof options?.body==='string'?options.body:'{}');
      if(path==='gateway/messages'){
        const message=String(body.message||'').slice(0,5000),household=/燈|冷氣|門|出門|回家|晚上|light|home/i.test(message),task=household?proposal(/冷氣/.test(message)?'回家時開啟臥室冷氣':/出門/.test(message)?'出門時關閉客廳燈':'玄關有人時開啟客廳燈'):null;
        if(task&&/冷氣/.test(message)){task.proposal.intent.actions=[{entity_id:'climate.demo_bedroom',action:'turn_on'}];task.proposal.final=compile(task.proposal.intent,task.proposal.final.id);task.proposal.digest=hash(new TextEncoder().encode(JSON.stringify(task.proposal.final)))}
        if(task&&/出門/.test(message)){task.proposal.intent.actions[0].action='turn_off';task.proposal.final=compile(task.proposal.intent,task.proposal.final.id);task.proposal.digest=hash(new TextEncoder().encode(JSON.stringify(task.proposal.final)))}
        result={id:nextId(64),thread_id:messages.find(m=>m.id===body.reply_to)?.thread_id||nextId(64),case_id:'5'.repeat(32),source:'web',runtime:'host',provider:'codex',coordinator:'hermes',message,answer:task?'提案已準備好。先看精確內容，再決定是否批准。':'可以先看看設備與規則。想試試提案，可以說「有人經過玄關時打開燈」。',state:task?'awaiting_approval':'succeeded',created_at:time(serial*1000),task_id:task?.id||null};messages.unshift(result);
      }else if(/^proposals\/[^/]+\/approve$/.test(path)){
        const task=tasks.find(t=>t.proposal?.id===path.split('/')[1]);
        if(!task||task.state!=='awaiting_approval'||body.digest!==task.proposal.digest){status=409;result={detail:'提案已變更，請重新查看。'}}else{starts.set(task.id,performance.now());task.state='executing';task.message='正在備份示範設定。';result={id:task.id,state:task.state}}
      }else if(path==='policy'){data.inventory.policy={entities:(body.entities||[]).filter((p:Row)=>data.inventory.entities.some((e:Row)=>e.entity_id===p.entity_id)).map((p:Row)=>({entity_id:p.entity_id,read:!!p.read,write:['none','lighting','dedicated_helper','low_risk_switch'].includes(p.write)?p.write:'none',purpose:String(p.purpose||'').slice(0,200)}))};result={ok:true}}
      else if(/^automations\/[^/]+\/propose$/.test(path)){const rule=data.automations.automations.find((r:Row)=>r.id===path.split('/')[1]);if(rule&&['disable','enable','rollback'].includes(body.action))result=proposal(rule.intent.title,body.action==='enable'?'upsert':body.action,rule)}
      else if(path==='worker/jobs'){if(jobStarts.size){status=409;result={detail:'請等目前的示範工作完成。'}}else{result={...clone(initialJob),id:nextId(32),goal:String(body.goal||'示範調查').slice(0,4000),worker_id:body.worker_id==='home-coder'?'home-coder':'host-coder',mode:body.mode==='development'?'development':'research',state:'running',phase:'queued',stop_confirmed:false,facts:[],artifacts:[],verification:null,created_at:time(serial*1000)};jobs.unshift(result);jobStarts.set(result.id,performance.now());}}
      else if(/^worker\/jobs\/[^/]+\/cancel$/.test(path)){const job=jobs.find(j=>j.id===path.split('/')[2]);if(job){jobStarts.delete(job.id);Object.assign(job,{state:'cancelled',stop_confirmed:true});result={id:job.id}}}
      else if(/^tasks\/[^/]+\/cancel$/.test(path)){const task=tasks.find(t=>t.id===path.split('/')[1]);if(task){starts.delete(task.id);task.message=task.state==='verifying'?'任務追蹤已取消；已部署規則保持原狀':'已取消，沒有變更家庭';task.state='cancelled';result={id:task.id}}}
      else if(path==='pause'){data.status.paused=!!body.paused;result={ok:true}}
      if(result===undefined){status=409;result={detail:'示範中無法進行這個操作。'}}
    }else{status=409;result={detail:'示範中無法進行這個操作。'}}
    return new Response(JSON.stringify(clone(result)),{status,headers:{'Content-Type':'application/json'}});
  };
}
