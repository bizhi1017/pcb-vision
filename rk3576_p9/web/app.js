'use strict';
const profiles={smooth:{fps:8,mbps:2},source:{fps:5,mbps:12}};
const names=['缺件','污渍','划痕'],colors=['#00e650','#ff4d4f','#ffd43b'];
const el=id=>document.getElementById(id),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const channels={};let closing=false,statusGeneration=0;let phonePushState=null,phoneCaptureState=null;function captureAllowed(c){return phoneCaptureState?.channels?.[c]?.enabled!==false&&(!phoneCaptureState?.capture?.state||phoneCaptureState.capture.state==='running');}function phonePushAllowed(c){return captureAllowed(c)&&phonePushState?.[c]!==false;}function unavailableText(c){return !captureAllowed(c)?(phoneCaptureState?.capture?.state==='switching'?'采集模式切换中…':'单路模式：相机 '+c+' 已停止'):'本路手机推送已关闭';}
let saved={};try{saved=JSON.parse(localStorage.getItem('pcb-mobile-v1')||'{}');}catch(_e){}
const positionEnabled={A:saved.positionA===true,B:saved.positionB===true};
const positionStates={A:{},B:{}};
el('profile').value=profiles[saved.profile]?saved.profile:'smooth';
function persist(){try{localStorage.setItem('pcb-mobile-v1',JSON.stringify({profile:el('profile').value,A:el('enable-A').checked,B:el('enable-B').checked,sd:el('save-sd').checked,phone:el('save-phone').checked,positionA:positionEnabled.A,positionB:positionEnabled.B}));}catch(_e){}}
function blank(c,text){const canvas=el('canvas-'+c),ctx=canvas.getContext('2d');ctx.fillStyle='#091726';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.fillStyle='#fff';ctx.font=Math.max(22,canvas.width/27)+'px sans-serif';ctx.textAlign='center';ctx.fillText(text,canvas.width/2,canvas.height/2);ctx.textAlign='start';el('state-'+c).textContent=text;}
function paint(c,image,meta,boxes){const canvas=el('canvas-'+c);canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);if(!boxes)return;
 ctx.lineWidth=Math.max(2,canvas.width/400);ctx.font='bold '+Math.max(16,canvas.width/55)+'px sans-serif';
 for(const d of meta.boxes||[]){if(!Number.isInteger(d.class_id)||!colors[d.class_id]||!Array.isArray(d.box)||d.box.length!==4)continue;const[x0,y0,x1,y1]=d.box,xx=x0*canvas.width,yy=y0*canvas.height;ctx.strokeStyle=colors[d.class_id];ctx.strokeRect(xx,yy,(x1-x0)*canvas.width,(y1-y0)*canvas.height);const text=names[d.class_id]+' '+Number(d.score).toFixed(2),height=Math.max(23,canvas.width/40),width=ctx.measureText(text).width+10,x=Math.max(0,Math.min(xx,canvas.width-width)),y=Math.max(height,yy-4);ctx.fillStyle='#091826e8';ctx.fillRect(x,y-height,width,height);ctx.fillStyle=colors[d.class_id];ctx.fillText(text,x+5,y-5);}}
