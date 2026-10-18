import {useCallback,useEffect,useRef,useState} from 'react';
import {api,ApiError,haPath,haTarget,requestKey} from './api';
import {Button,Confirm,Notice,Status,timestamp} from './ui';
import {names,plainError} from './labels';

type Recipient={alias:string;label:string;channel:string;chat:string};
type Config={enabled:boolean;revision:string;recipients:Recipient[]};
type Destination={channel:string;chat:string;label:string};
type Channel={channel:string;transport:string;login:string};
type Delivery={id:string;recipient:string;channel:string;state:string;created_at:string;updated_at:string;reason:string|null};
type Snapshot={config:Config;integration:{state:string;source_id:string|null;last_seen:string|null;reason:string|null};channels:Channel[];destinations:Destination[];deliveries:Delivery[]};
const integrationLabels:Record<string,string>={disabled:'已停用',not_installed:'尚未安裝',unavailable:'暫時無法連線',ready:'已就緒',needs_rebind:'需要重新連接'};
const transportLabels:Record<string,string>={connected:'已連線',connecting:'連線中',disconnected:'未連線',error:'連線異常',disabled:'已停用'};
const loginLabels:Record<string,string>={ready:'已登入',needs_login:'需要登入',unknown:'尚未確認',not_required:'不需登入'};
const deliveryLabels:Record<string,string>={claiming:'確認中',sending:'送出中',submitted:'已交給通訊服務',unknown:'結果不明',not_sent:'未送出'};
const destinationKey=(item:Pick<Destination,'channel'|'chat'>)=>JSON.stringify([item.channel,item.chat]);
const copyConfig=(config:Config):Config=>({...config,recipients:config.recipients.map(item=>({...item}))});
const recipientPattern=/^[a-z][a-z0-9_]{0,31}$/;
const record=(value:unknown):value is Record<string,any>=>!!value&&typeof value==='object'&&!Array.isArray(value);
function snapshot(value:unknown):Snapshot{
  const recipient=(item:unknown)=>record(item)&&['alias','label','channel','chat'].every(key=>typeof item[key]==='string');
  if(!record(value)||!record(value.config)||typeof value.config.enabled!=='boolean'||typeof value.config.revision!=='string'||!Array.isArray(value.config.recipients)||!value.config.recipients.every(recipient)||!record(value.integration)||typeof value.integration.state!=='string'||!Array.isArray(value.channels)||!value.channels.every((item:unknown)=>record(item)&&['channel','transport','login'].every(key=>typeof item[key]==='string'))||!Array.isArray(value.destinations)||!value.destinations.every((item:unknown)=>record(item)&&['channel','chat','label'].every(key=>typeof item[key]==='string'))||!Array.isArray(value.deliveries)||!value.deliveries.every((item:unknown)=>record(item)&&['id','recipient','channel','state','created_at','updated_at'].every(key=>typeof item[key]==='string')))
    throw new Error('通知設定格式不完整，請重新讀取。');
  return value as Snapshot;
}
function stateStyle(state:string){
  if(['ready','connected','submitted','not_required'].includes(state))return 'connected';
  if(['unknown','needs_rebind','needs_login'].includes(state))return 'needs_reconcile';
  if(['error','unavailable','not_sent'].includes(state))return 'failed';
  if(['claiming','sending','connecting'].includes(state))return 'checking';
  return 'not_started';
}

