import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
const req=createRequire('/workspace/package.json');
const ts=req('typescript'), React=req('react'), {renderToStaticMarkup}=req('react-dom/server');
const file='/workspace/src/app/page.tsx', raw=fs.readFileSync(file,'utf8');
const ast=ts.createSourceFile(file,raw,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const found=[];let mainClass='';
function visit(node){
 if(ts.isJsxElement(node)){
  const tag=node.openingElement.tagName.getText(ast);
  if(tag==='nav' && node.getText(ast).includes('href="/library"'))found.push(node);
  if(tag==='main')for(const a of node.openingElement.attributes.properties)if(ts.isJsxAttribute(a)&&a.name.text==='className'&&a.initializer&&ts.isStringLiteral(a.initializer))mainClass=a.initializer.text;
 }
 ts.forEachChild(node,visit);
}
visit(ast);if(found.length!==1)throw Error('navigation_source_not_unique');
const nav=found[0].getText(ast), module={exports:{}};
const js=ts.transpileModule('const Link=require("next/link").default; exports.Nav=()=>('+nav+');',{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
new Function('require','module','exports',js)(req,module,module.exports);
const htmlNav=renderToStaticMarkup(React.createElement(module.exports.Nav));
function files(dir){return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?files(path.join(dir,e.name)):[path.join(dir,e.name)]);}
const cssFiles=files('/workspace/.next/static').filter(p=>p.endsWith('.css'));if(!cssFiles.length)throw Error('compiled_css_missing');
const css=cssFiles.map(p=>fs.readFileSync(p,'utf8')).join('\n');
fs.writeFileSync('/tmp/navigation.html','<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css.replaceAll('</style','<\\/style')+'</style></head><body><main class="'+mainClass+'">'+htmlNav+'</main></body></html>');
const chrome=spawn('chromium',['--headless=new','--no-sandbox','--disable-gpu','--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir=/tmp/chrome','about:blank'],{stdio:['ignore','ignore','pipe']});
let diagnostic='';chrome.stderr.on('data',x=>{diagnostic=(diagnostic+x.toString()).slice(-4000);});
let ws;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
try{
 let port;for(let i=0;i<200;i++){if(fs.existsSync('/tmp/chrome/DevToolsActivePort')){port=fs.readFileSync('/tmp/chrome/DevToolsActivePort','utf8').split('\n')[0];break;}await sleep(100);}
 if(!port)throw Error('browser_did_not_start:'+diagnostic);
 const targets=await (await fetch('http://127.0.0.1:'+port+'/json/list')).json();const target=targets.find(t=>t.type==='page');if(!target)throw Error('page_target_missing');
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((res,rej)=>{ws.addEventListener('open',res,{once:true});ws.addEventListener('error',rej,{once:true});});
 let id=0;const pending=new Map();ws.addEventListener('message',event=>{const d=JSON.parse(event.data);if(d.id&&pending.has(d.id)){const p=pending.get(d.id);pending.delete(d.id);clearTimeout(p.timer);d.error?p.reject(Error(JSON.stringify(d.error))):p.resolve(d.result);}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;const timer=setTimeout(()=>{pending.delete(n);reject(Error('cdp_timeout:'+method));},15000);pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error('page_evaluation_failed');return r.result.value;};
 await call('Page.enable');await call('Runtime.enable');await call('Network.enable');await call('Network.setBlockedURLs',{urls:['http://*','https://*']});
 await call('Emulation.setDeviceMetricsOverride',{width:420,height:520,deviceScaleFactor:1,mobile:false});
 const observations=[];
 for(const scheme of ['light','dark']){
  await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:scheme}]});
  for(const state of ['normal','hover','pressed','keyboard-focus']){
   await call('Page.navigate',{url:'file:///tmp/navigation.html'});
   for(let i=0;i<100;i++){if(await evaluate(`document.readyState==="complete" && !!document.querySelector('nav a[href="/library"]')`))break;await sleep(100);}
   await call('Input.dispatchMouseEvent',{type:'mouseMoved',x:0,y:0});
   if(state==='keyboard-focus'){await evaluate('document.activeElement.blur()');await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});}
   const targetBox=await evaluate(`(()=>{const r=document.querySelector('nav a[href="/library"]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
   if(state==='hover'||state==='pressed')await call('Input.dispatchMouseEvent',{type:'mouseMoved',...targetBox});
   if(state==='pressed')await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...targetBox});
   await sleep(80);
   const metrics=await evaluate('(()=>{const toRGB=(color)=>{const c=document.createElement("canvas");c.width=1;c.height=1;const x=c.getContext("2d");x.fillStyle=color;x.fillRect(0,0,1,1);return [...x.getImageData(0,0,1,1).data].slice(0,3)};const a=[...document.querySelectorAll("nav a")].map(e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return {text:e.textContent.trim(),href:e.getAttribute("href"),background:s.backgroundColor,color:s.color,backgroundRGB:toRGB(s.backgroundColor),textRGB:toRGB(s.color),outline:s.outline,boxShadow:s.boxShadow,focused:e===document.activeElement,focusVisible:e.matches(":focus-visible"),hovered:e.matches(":hover"),rect:{x:r.x,y:r.y,width:r.width,height:r.height}}});const r=document.querySelector("nav").getBoundingClientRect();return {capabilities:{hover:matchMedia("(hover: hover)").matches,pointerFine:matchMedia("(pointer: fine)").matches},buttons:a,clip:{x:Math.max(0,r.x-6),y:Math.max(0,r.y-6),width:r.width+12,height:r.height+12,scale:1}};})()');
   const shot=await call('Page.captureScreenshot',{format:'png',clip:metrics.clip,captureBeyondViewport:true});const name=scheme+'-'+state+'.png';fs.writeFileSync('/out/'+name,Buffer.from(shot.data,'base64'));observations.push({scheme,state,file:name,...metrics});
   if(state==='pressed')await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,x:0,y:0});
  }
 }
 const result={status:'NAVIGATION_COMPONENT_CAPTURED_PENDING_ACCEPTANCE',sourceSha256:crypto.createHash('sha256').update(raw).digest('hex'),navigationSha256:crypto.createHash('sha256').update(nav).digest('hex'),cssFiles:cssFiles.map(p=>path.relative('/workspace',p)),viewport:{width:420,height:520},scope:'Exact home navigation JSX rendered with installed React/Next Link and actual compiled app CSS. Not full authenticated application or production-backend navigation acceptance.',network:'Docker network none plus browser HTTP/HTTPS requests blocked',observations};
 fs.writeFileSync('/out/preview-result.json',JSON.stringify(result,null,2));const imageFiles=Object.fromEntries(observations.map(o=>[o.file,fs.readFileSync("/out/"+o.file).toString("base64")]));console.log(JSON.stringify({result,files:imageFiles}));
}finally{if(ws)ws.close();chrome.kill('SIGTERM');await sleep(300);if(chrome.exitCode===null)chrome.kill('SIGKILL');}