async function loop(c,run){const ch=channels[c];let last=0;
 while(!closing&&ch.run===run&&el('enable-'+c).checked&&!document.hidden){const start=performance.now(),profile=el('profile').value,cfg=profiles[profile];let url=null,size=0;if(!phonePushAllowed(c)){clearTimeout(ch.expiry);blank(c,unavailableText(c));await sleep(500);continue;}
  try{const controller=new AbortController();ch.controller=controller;const timeout=setTimeout(()=>controller.abort(),5000);let response;
   try{response=await fetch('/api/v2/browser/'+c+'/frame?profile='+profile+'&after='+last,{signal:controller.signal,cache:'no-store'});if(response.status===204){await sleep(1000/cfg.fps);continue;}if(!response.ok)throw Error('HTTP '+response.status);const blob=await response.blob();size=blob.size;url=URL.createObjectURL(blob);
    const image=new Image();image.src=url;await image.decode();if(ch.run!==run||document.hidden)break;if(!phonePushAllowed(c))continue;
    const meta=JSON.parse(atob(response.headers.get('X-Browser-Meta')||'e30=')),remaining=Math.max(0,Number(meta.remaining_ms||0)-(performance.now()-start));
    last=Number(response.headers.get('X-Browser-ID'));paint(c,image,meta,remaining>0);samplePosition(c,image);clearTimeout(ch.expiry);
    if(remaining>0)ch.expiry=setTimeout(()=>{if(ch.run===run&&phonePushAllowed(c))paint(c,image,meta,false);},remaining);
    el('state-'+c).textContent='实时预览';
   }finally{clearTimeout(timeout);}
  }catch(error){if(ch.run!==run||closing)break;clearTimeout(ch.expiry);blank(c,phonePushAllowed(c)?'连接中 / 暂无图像':unavailableText(c));await sleep(1000);}
  finally{if(url)URL.revokeObjectURL(url);}
  await sleep(Math.max(0,Math.max(1000/cfg.fps,size*8/cfg.mbps/1000)-(performance.now()-start)));
 }}
function restart(c){const ch=channels[c];ch.run++;if(ch.controller)ch.controller.abort();clearTimeout(ch.expiry);const enabled=el('enable-'+c).checked;el('card-'+c).hidden=!enabled;blank(c,enabled?'等待图像':'通道 '+c+' 已关闭');resetPosition(c);if(enabled&&!document.hidden&&!closing)void loop(c,ch.run);}
function layout(){el('cameras').classList.toggle('single',['A','B'].filter(c=>el('enable-'+c).checked).length===1);}
for(const c of ['A','B']){channels[c]={run:0,controller:null,expiry:null};el('enable-'+c).checked=saved[c]!==false;el('enable-'+c).addEventListener('change',()=>{restart(c);layout();persist();});restart(c);}layout();
el('profile').addEventListener('change',()=>{persist();for(const c of ['A','B'])restart(c);});
document.addEventListener('visibilitychange',()=>{for(const c of ['A','B'])restart(c);});
window.addEventListener('pagehide',()=>{closing=true;statusGeneration++;for(const c of ['A','B'])restart(c);});
window.addEventListener('pageshow',()=>{if(closing){closing=false;for(const c of ['A','B'])restart(c);void statusLoop();}});
async function requestJSON(path,body,timeout=4000){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);try{const options={signal:controller.signal,cache:'no-store'};if(body!==undefined){options.method='POST';options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}const r=await fetch(path,options);const data=await r.json();if(!r.ok)throw Error(data.error||'HTTP '+r.status);return data;}catch(error){if(error.name==='AbortError')throw Error('请求超时，操作可能已生效；请查看状态后再操作，勿连续重复提交。');throw error;}finally{clearTimeout(timer);}}
function getJSON(path){return requestJSON(path);}
let boardState=null,boardStateAt=0,configBusy=false,mutationVersion=0,hdmiDirty=false;
const actionBusy={A:null,B:null},resultUrls={A:null,B:null},resultModes={A:null,B:null};
function feedback(id,text,tone='progress'){el(id).textContent=text;el(id).dataset.tone=tone;}
function renderCameraStatus(s){for(const c of ['A','B']){const row=s.channels[c];const allowed=s.config.browser_push?.[c]!==false;el('alarm-'+c).hidden=!allowed;if(allowed)updateAlarm(c,row,!!s.config.detect[c]);el('detail-'+c).textContent='采集 '+row.fps.toFixed(1)+' fps · '+(row.online?'相机在线':'相机无信号');
 if(!s.config.detect[c])el('detection-'+c).textContent='缺陷检测：关闭';else if(row.infer_error)el('detection-'+c).textContent='检测异常：'+row.infer_error;else{const fresh=row.client_detection_fresh,counts=[0,0,0];for(const d of row.client_detections||[])if(Number.isInteger(d.class_id)&&d.class_id>=0&&d.class_id<3)counts[d.class_id]++;
 el('detection-'+c).textContent=(fresh?names.map((name,i)=>name+' '+counts[i]).join(' · '):'等待新检测结果')+'\n真实推理 '+row.infer_fps.toFixed(1)+' fps · '+(row.infer_ms==null?'—':row.infer_ms.toFixed(1)+' ms');}}}
