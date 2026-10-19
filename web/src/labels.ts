import {ApiError} from './api';
import {errorCode,sentenceFor} from './errors';

export type Runtime='host'|'haos';
export const names:Record<string,string>={web:'Web',line:'LINE',discord:'Discord',telegram:'Telegram',host:'主機',haos:'家庭現場',codex:'Codex',claude:'Claude Code',agy:'Antigravity',grok:'Grok Build'};
export const connection:Record<string,string>={disabled:'尚未啟用',connecting:'連線中',connected:'已連線',unavailable:'連線中斷',needs_reauth:'需要登入',idle:'尚未登入',ready:'已登入',authenticating:'等待掃碼',restoring:'正在恢復登入',failed:'登入未完成',qr_expired:'QR 已過期',pin_failed:'PIN 未通過',login_rejected:'登入被拒絕',service_unavailable:'服務暫時無法使用',invalid_token:'權杖無效',webhook_conflict:'連線衝突',rate_limited:'暫時限制'};
export function shortCommit(value?:string|null){return typeof value==='string'&&/^[a-f0-9]{40}$/.test(value)?value.slice(0,7):'';}

export function plainError(e:unknown):{text:string;code?:string}{
  if(e instanceof ApiError)return {text:e.message,code:typeof e.code==='string'&&e.code?e.code:undefined};
  const message=typeof e==='string'?e:e instanceof Error?e.message:'';
  if(errorCode.test(message))return {text:sentenceFor(message),code:message};
  return {text:message};
}
export function stateName(state:string){return state==='on'?'開啟':state==='off'?'關閉':state==='unavailable'?'無法使用':state==='unknown'?'狀態未知':state}

export type AppUpdateView={chip:string;sentence:string;action:'update'|'recheck'|'none';tone:string};
type AppUpdate={available?:boolean;from?:string|null;to?:string|null;blocked_code?:string|null}|null;
type AppStatus={code?:string|null;operation?:{kind?:string|null;state?:string|null;job_id?:string|null}|null;update?:AppUpdate};

/** Owner-facing update row. Null keeps the caller's existing state copy. */
export function appUpdateView(app:string,prefix:'bridge'|'home_worker',record?:AppStatus|null):AppUpdateView|null{
  if(!record)return null;
  const code=record.code||'',update=record.update,blocked=!!update?.blocked_code;
  const from=update?.from||'未知',to=update?.to||'未知';
  if(record.operation?.kind==='update')return {chip:'更新中',sentence:`正在更新${app}，通常需要幾分鐘。`,action:'none',tone:'s-pending'};
  if(update?.available&&!blocked&&code===`${prefix}_update_available`)return {chip:'有新版本',sentence:`${app} 可以更新到 ${to}（目前 ${from}）。`,action:'update',tone:'s-pending'};
  if(update?.available&&!blocked&&code===`${prefix}_update_failed`)return {chip:'更新未完成',sentence:`上次更新沒有完成，目前仍是 ${from}，可以再更新一次。`,action:'update',tone:'s-failed'};
  if(code===`${prefix}_update_starting`)return {chip:'啟動中',sentence:`${app} 已更新，正在啟動。`,action:'none',tone:'s-pending'};
  if(code===`${prefix}_release_not_published`)return {chip:'等待中',sentence:'目前還沒取得需要的版本，可以重新檢查。',action:'recheck',tone:'s-pending'};
  if(code===`${prefix}_release_ahead`)return {chip:'需要更新主機',sentence:'App 商店的版本比主機新，請先更新主機。',action:'none',tone:'s-failed'};
  if(code===`${prefix}_release_channel_changed`)return {chip:'需要處理',sentence:`目前裝的是舊發行頻道的${app}。請在 Home Assistant 移除它，再回來重新啟用。`,action:'recheck',tone:'s-needs_reconcile'};
  if(code===`${prefix}_update_app_missing`||new RegExp(`^${prefix}_update_[a-z0-9]+_mismatch$`).test(code))return {chip:'需要處理',sentence:`更新後的${app}和預期不同，需要你確認。`,action:'recheck',tone:'s-needs_reconcile'};
  if(code===`${prefix}_release_unparsable`)return {chip:'需要處理',sentence:'App 商店的版本資料無法判讀，需要你確認。',action:'recheck',tone:'s-needs_reconcile'};
  return null;
}
export function updateVersionLine(update?:{from?:string|null;to?:string|null}|null){
  if(!update||typeof update!=='object'||(!update.from&&!update.to))return '';
  return `${update.from||'—'} → ${update.to||'—'}`;
}
export function operationLine(operation?:{kind?:string|null;state?:string|null;job_id?:string|null}|null){
  if(!operation?.kind)return '';
  return [operation.kind,operation.state,operation.job_id].filter(Boolean).join(' · ');
}
