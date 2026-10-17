import {useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode, type RefObject} from 'react';
import type {DemoCandidate, DemoDevice, DemoModel} from './model';
import './home.css';

type IconName = 'home'|'assistant'|'automation'|'settings'|'menu'|'close'|'arrow'|'light'|'climate'|'sensor'|'devices'|'external';
function Icon({name, className = ''}:{name:IconName;className?:string}) {
  const paths:Record<IconName, ReactNode> = {
    home:<><path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/></>,
    assistant:<><rect x="3" y="5" width="18" height="13" rx="4"/><path d="M8 18v3l4-3M8 10h.01M16 10h.01M9 14h6"/></>,
    automation:<><path d="M5 5h6v6H5zM13 13h6v6h-6zM11 8h5v5M8 11v5h5"/></>,
    settings:<><path d="m10 3-.7 2.4-2.2 1.3-2.5-.6-2 3.5 1.8 1.8v2.5l-1.8 1.8 2 3.4 2.5-.6 2.2 1.3.7 2.2h4l.7-2.2 2.2-1.3 2.5.6 2-3.4-1.8-1.8v-2.5l1.8-1.8-2-3.5-2.5.6-2.2-1.3L14 3Z"/><circle cx="12" cy="12" r="3"/></>,
    menu:<path d="M4 6h16M4 12h16M4 18h16"/>,
    close:<path d="m6 6 12 12M6 18 18 6"/>,
    arrow:<path d="m9 5 7 7-7 7"/>,
    light:<><path d="M8 16c0-3-3-3.5-3-7a7 7 0 0 1 14 0c0 3.5-3 4-3 7ZM8 19h8M10 22h4"/><path d="M10 16v-5l2 2 2-2v5"/></>,
    climate:<><path d="M12 2v20M3.3 7l17.4 10M3.3 17 20.7 7M9 4l3 3 3-3M9 20l3-3 3 3M4 10l4-1-1-4M20 14l-4 1 1 4M4 14l4 1-1 4M20 10l-4-1 1-4"/></>,
    sensor:<><circle cx="12" cy="5" r="2"/><path d="m7 12 4-3 4 2 3-2M11 9l-1 7-4 5M10 16l5 1 2 4M3 7a8 8 0 0 1 3-4M20 5a10 10 0 0 1 1 8"/></>,
    devices:<><rect x="3" y="3" width="11" height="18" rx="2"/><path d="M7 17h3M17 7h4v14h-4M17 17h4"/></>,
    external:<><path d="M14 3h7v7M21 3l-9 9M10 5H4v15h15v-6"/></>,
  };
  return <svg className={`ha-demo-icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function useFocusDialog(ref:RefObject<HTMLElement|null>, active:boolean, close:()=>void) {
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!active || !ref.current) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const element = ref.current;
    const focusable = () => Array.from(element.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]')).filter(item => item.getClientRects().length > 0);
    (focusable()[0] || element).focus();
    const keydown = (event:KeyboardEvent) => {
      if (event.key === 'Escape') {event.preventDefault(); closeRef.current();}
      if (event.key !== 'Tab') return;
      const items = focusable(), first = items[0], last = items[items.length - 1];
      if (!first) {event.preventDefault(); element.focus(); return;}
      if (event.shiftKey && (document.activeElement === first || !element.contains(document.activeElement))) {event.preventDefault(); last.focus();}
      else if (!event.shiftKey && (document.activeElement === last || !element.contains(document.activeElement))) {event.preventDefault(); first.focus();}
    };
    document.addEventListener('keydown', keydown);
    return () => {document.removeEventListener('keydown', keydown); previouslyFocused?.focus();};
  }, [active, ref]);
}

function Dialog({title, children, close}:{title:string;children:ReactNode;close:()=>void}) {
  const ref = useRef<HTMLDivElement>(null), titleId = useId();
  useFocusDialog(ref, true, close);
  return <div className="ha-demo-modal-backdrop" onClick={event => {if (event.target === event.currentTarget) close();}}>
    <div className="ha-demo-dialog" ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
      <header><h2 id={titleId}>{title}</h2><button className="ha-demo-icon-button" onClick={close} aria-label="關閉對話框"><Icon name="close"/></button></header>
      {children}
    </div>
  </div>;
}

const roomRoutes:Record<string,string> = {'客廳':'#/lovelace/living_room','臥室':'#/lovelace/bedroom','玄關':'#/lovelace/entry'};
const routeRooms:Record<string,string> = Object.fromEntries(Object.entries(roomRoutes).map(([room,route])=>[route,room]));
function deviceIcon(device:DemoDevice):IconName {return device.domain === 'light' ? 'light' : device.domain === 'climate' ? 'climate' : 'sensor';}
function deviceState(device:DemoDevice):string {
  if (device.domain === 'light') return device.state === 'on' ? `開啟${device.brightness === undefined ? '' : ` · ${device.brightness}%`}` : '關閉';
  if (device.domain === 'climate') return device.state === 'off' ? '關閉' : `冷氣 · ${device.temperature ?? '—'} °C`;
  if (device.domain === 'binary_sensor') return device.state === 'on' ? '偵測到動作' : '沒有動作';
  return device.state;
}
function isOn(device:DemoDevice) {return device.state !== 'off' && device.state !== 'unavailable' && device.state !== 'unknown';}

function DeviceTile({device,model,details}:{device:DemoDevice;model:DemoModel;details:(id:string)=>void}) {
  const controllable = device.domain === 'light' || device.domain === 'climate';
  return <article className={`ha-demo-tile ${isOn(device) && controllable ? `ha-demo-tile-${device.domain}-on` : ''}`}>
    <div className="ha-demo-tile-top">
      {controllable ? <button className="ha-demo-device-icon" onClick={()=>model.control(device.entity_id,'toggle')} aria-label={`${device.name}電源`} aria-pressed={isOn(device)}><Icon name={deviceIcon(device)}/></button>
        : <span className="ha-demo-device-icon"><Icon name={deviceIcon(device)}/></span>}
      {controllable ? <button className="ha-demo-tile-label" onClick={()=>details(device.entity_id)} aria-label={`查看${device.name}`}><strong>{device.name}</strong><span>{deviceState(device)}</span></button>
        : <div className="ha-demo-tile-label"><strong>{device.name}</strong><span>{deviceState(device)}</span></div>}
    </div>
    {device.domain === 'light' && <label className="ha-demo-brightness"><span className="ha-demo-visually-hidden">{device.name}亮度</span><input type="range" min="0" max="100" step="1" value={device.state === 'off' ? 0 : device.brightness ?? 100} onChange={event=>model.control(device.entity_id,'brightness',Number(event.target.value))}/></label>}
    {device.domain === 'climate' && <div className="ha-demo-temperature">
      <button aria-label={`${device.name}降低溫度`} disabled={(device.temperature ?? 24) <= 16} onClick={()=>model.control(device.entity_id,'temperature',(device.temperature ?? 24)-1)}>−</button>
      <span>{device.temperature ?? '—'} °C</span>
      <button aria-label={`${device.name}提高溫度`} disabled={(device.temperature ?? 24) >= 30} onClick={()=>model.control(device.entity_id,'temperature',(device.temperature ?? 24)+1)}>+</button>
    </div>}
  </article>;
}

function DeviceDetails({device,model,close}:{device:DemoDevice;model:DemoModel;close:()=>void}) {
  return <Dialog title={device.name} close={close}>
    <div className="ha-demo-device-detail">
      <span className={`ha-demo-detail-icon ${isOn(device) ? 'ha-demo-detail-on' : ''}`}><Icon name={deviceIcon(device)}/></span>
      <p className="ha-demo-detail-state" aria-live="polite">{deviceState(device)}</p>
      <button className="ha-demo-button ha-demo-button-primary" aria-label={`${device.name}詳細電源`} aria-pressed={isOn(device)} onClick={()=>model.control(device.entity_id,'toggle')}>{isOn(device) ? '關閉' : '開啟'}</button>
    </div>
    {device.domain === 'light' && <label className="ha-demo-detail-control"><span>亮度 <strong>{device.state === 'off' ? 0 : device.brightness ?? 100}%</strong></span><input aria-label={`${device.name}詳細亮度`} type="range" min="0" max="100" value={device.state === 'off' ? 0 : device.brightness ?? 100} onChange={event=>model.control(device.entity_id,'brightness',Number(event.target.value))}/></label>}
    {device.domain === 'climate' && <div className="ha-demo-detail-control"><p>目前溫度 <strong>{device.current_temperature ?? '—'} °C</strong></p><label><span>目標溫度</span><input aria-label={`${device.name}目標溫度`} type="number" min="16" max="30" step="1" value={device.temperature ?? 24} onChange={event=>{if(event.target.value !== '') model.control(device.entity_id,'temperature',Number(event.target.value));}}/></label></div>}
    <p className="ha-demo-dialog-note">操作只會改變這個示範家庭的狀態。</p>
  </Dialog>;
}

export function HomeDemo({model,route,navigate,children}:{model:DemoModel;route:string;navigate:(route:string)=>void;children?:ReactNode}) {
  useSyncExternalStore(model.subscribe,model.getVersion);
  const home = model.home();
  const [drawerOpen,setDrawerOpen] = useState(false);
  const [selectedDevice,setSelectedDevice] = useState<string|null>(null);
  const [setup,setSetup] = useState<DemoCandidate|null>(null);
  const [notice,setNotice] = useState('');
  const sidebar = useRef<HTMLElement>(null);
  const sidebarId = useId();
  useFocusDialog(sidebar,drawerOpen,()=>setDrawerOpen(false));
  useEffect(()=>{setDrawerOpen(false);setSelectedDevice(null);setSetup(null);setNotice('');},[route]);
  useEffect(()=>{const desktop=window.matchMedia('(min-width: 901px)');const closeOnDesktop=()=>{if(desktop.matches)setDrawerOpen(false);};desktop.addEventListener('change',closeOnDesktop);return()=>desktop.removeEventListener('change',closeOnDesktop);},[]);
  const go = (next:string) => {setDrawerOpen(false);navigate(next);};
  const assistant = route === '#/agentic-home';
  const overview = route === '#/lovelace/home' || route in routeRooms;
  const integrations = route === '#/config/integrations';
  const settings = route === '#/config';
  const automations = route === '#/config/automation';
  const title = assistant ? '智能管家' : integrations ? '裝置與服務' : settings ? '設定' : automations ? '自動化' : overview ? '概覽' : '示範頁面';
  const rooms = Object.keys(roomRoutes).filter(room => !routeRooms[route] || routeRooms[route] === room);
  const device = home.devices.find(item=>item.entity_id === selectedDevice);
  const nav = (label:string,path:string,icon:IconName,active:boolean) => <button className={`ha-demo-nav-item${active ? ' ha-demo-nav-active' : ''}`} onClick={()=>go(path)} aria-current={active ? 'page' : undefined}><Icon name={icon}/><span>{label}</span></button>;
  return <div className="ha-demo">
    {drawerOpen && <div className="ha-demo-drawer-backdrop" aria-hidden="true" onClick={()=>setDrawerOpen(false)}/>}
    <aside id={sidebarId} ref={sidebar} className={`ha-demo-sidebar${drawerOpen ? ' ha-demo-sidebar-open' : ''}`} role={drawerOpen ? 'dialog' : undefined} aria-modal={drawerOpen ? true : undefined} aria-label={drawerOpen ? '家庭選單' : '家庭側欄'} tabIndex={-1} inert={!!device || !!setup}>
      <div className="ha-demo-brand"><button className="ha-demo-icon-button ha-demo-drawer-close" onClick={()=>setDrawerOpen(false)} aria-label="關閉家庭選單"><Icon name="close"/></button><span>Home Assistant</span></div>
      <nav aria-label="家庭導覽">
        {nav('概覽','#/lovelace/home','home',overview)}
        {nav('智能管家','#/agentic-home','assistant',assistant)}
        {nav('自動化','#/config/automation','automation',automations)}
      </nav>
      <div className="ha-demo-sidebar-bottom">
        {nav('設定','#/config','settings',settings || integrations)}
        <div className="ha-demo-user"><span className="ha-demo-avatar" aria-hidden="true">D</span><div><strong>Demo User</strong><span>虛構使用者</span></div></div>
        <p className="ha-demo-attribution">Home Assistant 介面模擬</p>
      </div>
    </aside>
    <div className="ha-demo-main" inert={drawerOpen || !!device || !!setup}>
      <header className="ha-demo-toolbar">
        <button className="ha-demo-icon-button ha-demo-menu" onClick={()=>setDrawerOpen(true)} aria-label="開啟家庭選單" aria-expanded={drawerOpen} aria-controls={sidebarId}><Icon name="menu"/></button>
        {assistant ? <span className="ha-demo-toolbar-context">家庭</span> : <h1>{title}</h1>}
        <button className="ha-demo-toolbar-link" onClick={()=>go('#gateway')}><Icon name="external"/><span>管理與救援</span></button>
      </header>
      <div className="ha-demo-bridge" hidden={!assistant}>{children}</div>
      {!assistant && <main className="ha-demo-content" aria-label={title}>
        {overview && <>
          <div className="ha-demo-view-heading"><div><h2>{routeRooms[route] || '我的家'}</h2><p>燈光、室溫與感應器，集中在這裡。</p></div>{routeRooms[route] && <button className="ha-demo-button" onClick={()=>go('#/lovelace/home')}>所有房間</button>}</div>
          <div className="ha-demo-room-grid">
            {rooms.map(room=><section className="ha-demo-room" key={room} aria-label={room}><h3><button onClick={()=>go(roomRoutes[room])}>{room}<Icon name="arrow"/></button></h3><div className="ha-demo-room-tiles">{home.devices.filter(item=>item.area===room).map(item=><DeviceTile key={item.entity_id} device={item} model={model} details={setSelectedDevice}/>)}</div></section>)}
          </div>
          <div className="ha-demo-overview-links"><button className="ha-demo-text-link" onClick={()=>go('#/config/integrations')}>新增與管理設備<Icon name="arrow"/></button><button className="ha-demo-text-link" onClick={()=>go('#/agentic-home')}>找智能管家協助<Icon name="arrow"/></button></div>
        </>}
        {settings && <div className="ha-demo-settings-list">
          <h2>設定家庭</h2>
          <button onClick={()=>go('#/config/integrations')}><Icon name="devices"/><span><strong>裝置與服務</strong><small>管理整合與已發現的設備</small></span><Icon name="arrow"/></button>
          <button onClick={()=>go('#/config/automation')}><Icon name="automation"/><span><strong>自動化</strong><small>查看這個示範家庭的規則</small></span><Icon name="arrow"/></button>
          <button onClick={()=>go('#inventory')}><Icon name="assistant"/><span><strong>智能管家設備授權</strong><small>選擇管家可以讀取的設備</small></span><Icon name="external"/></button>
        </div>}
        {integrations && <>
          <div className="ha-demo-settings-heading"><span className="ha-demo-current-tab">整合</span><button className="ha-demo-text-link" onClick={()=>go('#inventory')}>管家設備授權<Icon name="external"/></button></div>
          <div className="ha-demo-integrations">
            {notice && <p className="ha-demo-notice" role="status">{notice}</p>}
            <section aria-labelledby="ha-demo-discovered"><h2 id="ha-demo-discovered">已發現</h2>
              {home.candidates.length ? <div className="ha-demo-integration-grid">{home.candidates.map(candidate=><article className="ha-demo-integration-card ha-demo-discovered-card" key={candidate.id}><div className="ha-demo-integration-title"><span className="ha-demo-integration-symbol"><Icon name="devices"/></span><h3>{candidate.title}</h3></div><p>等待設定的示範設備</p><footer><button className="ha-demo-button ha-demo-button-primary" onClick={()=>setSetup(candidate)} aria-label={`設定${candidate.title}`}>設定</button></footer></article>)}</div> : <p className="ha-demo-empty">目前沒有等待設定的示範設備。</p>}
            </section>
            {home.inventorySuggestions.length>0&&<section aria-labelledby="ha-demo-manual"><h2 id="ha-demo-manual">手動新增整合</h2><p className="ha-demo-dialog-note">示範可接續管家建議；正式家庭需在「新增整合」搜尋對應品牌。</p><div className="ha-demo-integration-grid">{home.inventorySuggestions.map(candidate=><article className="ha-demo-integration-card" key={candidate.id}><div className="ha-demo-integration-title"><h3>{candidate.title}</h3></div><p>網路線索仍需核對</p><footer><button className="ha-demo-button ha-demo-button-primary" aria-label={`新增${candidate.title}`} onClick={()=>setSetup(candidate)}>新增整合</button></footer></article>)}</div></section>}
            <section aria-labelledby="ha-demo-configured"><h2 id="ha-demo-configured">已設定</h2><div className="ha-demo-integration-grid">{home.integrations.map(integration=><article className="ha-demo-integration-card" key={integration.id}><div className="ha-demo-integration-title"><span className="ha-demo-integration-symbol"><Icon name="devices"/></span><h3>{integration.title}</h3></div><p>{integration.device_count} 個設備</p><footer><span>{integration.state === 'loaded' ? '已載入' : integration.state === 'not_loaded' ? '未載入' : '狀態待確認'}</span></footer></article>)}</div></section>
          </div>
        </>}
        {automations && <div className="ha-demo-automations"><div className="ha-demo-view-heading"><div><h2>家庭規則</h2><p>規則來自同一個示範家庭，這裡提供唯讀檢視。</p></div></div>{home.automations.length ? <ul>{home.automations.map(automation=><li key={automation.id}><Icon name="automation"/><span>{automation.title}</span><span className="ha-demo-state-label">{automation.enabled ? '開啟' : '關閉'}</span></li>)}</ul> : <p className="ha-demo-empty">尚未建立示範規則。</p>}<button className="ha-demo-text-link" onClick={()=>go('#/agentic-home')}>讓智能管家協助規劃<Icon name="arrow"/></button></div>}
        {!overview && !settings && !integrations && !automations && <div className="ha-demo-unavailable"><h2>這個頁面未納入示範</h2><p>可以回到家庭，繼續體驗設備與智能管家。</p><button className="ha-demo-button ha-demo-button-primary" onClick={()=>go('#/lovelace/home')}>回到家庭</button></div>}
      </main>}
    </div>
    {device && <DeviceDetails device={device} model={model} close={()=>setSelectedDevice(null)}/>}
    {setup && <Dialog title="設定示範設備" close={()=>setSetup(null)}><div className="ha-demo-setup-body"><p><strong>{setup.title}</strong></p><p>{setup.id==='demo-pending'?'這是虛構的連接流程。完成後，示範書桌燈會出現在客廳與管家設備清單。':'這是虛構的手動設定流程。完成後可返回接入進度，明確選擇整合並讀回示範狀態。'}</p><p className="ha-demo-dialog-note">不會登入品牌帳號或連接實際設備。</p></div><footer className="ha-demo-dialog-actions"><button className="ha-demo-button" onClick={()=>setSetup(null)}>取消</button><button className="ha-demo-button ha-demo-button-primary" onClick={()=>{model.connectCandidate(setup.id);setSetup(null);setNotice(setup.id==='demo-pending'?'示範設備已加入，可在客廳查看書桌燈。':'示範整合已加入；請回到接入進度選擇設定結果。');}}>完成示範設定</button></footer></Dialog>}
  </div>;
}