function updateBoardState(state){const changed=phoneCaptureState&&phoneCaptureState.capture?.generation!==state.capture?.generation;phoneCaptureState=state;boardState=state;if(changed){for(const c of ['A','B']){clearResult(c);restart(c);}}phonePushState=state.config.browser_push;boardStateAt=Date.now();for(const c of ['A','B'])if(!phonePushAllowed(c)){clearTimeout(channels[c].expiry);blank(c,unavailableText(c));}renderControls();renderCameraStatus(state);}
function renderControls(){const connected=boardState&&Date.now()-boardStateAt<5000,cap=boardState?.capabilities||{};
 el('apply-hdmi').disabled=!connected||!cap.hdmi||configBusy;
 el('hdmi-mode').disabled=!connected||!cap.hdmi||configBusy;for(const option of el('hdmi-mode').options){option.disabled=option.value==='split'?!captureAllowed('A')||!captureAllowed('B'):['A','B'].includes(option.value)&&!captureAllowed(option.value);}
 const outputNames={off:'关闭',A:'相机 A',B:'相机 B',split:'A/B 双路分屏'};
 if(connected){if(!hdmiDirty&&!configBusy)el('hdmi-mode').value=boardState.config.hdmi;el('hdmi-current').textContent=cap.hdmi?'当前 HDMI：'+outputNames[boardState.config.hdmi]:'板端尚未启用 HDMI 显示服务';const sd=boardState.storage?.sd||{};el('sd-status').textContent=sd.mounted&&sd.writable?'SD 卡可写 · '+sd.snapshot_root:'SD 卡未就绪：'+(sd.error||'未挂载或不可写');}
 for(const c of ['A','B']){const row=boardState?.channels?.[c]||{},active=captureAllowed(c),enabled=!!boardState?.config?.detect?.[c],busy=actionBusy[c];
  setOperation(c,'detect','开启检测',enabled,configBusy);
  el('detect-'+c).disabled=!connected||!cap.detect||configBusy||!!busy;
  setOperation(c,'measure','测量',busy==='measure'||resultModes[c]==='measure',busy==='measure');
  el('measure-'+c).disabled=!connected||!row.online||!row.calibrated||!!row.mode_applied||!!busy||configBusy;
  setOperation(c,'position','位置检测',positionEnabled[c]);el('position-'+c).disabled=false;renderPosition(c);
  setOperation(c,'inspect','缺陷放大',busy==='inspect'||resultModes[c]==='inspect',busy==='inspect');el('inspect-'+c).disabled=!!busy||configBusy||(resultModes[c]!=='inspect'&&(!connected||!cap.defect_crops||!enabled||!row.client_detection_fresh||!(row.client_detections||[]).length));
  if(resultModes[c]==='measure'&&!busy&&!configBusy)el('measure-'+c).disabled=false;
  el('snapshot-'+c).setAttribute('aria-busy',String(busy==='snapshot'));el('snapshot-'+c).title=busy==='snapshot'?'截图中…':'截取相机 '+c+' 高清画面';
  el('snapshot-'+c).disabled=!connected||!row.online||!cap.snapshots||!!busy||!(el('save-sd').checked||el('save-phone').checked);
  if(!active){for(const name of ['detect','measure','position','inspect','snapshot'])el(name+'-'+c).disabled=true;el('position-badge-'+c)?.setAttribute('hidden','');}
  el('measure-hint-'+c).textContent=!connected?'等待设备连接':!row.calibrated?'本路尚未标定，请先在 Qt 完成棋盘标定。':row.mode_applied?'测量需彩色原图，请先在 Qt 切回彩色模式。':'已标定 · 测量时保持相机、焦距和板面高度不变。';
 }}
