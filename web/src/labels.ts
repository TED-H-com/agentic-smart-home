import {ApiError} from './api';

export type Runtime='host'|'haos';
export const names:Record<string,string>={web:'Web',line:'LINE',discord:'Discord',telegram:'Telegram',host:'主機',haos:'家庭現場',codex:'Codex',claude:'Claude Code',agy:'Antigravity',grok:'Grok Build'};
export const connection:Record<string,string>={disabled:'尚未啟用',connecting:'連線中',connected:'已連線',unavailable:'連線中斷',needs_reauth:'需要登入',idle:'尚未登入',ready:'LINE 已登入',authenticating:'等待掃碼',restoring:'正在恢復登入',failed:'登入未完成',qr_expired:'QR code 已過期',pin_failed:'PIN 未通過',login_rejected:'登入被拒絕',service_unavailable:'服務暫時無法使用',invalid_token:'權杖無效',webhook_conflict:'連線衝突',rate_limited:'暫時限制'};
export function shortCommit(value?:string|null){return typeof value==='string'&&/^[a-f0-9]{40}$/.test(value)?value.slice(0,7):'';}

const errorSentence:Record<string,string>={
  coding_auth_pending:'開發助手正在登入，完成後再送出。',
  coding_job_must_stop_before_auth:'請先停止進行中的工作。',
  coding_job_or_auth_must_stop:'請先停止進行中的工作。',
  coding_job_must_stop_before_preferences:'請先停止進行中的工作。',
  coding_auth_stop_unconfirmed:'還在確認工作已停止，請稍後再試。',
  worker_stop_unconfirmed:'還在確認工作已停止，請稍後再試。',
  runner_stop_unconfirmed:'還在確認工作已停止，請稍後再試。',
  handoff_stop_unconfirmed:'還在確認工作已停止，請稍後再試。',
  worker_job_unconfirmed:'還在確認工作已停止，請稍後再試。',
  runner_identity_changed:'執行環境已重新配對，請重新整理頁面。',
  owner_authority_required:'請重新登入主機後再試。',
  owner_authority_revoked:'請重新登入主機後再試。',
  worker_authority_revoked:'請重新登入主機後再試。',
  worker_queue_full:'目前工作太多，請稍後再試。',
  case_job_budget_exhausted:'目前工作太多，請稍後再試。',
  worker_job_expired:'這份資料已過期或不存在。',
  worker_job_not_found:'這份資料已過期或不存在。',
  artifact_not_found:'這份資料已過期或不存在。',
  case_not_found_or_expired:'這份資料已過期或不存在。',
  retention_sync_pending:'正在套用保存設定，請稍後再試。',
  unsupported_coding_preference:'開發助手目前無法使用這個設定。',
  capability_expired_or_revoked:'這次授權已到期，請重新開始。',
  request_too_large:'內容太長，請縮短後再試。',
  bridge_request_too_large:'內容太長，請縮短後再試。',
  bridge_unpaired:'側欄需要重新連接，請到主機管理頁處理。',
  bridge_pairing_needs_repair:'側欄需要重新連接，請到主機管理頁處理。',
  bridge_host_unavailable:'暫時連不到主機，請稍後再試。',
  bridge_owner_required:'請以家庭管理員身分重新開啟。',
  bridge_owner_changed:'請以家庭管理員身分重新開啟。',
  bridge_authority_revoked:'請以家庭管理員身分重新開啟。',
};
const errorCode=/^[a-z][a-z0-9_]{2,80}$/;
export function plainError(e:unknown):{text:string;code?:string}{
  const message=e instanceof Error?e.message:typeof e==='string'?e:'';
  if(errorCode.test(message))return {text:errorSentence[message]||'這次沒有完成，請稍後再試。',code:message};
  const code=e instanceof ApiError&&typeof e.code==='string'&&e.code?e.code:undefined;
  return {text:message,code};
}
