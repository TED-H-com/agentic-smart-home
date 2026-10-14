import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
// Only this build's default destination or an explicit CLI destination may be emptied.
export default defineConfig(({command})=>{
  const args=process.argv,flag=args.findIndex(value=>value==='--outDir');
  const explicit=flag>=0?args[flag+1]:args.find(value=>value.startsWith('--outDir='))?.slice(9);
  if(flag>=0&&!explicit)throw new Error('--outDir requires a path');
  const root=fileURLToPath(new URL('.',import.meta.url));
  const allowed=explicit?resolve(process.cwd(),explicit):resolve(root,'../../site/demo');
  return {root,base:'./',build:{outDir:allowed,emptyOutDir:true,target:'es2022'},
    plugins:[{name:'demo-output-fence',configResolved(config){if(command==='build'&&resolve(config.root,config.build.outDir)!==allowed)throw new Error('Unsafe demo output directory');}}]};
});