async function configureBoard(body,target,description){if(configBusy)return;configBusy=true;renderControls();feedback(target,description+'…');
 try{const state=await requestJSON('/api/v2/config',body,8000);mutationVersion++;if('hdmi' in body)hdmiDirty=false;updateBoardState(state);feedback(target,description+'成功，已与板端同步。','success');}
 catch(error){feedback(target,error.message,'error');}
 finally{mutationVersion++;configBusy=false;renderControls();}}
function setOperation(c,action,label,on,busy=false){const button=el(action+'-'+c);button.querySelector('.switch-label').textContent=label;button.querySelector('.switch-state').textContent=busy?'…':on?'ON':'OFF';button.setAttribute('aria-checked',String(on));button.setAttribute('aria-busy',String(busy));}
function clearResult(c,keepCrops=false){resultModes[c]=null;if(!keepCrops)el('crops-'+c).replaceChildren();el('result-'+c).hidden=true;el('result-image-'+c).removeAttribute('src');el('download-'+c).removeAttribute('href');el('open-result-'+c).removeAttribute('href');if(resultUrls[c])URL.revokeObjectURL(resultUrls[c]);resultUrls[c]=null;}
function pngBlob(encoded){if(typeof encoded!=='string'||encoded.length>32*1024*1024)throw Error('图片数据缺失或过大');const binary=atob(encoded),bytes=Uint8Array.from(binary,ch=>ch.charCodeAt(0));if(bytes.length<8||[137,80,78,71,13,10,26,10].some((n,i)=>bytes[i]!==n))throw Error('返回的 PNG 图片无效');return new Blob([bytes],{type:'image/png'});}
function showResult(c,encoded,title,filename){const blob=pngBlob(encoded),url=URL.createObjectURL(blob);clearResult(c,true);resultUrls[c]=url;el('result-title-'+c).textContent=title;el('result-image-'+c).src=url;el('download-'+c).href=url;el('download-'+c).download=filename.replace(/[\\/:\x00-\x1f]/g,'_');el('open-result-'+c).href=url;el('result-'+c).hidden=false;}
async function measure(c){if(el('measure-'+c).disabled||actionBusy[c])return;if(resultModes[c]==='measure'){clearResult(c);feedback('feedback-'+c,'测量结果已收起。');renderControls();return;}actionBusy[c]='measure';clearResult(c);renderControls();feedback('feedback-'+c,'正在测量本路 PCB 板框…');
 try{const result=await requestJSON('/api/v2/camera/'+c+'/measure',{},35000),size=result.result;if(!Number.isFinite(size?.long_mm)||!Number.isFinite(size?.short_mm))throw Error('板框尺寸返回无效');const title='板框 '+size.long_mm.toFixed(2)+' × '+size.short_mm.toFixed(2)+' mm';showResult(c,result.png,title,'measure_'+c+'_'+new Date().toISOString().replace(/[:.]/g,'-')+'.png');resultModes[c]='measure';feedback('feedback-'+c,title+'（初测）\n绿色板框定格图已生成，点击下方可放大或下载。','success');}
 catch(error){feedback('feedback-'+c,'测量失败：'+error.message,'error');}finally{actionBusy[c]=null;renderControls();}}
async function snapshot(c){if(el('snapshot-'+c).disabled||actionBusy[c])return;const sd=el('save-sd').checked,phone=el('save-phone').checked;if(!sd&&!phone)return;actionBusy[c]='snapshot';clearResult(c);renderControls();feedback('feedback-'+c,'正在获取高清截图'+(sd?'并保存 SD 卡':'')+'…');
 try{const result=await requestJSON('/api/v2/camera/'+c+'/snapshot',{sd,pc:phone,client_time:new Date().toISOString()},45000),messages=[];let failed=false;
  if(sd){if(result.sd?.ok)messages.push('SD 卡已保存：'+result.sd.path);else{failed=true;messages.push('SD 卡保存失败：'+(result.sd?.error||'未知错误'));}}
  if(phone){try{showResult(c,result.png,'高清截图 · '+result.width+'×'+result.height,result.filename);messages.push('手机图片已就绪，请点击“下载到手机”完成保存。');}catch(error){failed=true;messages.push('手机图片准备失败：'+error.message);}}
  feedback('feedback-'+c,messages.join('\n'),failed?'error':'success');
 }catch(error){feedback('feedback-'+c,'截图操作未完成：'+error.message,'error');}finally{actionBusy[c]=null;renderControls();}}
