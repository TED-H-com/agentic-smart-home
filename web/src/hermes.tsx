import {useCallback,useEffect,useState} from 'react';
import {api} from './api';
import {Button,Notice} from './ui';

const labels:Record<string,string>={needs_auth:'尚待登入',configured:'已儲存，待實際回覆驗證',ready:'已完成推論驗證',needs_reauth:'需要重新登入',unavailable:'服務尚未連線'};

export function HermesPanel(){
  const [data,setData]=useState<any>(null),[model,setModel]=useState(''),[key,setKey]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const refresh=useCallback(async()=>{const value=await api('hermes');setData(value);return value},[]);
  useEffect(()=>{refresh().then(value=>setModel(value.runtime?.model||'')).catch(e=>setError(e.message))},[refresh]);
  async function save(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');setNotice('');try{await api('hermes/auth',{provider:'openrouter',model,api_key:key});setKey('');await refresh();setNotice('登入資料已儲存。到對話窗口提出第一個問題，即可驗證模型連線。')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <section className="runtime-panel" aria-label="Hermes 協調服務"><div className="section-heading"><div><span className="eyebrow">HERMES</span><h2>登入你的協調助手</h2></div><span className="subtle" role="status">{data?.runtime?.execution_state==='stop_unconfirmed'?'工作尚未確認停止':data?labels[data.runtime?.state]||'服務尚未連線':'讀取狀態…'}</span></div>
    <p>由 Hermes 接收需求、查詢主機並彙整結果。Home Assistant、LINE 與內層 worker 都可以稍後設定。</p>
    {error&&<Notice error>{error}</Notice>}{notice&&<Notice>{notice}</Notice>}
    <form onSubmit={save}><div className="channel-fields"><label>OpenRouter 模型 ID<input required maxLength={200} value={model} onChange={e=>setModel(e.target.value)} placeholder="供應商／模型 ID"/></label><label>OpenRouter API key<input required type="password" autoComplete="off" maxLength={4096} value={key} onChange={e=>setKey(e.target.value)}/></label></div>
      <p className="helper-text">Hermes 與 coding CLI 分別登入。儲存金鑰不會呼叫模型；提出問題時，模型供應商可能計費。</p>
      <div className="row-actions"><Button type="submit" disabled={busy||!model.trim()||!key.trim()}>{busy?'儲存中…':'儲存 Hermes 登入'}</Button><Button secondary type="button" disabled={busy} onClick={()=>refresh().catch(e=>setError(e.message))}>重新檢查</Button>{data?.runtime?.state&&['configured','ready','needs_reauth'].includes(data.runtime.state)&&<Button secondary type="button" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{await api('hermes/auth/logout',{});setKey('');setNotice('Hermes 已登出。');await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}>登出 Hermes</Button>}</div>
    </form><details><summary>目前可使用的能力</summary><p>主機狀態查詢可獨立於 HA 使用。自動寫程式與派工仍待完成隔離驗證，目前不會執行。</p>{data?.workers?.map((worker:any)=><p key={worker.worker_id}>{worker.worker_id==='host-coder'?'外層 Coding Worker':'HAOS Coding Worker'} · {worker.available?'可用':'尚未啟用'}</p>)}</details>
  </section>
}
