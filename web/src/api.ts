let csrf = '';
let hostMode = false;
let loginGeneration = 0;
export function configureDeployment(mode: string) { hostMode = mode === 'host'; }
export function haPath(path = '/') { return hostMode ? `http://localhost:8123${path}` : path; }
export function haTarget() { return hostMode ? '_blank' : '_top'; }
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function session() {
  const result = await api('session');
  csrf = result.csrf;
  return result;
}
function requestKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}
export async function api(path: string, data?: unknown, raw = false, key = requestKey()): Promise<any> {
  const generation = loginGeneration;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(`api/${path}`, {
      method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', signal: controller.signal,
      headers: data === undefined ? {} : {'Content-Type':'application/json', 'X-Agentic-CSRF':csrf, 'Idempotency-Key':key},
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({detail:'服務暫時無法回應'}));
      if (response.status === 401 && hostMode && path !== 'host/login' && generation === loginGeneration) window.dispatchEvent(new Event('agentic-session-expired'));
      throw new ApiError(typeof error.detail === 'string' ? error.detail : '操作未完成，請檢查填寫內容後再試。', response.status);
    }
    if (path === 'host/login') loginGeneration++;
    return raw ? response.blob() : response.json();
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(data === undefined ? '暫時連不上管理服務，請確認主機運作後重試。' : '尚未收到操作結果。請先查看任務或主機紀錄，確認狀態後再試。', 0);
  } finally { clearTimeout(timer); }
}
export function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
