import {useEffect,useRef,useState} from 'react';
import {api,requestKey} from './api';
import {plainError} from './labels';
import {Button,Notice,Status,timestamp} from './ui';

type HomeObservation={state:string;reason?:string;ipv4?:string[];ipv6?:string[];interface?:string|null;internal_url?:string|null;observed_at?:string|null};
type LanState={contract_version:1;mode:'nat'|'wired';state:string;selected_interface_id:string|null;interface:string|null;guest_mac:string|null;interfaces:{id:string;label:string;interface:string;address:string|null;network:string|null}[];vm_state:string;can_configure:boolean;can_connect?:boolean;core_configuration_pending?:boolean;fallback_complete?:boolean;limitations:string[];home?:HomeObservation;ha_lan_ip?:string|null;message?:string|null};
type Job={id:string;state:string;message?:string};
const homeCopy:Record<string,string>={
  needs_home_setup:'已改用有線網路，正在確認家庭區網位址。',
  unavailable:'正在重新核對家庭網路。',
  waiting_for_address:'正在等待路由器分配家庭位址。',
  adapter_selection_required:'正在完成家庭設備探索設定。',
  callback_address_required:'正在完成家庭本機網址設定。',
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
      if(!selectionDirty.current)setChoice(next.selected_interface_id||'');
      if(observed){jobRef.current=observed;setJob(observed)}
    }catch{if(alive.current&&request===serial.current)setReadError(true)}
  }
  useEffect(()=>{alive.current=true;let timer:ReturnType<typeof setTimeout>;async function poll(){await refresh();if(alive.current)timer=setTimeout(poll,5000)}void poll();return()=>{alive.current=false;++serial.current;clearTimeout(timer)}},[]);
  async function save(disable=false){
    if(!disable&&(choiceMissing||((selectionDirty.current||value?.interfaces.length!==1)&&!choiceAvailable)))return;
    setBusy(true);setError(null);
    const selection=disable?'disabled':!reauthorization&&!selectionDirty.current&&value?.interfaces.length===1?'auto':choice;
    if(pending.current?.choice!==selection)pending.current={choice:selection,key:requestKey()};
    const body=disable?{action:'configure_lan',interface_id:null}:selection==='auto'?{action:'configure_lan'}:{action:'configure_lan',interface_id:choice};
    try{
      const result=await api('host/actions',body,false,pending.current.key);
      if(alive.current){jobRef.current=result;setJob(result);pending.current=null;await refresh()}
    }catch(e){if(alive.current)setError(plainError(e))}
    finally{if(alive.current)setBusy(false)}
  }
  const running=job?.state==='running',blocked=busy||running||readError||!(value?.can_connect??value?.can_configure);
  const choiceAvailable=!!value?.interfaces.some(item=>item.id===choice),choiceMissing=!!choice&&!choiceAvailable;
  const wired=value?.mode==='wired'&&value.state==='configured';
  const home=value?.home,ip=(home?.state==='ready'?home.ipv4?.[0]||home.ipv6?.[0]:null)||value?.ha_lan_ip;
  const reauthorization=home?.state==='unavailable'&&home.reason==='home_lan_reauthorization_required';
  const explain=reauthorization?'家庭網路設定已暫停；請再按一次「連上家庭網路」確認。':ip?`HA 已連上家庭網路：${ip}`:homeCopy[home?.state||'']||'已改用有線網路，正在確認家庭區網位址。';
  const connectionComplete=!reauthorization&&wired&&(home?.state==='ready'||value?.core_configuration_pending===true);
  const fallbackComplete=value?.fallback_complete===true&&value.mode==='nat'&&value.state==='disabled'&&value.vm_state==='running';
  const failed=job&&['failed','needs_reconcile'].includes(job.state);
  return <section className="maintenance-content" aria-label="家庭區網接入" aria-busy={busy}>
    <p>讓家庭連上家中設備；系統會自動備份、重新啟動並確認位址。</p>
    {readError&&<Notice error><p>暫時無法讀取家庭網路，恢復連線後會自動重試。</p></Notice>}
    {error&&<Notice error reason={error.code}>{error.text}</Notice>}
    {fallbackComplete&&value?.message&&!failed&&<Notice>{value.message}</Notice>}
    {failed&&<Notice error reason={job.message}>{fallbackComplete?'家庭網路未能連上，已恢復基本網路，可稍後重試。':'自動恢復尚未完成；資料已保留，可稍後重試或聯絡支援。'}</Notice>}
    {!value?<p role="status">正在檢查可用的有線網路…</p>:<>
      <div className="section-heading"><h3>家庭網路</h3><Status value={running?'running':reauthorization?'blocked':value.state==='needs_reconcile'?'needs_reconcile':wired&&ip?'succeeded':wired?'running':'not_started'} label={running?'正在連接':reauthorization?'已暫停':value.state==='needs_reconcile'?'需要核對':wired&&ip?'已連接':wired?'正在確認':'尚未接入'}/></div>
      {wired&&<p role="status">{explain}</p>}
      {wired&&home?.state==='needs_home_setup'&&ip&&<p className="helper-text">家庭已取得區網位址；完成家庭授權後會自動核對探索功能。</p>}
      {!wired&&<p className="helper-text">連接期間家庭自動化會短暫暫停；未能連上時會自動恢復基本網路。</p>}
      {choiceMissing&&<p className="helper-text">原先選擇的網路卡目前無法使用，請重新選擇。</p>}
      {(value.interfaces.length>1||selectionDirty.current||choiceMissing)&&<label>接到家庭路由器的網路線<select value={choice} disabled={!!blocked} onChange={event=>{selectionDirty.current=true;setChoice(event.target.value);setError(null);jobRef.current=null;setJob(null)}}>
        <option value="">選擇有線網路</option>
        {choiceMissing&&<option value={choice} disabled>原先選擇的網路卡無法使用</option>}
        {value.interfaces.map(item=><option key={item.id} value={item.id}>{item.interface}{item.address?` · ${item.address}`:''}</option>)}
      </select></label>}
      {!value.interfaces.length&&<p className="helper-text">請插上網路線，或接上 USB 有線網路卡。</p>}
      <Button onClick={()=>void save()} disabled={!!blocked||!!connectionComplete||choiceMissing||!value.interfaces.length||((value.interfaces.length>1||selectionDirty.current)&&!choiceAvailable)}>{busy||running?'正在連接…':'連上家庭網路'}</Button>
      {wired&&<Button secondary disabled={!!blocked} onClick={()=>void save(true)}>使用基本網路</Button>}
      {wired&&<div className="service-row">
        {home?.state==='ready'&&<p className="helper-text">可到「裝置與服務」新增設備，再實測控制。</p>}
        <details><summary>連線資料</summary><dl className="worker-evidence"><dt>主機網路卡</dt><dd>{value.interface||'尚無'}</dd><dt>家庭網路卡</dt><dd>{home?.interface||'尚未讀回'}</dd><dt>家庭區網位址</dt><dd>{[...(home?.ipv4||[]),...(home?.ipv6||[])].join('、')||value.ha_lan_ip||'尚未讀回'}</dd><dt>家庭本機網址</dt><dd>{home?.internal_url||'尚未核對'}</dd><dt>讀回時間</dt><dd>{timestamp(home?.observed_at||undefined)}</dd></dl><p className="helper-text">網路隔離會讓主機無法直接連到家庭區網位址，這是正常情況；系統透過專用管理連線核對。這項檢查不代表所有設備已接通。</p></details>
      </div>}
    </>}
  </section>
}

export function HomeLanFold({setup=false}:{setup?:boolean}){
  const [open,setOpen]=useState(false);
  return <details className={setup?'bootstrap-manual':'host-fold'} onToggle={event=>setOpen(event.currentTarget.open)}><summary>{setup?'家庭網路（有線設備探索）':'家庭區網接入'}</summary>{open&&<HomeLanSettings/>}</details>
}
