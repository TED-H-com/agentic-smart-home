import type {DemoSurface} from './model';

const outer=/^#(?:entry|gateway(?:\/(?:channels|[a-f0-9]{64}))?|home(?:\/[A-Za-z0-9_-]{1,100})?|inventory|automations|history|settings|agents|host|onboarding|workers(?:\/[a-f0-9]{32})?)$/;
export const isOuterRoute=(route:string)=>outer.test(route);
export const isNativeRoute=(route:string)=>/^#\/[a-zA-Z0-9_/-]{1,220}$/.test(route);
export const homeRoute='#/lovelace/home';
export function initialRoute(){
  const hash=location.hash;
  if(new URLSearchParams(location.search).get('surface')==='bridge')return /^#(?:home\/[A-Za-z0-9_-]{1,100}|gateway\/[a-f0-9]{64})$/.test(hash)?`#/agentic-home/${hash.slice(1)}`:'#/agentic-home';
  if(!hash||hash==='#entry'||hash.startsWith('#desktop-login'))return homeRoute;
  return isOuterRoute(hash)||isNativeRoute(hash)?hash:homeRoute;
}
export function bridgeHash(route:string){
  const tail=route.slice('#/agentic-home'.length);
  const hash=tail?`#${tail.slice(1)}`:'#gateway';
  return /^#(?:gateway(?:\/[a-f0-9]{64})?|home\/[A-Za-z0-9_-]{1,100})$/.test(hash)?hash:'#gateway';
}
export function nativeLink(raw:string,url:URL,surface:DemoSurface|null){
  const loopback=url.protocol==='http:'&&['localhost','127.0.0.1'].includes(url.hostname)&&url.port==='8123';
  const bridgeRelative=surface==='bridge'&&raw.startsWith('/')&&!raw.startsWith('//')&&url.origin===location.origin;
  if(!loopback&&!bridgeRelative)return null;
  if(url.username||url.password||url.search||url.hash)return null;
  if(url.pathname==='/')return homeRoute;
  if(url.pathname==='/config/automation/dashboard')return '#/config/automation';
  if(/^\/(?:config|lovelace)(?:\/[a-zA-Z0-9_-]+)*\/?$/.test(url.pathname))return '#'+url.pathname.replace(/\/$/,'');
  return null;
}
