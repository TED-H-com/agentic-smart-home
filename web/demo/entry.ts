import {installDemo} from './fake-api';
import {homeRoute,initialRoute,nativeLink} from './navigation';
import type {DemoSurface} from './model';
import './demo.css';

const parameters=new URLSearchParams(location.search);
const frame=parameters.get('frame');
let surface:DemoSurface|null=frame==='outer'||frame==='bridge'?frame:null;
// A pasted desktop ticket is never a demo credential or a child-frame route.
if(location.hash.startsWith('#desktop-login'))history.replaceState(null,'',`${location.pathname}${location.search}${homeRoute}`);
let parentShell:Window|null=null;
try{if(surface&&window.parent!==window&&window.parent.location.origin===location.origin&&window.frameElement?.id===`agentic-demo-frame-${surface}`&&window.parent.__agenticDemoModel)parentShell=window.parent}catch{/* Standalone frames use their own fictional state. */}
// Internal frame URLs opened directly must retain the public demo labels and navigation.
if(surface&&!parentShell){
  const url=new URL(location.href);url.searchParams.delete('frame');
  if(surface==='bridge')url.searchParams.set('surface','bridge');
  history.replaceState(null,'',url.pathname+url.search+url.hash);surface=null;
}
const model=installDemo(parentShell?.__agenticDemoModel,surface||'outer',parentShell?.__agenticDemo);
function navigate(route:string){
  if(parentShell?.__agenticDemoNavigate)parentShell.__agenticDemoNavigate(route);
  else if(surface){const url=new URL(location.href);url.searchParams.delete('frame');url.searchParams.delete('surface');url.hash=route;location.assign(url.href)}
  else location.hash=route;
}
const toast=document.createElement('div');toast.className='agentic-demo-toast';toast.setAttribute('role','status');document.body.append(toast);
let timer:ReturnType<typeof setTimeout>;
function guard(event:MouseEvent){
  if(event.type==='auxclick'&&event.button!==1)return;
  const anchor=(event.target as Element)?.closest('a');if(!anchor)return;
  const raw=anchor.getAttribute('href')||'',url=new URL(anchor.href,location.href);
  if(raw.startsWith('#'))return;
  const route=nativeLink(raw,url,surface);
  if(route){event.preventDefault();navigate(route);return}
  const host=url.hostname.toLowerCase();
  const privateHost=host==='localhost'||host.endsWith('.localhost')||host.endsWith('.local')||host.endsWith('.internal')||host.endsWith('.invalid')||!host.includes('.')||/^\d+\.\d+\.\d+\.\d+$/.test(host)||host.includes(':');
  if(url.protocol==='https:'&&!privateHost)return;
  if(anchor.closest('.agentic-demo-bar')&&raw==='../')return;
  event.preventDefault();
  toast.textContent=raw.includes('downloads/')||anchor.hasAttribute('download')?'示範中無法下載。':'此連結未包含在示範中。';
  toast.classList.add('agentic-demo-show');clearTimeout(timer);timer=setTimeout(()=>toast.classList.remove('agentic-demo-show'),3000);
}
document.addEventListener('click',guard,true);document.addEventListener('auxclick',guard,true);
if(surface){
  document.body.classList.add('agentic-demo-frame');
  // Only validated, explicit product routes are relayed to the parent demo URL.
  const frameSurface=surface;
  const reportRoute=()=>parentShell?.__agenticDemoFrameRoute?.(frameSurface,location.hash);
  window.addEventListener('hashchange',reportRoute);
  // New-conversation and unsaved-edit recovery use replaceState without hashchange.
  const replaceState=history.replaceState.bind(history);
  history.replaceState=(...args)=>{const previous=location.hash;replaceState(...args);if(previous!==location.hash)queueMicrotask(reportRoute)};
  await import('../src/main.tsx');
}else{
  const route=initialRoute(),url=new URL(location.href);url.searchParams.delete('surface');url.hash=route;
  history.replaceState(null,'',url.pathname+url.search+url.hash);
  const {mountDemo}=await import('./shell');mountDemo(model);
}
