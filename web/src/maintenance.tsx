import {useEffect,useRef,useState} from 'react';
import {api,requestKey} from './api';
import {HomeLanFold} from './ha-lan';
import {HostUpdateSettings} from './host-updates';
import {plainError,shortCommit} from './labels';
import {Button,Confirm,CopyButton,Notice,Status} from './ui';

type Network={lan:string;remote:string;managed:boolean;tailscale:{state:string;login_url?:string;identity?:string;address?:string};tunnel_command?:string};
type Release={candidate_digest:string;manifest:{version:string;source_commit:string;tests:{name:string;result:string}[]};state:string;approval_id?:string;approval_expires_at?:number;rollback_approval_id?:string;rollback_expires_at?:number;error_code?:string;source_sync?:string|null};
type Releases={trusted_publisher_configured:boolean;records:Release[];deployed_source_commit?:string|null};
const sourceSync:Record<string,string>={pending:'程式碼同步中',moved:'程式碼已同步',reverted:'程式碼已還原',needs_reconcile:'程式碼需要處理'};
const errorText=(e:unknown)=>(e as Error).message;

function NetworkSettings({onRemote}:{onRemote?:(remote:string)=>void}){
  const [value,setValue]=useState<Network|null>(null),[lan,setLan]=useState('dhcp'),[remote,setRemote]=useState('disabled'),[busy,setBusy]=useState(false),[error,setError]=useState<{text:string;code?:string}|null>(null),[readError,setReadError]=useState<{text:string;code?:string}|null>(null),[notice,setNotice]=useState(''),[revokeAsk,setRevokeAsk]=useState(false);
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;let initial=true;async function poll(){try{const next=await api('network');if(active&&next?.lan&&next?.remote){setValue(next);setReadError(null);onRemote?.(next.remote);if(initial){setLan(next.lan);setRemote(next.remote);initial=false}}}catch(e){if(active)setReadError(plainError(e))}if(active)timer=setTimeout(poll,4000)}poll();return()=>{active=false;clearTimeout(timer)}},[onRemote]);
  async function act(action:string,body:object){setBusy(true);setError(null);setNotice('');try{const next=await api(`network/${action}`,body);setValue(next);setLan(next.lan);setRemote(next.remote);onRemote?.(next.remote);setNotice(action==='preferences'?'連線偏好已保存。':action==='revoke'?'這台主機已登出 Tailscale。':'請完成 Tailscale 官方授權。')}catch(e){setError(plainError(e))}finally{setBusy(false)}}
  useEffect(()=>{if(value?.tailscale.state==='Running')setNotice('Tailscale 連線已確認。')},[value?.tailscale.state]);
  const changed=value&&(value.lan!==lan||value.remote!==remote),url=value?.tailscale.login_url;
  async function reload(){try{const next=await api('network');if(next?.lan&&next?.remote){setValue(next);setReadError(null);onRemote?.(next.remote)}else setReadError({text:''})}catch(e){setReadError(plainError(e))}}
  return <div className="maintenance-content">{error?<Notice error reason={error.code}>{error.text}</Notice>:readError&&<Notice error reason={readError.code}><p>無法讀取網路狀態。</p>{readError.text&&<p>{readError.text}</p>}<Button secondary disabled={busy} onClick={reload}>重試</Button></Notice>}{notice&&<Notice>{notice}</Notice>}<form onSubmit={e=>{e.preventDefault();act('preferences',{lan,remote})}}><fieldset className="choice-pair choice-stack" disabled={busy||!value}><legend>家中網路</legend><label className="choice-card"><input type="radio" name="lan" value="dhcp" checked={lan==='dhcp'} onChange={()=>setLan('dhcp')}/>自動取得 IP（建議）</label><label className="choice-card"><input type="radio" name="lan" value="reservation" checked={lan==='reservation'} onChange={()=>setLan('reservation')}/>路由器保留固定 IP</label></fieldset>{lan==='reservation'&&<p className="helper-text">請在路由器保留這台主機的位址，保存後仍需完成該設定。</p>}<fieldset className="choice-pair choice-stack" disabled={busy||!value}><legend>外出連線</legend><label className="choice-card"><input type="radio" name="remote" value="disabled" checked={remote==='disabled'} onChange={()=>setRemote('disabled')}/>暫不啟用</label><label className="choice-card"><input type="radio" name="remote" value="tailscale" checked={remote==='tailscale'} onChange={()=>setRemote('tailscale')}/>Tailscale 私人網路</label></fieldset><Button secondary type="submit" disabled={busy||!changed}>{busy?'處理中…':'保存連線偏好'}</Button></form>
    {value?.remote==='tailscale'&&<section aria-label="Tailscale 連線"><h3>Tailscale</h3>{value.tailscale.state==='Running'?<><p>已連接{value.tailscale.identity?` · ${value.tailscale.identity}`:''}</p>{value.tunnel_command&&<><p>在另一台已登入 Tailscale 的電腦執行這行指令。</p><pre className="tunnel-command one-line">{value.tunnel_command}</pre><CopyButton text={value.tunnel_command}>複製遠端連線指令</CopyButton><p className="helper-text">保持終端開啟；管理頁是 localhost:8099，家庭是 localhost:8123。</p></>}</>:<><p>{value.tailscale.state==='NeedsMachineAuth'?'請到 Tailscale 管理頁核准這台裝置。':value.tailscale.state==='not_installed'?'主機尚未安裝鎖定版本的 Tailscale。':'使用你的 Tailscale 帳號連接這台主機。'}</p>{url&&/^https:\/\/login\.tailscale\.com\/a\/[A-Za-z0-9_-]{8,200}$/.test(url)?<a className="button" href={url} target="_blank" rel="noreferrer">開啟 Tailscale 官方登入 ↗</a>:<Button disabled={busy||value.tailscale.state==='not_installed'} onClick={()=>act('login',{})}>開始 Tailscale 登入</Button>}</>}{value.managed&&(revokeAsk?<Confirm sentence="登出 Tailscale？外出時將無法連回這台主機。" confirmLabel="登出" busy={busy} onCancel={()=>setRevokeAsk(false)} onConfirm={()=>{setRevokeAsk(false);void act('revoke',{})}}/>:<Button secondary disabled={busy} onClick={()=>setRevokeAsk(true)}>登出此主機的 Tailscale</Button>)}</section>}
  </div>
}

