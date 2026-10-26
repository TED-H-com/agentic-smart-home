import {useCallback,useEffect,useRef,useState} from 'react';
import QRCode from 'qrcode';
import {api,requestKey} from './api';
import {Button,Confirm,CopyButton,Notice,friendlySentence} from './ui';
import {RuntimePanel} from './providers';
import {HostPanel} from './host';
import {HermesPanel} from './hermes';
import {connection,names,plainError} from './labels';
import type {View} from './setup';

type Pairing={state:string;code:string|null;expires_at:number|null;chat:string|null};
const channelOrder=['line','telegram','discord','web'];
const hints:Record<string,string>={line:'建議',web:'不需帳號'};
// Each of these ends the login attempt; the one action starts a new attempt.
const lineRecovery:Record<string,{text:string;action:string}>={
  qr_expired:{text:'QR 已過期，請重新開始。',action:'重新開始'},
  pin_failed:{text:'PIN 驗證沒有通過，請重新開始。',action:'重新開始'},
  login_rejected:{text:'LINE 拒絕這次登入，請稍後再試。',action:'重新開始'},
  service_unavailable:{text:'LINE 服務暫時無法使用，請稍後再試。',action:'再試一次'},
};
const lineEnded=['ready','unavailable','failed',...Object.keys(lineRecovery)];
function telegramSentence(channel:any){
  const reason=channel?.reason,status=channel?.status;
  const fallback=reason==='webhook_active'?'這個機器人已接到別的服務，請先在那裡解除。':reason==='poller_conflict'?'這個機器人正被另一個程式使用，請先停止它。':status==='invalid_token'||reason==='invalid_token'?'權杖無效，請重新貼上。':status==='rate_limited'||reason==='rate_limited'?'Telegram 暫時限制請求，請稍後再試。':status==='unavailable'||reason==='unavailable'?'暫時連不上 Telegram。':'';
  return fallback?friendlySentence(channel?.message,fallback):'';
}
function ChannelPairing({channel,chat,secret,hasSecret,enabled,onChange,onSaved,onState}:{channel:string;chat:string;secret:string;hasSecret:boolean;enabled:boolean;onChange:()=>void;onSaved:()=>void;onState:(value:Pairing,channel:string)=>void}){
  const [pair,setPair]=useState<Pairing|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState<{text:string;code?:string}|null>(null),[readError,setReadError]=useState<{text:string;code?:string}|null>(null);
  const previous=useRef(''),pending=useRef<{signature:string;key:string}|null>(null);
  const endpoint=`gateway/channels/${channel}/pairing`;
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{const value=await api(endpoint);if(active){setReadError(null);setPair(value);onState(value,channel);if(value.state!==previous.current){previous.current=value.state;if(value.state==='paired')onChange()}}}catch(e){if(active)setReadError(plainError(e))}if(active)timer=setTimeout(poll,2000)}poll();return()=>{active=false;clearTimeout(timer)}},[endpoint,onChange,onState,channel]);
  async function mutate(cancel=false){setBusy(true);setError(null);const path=cancel?`${endpoint}/cancel`:endpoint,body=cancel?{}:channel==='line'?{chat}:{secret};const signature=JSON.stringify([path,body]);if(pending.current?.signature!==signature)pending.current={signature,key:requestKey()};try{const value=await api(path,body,false,pending.current.key);pending.current=null;setPair(value);onState(value,channel);if(!cancel)onSaved()}catch(e){setError(plainError(e))}finally{setBusy(false)}}
  const waiting=pair?.state==='pending';
  const shown=error||readError;
  return <div className="channel-pairing">{shown&&<Notice error reason={shown.code}>{shown.text}</Notice>}{enabled?<p className="helper-text">已啟用，正在確認連線。更換帳號請先停用。</p>:pair?.state==='paired'?<Notice>帳號已配對。正在確認通訊連線。</Notice>:<>{waiting?<><p>從自己的帳號送出這則訊息。</p><div className="pairing-command"><pre className="pairing-code" aria-label="通訊配對指令">/pair {pair.code}</pre><CopyButton text={`/pair ${pair.code}`}>複製配對指令</CopyButton><span>3 分鐘後失效</span></div><Button secondary disabled={busy} onClick={()=>mutate(true)}>取消這次配對</Button></>:<>{pair?.state==='expired'&&<Notice error>通訊配對碼已過期，請產生新的配對碼。</Notice>}<Button disabled={busy||(channel==='line'?!chat:!secret.trim()&&!hasSecret)} onClick={()=>mutate()}>{busy?'準備配對碼…':'產生通訊配對碼'}</Button></>}</>}</div>
}

