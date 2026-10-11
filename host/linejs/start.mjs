// Supervise a pinned LINE.js, reloading only the product's typed allowlist file.
import {readFile, writeFile, symlink} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';
process.umask(0o077);
await symlink('/opt/linejs/dist', '/data/dist').catch(error=>{if(error.code!=='EEXIST')throw error});
let child, stopping=false;
async function stop() {
  if(!child || child.exitCode!==null)return;
  const ended=new Promise(resolve=>child.once('exit',resolve));
  child.kill('SIGTERM');
  const timeout=setTimeout(()=>child?.kill('SIGKILL'),10000);
  await ended;
  clearTimeout(timeout);
}
for(const signal of ['SIGTERM','SIGINT']) process.on(signal,()=>{stopping=true;void stop()});
let last;
while(!stopping){
  let config;
  try { config=JSON.parse(await readFile('/config/gateway.json','utf8')); }
  catch {config={enabled:false,chats:[]};}
  if(typeof config.enabled!=='boolean'||!Array.isArray(config.chats)||config.chats.length>20||
     config.chats.some(id=>!/^([ucrs])[0-9a-f]{32}$/.test(id))||config.enabled&&!config.chats.length){
    console.error('Invalid gateway configuration; LINE disabled.');
    config={enabled:false,chats:[]};
  }
  const serialized=JSON.stringify(config);
  if(serialized!==last||!child||child.exitCode!==null){
    await stop();
    const settings={server:{host:'127.0.0.1',port:3789},line:{device:'ANDROIDSECONDARY'},
      history:{defaultLimit:20},cache:{messagesPerChannel:50,mediaMaxBytes:10485760},
      limits:{frameMaxBytes:262144,textMaxLength:8000,sendsPerSecond:2,uploadMaxBytes:1048576,uploadsPerMinute:1},
      chat:{sendReadReceipts:false},update:{check:false},api:{...config,sendsPerMinute:10}};
    await writeFile('/data/config.yaml',JSON.stringify(settings),{mode:0o600});
    child=spawn('node',['/opt/linejs/dist/main.js'],{cwd:'/data',stdio:['ignore','inherit','inherit']});
    last=serialized;
  }
  await delay(2000);
}
await stop();
