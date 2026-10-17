import {useEffect,useRef,useState} from 'react';
import {api,haPath,requestKey} from './api';
import {plainError} from './labels';
import {Button,Notice,Status,timestamp} from './ui';

type HomeObservation={state:string;ipv4?:string[];ipv6?:string[];interface?:string|null;internal_url?:string|null;observed_at?:string|null;discovery_enabled?:boolean|null;default_adapter?:boolean|null};
type LanState={contract_version:1;mode:'nat'|'wired';state:string;selected_interface_id:string|null;interface:string|null;guest_mac:string|null;interfaces:{id:string;label:string;interface:string;address:string|null;network:string|null}[];vm_state:string;can_configure:boolean;limitations:string[];home?:HomeObservation};
type Job={id:string;state:string;message?:string};
const homeCopy:Record<string,string>={
  not_configured:'尚未讓家庭系統直接接入區網。',
  needs_home_setup:'接線設定已保存；完成家庭帳號與授權後，會檢查實際連線。',
  unavailable:'暫時無法核對家庭網路，恢復連線後會再讀取。',
  waiting_for_address:'家庭系統尚未取得區網位址，請確認網路線與路由器。',
  adapter_selection_required:'請到家庭網路設定，選用接上區網的網路卡來探索設備。',
  callback_address_required:'請到家庭網路設定，將本機網址設為這個區網位址。',
  ready:'已讀回家庭的區網位址、探索介面與本機網址。',
};