export function ChannelSettings({onChange,guided=false,initialChannel,onChannelChange}:{onChange?:()=>void;guided?:boolean;initialChannel?:string;onChannelChange?:(channel:string)=>Promise<void>}){
  const [data,setData]=useState<any>(null),[channel,setChannel]=useState('line'),[secret,setSecret]=useState(''),[chats,setChats]=useState(''),[senders,setSenders]=useState(''),[login,setLogin]=useState<any>(null),[qr,setQr]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[dirty,setDirty]=useState(false),[qrNote,setQrNote]=useState(''),[confirmDisable,setConfirmDisable]=useState(false),[readError,setReadError]=useState(''),[loginError,setLoginError]=useState('');
  const dirtyRef=useRef(false),placed=useRef(false);
  function setFormDirty(value:boolean){dirtyRef.current=value;setDirty(value)}
  const refresh=useCallback(async()=>{const value=await api('gateway');setData(value);setReadError('');return value},[]);
  const [pairChat,setPairChat]=useState(''),[pairState,setPairState]=useState<(Pairing&{channel:string})|null>(null);
  const pairingChanged=useCallback((value:Pairing,channel:string)=>setPairState({...value,channel}),[]);
  const seenEpoch=useRef(0),tokenRef=useRef<HTMLInputElement>(null);
  const pendingPair=pairState?.channel===channel&&pairState.state==='pending'?pairState:null;
  const paired=useCallback(()=>{refresh().catch(e=>setReadError((e as Error).message));onChange?.()},[refresh,onChange]);
  const pairedSaved=useCallback(()=>{setSecret('');refresh().catch(e=>setReadError((e as Error).message))},[refresh]);
  useEffect(()=>{refresh().then(value=>{placed.current=true;setChannel(initialChannel||value.preferences.primary)}).catch(e=>setReadError((e as Error).message))},[refresh,initialChannel]);
  // When the first read failed, the first later read places the saved channel.
  useEffect(()=>{if(!data||placed.current)return;placed.current=true;setChannel(initialChannel||data.preferences.primary)},[data,initialChannel]);
  useEffect(()=>{if(dirty)return;let alive=true;const timer=setInterval(()=>{api('gateway').then(value=>{if(alive){setReadError('');if(!dirtyRef.current)setData(value)}}).catch(e=>{if(alive)setReadError((e as Error).message)})},5000);return()=>{alive=false;clearInterval(timer)}},[dirty]);
  useEffect(()=>{if(guided&&channel==='line')api('gateway/line/login').then(value=>{setLogin(value);setLoginError('')}).catch(e=>setLoginError((e as Error).message))},[guided,channel]);
  useEffect(()=>{if(!data||dirty)return;const current=data.channels.find((c:any)=>c.id===channel);setChats(current?.chats.join('\n')||'');setSenders(current?.senders.join('\n')||'')},[channel,data,dirty]);
  useEffect(()=>{if(!login||lineEnded.includes(login.status))return;let alive=true;const timer=setInterval(()=>api('gateway/line/login').then(value=>{if(alive){setLogin(value);setLoginError('')}}).catch(e=>{if(alive)setLoginError((e as Error).message)}),1500);return()=>{alive=false;clearInterval(timer)}},[login?.status]);
  useEffect(()=>{let active=true;setQr('');if(login?.qr)QRCode.toDataURL(login.qr,{width:240,margin:2}).then(value=>{if(active)setQr(value)});return()=>{active=false}},[login?.qr]);
  useEffect(()=>{if(!login)return;const epoch=Number(login.qr_epoch)||0;if(epoch>0&&seenEpoch.current>0&&epoch>seenEpoch.current)setQrNote('已更新 QR code');else if(login.status!=='authenticating')setQrNote('');if(epoch>0)seenEpoch.current=epoch},[login]);
  const current=data?.channels.find((c:any)=>c.id===channel);
  useEffect(()=>{if(channel==='telegram'&&(current?.status==='invalid_token'||current?.reason==='invalid_token'))tokenRef.current?.focus()},[channel,current?.status,current?.reason]);
  const lineStatus=login?.status as string|undefined,lineSpec=lineStatus?lineRecovery[lineStatus]:undefined,lineText=lineSpec?friendlySentence(login?.message,lineSpec.text):'';
  const lineRestart=!!lineSpec;
  const telegramText=channel==='telegram'?telegramSentence(current):'';
  async function startLine(){setBusy(true);setError('');try{setLogin(await api('gateway/line/login',{}));setLoginError('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const ids=(text:string)=>Array.from(new Set(text.split(/[\s,]+/).filter(Boolean)));
  async function choose(id:string){if(id===channel||busy)return;setBusy(true);setError('');try{await onChannelChange?.(id);placed.current=true;setChannel(id);setFormDirty(false);setConfirmDisable(false);setSecret('');setNotice('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function save(enabled=true){setBusy(true);setError('');setNotice('');try{if(channel!=='web')await api(`gateway/channels/${channel}`,{enabled,secret,chats:ids(chats),senders:ids(senders)});await api('gateway/preferences',{...data.preferences,primary:channel});setSecret('');setFormDirty(false);setConfirmDisable(false);await refresh();onChange?.();setNotice(channel==='web'?'Web 已設為主要入口。':enabled?'設定已儲存，正在連接通訊服務。':'此通道已停用，Web 仍可使用。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const disableControl=confirmDisable?<Confirm sentence="停用這個通訊？" confirmLabel="停用" busy={busy} onConfirm={()=>save(false)} onCancel={()=>setConfirmDisable(false)}/>:current?.enabled?<Button secondary disabled={busy} onClick={()=>setConfirmDisable(true)}>停用此通道</Button>:null;
  const advanced=<details><summary>進階：手動設定</summary><div className="channel-fields"><label>允許的聊天室 ID<textarea rows={2} value={chats} onChange={e=>{setChats(e.target.value);setFormDirty(true)}} placeholder="每行一個 ID"/></label><label>允許的使用者 ID<textarea rows={2} value={senders} onChange={e=>{setSenders(e.target.value);setFormDirty(true)}} placeholder="每行一個 ID"/></label></div>{guided&&<div className="row-actions"><Button secondary disabled={busy||!chats.trim()||!senders.trim()} onClick={()=>save(true)}>儲存並啟用通訊</Button>{disableControl}</div>}<p className="helper-text">只有清單內的聊天室和使用者可以使用。</p></details>;
  return <section aria-label="對外通訊設定" className={`channel-settings ${guided?'channel-guided':''}`}><div className="section-heading"><h2>選擇你的通訊入口</h2></div><p>選一個平常用的入口，之後可以更換。</p>
    <div className="channel-options" role="radiogroup" aria-label="主要通訊入口">{channelOrder.map(id=><label key={id} className={`channel-option radio-card ${channel===id?'chosen':''}`}><input type="radio" name="primary-channel" value={id} checked={channel===id} disabled={busy} onChange={()=>choose(id)}/><strong>{names[id]}</strong>{hints[id]&&<small>{hints[id]}</small>}</label>)}</div>
    {error?<Notice error>{error}</Notice>:(readError||loginError)&&<Notice error>{readError||loginError}</Notice>}{notice&&<Notice>{notice}</Notice>}
    {!data?<p role="status">讀取通訊設定…</p>:<>{channel==='web'?<p>不需要其他帳號，登入後就能對話。</p>:<>
      <p className="connection-state" role="status">{names[channel]} · {connection[current?.status]||'尚未設定'}</p>
      {channel==='line'?<><p className="helper-text">LINE 使用非官方方式連線，建議用次要帳號。</p><Button hidden={(guided&&login?.status==='authenticating')||lineRestart||!!pendingPair} disabled={busy||login?.status==='authenticating'} onClick={startLine}>{login?.status==='ready'?'重新讀取 LINE 聊天室':'開始 LINE 掃碼登入'}</Button>{qrNote&&<p role="status">{qrNote}</p>}{login&&<div className={`line-login ${qr?'has-qr':''}`} role="status" data-reason={lineSpec?lineStatus:undefined}>{qr&&<img src={qr} width={240} height={240} alt="用 LINE 掃描這個 QR"/>}<div><p>{lineText||connection[login.status]||login.status}{login.profile&&` · ${login.profile}`}</p>{qr&&!lineText&&<p>用 LINE 掃描，再在手機確認。</p>}{login.pin&&<p className="line-pin-prompt">手機驗證碼 <strong className="line-pin">{login.pin}</strong></p>}{login.status==='unavailable'&&<p>請確認服務已啟動後再試。</p>}{lineSpec&&<Button type="button" disabled={busy} onClick={startLine}>{lineSpec.action}</Button>}</div></div>}{!!login?.channels?.length&&<label>{guided?'家庭聊天室':'加入家庭聊天室'}<select aria-label={guided?'家庭聊天室':'加入家庭聊天室'} value={guided?(pendingPair?.chat||pairChat):''} disabled={guided&&!!pendingPair} onChange={e=>{if(guided)setPairChat(e.target.value);else{setChats(Array.from(new Set([...ids(chats),e.target.value])).join('\n'));setFormDirty(true)}}}><option value="">選擇聊天室…</option>{login.channels.map((c:any)=><option key={c.channelId} value={c.channelId}>{c.name}{!guided&&` · ${c.channelId}`}</option>)}</select></label>}</>:<label>{names[channel]} 機器人權杖<input ref={tokenRef} type="password" autoComplete="off" aria-invalid={channel==='telegram'&&(current?.status==='invalid_token'||current?.reason==='invalid_token')||undefined} value={secret} placeholder={current?.has_secret?'已儲存；留空保留原權杖':''} onChange={e=>{setSecret(e.target.value);setFormDirty(true)}}/></label>}
      {channel==='telegram'&&telegramText&&<p className="recovery-line" role="status" data-reason={current?.reason||current?.status}><span>{telegramText}</span></p>}
      {channel==='telegram'&&!telegramText&&<p className="helper-text">{guided?'向 Telegram 的 BotFather 取得權杖，再產生配對碼。':'向 Telegram 的 BotFather 取得權杖，再填聊天室與使用者。'}</p>}
      {channel==='discord'&&<p className="helper-text">在 Discord 開發者頁建立機器人，並允許讀寫訊息。</p>}
      {guided&&['line','telegram'].includes(channel)&&<div hidden={channel==='line'&&login?.status!=='ready'&&!pendingPair&&!current?.enabled}><ChannelPairing key={channel} channel={channel} chat={pairChat} secret={secret} hasSecret={!!current?.has_secret} enabled={!!current?.enabled} onChange={paired} onSaved={pairedSaved} onState={pairingChanged}/></div>}
      {advanced}</>}
      {!guided&&<div className="row-actions"><Button disabled={busy||(channel!=='web'&&(!chats.trim()||!senders.trim()))} onClick={()=>save(true)}>{busy?'儲存中…':channel==='web'?'使用 Web 作為主要入口':'儲存並啟用通訊'}</Button>{disableControl}</div>}</>}
  </section>
}

export function ManualOnboarding({status,onChange,navigate,storageScope}:{storageScope?:string;status:any;onChange:()=>void;navigate:(view:View)=>void}){
  const [step,setStep]=useState(0),[error,setError]=useState(''),[readError,setReadError]=useState('');
  const readEpoch=useRef(0),initialRead=useRef(true),changing=useRef(false);
  const titles=['Web','智能管家','通訊','家庭','對話'];
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function read(){const epoch=readEpoch.current;if(!changing.current&&initialRead.current){try{const value=await api('gateway');if(active&&epoch===readEpoch.current){setStep(value.step||0);setReadError('');initialRead.current=false}}catch(e){if(active&&epoch===readEpoch.current)setReadError((e as Error).message)}}if(active&&initialRead.current)timer=setTimeout(read,5000)}read();return()=>{active=false;clearTimeout(timer)}},[]);
  async function go(next:number){readEpoch.current++;changing.current=true;try{await api('gateway/onboarding',{step:next});initialRead.current=false;setStep(next);setError('');setReadError('')}catch(e){setError((e as Error).message)}finally{changing.current=false}}
  return <div className="onboarding"><ol className="onboarding-steps" aria-label="首次設定進度">{titles.map((title,index)=><li key={title}><button aria-current={step===index?'step':undefined} onClick={()=>go(index)}><span className="step-number">{index+1}</span><strong>{title}</strong></button></li>)}</ol>{error&&<Notice error>{error}</Notice>}{readError&&<Notice error>{readError}</Notice>}
    <div className="onboarding-body">{step===0&&<section><h2>你的家庭管理入口已經就緒</h2><p>這台主機已可對話，家庭可稍後連接。</p><details><summary>技術資料</summary><dl className="setup-facts"><div><dt>管理介面</dt><dd>http://localhost:8099</dd></div><div><dt>管理者驗證</dt><dd>已通過這台主機的登入金鑰驗證</dd></div></dl><p>在自己的電腦執行下列指令後，開啟 localhost:8099。</p><pre>ssh -L 8099:127.0.0.1:8099 -L 8123:127.0.0.1:8123 使用者@主機</pre></details></section>}
    {step===1&&<><HermesPanel/><details><summary>進階：AI 帳號</summary><RuntimePanel only="host" onChange={onChange}/></details></>}{step===2&&<ChannelSettings onChange={onChange}/>}{step===3&&<><HostPanel storageScope={storageScope} appStatus={status} onConnected={onChange} onAgent={()=>document.getElementById('inner-agent')?.scrollIntoView({behavior:'smooth'})}/><div id="inner-agent"><RuntimePanel only="haos" onChange={onChange}/></div></>}
    {step===4&&<section><h2>開始對話</h2><p>家庭變更仍會先顯示提案，再由你批准。</p><div className="setup-facts"><p>智能管家 · 可回第二步查看登入狀態</p><p>通訊入口 · {status?.gateway?.selected?names[status.gateway.preferences.primary]:'尚未選定，Web 可直接使用'}</p><p>家庭 · {status?.ha?.status==='connected'?'已連接':'可稍後連接'}</p></div><Button onClick={()=>navigate('gateway')}>開始對話</Button></section>}</div>
    <div className="onboarding-actions">{step>0&&<Button secondary onClick={()=>go(step-1)}>上一步</Button>}{step<4&&<Button onClick={()=>go(step+1)}>{step===3?'繼續':'下一步'}</Button>}<button className="text-button" type="button" onClick={()=>navigate('gateway')}>稍後再設定</button></div></div>
}
