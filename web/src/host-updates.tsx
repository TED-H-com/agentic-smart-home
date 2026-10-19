import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {plainError,shortCommit} from './labels';
import {Button,Notice,Status,timestamp} from './ui';

type Compatibility={host_protocol:1;controller_protocol:1;controller_schema:3;reconcile:1};
type BundleManifest={version:string;source_commit:string;bundle:{sha256:string;size:number};compatibility:Compatibility};
type BundleRecord={candidate_digest:string;manifest:BundleManifest;state:string;created_at:number;updated_at:number;message:string;error_code?:string|null;auto_rollback?:boolean;approval_id?:string|null;approval_expires_at?:number|null;rollback_approval_id?:string|null;rollback_expires_at?:number|null;restore_point?:{scope?:string|null;state?:string|null;ha_snapshot?:string|null;root_snapshot_name?:string|null}|null};
type BundleStatus={contract:1;scope:'host_bundle_same_schema';compatibility:Compatibility|null;available:boolean;code?:string|null;message:string;records:BundleRecord[];current_source_commit?:string|null;dispatch?:{candidate_digest:string;operation:'apply'|'rollback';message:string}|null;check:{state:string;code?:string|null;message:string;candidate_digest?:string|null;checked_at?:number|null}};
const compatible=(value:Compatibility|null|undefined)=>value?.host_protocol===1&&value.controller_protocol===1&&value.controller_schema===3&&value.reconcile===1;
const labels:Record<string,string>={staging:'正在準備',staged:'等待你的確認',queued:'準備更新',activating:'正在更新',deployed:'已更新',rolling_back:'正在復原',rolled_back:'已恢復前一版本',failed:'未完成',needs_reconcile:'需要支援協助'};
const activeStates=['staging','queued','activating','rolling_back'];
function checkedStatus(value:BundleStatus){
  if(value?.contract!==1||value.scope!=='host_bundle_same_schema'||!Array.isArray(value.records)||!value.check||typeof value.available!=='boolean'||value.available&&!compatible(value.compatibility))throw new Error('主機更新介面不相容，請聯絡支援核對。');
  for(const item of value.records)if(!item?.candidate_digest?.match(/^[a-f0-9]{64}$/)||!compatible(item.manifest?.compatibility)||!labels[item.state])throw new Error('無法核對主機更新版本，請重新讀取。');
  return value;
}
const expiry=(value?:number|null)=>typeof value==='number'&&Number.isFinite(value)&&value*1000>Date.now();

