export type View = 'entry'|'home'|'inventory'|'automations'|'history'|'settings'|'host'|'onboarding'|'gateway'|'agents'|'workers';
export function currentRoute(hostMode: boolean): {view:View; task:string|null} {
  const [view, task] = location.hash.slice(1).split('/');
  const allowed = ['home','inventory','automations','history','settings', ...(hostMode?['entry','host','onboarding','gateway','agents','workers']:[])];
  return {view: allowed.includes(view) ? view as View : hostMode?(view?'gateway':'entry'):'home', task: view==='home' && /^[a-zA-Z0-9_-]{1,100}$/.test(task||'') ? task : null};
}
export function SetupGuide({status,hostMode,navigate,view}:{status:any;hostMode:boolean;navigate:(view:View)=>void;view:View}) {
  if (!status) return <div className="setup-loading" role="status">正在讀取家庭與 AI 帳號的連線狀態…</div>;
  // A paired Runner or a once-connected home that is still starting is not unfinished setup; the waiting parts say so.
  if (status.runner?.status==='unavailable'||(status.ha?.status==='unavailable'&&status.onboarding?.policy_confirmed===true)) return null;
  const paired = status.runner?.protocol === 1;
  const connected = status.runner?.providers?.some((p:any)=>p.status==='connected');
  const steps = hostMode ? [
    {title:'登入 AI 帳號',done:paired&&connected,view:'onboarding' as View},
    {title:'選擇通訊',done:!!status.gateway?.selected,view:'onboarding' as View},
    {title:'連接家庭',done:status.ha?.status==='connected'&&!!status.onboarding?.policy_confirmed,view:'onboarding' as View},
  ] : [
    {title:'連接家庭', done:status.ha?.status==='connected', view:'settings' as View},
    {title:'登入 AI 帳號', done:paired&&connected, view:'settings' as View},
    {title:'選擇設備', done:!!status.onboarding?.policy_confirmed&&(status.policy_readable??status.policy_entities)>0, view:'inventory' as View},
  ];
  const next = steps.find(step=>!step.done);
  if (!next) return null;
  if(hostMode)return <section className="section-intro" aria-label="開始使用"><p>設定尚未完成</p><button className="secondary" onClick={()=>navigate('onboarding')}>繼續設定</button></section>;
  return <section className="setup-guide" aria-label="開始使用"><div className="section-heading"><h2>開始使用</h2>{next.view!==view&&<button className="secondary" onClick={()=>navigate(next.view)}>繼續設定</button>}</div><ol className="setup-steps">{steps.map((step,index)=><li key={step.title}><button className={`setup-step ${step.done?'complete':''}`} onClick={()=>navigate(step.view)}><span className="step-number" aria-hidden>{step.done?'✓':index+1}</span><span><strong>{step.title}</strong><small>{step.done?'已完成':step===next?'下一步':'可隨時設定'}</small></span></button></li>)}</ol></section>;
}
