// Real packaged LINE.js, no network and no LINE account. Never print the token.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {request} from 'node:http';
import {writeFile} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
const {WebSocket}=createRequire('/opt/linejs/package.json')('ws');
const base='http://127.0.0.1:3789';
function http(headers={}) {
  return new Promise((resolve,reject)=>{
    const req=request(base,{headers},res=>{res.resume();res.on('end',()=>resolve(res))});
    req.on('error',reject);req.end();
  });
}
async function ready() {
  for(let i=0;i<80;i++) {try{const res=await http();if(res.statusCode===200)return res}catch{}await delay(250)}
  throw new Error('LINE.js failed to start');
}
function frame(socket, type, action) {
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{socket.terminate();reject(new Error('Missing '+type))},10000);
    socket.on('error',reject);
    socket.on('message',data=>{const value=JSON.parse(data);if(value.type===type){clearTimeout(timer);resolve(value)}});
    if(action)socket.once('open',action);
  });
}
async function denied(path, headers) {
  const socket=new WebSocket(base+path,{headers});
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{socket.terminate();reject(new Error('Expected denied socket'))},5000);
    socket.once('unexpected-response',(_,res)=>{clearTimeout(timer);assert.equal(res.statusCode,403);res.resume();resolve()});
    socket.once('open',()=>{clearTimeout(timer);socket.close();reject(new Error('Unexpected authorized socket'))});
    socket.on('error',()=>{});
  });
}
let response=await ready();
assert.equal((await http({Host:'attacker.example:3789'})).statusCode,403);
await denied('/ws',{Origin:base});
await denied('/api/ws',{Authorization:'Bearer invalid'});
await writeFile('/config/gateway.json',JSON.stringify({enabled:true,chats:['c'+'1'.repeat(32)]}));
await delay(3000);
response=await ready();
const admin=new WebSocket(base+'/ws',{headers:{Origin:base,Cookie:response.headers['set-cookie'][0].split(';')[0]}});
const token=(await frame(admin,'api:token',()=>admin.send(JSON.stringify({type:'api:token:create'})))).token;
assert.ok(token.length>20);
admin.close();
await writeFile('/config/gateway.json',JSON.stringify({enabled:true,chats:['c'+'2'.repeat(32)]}));
await delay(3000);
response=await ready();
await denied('/api/ws',{Origin:base,Authorization:'Bearer '+token});
await denied('/api/ws',{Host:'attacker.example:3789',Authorization:'Bearer '+token});
const bot=new WebSocket(base+'/api/ws',{headers:{Authorization:'Bearer '+token}});
const state=await frame(bot,'auth:state');
assert.ok(['idle','restoring'].includes(state.state));
const forbidden=frame(bot,'error');
bot.send(JSON.stringify({type:'api:token:create',requestId:'probe'}));
await forbidden;
bot.close();
console.log(JSON.stringify({http:'passed',host_pin:'passed',admin_cookie_origin:'passed',bot_bearer_no_origin:'passed',allowlist_reload:'passed',cloud_login:'not_run',external_messages:'not_run'}));