export function HomeLanSettings(){
  const [value,setValue]=useState<LanState|null>(null),[choice,setChoice]=useState(''),[job,setJob]=useState<Job|null>(null);
  const [busy,setBusy]=useState(false),[readError,setReadError]=useState(false),[error,setError]=useState<{text:string;code?:string}|null>(null);
  const pending=useRef<{choice:string;key:string}|null>(null),jobRef=useRef<Job|null>(null),selectionDirty=useRef(false);
  const alive=useRef(false),serial=useRef(0);
  async function refresh(){
    const request=++serial.current;
    try{
      const next=await api('network/ha-lan');
      if(next?.contract_version!==1||!Array.isArray(next.interfaces)||!['nat','wired'].includes(next.mode))throw new Error('ha_lan_contract_unavailable');
      let observed=jobRef.current;
      if(observed?.state==='running'){
        const status=await api('host/status');
        observed=status.jobs?.find((item:Job)=>item.id===observed!.id)||observed;
      }
      if(!alive.current||request!==serial.current)return;
      setValue(next);setReadError(false);
      if(!selectionDirty.current)setChoice(next.selected_interface_id||'')
      if(observed){jobRef.current=observed;setJob(observed)}
    }catch{if(alive.current&&request===serial.current)setReadError(true)}
  }
  useEffect(()=>{alive.current=true;let timer:ReturnType<typeof setTimeout>;async function poll(){await refresh();if(alive.current)timer=setTimeout(poll,5000)}void poll();return()=>{alive.current=false;++serial.current;clearTimeout(timer)}},[]);
  async function save(){
    setBusy(true);setError(null);
    if(pending.current?.choice!==choice)pending.current={choice,key:requestKey()};
    try{
      const result=await api('host/actions',{action:'configure_lan',interface_id:choice||null},false,pending.current.key);
      if(alive.current){jobRef.current=result;setJob(result);pending.current=null;selectionDirty.current=false;await refresh()}
    }catch(e){if(alive.current)setError(plainError(e))}
    finally{if(alive.current)setBusy(false)}
  }
  const running=job?.state==='running',blocked=busy||running||readError||!value?.can_configure;
  const changed=value&&(choice!==(value.selected_interface_id||'')||value.state==='needs_reconcile');
  const home=value?.home,explain=homeCopy[home?.state||'']||'家庭啟動並授權後，會檢查實際連線。';
  const failed=job&&['failed','needs_reconcile'].includes(job.state);
  return <section className="maintenance-content" aria-label="家庭區網接入" aria-busy={busy}>
    <p>讓 Home Assistant 透過網路線連到家中設備，也讓同一區網的手機開啟家庭。</p>
    {readError&&<Notice error><p>暫時無法讀取接線狀態，恢復連線後會自動重試。</p><Button secondary disabled={busy} onClick={()=>void refresh()}>重新讀取</Button></Notice>}
    {error&&<Notice error reason={error.code}>{error.text}</Notice>}
    {failed&&<Notice error reason={job.message}>{job.message&&/^[a-z][a-z0-9_]+$/.test(job.message)?plainError(job.message).text:'這次接線設定未完成，請重新讀取並核對主機。'}</Notice>}
    {!value?<p role="status">正在檢查可用的有線網路…</p>:<>
      <div className="section-heading"><h3>{value.mode==='wired'?'有線家庭網路':'基本網路'}</h3><Status value={running?'running':value.state==='needs_reconcile'?'needs_reconcile':value.state==='unavailable'?'unavailable':value.mode==='wired'?'succeeded':'not_started'} label={running?'正在保存':value.state==='needs_reconcile'?'需要核對':value.state==='unavailable'?'接線待檢查':value.mode==='wired'?'已設定接線':'尚未接入區網'}/></div>
      {value.mode==='nat'&&<p className="helper-text">目前可用雲端整合或手填設備 IP；自動探索、Matter 與設備回連仍受限制。</p>}
      {!value.can_configure&&<p className="helper-text">更改接線前，請在主機維護將家庭正常關機。期間家庭自動化會暫停。</p>}
      <form onSubmit={event=>{event.preventDefault();void save()}}>
        <label>接到家庭路由器的網路線<select value={choice} disabled={!!blocked} onChange={event=>{selectionDirty.current=true;setChoice(event.target.value);setError(null);jobRef.current=null;setJob(null)}}>
          <option value="">暫不接入，使用基本網路</option>
          {choice&&!value.interfaces.some(item=>item.id===choice)&&<option value={choice}>原網路卡目前無法使用</option>}
          {value.interfaces.map(item=><option key={item.id} value={item.id}>{item.interface}{item.address?` · ${item.address}`:''}</option>)}
        </select></label>
        {!value.interfaces.length&&<p className="helper-text">尚未找到可用的有線網路。請插上網路線，或接上 USB 網路卡；此接入方式不支援 Wi-Fi。</p>}
        {choice&&<p className="helper-text">家庭會取得自己的 IP；HA 與已開放連接埠的 App 可從區網連線。請選家用網路，並保留登入保護。主機原本的 IP 保持不變。</p>}
        <Button secondary type="submit" disabled={!!blocked||!changed}>{busy||running?'正在保存…':value.state==='needs_reconcile'?'核對並套用接線':'保存家庭接線'}</Button>
      </form>
      {value.mode==='wired'&&<div className="service-row"><p role="status">{readError?'等待重新核對家庭網路。':explain}</p>
        {value.vm_state==='shut off'&&<p>接線保存後，回到家庭系統按「啟動」。</p>}
        {home?.state==='ready'&&<p className="helper-text">接著到「裝置與服務」新增設備，並實測控制；這項檢查不代表所有設備已接通。</p>}
        {home&&['adapter_selection_required','callback_address_required'].includes(home.state)&&<a className="button secondary" href={haPath('/config/network')} target="_blank" rel="noreferrer">開啟家庭網路設定 ↗</a>}
        <details><summary>連線資料</summary><dl className="worker-evidence"><dt>主機網路卡</dt><dd>{value.interface||'尚無'}</dd><dt>家庭網路卡</dt><dd>{home?.interface||'尚未讀回'}</dd><dt>家庭區網位址</dt><dd>{[...(home?.ipv4||[]),...(home?.ipv6||[])].join('、')||'尚未讀回'}</dd><dt>家庭本機網址</dt><dd>{home?.internal_url||'尚未核對'}</dd><dt>讀回時間</dt><dd>{timestamp(home?.observed_at||undefined)}</dd></dl><p className="helper-text">重新讀回只核對網路狀態，不替你修改 Home Assistant 的網路選擇。</p></details>
      </div>}
    </>}
  </section>
}

export function HomeLanFold({setup=false}:{setup?:boolean}){
  const [open,setOpen]=useState(false);
  return <details className={setup?'bootstrap-manual':'host-fold'} onToggle={event=>setOpen(event.currentTarget.open)}><summary>{setup?'先設定家庭區網（有線設備探索）':'家庭區網接入'}</summary>{open&&<HomeLanSettings/>}</details>
}
