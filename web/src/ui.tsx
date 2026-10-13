import {useEffect,useState, type ReactNode} from 'react';
import type React from 'react';
export const labels: Record<string,string> = {staged:'待確認更新',staging:'檢查版本中',deploying:'更新中',deployed:'已更新',rolling_back:'退版中',rolled_back:'已退版',dispatching:'正在派工',cancel_requested:'正在停止',stop_unconfirmed:'停止尚未確認',running:'處理中',blocked:'需要處理',completed:'已完成',loading:'讀取中',queued:'排隊中', planning:'分析中', awaiting_user:'需要補充', awaiting_approval:'等待批准', executing:'部署中', verifying:'等待真實觸發', succeeded:'已完成', failed:'未完成', cancelled:'已取消', needs_reconcile:'需要核對', connected:'已連線', needs_reauth:'需要登入', unavailable:'無法連線', unpaired:'尚未配對', pending:'等待登入', checking:'確認中', expired:'已過期', not_started:'尚未開始', waiting_for_user:'等待設定', skipped:'已略過', ready_for_voice_test:'可進行語音測試', user_reported_connected:'已記錄你的實測'};
export const timestamp = (date?:string) => date ? new Date(date).toLocaleString('zh-TW', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '尚無觀測';
export function Status({value}:{value:string}) {return <span className={`status s-${value}`}>{labels[value] || value}</span>}
export function Notice({children, error=false}:{children:React.ReactNode; error?:boolean}) {return <div role={error?'alert':'status'} className={`notice ${error?'error':''}`}>{children}</div>}
export function Button({children, secondary=false, danger=false, ...props}:React.ButtonHTMLAttributes<HTMLButtonElement>&{secondary?:boolean;danger?:boolean}) {return <button {...props} className={`${secondary?'secondary':''} ${danger?'danger':''} ${props.className||''}`}>{children}</button>}
export function Empty({title, children, action}:{title:string;children:React.ReactNode;action?:React.ReactNode}) {return <div className="empty"><span className="empty-rule"/><h3>{title}</h3><p>{children}</p>{action&&<div className="empty-action">{action}</div>}</div>}

/** A short Traditional Chinese sentence from the API, or the local fallback. */
export function friendlySentence(message: unknown, fallback: string) {
  if (typeof message === 'string') {
    const text = message.trim();
    const han = text.match(/\p{Script=Han}/gu);
    if ((han?.length || 0) >= 4 && text.length <= 40 && !/[A-Za-z0-9]+_[A-Za-z0-9_]+/.test(text) && !/\b(Hermes|CLI|Runner|Controller|worker|digest|fixture|observed_from)\b/.test(text)) return text;
  }
  return fallback;
}

export function CopyButton({text,children='複製'}:{text:string;children?:ReactNode}) {
  const [message,setMessage]=useState('');
  useEffect(()=>setMessage(''),[text]);
  return <span className="copy-control"><Button secondary type="button" onClick={async()=>{
    try {await navigator.clipboard.writeText(text);setMessage('已複製')}
    catch {setMessage('無法自動複製，請手動選取')}
  }}>{children}</Button><span className="helper-text" role="status">{message}</span></span>
}
