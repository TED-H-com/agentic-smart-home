export type View = 'home'|'inventory'|'automations'|'history'|'settings'|'host'|'onboarding'|'gateway'|'agents'|'workers';
export function currentRoute(hostMode: boolean): {view:View; task:string|null} {
  const [view, task] = location.hash.slice(1).split('/');
  const allowed = ['home','inventory','automations','history','settings', ...(hostMode?['host','onboarding','gateway','agents','workers']:[])];
  return {view: allowed.includes(view) ? view as View : hostMode?(view?'gateway':'onboarding'):'home', task: view==='home' && /^[a-zA-Z0-9_-]{1,100}$/.test(task||'') ? task : null};
}
export function SetupGuide({status,hostMode,navigate,view}:{status:any;hostMode:boolean;navigate:(view:View)=>void;view:View}) {
  if (!status) return <div className="setup-loading" role="status">正在讀取家庭與 Agent 的連線狀態…</div>;
  const paired = status.runner?.protocol === 1;
  const connected = status.runner?.providers?.some((p:any)=>p.status==='connected');
  const steps = hostMode ? [
    {title:'登入外層 Agent',detail:'Web 已就緒。先登入外層 Agent，HAOS 尚未啟動也能對話。',done:paired&&connected,view:'onboarding'},
    {title:'選擇通訊入口',detail:'Web 隨時可用；預設推薦 LINE，也可選 Discord 或 Telegram。',done:!!status.gateway?.selected,view:'onboarding'},
    {title:'連接家庭',detail:'連接 HAOS 與內層 Agent，再選擇可分享的設備。',done:status.ha?.status==='connected'&&!!status.onboarding?.policy_confirmed,view:'onboarding'},
  ] : [
    {title:'連接家庭', detail:'完成 HA 設定並連接管理者授權。', done:status.ha?.status==='connected', view:hostMode?'host':'settings'},
    {title:'登入 Agent', detail:paired?'選擇 Codex 或 Claude Code，完成官方登入。':'先連接本機 Runner，再登入你的 Agent。', done:paired&&connected, view:'settings'},
    {title:'選擇設備', detail:'勾選可分享的設備，儲存讀取與操作範圍。', done:!!status.onboarding?.policy_confirmed&&(status.policy_readable??status.policy_entities)>0, view:'inventory'},
  ];
  const next = steps.find(step=>!step.done);
  if (!next) return null;
  if(hostMode)return <section className="section-intro" aria-label="開始使用"><p>設定尚未完成</p><button className="secondary" onClick={()=>navigate('onboarding')}>繼續設定</button></section>;
  return <section className="setup-guide" aria-label="開始使用"><div className="setup-copy"><span className="eyebrow">開始使用 · {steps.filter(s=>s.done).length} / 3</span><h2>{next.title}</h2><p>{next.detail}</p>{next.view!==view?<button onClick={()=>navigate(next.view as View)}>繼續設定 <span aria-hidden>→</span></button>:<p className="helper-text">在下方完成設定，進度會自動更新。</p>}</div><ol className="setup-steps">{steps.map((step,index)=><li key={step.title}><button className={`setup-step ${step.done?'complete':''}`} onClick={()=>navigate(step.view as View)}><span className="step-number" aria-hidden>{step.done?'✓':index+1}</span><span><strong>{step.title}</strong><small>{step.done?'已完成':step===next?'下一步':'可隨時設定'}</small></span></button></li>)}</ol></section>;
}