export function NotificationsPanel(){
  const [data,setData]=useState<Snapshot|null>(null),[draft,setDraft]=useState<Config|null>(null);
  const [edited,setEdited]=useState(false),[editor,setEditor]=useState<{index:number;value:Recipient}|null>(null);
  const [remove,setRemove]=useState<number|null>(null),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true);
  const [readError,setReadError]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [conflict,setConflict]=useState(false),[uncertain,setUncertain]=useState(false);
  const dirty=useRef(false),readEpoch=useRef(0),pending=useRef<{body:string;key:string}|null>(null);
  dirty.current=edited||editor!==null||remove!==null||busy||conflict||uncertain;
  const accept=useCallback((value:Snapshot,replace=false)=>{
    setData(value);setReadError('');
    if(replace||!dirty.current){setDraft(copyConfig(value.config));setEdited(false)}
  },[]);
  useEffect(()=>{
    let active=true;let timer:ReturnType<typeof setTimeout>;
    async function poll(){const epoch=readEpoch.current;try{const value=snapshot(await api('notifications'));if(active&&epoch===readEpoch.current)accept(value)}catch(e){if(active&&epoch===readEpoch.current)setReadError(plainError(e).text)}finally{if(active){setLoading(false);timer=setTimeout(poll,5000)}}}
    poll();return()=>{active=false;clearTimeout(timer)};
  },[accept]);
  const stale=conflict||!!(data&&draft&&data.config.revision!==draft.revision);
  const blocked=busy||stale||uncertain;
  const invalidRecipient=!!draft?.recipients.some(item=>!data?.destinations.some(destination=>destinationKey(destination)===destinationKey(item)));
  function changed(value:Config){setDraft(value);setEdited(true);setNotice('');setError('')}
  async function reload(){
    readEpoch.current++;setBusy(true);setError('');
    try{accept(snapshot(await api('notifications')),true);setEditor(null);setRemove(null);setConflict(false);setUncertain(false);pending.current=null;setNotice('已重新讀取通知設定。')}
    catch(e){setReadError(plainError(e).text)}finally{readEpoch.current++;setBusy(false)}
  }
  async function save(){
    if(!draft||blocked||editor||(draft.enabled&&(invalidRecipient||!!readError)))return;
    readEpoch.current++;setBusy(true);setError('');setNotice('');
    const body={enabled:draft.enabled,expected_revision:draft.revision,recipients:draft.recipients};
    const serialized=JSON.stringify(body);
    if(pending.current?.body!==serialized)pending.current={body:serialized,key:requestKey()};
    try{accept(snapshot(await api('notifications/config',body,false,pending.current.key)),true);pending.current=null;setNotice('設定已儲存；通知結果請看送出紀錄。')}
    catch(e){if(e instanceof ApiError&&e.status===409)setConflict(true);else if(e instanceof ApiError&&e.status===0)setUncertain(true);else setError(plainError(e).text)}
    finally{readEpoch.current++;setBusy(false)}
  }
  function applyRecipient(){
    if(!editor||!draft||blocked)return;
    const value={...editor.value,alias:editor.value.alias.trim(),label:editor.value.label.trim()};
    if(!recipientPattern.test(value.alias)){setError('收件人代稱請用小寫英文字母開頭，最多 32 個英數字或底線。');return}
    if(!value.label){setError('請填寫收件人的顯示名稱。');return}
    if(draft.recipients.some((item,index)=>index!==editor.index&&item.alias===value.alias)){setError('這個收件人代稱已存在。');return}
    if(!data?.destinations.some(destination=>destinationKey(destination)===destinationKey(value))){setError('請選擇已允許的聊天室。');return}
    changed({...draft,recipients:editor.index<0?[...draft.recipients,value]:draft.recipients.map((item,index)=>index===editor.index?value:item)});setEditor(null);
  }
  return <section className="notification-settings" aria-labelledby="notification-heading">
    <div className="section-heading"><h2 id="notification-heading">家庭通知</h2>{data&&<Status value={readError?'unavailable':stateStyle(data.integration.state)} label={readError?'讀取失敗':integrationLabels[data.integration.state]||'尚未確認'}/>}</div>
    <p className="helper-text">讓家庭自動化傳送文字到指定收件人。</p>
    {readError&&<Notice error>{readError}<div className="row-actions"><Button secondary disabled={busy} onClick={reload}>{edited||editor?'捨棄修改並重新載入':'重新讀取'}</Button></div></Notice>}
    {error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    {(stale||uncertain)&&<Notice error>{stale?'設定已在別處更新，請重新載入後修改。':'尚未確認設定是否儲存，請重新載入確認。'}<div className="row-actions"><Button secondary disabled={busy} onClick={reload}>捨棄修改並重新載入</Button></div></Notice>}
    {!data||!draft?<p role="status">{loading?'讀取通知設定…':'尚未讀取通知設定。'}</p>:<>
      <label className="check notification-enable"><input type="checkbox" checked={draft.enabled} disabled={blocked||!!readError&&!draft.enabled} onChange={event=>changed({...draft,enabled:event.target.checked})}/>啟用家庭通知</label>
      {data.integration.state==='not_installed'&&<p className="helper-text">家庭尚未安裝通知整合，儲存設定不會自動安裝。</p>}
      {data.integration.state==='needs_rebind'&&<p className="helper-text">家庭連接的主機已變更，請依安裝說明重新連接。</p>}
      {data.integration.state==='unavailable'&&<p className="helper-text">暫時讀不到家庭通知整合，請確認家庭服務。</p>}
      <details className="notification-install"><summary>安裝與使用</summary><p>下載並依說明安裝，再到家庭的「設定 → 裝置與服務」新增通知整合。</p><div className="row-actions"><a href="downloads/agentic-home-notifications.zip" download>下載通知整合</a><a href="https://github.com/TED-H-com/agentic-smart-home/blob/main/docs/HA_NOTIFICATIONS.md" target="_blank" rel="noreferrer">安裝說明 ↗</a><a href={haPath('/config/integrations')} target={haTarget()} rel="noreferrer">開啟家庭設定 ↗</a></div><p className="helper-text">僅支援文字通知。</p></details>
      <div className="notification-connections" aria-label="通知連線狀態">{readError?<p className="helper-text">連線狀態待重新確認；下方保留上次讀取的設定與紀錄。</p>:data.channels.map(channel=><div key={channel.channel}><strong>{names[channel.channel]||channel.channel}</strong><dl><div><dt>本機傳輸</dt><dd><Status value={stateStyle(channel.transport)} label={transportLabels[channel.transport]||'尚未確認'}/></dd></div><div><dt>{channel.channel==='line'?'LINE 登入':'帳號登入'}</dt><dd><Status value={stateStyle(channel.login)} label={loginLabels[channel.login]||'尚未確認'}/></dd></div></dl></div>)}</div>
      <details className="settings-fold notification-recipients" open>
        <summary>收件人 · {draft.recipients.length}</summary>
        {!draft.recipients.length&&<p className="helper-text">還沒有收件人，請先加入已允許的聊天室。</p>}
        {draft.recipients.map((item,index)=><div className="notification-recipient" key={index}><div><strong>{item.label||item.alias}</strong><small><code>{item.alias}</code> · {names[item.channel]||item.channel}</small><details><summary>技術資料</summary><code>{item.chat}</code></details></div><div className="row-actions"><Button secondary disabled={blocked||editor!==null} onClick={()=>{setEditor({index,value:{...item}});setError('');setRemove(null)}} aria-label={`修改收件人 ${item.alias}`}>修改</Button><Button secondary disabled={blocked||editor!==null} onClick={()=>setRemove(index)} aria-label={`移除收件人 ${item.alias}`}>移除</Button></div>{remove===index&&<Confirm sentence={`移除收件人 ${item.alias}？儲存後生效。`} confirmLabel="移除" busy={blocked} onConfirm={()=>{changed({...draft,recipients:draft.recipients.filter((_,i)=>i!==index)});setRemove(null)}} onCancel={()=>setRemove(null)}/>}</div>)}
        {invalidRecipient&&<p className="helper-text">有聊天室已不在允許清單，請修改或移除該收件人。</p>}
        {editor?<form className="notification-editor" onSubmit={event=>{event.preventDefault();applyRecipient()}}>
          <label>收件人代稱<input required pattern="[a-z][a-z0-9_]{0,31}" maxLength={32} value={editor.value.alias} disabled={blocked} placeholder="例如 family" onChange={event=>setEditor({...editor,value:{...editor.value,alias:event.target.value}})}/><small>自動化使用此代稱；修改後請同步更新規則。</small></label>
          <label>顯示名稱<input required maxLength={120} value={editor.value.label} disabled={blocked} placeholder="例如 家庭聊天室" onChange={event=>setEditor({...editor,value:{...editor.value,label:event.target.value}})}/></label>
          <label>聊天室<select required value={editor.value.chat?destinationKey(editor.value):''} disabled={blocked} onChange={event=>{const destination=data.destinations.find(item=>destinationKey(item)===event.target.value);if(destination)setEditor({...editor,value:{...editor.value,channel:destination.channel,chat:destination.chat}})}}><option value="">選擇已允許的聊天室…</option>{data.destinations.map(destination=><option key={destinationKey(destination)} value={destinationKey(destination)}>{names[destination.channel]||destination.channel} · {destination.label||destination.chat}</option>)}</select></label>
          <div className="row-actions"><Button secondary type="submit" disabled={blocked}>{editor.index<0?'加入清單':'套用修改'}</Button><Button secondary type="button" disabled={busy} onClick={()=>{setEditor(null);setError('')}}>取消</Button></div>
        </form>:<div className="row-actions"><Button secondary disabled={blocked||draft.recipients.length>=16||!data.destinations.length} onClick={()=>{setEditor({index:-1,value:{alias:'',label:'',channel:'',chat:''}});setRemove(null);setError('')}}>新增收件人</Button></div>}
        {!data.destinations.length&&<p className="helper-text">請先在上方通訊設定允許聊天室，再加入收件人。</p>}
      </details>
      <div className="row-actions notification-save"><Button disabled={blocked||!edited||!!editor||draft.enabled&&(invalidRecipient||!!readError)} onClick={save}>{busy?'儲存中…':'儲存通知設定'}</Button>{edited&&<span className="helper-text">修改尚未儲存</span>}</div>
      <details className="settings-fold notification-history"><summary>送出紀錄 · {data.deliveries.length}</summary><p className="helper-text">交給通訊服務不代表已送達或已讀；結果不明時不會自動重送。</p>{!data.deliveries.length?<p className="helper-text">還沒有通知紀錄。</p>:<ol>{data.deliveries.map(delivery=><li key={delivery.id}><div className="notification-delivery-heading"><strong>{delivery.recipient}</strong><Status value={stateStyle(delivery.state)} label={deliveryLabels[delivery.state]||'尚未確認'}/></div><p className="helper-text">{names[delivery.channel]||delivery.channel} · <time dateTime={delivery.updated_at}>{timestamp(delivery.updated_at)}</time></p>{delivery.state==='unknown'&&<p className="helper-text">請到聊天室確認這則通知，避免重複傳送。</p>}<details><summary>技術資料</summary><dl className="setup-facts"><div><dt>紀錄</dt><dd><code>{delivery.id}</code></dd></div>{delivery.reason&&<div><dt>原因</dt><dd><code>{delivery.reason}</code></dd></div>}</dl></details></li>)}</ol>}</details>
    </>}
  </section>;
}
