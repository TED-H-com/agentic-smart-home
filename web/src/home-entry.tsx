import {useEffect,useRef,useState} from 'react';
import {api,haPath} from './api';
import {Button,Notice} from './ui';
import type {View} from './setup';

type Entry = {contract_version:1;state:'ready'|'setup'|'unavailable'|'unsupported';native_path:'/';reason_code:string|null};
const signedCoding=(state?:string)=>['configured','inference_verified','connected'].includes(state||'');
function codingSkipped(){try{return localStorage.getItem('agentic-skip-coding-login')==='1'}catch{return false}}

async function readEntry():Promise<Entry>{
  const value=await api('host/home-entry');
  if(!value||typeof value!=='object'||value.contract_version!==1||value.native_path!=='/'||!['ready','setup','unavailable','unsupported'].includes(value.state)||!(value.reason_code===null||typeof value.reason_code==='string')){
    throw new Error('無法確認家庭入口，仍可手動開啟或使用主機維護。');
  }
  return value;
}

export function HomeEntry({automatic=false,completion=false,navigate}:{automatic?:boolean;completion?:boolean;navigate:(view:View)=>void}){
  const [entry,setEntry]=useState<Entry|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(true),[codingPrompt,setCodingPrompt]=useState(false);
  const initial=useRef<Promise<Entry>|null>(null),autoAllowed=useRef<boolean|null>(null);
  const navigateRef=useRef(navigate);navigateRef.current=navigate;
  useEffect(()=>{
    if(!completion||codingSkipped()){setCodingPrompt(false);return}
    let alive=true;
    api('workers').then(value=>{
      if(!alive)return;
      const worker=Array.isArray(value?.workers)?value.workers.find((item:any)=>item?.worker_id==='host-coder'):null;
      const provider=Array.isArray(worker?.providers)?worker.providers.find((item:any)=>item?.id==='codex'):null;
      const state=provider?.credential_state;
      setCodingPrompt(worker?.available===true&&typeof state==='string'&&!signedCoding(state));
    }).catch(()=>{if(alive)setCodingPrompt(false)});
    return()=>{alive=false};
  },[completion]);
  useEffect(()=>{
    let alive=true;
    if(!automatic)autoAllowed.current=false;
    if(autoAllowed.current===null){
      autoAllowed.current=automatic&&(!location.hash||location.hash==='#onboarding');
      // Leave an explicit outer return point before crossing to native HA.
      // Back, reload and a later login must never re-enter an automatic loop.
      if(autoAllowed.current)history.replaceState(null,'','#entry');
    }
    initial.current ||= readEntry();
    initial.current.then(value=>{
      if(!alive)return;
      setEntry(value);setError('');setBusy(false);
      if(!autoAllowed.current||location.hash!=='#entry')return;
      autoAllowed.current=false;
      if(value.state==='ready')location.assign(haPath('/'));
      else if(value.state==='setup')navigateRef.current('onboarding');
    }).catch(e=>{if(alive){autoAllowed.current=false;setError(e.message);setBusy(false)}});
    return()=>{alive=false};
  },[automatic]);
  async function retry(){
    autoAllowed.current=false;setBusy(true);setError('');
    try{setEntry(await readEntry())}catch(e){setEntry(null);setError((e as Error).message)}finally{setBusy(false)}
  }
  const ready=entry?.state==='ready',setup=entry?.state==='setup',rescue=!busy&&!ready&&!setup;
  return <section className="home-entry settings-section" aria-label="家庭入口">
    <h2>{completion?'設定完成':busy?'正在確認家庭入口':ready?'家庭已就緒':setup?'繼續完成設定':'家庭暫時無法連接'}</h2>
    {busy?<p role="status">正在確認家庭連線。</p>:<p>{ready?'日常操作直接進入家庭。':setup?'已完成的部分會保留。':'可以請智能管家協助，或檢查主機。'}</p>}
    {error&&<Notice error>{error}</Notice>}
    <div className="row-actions">
      {setup?<Button onClick={()=>navigate('onboarding')}>繼續設定</Button>:<a className={`button ${ready?'':'secondary'}`} href={haPath('/')} target="_blank" rel="noreferrer" onClick={()=>{autoAllowed.current=false}}>{ready?'進入家庭':'手動開啟 Home Assistant'} ↗</a>}
      <Button secondary onClick={()=>navigate('gateway')}>智能管家</Button>
      {rescue&&<button type="button" className="text-button" onClick={()=>navigate('host')}>主機維護</button>}
      {!busy&&!ready&&<button type="button" className="text-button" onClick={retry}>重新檢查</button>}
    </div>
    {codingPrompt&&<div className="coding-login-prompt" role="status"><span>開發助手尚未登入</span><button type="button" className="text-button" onClick={()=>navigate('workers')}>登入開發助手</button><button type="button" className="text-button" onClick={()=>{try{localStorage.setItem('agentic-skip-coding-login','1')}catch{/* storage unavailable */}setCodingPrompt(false)}}>略過</button></div>}
  </section>
}
