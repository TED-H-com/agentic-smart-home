import {installDemo} from './fake-api';
import './demo.css';
installDemo();
if(!location.hash||location.hash==='#entry'||location.hash.startsWith('#desktop-login'))history.replaceState(null,'',`${location.pathname}${location.search}#gateway`);
const bar=document.createElement('div');bar.className='agentic-demo-bar';
bar.innerHTML='<span>示範模式 · 介面是真的，資料是虛構的</span><button type="button">重設</button><a href="../">回到介紹</a>';
bar.querySelector('button')!.onclick=()=>{Object.keys(sessionStorage).filter(key=>key.startsWith('agentic-')).forEach(key=>sessionStorage.removeItem(key));location.hash='gateway';location.reload()};
if(new URLSearchParams(location.search).get('screenshot')!=='1'){document.body.prepend(bar);document.body.classList.add('agentic-demo-active')}
const toast=document.createElement('div');toast.className='agentic-demo-toast';toast.setAttribute('role','status');document.body.append(toast);
let timer:ReturnType<typeof setTimeout>;
document.addEventListener('click',event=>{
  const a=(event.target as Element)?.closest('a');if(!a)return;
  const raw=a.getAttribute('href')||'',url=new URL(a.href,location.href);
  if(raw.startsWith('#'))return;
  const host=url.hostname.toLowerCase();
  const privateHost=host==='localhost'||host.endsWith('.localhost')||host.endsWith('.local')||host.endsWith('.internal')||host.endsWith('.invalid')||!host.includes('.')||/^\d+\.\d+\.\d+\.\d+$/.test(host)||host.includes(':');
  if(url.protocol==='https:'&&!privateHost)return;
  if(a.closest('.agentic-demo-bar')&&raw==='../')return;
  event.preventDefault();
  toast.textContent=raw.includes('downloads/')||a.hasAttribute('download')?'示範中無法下載。':'示範中不會開啟 Home Assistant。';
  toast.classList.add('agentic-demo-show');clearTimeout(timer);timer=setTimeout(()=>toast.classList.remove('agentic-demo-show'),3000);
},true);
await import('../src/main.tsx');
