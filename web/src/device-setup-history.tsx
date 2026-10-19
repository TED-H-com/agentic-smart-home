import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {Button,Notice,timestamp} from './ui';
import {DeviceSetupPanel,parseDeviceSetupList,setupStateNames,type DeviceSetup} from './device-setup';

/** History reads start with an explicit open; selection always pins one saved ID. */
export function DeviceSetupHistory({disabled,onShare}:{disabled:boolean;onShare:(id:string)=>void}){
  const [open,setOpen]=useState(false),[items,setItems]=useState<DeviceSetup[]|null>(null),[selected,setSelected]=useState<DeviceSetup|null>(null);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[fenced,setFenced]=useState(false);
  const mounted=useRef(true),expanded=useRef(false),generation=useRef(0),inFlight=useRef(false),revoked=useRef(false),enabled=useRef(!disabled);
  enabled.current=!disabled;
  const current=(ticket:number)=>mounted.current&&expanded.current&&!revoked.current&&generation.current===ticket;
  function close(){
    generation.current++;expanded.current=false;inFlight.current=false;
    setOpen(false);setItems(null);setSelected(null);setBusy(false);setError('');
  }
  async function load(){
    if(!expanded.current||revoked.current||inFlight.current)return;
    const ticket=++generation.current;inFlight.current=true;setBusy(true);setItems(null);setSelected(null);setError('');
    try{
      const values=parseDeviceSetupList(await api('device-setups'));
      if(current(ticket))setItems(values);
    }catch(e){if(current(ticket))setError((e as Error).message)}
    finally{if(current(ticket)){inFlight.current=false;setBusy(false)}}
  }
  function toggle(){
    if(!enabled.current||revoked.current)return;
    if(expanded.current){close();return}
    expanded.current=true;setOpen(true);void load();
  }
  useEffect(()=>{
    mounted.current=true;
    // A fresh session remounts this entry after the parent restores chat.
    const expire=()=>{revoked.current=true;setFenced(true);close()};
    const events=['agentic-session-expired','agentic-bridge-expired','agentic-bridge-unavailable'];
    events.forEach(event=>window.addEventListener(event,expire));
    return()=>{mounted.current=false;generation.current++;events.forEach(event=>window.removeEventListener(event,expire))};
  },[]);
  return <div className="device-setup-history">
    <Button secondary type="button" disabled={disabled||fenced} aria-expanded={open} onClick={toggle}>設備接入進度</Button>
    {open&&<section className="device-setup-history-panel" aria-label="設備接入進度" aria-busy={busy}>
      <div className="device-setup-history-heading"><h3>設備接入進度</h3><Button secondary type="button" onClick={close}>收起進度</Button></div>
      {error&&<Notice error>{error}</Notice>}
      {busy&&<p role="status">正在讀取接入紀錄…</p>}
      {selected?<>
        <Button secondary type="button" onClick={()=>setSelected(null)}>返回接入清單</Button>
        <DeviceSetupPanel key={selected.id} candidate={selected.candidate} exactId={selected.id} disabled={disabled||fenced} visible={!fenced} onShare={id=>{if(enabled.current&&expanded.current&&!revoked.current&&id===selected.id)onShare(id)}}/>
      </>:<>
        {items&&!items.length&&<p>還沒有接入紀錄，可以先探索家庭設備。</p>}
        {items&&items.length>0&&<ul className="device-setup-history-list">{items.map(item=><li key={item.id}><Button secondary type="button" disabled={disabled||fenced} onClick={()=>{if(enabled.current&&!revoked.current)setSelected(item)}}><strong>{item.candidate.title||'設備'}</strong><span>{setupStateNames[item.state]} · {timestamp(item.updated_at)}</span><small>紀錄 {item.id.slice(0,8)}</small></Button></li>)}</ul>}
        {!busy&&<Button secondary type="button" disabled={fenced} onClick={()=>void load()}>重新讀取進度</Button>}
      </>}
    </section>}
  </div>;
}