el('save-sd').checked=saved.sd!==false;el('save-phone').checked=saved.phone!==false;
for(const id of ['save-sd','save-phone'])el(id).addEventListener('change',()=>{persist();renderControls();});
el('hdmi-mode').addEventListener('change',()=>{hdmiDirty=true;});
el('apply-hdmi').addEventListener('click',()=>{void configureBoard({hdmi:el('hdmi-mode').value},'hdmi-feedback','应用 HDMI');});
for(const c of ['A','B']){el('detect-'+c).addEventListener('click',()=>{if(el('detect-'+c).disabled||!boardState)return;const enabled=!boardState.config.detect[c],body={detect:{[c]:enabled}};if(enabled&&boardState.capabilities.preprocess&&boardState.config.pre[c]!==0)body.pre={[c]:0};void configureBoard(body,'feedback-'+c,(enabled?'开启':'关闭')+'检测 '+c);});el('measure-'+c).addEventListener('click',()=>void measure(c));el('snapshot-'+c).addEventListener('click',()=>void snapshot(c));el('inspect-'+c).addEventListener('click',()=>void inspectDefects(c));el('position-'+c).addEventListener('click',()=>{positionEnabled[c]=!positionEnabled[c];resetPosition(c);persist();renderControls();});}
renderControls();
async function statusLoop(){const generation=++statusGeneration;let netAt=0;while(!closing&&generation===statusGeneration){if(!document.hidden){try{const version=mutationVersion,s=await getJSON('/api/v2/status');if(version!==mutationVersion)continue;updateBoardState(s);el('connection').textContent='服务已连接';
 if(performance.now()-netAt>10000||!netAt){const n=await getJSON('/api/v2/net');el('addresses').replaceChildren();for(const row of n.interfaces){const a=document.createElement('a');a.href=row.url;a.textContent=(row.wireless?'Wi-Fi ':'有线 ')+row.url;el('addresses').append(a);}el('network-tip').textContent=n.wifi_ready?'手机与板卡连接同一热点，使用上方 Wi-Fi 地址。':'尚无 Wi-Fi IPv4 地址；当前可通过有线地址访问。接好无线模块并连接手机热点后，这里自动更新。';netAt=performance.now();}
 }catch(_error){el('connection').textContent='连接中断，自动重试';boardStateAt=0;renderControls();for(const c of ['A','B'])showAlarm(c,'unknown');}}await sleep(1500);}}
void statusLoop();

// Unknown or stale inference never rearms the sound for an ongoing defect.
const alarmEpisodes={A:{latched:false,clearSince:null,clearSeq:null},B:{latched:false,clearSince:null,clearSeq:null}};
let soundContext=null,soundEnabled=false,lastRingAt=-Infinity;
function showAlarm(c,kind){const badge=el('alarm-'+c);badge.dataset.kind=kind;badge.textContent={defect:'⚠ 检测到缺陷',clear:'● 未发现缺陷',off:'● 检测未开启',unknown:'● 等待有效检测'}[kind];}
function alarmKind(row,enabled){if(!enabled)return 'off';if(!row.online||row.infer_error||row.mode_applied||!row.client_detection_fresh||!row.detection_frame_seq)return 'unknown';return (row.client_detections||[]).some(d=>Number.isInteger(d.class_id)&&d.class_id>=0&&d.class_id<3)?'defect':'clear';}
function updateAlarm(c,row,enabled){const e=alarmEpisodes[c],kind=alarmKind(row,enabled),now=performance.now();showAlarm(c,kind);
 if(kind==='off'){e.latched=false;e.clearSince=null;e.clearSeq=null;}
 else if(kind==='defect'){if(!e.latched&&el('enable-'+c).checked&&!document.hidden)ringAlarm();e.latched=true;e.clearSince=null;e.clearSeq=null;}
 else if(kind==='clear'){if(e.clearSince===null){e.clearSince=now;e.clearSeq=row.detection_frame_seq;}else if(now-e.clearSince>=1000&&row.detection_frame_seq!==e.clearSeq)e.latched=false;}
 else{e.clearSince=null;e.clearSeq=null;}}
