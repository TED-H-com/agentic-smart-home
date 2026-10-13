export type Runtime='host'|'haos';
export const names:Record<string,string>={web:'Web',line:'LINE',discord:'Discord',telegram:'Telegram',host:'主機',haos:'家庭現場',codex:'Codex',claude:'Claude Code',agy:'Antigravity',grok:'Grok Build'};
export const connection:Record<string,string>={disabled:'尚未啟用',connecting:'連線中',connected:'已連線',unavailable:'連線中斷',needs_reauth:'需要登入',idle:'尚未登入',ready:'LINE 已登入',authenticating:'等待掃碼',restoring:'正在恢復登入',failed:'登入未完成',qr_expired:'QR code 已過期',pin_failed:'PIN 未通過',login_rejected:'登入被拒絕',service_unavailable:'服務暫時無法使用',invalid_token:'權杖無效',webhook_conflict:'連線衝突',rate_limited:'暫時限制'};
export function shortCommit(value?:string|null){return typeof value==='string'&&/^[a-f0-9]{40}$/.test(value)?value.slice(0,7):'';}
