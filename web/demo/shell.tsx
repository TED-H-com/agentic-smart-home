import {useCallback,useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {HomeDemo} from './home';
import {bridgeHash,homeRoute,isNativeRoute,isOuterRoute} from './navigation';
import type {DemoModel,DemoSurface} from './model';

function frameUrl(surface:DemoSurface,hash:string){
  const url=new URL(location.href);url.searchParams.delete('surface');url.searchParams.set('frame',surface);url.hash=hash;
  return url.pathname+url.search+url.hash;
}
function DemoShell({model}:{model:DemoModel}){
  const [route,setRoute]=useState(location.hash||homeRoute);
  const native=isNativeRoute(route),assistant=route.startsWith('#/agentic-home');
  const [outerVisited,setOuterVisited]=useState(!native),[bridgeVisited,setBridgeVisited]=useState(assistant);
  const outer=useRef<HTMLIFrameElement>(null),bridge=useRef<HTMLIFrameElement>(null);
  const initialOuter=useRef(frameUrl('outer',isOuterRoute(route)?route:'#gateway'));
  const initialBridge=useRef(frameUrl('bridge',bridgeHash(assistant?route:'#/agentic-home')));
  const navigate=useCallback((value:string)=>{if(isNativeRoute(value)||isOuterRoute(value))location.hash=value},[]);
  function syncFrame(surface:DemoSurface,hash:string){
    const child=(surface==='outer'?outer:bridge).current?.contentWindow;
    if(!child||!child.__agenticDemoModel)return;
    if(child.location.hash!==hash){
      child.history.replaceState(null,'',child.location.pathname+child.location.search+hash);
      child.dispatchEvent(new HashChangeEvent('hashchange'));
    }
    child.dispatchEvent(new Event('focus'));
  }
  useEffect(()=>{
    const changed=()=>setRoute(isNativeRoute(location.hash)||isOuterRoute(location.hash)?location.hash:homeRoute);
    window.addEventListener('hashchange',changed);
    window.__agenticDemoNavigate=navigate;
    window.__agenticDemoFrameRoute=(surface,hash)=>{
      const current=location.hash;
      const active=surface==='bridge'?current.startsWith('#/agentic-home'):isOuterRoute(current);
      if(!active||!isOuterRoute(hash))return;
      if(surface==='bridge'&&!/^#(?:gateway(?:\/[a-f0-9]{64})?|home\/[A-Za-z0-9_-]{1,100})$/.test(hash))return;
      const next=surface==='bridge'?(hash==='#gateway'?'#/agentic-home':`#/agentic-home/${hash.slice(1)}`):hash;
      // The frame navigation already adds a browser history entry. Keep one Back step.
      history.replaceState(null,'',location.pathname+location.search+next);setRoute(next);
    };
    return()=>{window.removeEventListener('hashchange',changed);delete window.__agenticDemoNavigate;delete window.__agenticDemoFrameRoute};
  },[navigate]);
  useEffect(()=>{
    if(!native){setOuterVisited(true);syncFrame('outer',route)}
    else if(assistant){setBridgeVisited(true);syncFrame('bridge',bridgeHash(route))}
  },[route,native,assistant]);
  const screenshot=new URLSearchParams(location.search).get('screenshot')==='1';
  function reset(){Object.keys(sessionStorage).filter(key=>key.startsWith('agentic-')).forEach(key=>sessionStorage.removeItem(key));history.replaceState(null,'',location.pathname+location.search+homeRoute);location.reload()}
  return <div className="agentic-demo-shell">
    {!screenshot&&<div className="agentic-demo-bar"><div className="agentic-demo-label"><span>互動示範 · 虛構家庭</span>{native&&<small>HA 介面模擬</small>}</div><nav aria-label="示範入口"><a href={homeRoute} aria-current={native?'page':undefined}>家庭</a><a href="#gateway" aria-current={!native?'page':undefined}>管理與救援</a></nav><button type="button" onClick={reset}>重設</button><a href="../">回到介紹</a></div>}
    <div className="agentic-demo-native" hidden={!native}><HomeDemo model={model} route={assistant?'#/agentic-home':route} navigate={navigate}>
      {bridgeVisited&&<iframe ref={bridge} id="agentic-demo-frame-bridge" title="智能管家示範" src={initialBridge.current} sandbox="allow-scripts allow-same-origin allow-forms" onLoad={()=>{if(location.hash.startsWith('#/agentic-home'))syncFrame('bridge',bridgeHash(location.hash))}}/>}
    </HomeDemo></div>
    <div className="agentic-demo-outer" hidden={native}>{outerVisited&&<iframe ref={outer} id="agentic-demo-frame-outer" title="管理與救援示範" src={initialOuter.current} sandbox="allow-scripts allow-same-origin allow-forms" onLoad={()=>{if(isOuterRoute(location.hash))syncFrame('outer',location.hash)}}/>}</div>
  </div>;
}
export function mountDemo(model:DemoModel){createRoot(document.getElementById('root')!).render(<DemoShell model={model}/>)}
