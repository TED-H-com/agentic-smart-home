// This product reaches LINE.js only via 127.0.0.1. Pin Host on HTTP and both
// websocket endpoints so a browser cannot bypass the gateway by DNS rebinding.
import {readFile, writeFile} from 'node:fs/promises';
const path='/opt/linejs/src/http/server.ts';
let source=await readFile(path,'utf8');
for(const [before,after] of [
  ['if (!sameOrigin(request)) return false;', 'if (request.headers.host !== "127.0.0.1:3789" || !sameOrigin(request)) return false;'],
  ['if (!config.api.enabled || request.headers.origin !== undefined) return false;', 'if (request.headers.host !== "127.0.0.1:3789" || !config.api.enabled || request.headers.origin !== undefined) return false;'],
  ['if (!host || request.headers["sec-fetch-site"] === "cross-site") {', 'if (host !== "127.0.0.1:3789" || request.headers["sec-fetch-site"] === "cross-site") {'],
]) {
  if(source.split(before).length!==2) throw new Error('Pinned LINE.js source changed; review the local Host patch.');
  source=source.replace(before,after);
}
await writeFile(path,source);