type Publisher={contract:1;state:'unavailable'|'approval_required'|'trusted'|'conflict';message:string;code?:string;publisher:string|null;repository:string|null;channel:string|null;image_repository:string|null;key_sha256:string|null;review_digest:string|null;scope:'controller_only_same_schema';authority:'signed_controller_images_only'};
type PublisherOffer=Publisher&{approval:string;expires_at:number};
function checkedPublisher(value:Publisher){
  if(value?.contract!==1||!['unavailable','approval_required','trusted','conflict'].includes(value.state)||value.scope!=='controller_only_same_schema'||value.authority!=='signed_controller_images_only')throw new Error('無法核對官方更新來源，請稍後重新讀取。');
  if(['approval_required','trusted'].includes(value.state)&&(value.repository!=='TED-H-com/agentic-smart-home'||value.publisher!=='ted-h-com-agentic-smart-home'||value.channel!=='stable'||value.image_repository!=='ghcr.io/ted-h-com/agentic-smart-home-controller'||!value.key_sha256?.match(/^[a-f0-9]{64}$/)||!value.review_digest?.match(/^[a-f0-9]{64}$/)))throw new Error('無法核對官方更新來源，請稍後重新讀取。');
  return value;
}

function TrustedPublisher({onTrusted}:{onTrusted:()=>Promise<void>}){
  const [value,setValue]=useState<Publisher|null>(null),[review,setReview]=useState<PublisherOffer|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[readError,setReadError]=useState(false),[expired,setExpired]=useState(false);
  const serial=useRef(0),alive=useRef(false),onTrustedRef=useRef(onTrusted);onTrustedRef.current=onTrusted;
  async function refresh(){
    const request=++serial.current;
    try{
      const next=checkedPublisher(await api('updates/publisher'));
      if(!alive.current||request!==serial.current)return;
      setValue(next);setReadError(false);
      setReview(current=>current&&(next.state!=='approval_required'||next.review_digest!==current.review_digest)?null:current);
    }catch{if(alive.current&&request===serial.current){setReadError(true);setReview(null)}}
  }
  useEffect(()=>{alive.current=true;let timer:ReturnType<typeof setTimeout>;async function poll(){await refresh();if(alive.current)timer=setTimeout(poll,4000)}void poll();return()=>{alive.current=false;++serial.current;clearTimeout(timer)}},[]);
  useEffect(()=>{if(!review)return;const timer=setTimeout(()=>setExpired(true),Math.max(0,review.expires_at*1000-Date.now()));return()=>clearTimeout(timer)},[review]);
  async function offer(){
    ++serial.current;setBusy(true);setError('');setExpired(false);
    try{
      const next=await api('updates/publisher/offer',{});
      checkedPublisher(next);
      if(next.state!=='approval_required'||typeof next.approval!=='string'||!next.approval.match(/^[A-Za-z0-9_-]{32,100}$/)||!Number.isFinite(next.expires_at)||next.expires_at*1000<=Date.now())throw new Error('更新來源確認已過期，請重新查看。');
      if(alive.current){setValue(next);setReview(next);setReadError(false)}
    }catch(e){if(alive.current){setError(errorText(e));await refresh()}}
    finally{if(alive.current)setBusy(false)}
  }
  async function confirm(){
    if(!review)return;
    ++serial.current;setBusy(true);setError('');
    try{
      if(review.expires_at*1000<=Date.now())throw new Error('更新來源確認已過期，請重新查看。');
      const next=checkedPublisher(await api('updates/publisher/approve',{review_digest:review.review_digest,approval:review.approval}));
      if(next.state!=='trusted')throw new Error('更新來源尚未確認，請重新讀取。');
      if(alive.current){setValue(next);setReview(null);await onTrustedRef.current()}
    }catch(e){if(alive.current){setError(errorText(e));setReview(null);await refresh();await onTrustedRef.current().catch(()=>{})}}
    finally{if(alive.current)setBusy(false)}
  }
  return <section aria-label="官方更新來源"><h3>官方更新來源</h3>
    {readError?<Notice error>暫時無法核對官方更新來源，恢復連線後會自動重試。</Notice>:!value?<p role="status">正在核對更新來源…</p>:<>
      <p>{value.state==='trusted'?'已信任官方簽署的管理服務更新。':value.code==='official_publisher_pin_not_shipped'?'需取得含官方更新來源憑證的受信任主機版本。':value.message}</p>
      {value.key_sha256&&<dl className="worker-evidence"><dt>官方專案</dt><dd>{value.repository}</dd><dt>發布通道</dt><dd>{value.channel==='stable'?'穩定版':value.channel}</dd><dt>簽署金鑰 SHA-256 指紋</dt><dd><code>{value.key_sha256}</code></dd></dl>}
      {value.state==='approval_required'&&!review&&<Button disabled={busy} onClick={()=>void offer()}>設定官方更新來源</Button>}
      {review&&<div className="notice" role="region" aria-label="確認官方更新來源"><p>信任上方指紋對應的官方簽署金鑰？這只允許檢查管理服務更新，每個版本仍需你確認安裝。</p><p className="helper-text">更新範圍為管理服務；主機系統與家庭系統維持各自更新流程。</p>{expired?<><p>確認已過期，請重新查看來源。</p><Button disabled={busy} onClick={()=>{setReview(null);void offer()}}>重新查看來源</Button></>:<div className="row-actions"><Button disabled={busy} onClick={()=>void confirm()}>信任此官方更新來源</Button><Button secondary disabled={busy} onClick={()=>setReview(null)}>暫不設定</Button></div>}</div>}
    </>}{error&&<Notice error>{error}</Notice>}
  </section>
}