function ringAlarm(test=false){if(!soundEnabled||!soundContext)return;if(soundContext.state!=='running'){soundEnabled=false;el('alarm-sound').textContent='重新开启告警铃声（试听）';el('alarm-sound').setAttribute('aria-pressed','false');el('sound-note').textContent='浏览器暂停了声音，请点击重新开启。';return;}const now=performance.now();if(!test&&now-lastRingAt<700)return;lastRingAt=now;
 for(let i=0;i<3;i++){const osc=soundContext.createOscillator(),gain=soundContext.createGain(),at=soundContext.currentTime+i*.19;osc.frequency.value=i===1?1046:784;gain.gain.setValueAtTime(0,at);gain.gain.linearRampToValueAtTime(.16,at+.012);gain.gain.linearRampToValueAtTime(0,at+.14);osc.connect(gain);gain.connect(soundContext.destination);osc.onended=()=>{osc.disconnect();gain.disconnect();};osc.start(at);osc.stop(at+.15);}}
el('alarm-sound').addEventListener('click',async()=>{const button=el('alarm-sound');if(soundEnabled){soundEnabled=false;button.textContent='开启告警铃声（试听）';button.setAttribute('aria-pressed','false');el('sound-note').textContent='铃声已关闭，画面告警仍显示。';return;}button.disabled=true;try{const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)throw Error('当前浏览器不支持声音');if(!soundContext)soundContext=new Audio();await soundContext.resume();if(soundContext.state!=='running')throw Error('请再次点击开启声音');soundEnabled=true;button.textContent='关闭告警铃声';button.setAttribute('aria-pressed','true');el('sound-note').textContent='铃声已开启；试听一次，新缺陷告警时响一次。';ringAlarm(true);}catch(error){el('sound-note').textContent=error.message;}finally{button.disabled=false;}});
setInterval(()=>{if(document.hidden||!boardStateAt||Date.now()-boardStateAt>3000)for(const c of ['A','B'])showAlarm(c,'unknown');},500);

function resetPosition(c){const state=positionStates[c];state.key=null;state.count=0;state.seen=0;state.analyzedAt=0;state.result=null;el('position-badge-'+c).hidden=true;}
function renderPosition(c){const badge=el('position-badge-'+c),state=positionStates[c],row=boardState?.channels?.[c]||{};
 badge.hidden=!positionEnabled[c]||!el('enable-'+c).checked||document.hidden||!phonePushAllowed(c);if(badge.hidden)return;
 let result=state.result;
 if(!boardStateAt||Date.now()-boardStateAt>3000||!row.online)result=null;
 else if(row.mode_applied)result={kind:'pending',text:'位置检测需彩色原图'};
 else if(performance.now()-state.seen>1500)result=null;
 badge.dataset.kind=result?.kind||'pending';badge.textContent=result?.text||'等待新的位置画面';
 badge.style.bottom=el('alarm-'+c).hidden?'8px':'42px';
}
function samplePosition(c,image){if(!positionEnabled[c])return;const state=positionStates[c],now=performance.now(),row=boardState?.channels?.[c];
 if(!row?.online||row.mode_applied||!boardStateAt||Date.now()-boardStateAt>3000){resetPosition(c);renderPosition(c);return;}
 if(now-state.analyzedAt<500)return;
 if(now-state.seen>1500){state.key=null;state.count=0;}
 state.analyzedAt=now;
 try{if(!state.canvas){state.canvas=document.createElement('canvas');state.ctx=state.canvas.getContext('2d',{willReadFrequently:true});}
  const width=240,height=Math.max(1,Math.round(image.naturalHeight*width/image.naturalWidth));state.canvas.width=width;state.canvas.height=height;
  state.ctx.drawImage(image,0,0,width,height);const pixels=state.ctx.getImageData(0,0,width,height),result=locatePCB(pixels.data,width,height),key=result.kind+':'+result.directions.join(',');
  state.count=key===state.key?state.count+1:1;state.key=key;state.result=result.kind==='unknown'||state.count>=2?result:{kind:'pending',text:'正在确认PCB位置…'};
 }catch(_error){state.key=null;state.count=0;state.result={kind:'unknown',text:'无法识别位置，请完整入镜'};}
 state.seen=now;renderPosition(c);
}
setInterval(()=>{for(const c of ['A','B'])renderPosition(c);},500);

