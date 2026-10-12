import {useEffect,useRef,useState} from 'react';
import {api,haPath} from './api';
import {Button,Notice} from './ui';
import type {View} from './setup';

type Entry = {contract_version:1;state:'ready'|'setup'|'unavailable'|'unsupported';native_path:'/';reason_code:string|null};

async function readEntry():Promise<Entry>{
  const value=await api('host/home-entry');
  if(!value||typeof value!=='object'||value.contract_version!==1||value.native_path!=='/'||!['ready','setup','unavailable','unsupported'].includes(value.state)||!(value.reason_code===null||typeof value.reason_code==='string')){
    throw new Error('無法確認家庭入口，仍可手動開啟或使用主機維護。');
  }
  return value;
}

export function HomeEntry({automatic=false,completion=false,navigate}:{automatic?:boolean;completion?:boolean;navigate:(view:View)=>void}){
  const [entry,setEntry]=useState<Entry|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(true);
  const initial=useRef<Promise<Entry>|null>(null),autoAllowed=useRef<boolean|null>(null);
  const navigateRef=useRef(navigate);navigateRef.current=navigate;
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
  const ready=entry?.state==='ready',setup=entry?.state==='setup';
  return <section className="home-entry settings-section" aria-label="家庭入口">
    <h2>{completion?'設定完成':busy?'正在確認家庭入口':ready?'家庭已就緒':setup?'繼續設定你的家庭':'家庭暫時無法連接'}</h2>
    {busy?<p role="status">正在確認 Home Assistant…</p>:<p>{ready?'日常設備與自動化，直接在 Home Assistant 使用。':setup?'已完成的設定會保留。':'外層仍可使用，你可以請管家協助或檢查主機。'}</p>}
    {error&&<Notice error>{error}</Notice>}
    <div className="row-actions">
      {setup?<Button onClick={()=>navigate('onboarding')}>繼續設定</Button>:<a className={`button ${ready?'':'secondary'}`} href={haPath('/')} target="_blank" rel="noreferrer" onClick={()=>{autoAllowed.current=false}}>{ready?'進入家庭':'手動開啟 Home Assistant'} ↗</a>}
      <Button secondary onClick={()=>navigate('gateway')}>智能管家</Button>
      <button className="text-button" onClick={()=>navigate('host')}>主機維護</button>
      {!busy&&!ready&&<button className="text-button" onClick={retry}>重新檢查</button>}
    </div>
    <p className="helper-text">需要回來時，開啟桌面的 Agentic Home。</p>
  </section>
}