function UpdateSettings(){
  const [value,setValue]=useState<Releases|null>(null),[payload,setPayload]=useState(''),[fileName,setFileName]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[readError,setReadError]=useState(''),[selected,setSelected]=useState<string|null>(null);
  const pending=useRef<{signature:string;key:string}|null>(null),fileRef=useRef<HTMLInputElement>(null);
  const [review,setReview]=useState<{digest:string;action:'apply'|'rollback';approval:string;expires:number}|null>(null);
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{const next=await api('updates');if(active){setValue(next);setReadError('')}}catch(e){if(active)setReadError(errorText(e))}if(active)timer=setTimeout(poll,4000)}poll();return()=>{active=false;clearTimeout(timer)}},[]);
  async function reload(){setValue(await api('updates'));setReadError('')}
  async function stage(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{if(payload.length>65536)throw new Error('更新說明檔過大。');const body=JSON.parse(payload),signature=JSON.stringify(body);if(pending.current?.signature!==signature)pending.current={signature,key:requestKey()};const next=await api('updates/stage',body,false,pending.current.key);setSelected(next.candidate_digest);pending.current=null;setPayload('');setFileName('');await reload()}catch(e){setError(errorText(e));await reload().catch(()=>{})}finally{setBusy(false)}}
  const release=value?.records.find(item=>item.candidate_digest===selected)||value?.records.at(-1);
  const deployed=shortCommit(value?.deployed_source_commit);
  const sync=release?.source_sync?sourceSync[release.source_sync]||'':'';
  const sourceBlocked=release?.state==='failed'&&!!release.error_code?.startsWith('update_source_');
  async function offerRollback(){if(!release)return;setBusy(true);setError('');try{const next=await api('updates/rollback-offer',{candidate_digest:release.candidate_digest});setReview({digest:next.candidate_digest,action:'rollback',approval:next.rollback_approval_id,expires:next.rollback_expires_at});await reload()}catch(e){setError(errorText(e))}finally{setBusy(false)}}
  async function confirm(){if(!review)return;setBusy(true);setError('');try{if(review.expires*1000<=Date.now())throw new Error('確認已過期，請重新檢查版本。');await api(`updates/${review.action}`,{candidate_digest:review.digest,approval_id:review.approval});setReview(null);await reload()}catch(e){setError(errorText(e));await reload().catch(()=>{})}finally{setBusy(false)}}
  return <div className="maintenance-content"><h3>管家介面更新</h3><p>先檢查簽署的版本說明，確認後才更新。</p><TrustedPublisher onTrusted={reload}/>{error?<Notice error>{error}</Notice>:readError&&<Notice error>{readError}</Notice>}{value&&!value.trusted_publisher_configured?<Notice>尚未設定更新來源，目前無法檢查更新。</Notice>:<form onSubmit={stage}><div className="row-actions"><Button secondary type="button" onClick={()=>fileRef.current?.click()}>選擇更新說明檔</Button>{fileName&&<span>{fileName}</span>}</div><input ref={fileRef} className="sr-only" type="file" accept="application/json,.json" aria-label="更新說明檔" onChange={async e=>{const file=e.target.files?.[0];if(!file)return;try{if(file.size>65536)throw new Error('更新說明檔過大。');setPayload(await file.text());setFileName(file.name);setError('')}catch(error){setFileName('');setError(errorText(error))}}}/><details><summary>或貼上更新說明</summary><label>簽署的更新說明檔<textarea rows={4} value={payload} maxLength={65536} onChange={e=>setPayload(e.target.value)} placeholder="貼上更新說明" required/></label></details><Button secondary type="submit" disabled={busy||!value||!payload.trim()}>檢查候選版本</Button></form>}{deployed&&<details><summary>技術資料</summary><p>目前程式版本 <code title={value?.deployed_source_commit||undefined}>{deployed}</code></p></details>}
    {release&&<section className="update-review" aria-label="更新候選版本"><div className="section-heading"><h3>版本 {release.manifest.version}</h3><Status value={release.state}/></div>{sync&&<p className="helper-text">{sync}</p>}{value!.records.length>1&&<label>更新紀錄<select value={release.candidate_digest} onChange={e=>{setSelected(e.target.value);setReview(null)}}>{value!.records.map(item=><option key={item.candidate_digest} value={item.candidate_digest}>{item.manifest.version}</option>)}</select></label>}{release.error_code&&<Notice error reason={release.error_code}>{sourceBlocked?'開發助手的程式碼無法跟著這個版本同步，所以沒有更新。':'這次更新沒有完成。'}</Notice>}<details><summary>技術資料</summary><dl className="worker-evidence"><dt>候選版本指紋</dt><dd><code>{release.candidate_digest}</code></dd><dt>原始碼版本</dt><dd><code>{release.manifest.source_commit}</code></dd>{release.error_code&&<><dt>原因代碼</dt><dd><code>{release.error_code}</code></dd></>}</dl><ul>{release.manifest.tests.map(test=><li key={test.name}>{test.name} · {test.result}</li>)}</ul></details>
      {!review&&<div className="row-actions">{release.state==='staged'&&release.approval_id&&<Button disabled={busy} onClick={()=>setReview({digest:release.candidate_digest,action:'apply',approval:release.approval_id!,expires:release.approval_expires_at||0})}>檢查並確認更新</Button>}{release.state==='deployed'&&<Button secondary disabled={busy} onClick={offerRollback}>退回前一版本</Button>}</div>}
      {review&&review.digest===release.candidate_digest&&<div className="notice" role="region" aria-label="確認版本操作"><h4>{review.action==='apply'?`更新至 ${release.manifest.version}？`:'退回更新前的版本？'}</h4><p>這次只更新管理服務，服務會短暫重新啟動，新的 AI 工作會暫停。</p><p className="helper-text">主機系統與家庭系統維持各自更新流程；退版會保留目前的家庭操作紀錄。</p><div className="row-actions"><Button disabled={busy} onClick={confirm}>{review.action==='apply'?'確認更新這個版本':'確認退版'}</Button><Button secondary disabled={busy} onClick={()=>setReview(null)}>暫不操作</Button></div></div>}
    </section>}
  </div>
}

export function MaintenanceSettings({version,storageScope}:{version?:string;storageScope?:string}){
  const [network,setNetwork]=useState(false),[updates,setUpdates]=useState(false),[remote,setRemote]=useState<string|null>(null);
  const remember=useRef(setRemote);
  return <><HomeLanFold storageScope={storageScope}/><details className="host-fold" onToggle={e=>setNetwork(e.currentTarget.open)}><summary>網路與遠端{remote?` · ${remote==='tailscale'?'Tailscale':'僅家中'}`:''}</summary>{network&&<NetworkSettings onRemote={remember.current}/>}</details><details className="host-fold" onToggle={e=>setUpdates(e.currentTarget.open)}><summary>版本 {version||'讀取中'}</summary>{updates&&<><HostUpdateSettings/><UpdateSettings/></>}</details></>;
}