// PCB position geometry: pure image logic, also exercised by the Node CPU test.
function locatePCB(rgba,width,height){
 const unknown={kind:'unknown',directions:[],text:'未识别到PCB，请完整入镜'},n=width*height;
 if(width<32||height<32||rgba.length!==n*4)return unknown;
 const gray=new Uint8Array(n),hist=new Uint32Array(256),mask=new Uint8Array(n),dilated=new Uint8Array(n),queue=new Int32Array(n);
 let total=0;for(let i=0;i<n;i++){const v=(77*rgba[i*4]+150*rgba[i*4+1]+29*rgba[i*4+2])>>8;gray[i]=v;hist[v]++;total+=v;}
 let weight=0,sum=0,best=-1,threshold=0;
 for(let t=0;t<256;t++){weight+=hist[t];if(!weight)continue;if(weight===n)break;sum+=t*hist[t];const delta=sum/weight-(total-sum)/(n-weight),score=weight*(n-weight)*delta*delta;if(score>best){best=score;threshold=t;}}
 threshold=Math.max(35,Math.min(190,threshold));for(let i=0;i<n;i++)mask[i]=gray[i]<=threshold?1:0;
 // Close small silk-screen/trace holes without erasing objects at image edges.
 for(let y=0;y<height;y++)for(let x=0;x<width;x++){let hit=0;for(let dy=-1;dy<=1&&!hit;dy++)for(let dx=-1;dx<=1;dx++){const xx=Math.max(0,Math.min(width-1,x+dx)),yy=Math.max(0,Math.min(height-1,y+dy));if(mask[yy*width+xx]){hit=1;break;}}dilated[y*width+x]=hit;}
 for(let y=0;y<height;y++)for(let x=0;x<width;x++){let hit=1;for(let dy=-1;dy<=1&&hit;dy++)for(let dx=-1;dx<=1;dx++){const xx=Math.max(0,Math.min(width-1,x+dx)),yy=Math.max(0,Math.min(height-1,y+dy));if(!dilated[yy*width+xx]){hit=0;break;}}mask[y*width+x]=hit;}
 let background=0,backgroundCount=0,largest=null;
 for(let i=0;i<n;i++)if(!mask[i]){background+=gray[i];backgroundCount++;}
 for(let seed=0;seed<n;seed++){if(mask[seed]!==1)continue;let head=0,tail=1,x0=width,y0=height,x1=0,y1=0,brightness=0;queue[0]=seed;mask[seed]=2;
  while(head<tail){const at=queue[head++],y=Math.floor(at/width),x=at-y*width;brightness+=gray[at];x0=Math.min(x0,x);x1=Math.max(x1,x);y0=Math.min(y0,y);y1=Math.max(y1,y);
   for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++){const xx=x+dx,yy=y+dy;if(xx<0||xx>=width||yy<0||yy>=height)continue;const next=yy*width+xx;if(mask[next]===1){mask[next]=2;queue[tail++]=next;}}}
  if(!largest||tail>largest.area)largest={area:tail,x0,y0,x1:x1+1,y1:y1+1,brightness};
 }
 if(!largest||largest.area<n*.035||largest.area>n*.92||largest.x1-largest.x0<width*.12||largest.y1-largest.y0<height*.12||backgroundCount<n*.06||background/backgroundCount-largest.brightness/largest.area<25)return unknown;
 const b=[largest.x0/width,largest.y0/height,largest.x1/width,largest.y1/height],left=b[0]<=.02,right=b[2]>=.98,top=b[1]<=.02,bottom=b[3]>=.98;
 if(left&&right||top&&bottom)return {kind:'too_large',directions:[],text:'两侧均贴边，请拉远PCB'};
 const directions=[];if(left)directions.push('right');else if(right)directions.push('left');if(top)directions.push('down');else if(bottom)directions.push('up');
 if(!directions.length){const cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2;if(cx<.4)directions.push('right');else if(cx>.6)directions.push('left');if(cy<.4)directions.push('down');else if(cy>.6)directions.push('up');}
 const words={left:'左',right:'右',up:'上',down:'下'},arrows={left:'←',right:'→',up:'↑',down:'↓'};
 return {kind:directions.length?'move':'centered',bounds:b,directions,text:directions.length?directions.map(d=>arrows[d]).join('')+' 请向'+directions.map(d=>words[d]).join('、')+'移动PCB':'PCB 位置正常'};
}
// End PCB position geometry.