export function HostUpdateSettings(){
  const [value,setValue]=useState<BundleStatus|null>(null),[busy,setBusy]=useState(false),[readError,setReadError]=useState(false),[readReason,setReadReason]=useState(''),[error,setError]=useState<{text:string;code?:string}|null>(null),[notice,setNotice]=useState(''),[selected,setSelected]=useState<string|null>(null);
  const [rollback,setRollback]=useState<{digest:string;approval:string;expires:number}|null>(null);
  const [fileName,setFileName]=useState(''),[payload,setPayload]=useState('');
  const fileRef=useRef<HTMLInputElement>(null),alive=useRef(false),serial=useRef(0);
  async function refresh(){
    const request=++serial.current;
    try{
      const next=checkedStatus(await api('host-updates'));
      if(!alive.current||request!==serial.current)return;
      setValue(next);setReadError(false);setReadReason('');
      setRollback(current=>current&&!next.records.some(item=>item.candidate_digest===current.digest&&['deployed','needs_reconcile'].includes(item.state)&&item.rollback_approval_id===current.approval)?null:current);return next;
    }catch(e){if(alive.current&&request===serial.current){setReadError(true);setReadReason(plainError(e).text);setRollback(null)}}
  }
  useEffect(()=>{alive.current=true;let timer:ReturnType<typeof setTimeout>;async function poll(){await refresh();if(alive.current)timer=setTimeout(poll,3000)}void poll();return()=>{alive.current=false;++serial.current;clearTimeout(timer)}},[]);
  const record=value?.records.find(item=>item.candidate_digest===selected)||value?.records[0];
  const running=value?.check.state==='checking'||value?.records.some(item=>activeStates.includes(item.state));
  const available=!!value?.available&&compatible(value.compatibility),blocked=busy||readError||!value||!!running;
  async function act(action:'check'|'stage'|'apply'|'rollback-offer'|'rollback'|'cancel-offer',body:object={}){
    ++serial.current;setBusy(true);setError(null);setNotice('');
    try{
      const next=await api(`host-updates/${action}`,body);
      if(!alive.current)return;
      if(action==='check'){setValue(checkedStatus(next));setSelected(null);setRollback(null)}
      else if(action==='cancel-offer'){setRollback(null);setNotice(next.cancelled&&!activeStates.includes(next.record?.state)&&next.record?.state!=='needs_reconcile'?'已確認這次更新未開始，可重新檢查官方版本。':'這次更新已由主機受理，正在核對結果。')}
      else if(action==='rollback-offer'){
        if(!next.rollback_approval_id||!expiry(next.rollback_expires_at))throw new Error('退版確認已過期，請重新查看。');
        setRollback({digest:next.candidate_digest,approval:next.rollback_approval_id,expires:next.rollback_expires_at});
      }else{
        setSelected(next.candidate_digest);setRollback(null);
        if(action==='stage'){setPayload('');setFileName('')}
      }
      const observed=await refresh();
      if(action==='cancel-offer'&&next.cancelled&&!activeStates.includes(next.record?.state)&&next.record?.state!=='needs_reconcile'&&observed?.available&&!observed.dispatch){setValue(checkedStatus(await api('host-updates/check',{})));setSelected(null)}
    }catch(e){if(alive.current){setError(plainError(e));setRollback(null);await refresh()}}
    finally{if(alive.current)setBusy(false)}
  }
  const approve=()=>{if(record?.approval_id&&expiry(record.approval_expires_at))void act('apply',{candidate_digest:record.candidate_digest,approval_id:record.approval_id})};
  const restore=()=>{if(rollback&&expiry(rollback.expires))void act('rollback',{candidate_digest:rollback.digest,approval_id:rollback.approval})};
  async function stage(event:React.FormEvent){event.preventDefault();try{if(payload.length>65536)throw new Error('主機更新說明檔過大。');let body:object;try{body=JSON.parse(payload)}catch{throw new Error('無法讀取主機更新說明檔，請選擇官方簽署的檔案。')}await act('stage',body)}catch(e){setError(plainError(e))}}
  return <section className="maintenance-content" aria-label="主機程式更新" aria-busy={busy}>
    <h3>主機程式更新</h3><p>檢查官方簽署版本，確認一次後會自動備份、更新並核對。</p>
    {readError&&<Notice error>{readReason||'暫時無法讀取主機更新狀態，恢復連線後會自動核對結果。'}</Notice>}
    {error&&<Notice error reason={error.code}>{error.text}</Notice>}{notice&&<Notice>{notice}</Notice>}
    {!value?<p role="status">正在讀取主機更新狀態…</p>:<>
      {!value.available&&<Notice>{value.message}</Notice>}
      {value.check.state==='checking'&&<p role="status">正在檢查官方版本，準備完成後會請你確認。</p>}
      {value.check.state==='failed'&&<Notice error reason={value.check.code||undefined}>{value.check.message||'暫時無法檢查官方版本，請稍後重試。'}</Notice>}
      <Button disabled={!!blocked||!available||!!value.dispatch} onClick={()=>void act('check')}>{value.check.state==='checking'?'正在檢查…':'檢查主機更新'}</Button>
      {value.dispatch&&<Notice><p>{value.dispatch.message}</p><Button secondary disabled={busy||readError||!!running} onClick={()=>void act('cancel-offer')}>確認未開始並重新檢查</Button></Notice>}
      {record&&<section className="update-review" aria-label="主機更新版本">
        <div className="section-heading"><h4>主機版本 {record.manifest.version}</h4><Status value={record.state} label={labels[record.state]}/></div>
        {record.auto_rollback&&record.state==='rolled_back'?<Notice>這次更新未能通過檢查，已自動恢復更新前的主機程式；目前家庭資料保持原狀。</Notice>:record.state==='needs_reconcile'?<Notice error>主機復原尚未確認，請聯絡支援；系統會保留還原點。</Notice>:record.message&&<p role="status">{record.message}</p>}
        {record.error_code&&record.state==='failed'&&<Notice error reason={record.error_code}>主機更新未完成，請查看狀態後重試。</Notice>}
        {record.state==='staged'&&<div role="region" aria-label="確認主機更新">
          <p>更新至 {record.manifest.version}：系統會先建立還原點。家庭可能短暫暫停，主機管理服務會重新啟動；檢查失敗時會自動復原。</p>
          {expiry(record.approval_expires_at)&&record.approval_id?<Button disabled={!!blocked||!available} onClick={approve}>確認更新主機至 {record.manifest.version}</Button>:<p>{value.dispatch?'確認已過期，請先按「確認未開始並重新檢查」核對這次更新。':'確認已過期，請按「檢查主機更新」重新核對版本。'}</p>}
        </div>}
        {['deployed','needs_reconcile'].includes(record.state)&&!rollback&&<Button secondary disabled={!!blocked||!compatible(value.compatibility)} onClick={()=>void act('rollback-offer',{candidate_digest:record.candidate_digest})}>{record.state==='needs_reconcile'?'重試復原':'退回更新前版本'}</Button>}
        {rollback&&rollback.digest===record.candidate_digest&&<div className="notice" role="region" aria-label="確認主機退版"><p>退回這次更新前的主機程式？家庭可能短暫暫停，主機管理服務會重新啟動。</p>{expiry(rollback.expires)?<div className="row-actions"><Button disabled={!!blocked} onClick={restore}>確認退回更新前版本</Button><Button secondary disabled={busy} onClick={()=>setRollback(null)}>暫不退回</Button></div>:<p>退版確認已過期，請重新讀取更新狀態。</p>}</div>}
        <details><summary>主機更新技術資料</summary><dl className="worker-evidence"><dt>候選版本指紋</dt><dd><code>{record.candidate_digest}</code></dd><dt>來源版本</dt><dd><code>{record.manifest.source_commit}</code></dd><dt>更新檔指紋</dt><dd><code>{record.manifest.bundle.sha256}</code></dd><dt>最近核對</dt><dd>{timestamp(new Date(record.updated_at*1000).toISOString())}</dd>{record.restore_point&&<><dt>還原點</dt><dd>{record.restore_point.state||'建立中'}</dd></>}{record.error_code&&<><dt>原因代碼</dt><dd><code>{record.error_code}</code></dd></>}</dl></details>
      </section>}
      {value.current_source_commit&&<p className="helper-text">目前主機程式版本 <code title={value.current_source_commit}>{shortCommit(value.current_source_commit)}</code></p>}
      {available&&<details><summary>進階：匯入已簽署更新檔</summary><form onSubmit={stage}><input ref={fileRef} className="sr-only" type="file" accept="application/json,.json" aria-label="主機更新說明檔" onChange={async event=>{const file=event.target.files?.[0];if(!file)return;try{if(file.size>65536)throw new Error('主機更新說明檔過大。');setPayload(await file.text());setFileName(file.name);setError(null)}catch(e){setError(plainError(e))}}}/><div className="row-actions"><Button secondary type="button" disabled={!!blocked||!!value.dispatch} onClick={()=>fileRef.current?.click()}>選擇主機更新說明檔</Button>{fileName&&<span>{fileName}</span>}<Button secondary type="submit" disabled={!!blocked||!payload||!!value.dispatch}>核對主機更新檔</Button></div></form></details>}
    </>}
  </section>
}