async function inspectDefects(c){if(el('inspect-'+c).disabled||actionBusy[c])return;if(resultModes[c]==='inspect'){clearResult(c);feedback('feedback-'+c,'缺陷放大已收起。');renderControls();return;}actionBusy[c]='inspect';clearResult(c);renderControls();feedback('feedback-'+c,'正在获取同一检测帧的高清缺陷截图…');let url=null;
 try{const data=await requestJSON('/api/v2/camera/'+c+'/defects',{},30000);url=URL.createObjectURL(pngBlob(data.png));const image=new Image();image.src=url;await image.decode();if(image.naturalWidth!==data.width||image.naturalHeight!==data.height)throw Error('原图尺寸不匹配');const gallery=el('crops-'+c);let first=null;
 for(const [index,d] of data.detections.slice(0,16).entries()){if(!Array.isArray(d.box)||d.box.length!==4||!d.box.every(Number.isFinite))continue;const [x,y]=[Math.max(0,Math.floor(d.box[0])),Math.max(0,Math.floor(d.box[1]))],w=Math.min(data.width,Math.ceil(d.box[2]))-x,h=Math.min(data.height,Math.ceil(d.box[3]))-y;if(w<=0||h<=0)continue;
 const pick=()=>{const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;canvas.getContext('2d').drawImage(image,x,y,w,h,0,0,w,h);showResult(c,canvas.toDataURL('image/png').split(',')[1],names[d.class_id]+' '+Number(d.score).toFixed(2)+' · '+w+'×'+h+' px · 检测帧 '+data.seq,'PCB_'+c+'_Defect_f'+data.seq+'_'+(index+1)+'.png');resultModes[c]='inspect';};
 const thumb=document.createElement('canvas');thumb.width=120;thumb.height=80;const ratio=Math.min(120/w,80/h);thumb.getContext('2d').drawImage(image,x,y,w,h,(120-w*ratio)/2,(80-h*ratio)/2,w*ratio,h*ratio);const button=document.createElement('button'),icon=document.createElement('img');icon.src=thumb.toDataURL('image/png');icon.alt='缺陷截图 '+(index+1);button.append(icon,document.createTextNode((index+1)+'. '+names[d.class_id]));button.addEventListener('click',pick);gallery.append(button);if(!first)first=pick;}
 if(!first)throw Error('没有有效缺陷框');first();feedback('feedback-'+c,'高清缺陷截图已定格，点击缩略图切换；可放大查看或下载。','success');
 }catch(error){feedback('feedback-'+c,error.message,'error');}finally{if(url)URL.revokeObjectURL(url);actionBusy[c]=null;renderControls();}}
