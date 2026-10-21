// Actual recorded samples only. Drawing a box never confirms identity.
let traceEpoch=0, traceObservation=null, traceData=null, tracePreview=null;
const tc=$('traceCanvas'), tctx=tc.getContext('2d');
const tz=$('traceCropCanvas'), tzctx=tz.getContext('2d');
function resetTrace(observation=null){
  if(typeof resetAppearance==='function')resetAppearance();
  traceEpoch++;traceObservation=observation;traceData=null;tracePreview=null;
  $('traceReview').hidden=true;$('traceControls').hidden=true;
  $('traceStatus').textContent='';$('tracePointInfo').textContent='';
  $('tracePoint').innerHTML='';tc.removeAttribute('data-loaded');
  tz.removeAttribute('data-loaded');tzctx.clearRect(0,0,tz.width,tz.height);
  tctx.clearRect(0,0,tc.width,tc.height);
  $('openTrace').disabled=!observation;
  $('traceCorrect').disabled=true;$('traceAdd').disabled=true;
}
function traceFailure(message){
  tracePreview=null;tc.removeAttribute('data-loaded');
  tz.removeAttribute('data-loaded');tzctx.clearRect(0,0,tz.width,tz.height);
  tctx.clearRect(0,0,tc.width,tc.height);
  $('tracePointInfo').textContent=message;
  $('traceCorrect').disabled=true;$('traceAdd').disabled=true;
}
function syncTraceActions(){
  const task=state.tasks.find(t=>t.id===selectedTask);
  const usable=tracePreview&&tracePreview.task===selectedTask&&task?.status==='active'&&task.version===traceData?.task_version;
  $('traceCorrect').disabled=!usable||traceData?.superseded;
  $('traceAdd').disabled=!usable;
  if(typeof syncAppearanceActions==='function')syncAppearanceActions();
}
async function showTracePoint(index){
  const epoch=++traceEpoch, data=traceData, task=selectedTask;
  if(!data||!data.samples[index])return;
  const point=data.samples[index];
  traceFailure('正在核对源画面…');
  $('tracePoint').value=String(index);$('traceSlider').value=String(index);
  $('tracePrevious').disabled=index===0;
  $('traceNext').disabled=index===data.samples.length-1;
  try{
    const result=await api('/api/trace-frame?'+new URLSearchParams({
      task,observation:data.observation.id,sample:point.id}));
    if(epoch!==traceEpoch||task!==selectedTask)return;
    if(result.task_version!==data.task_version)throw Error('任务已变化，请刷新后重新打开本片段');
    const img=new Image();
    await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(Error('采样原画面读取失败'));img.src=result.image_data_url;});
    if(epoch!==traceEpoch||task!==selectedTask)return;
    tc.width=img.naturalWidth;tc.height=img.naturalHeight;
    tctx.drawImage(img,0,0);const b=result.sample.detection.box;
    tctx.strokeStyle='#e24639';tctx.lineWidth=Math.max(3,tc.width/300);
    tctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);
    const margin=Math.max(20,(b[3]-b[1])*.15);
    const x=Math.max(0,Math.floor(b[0]-margin)),y=Math.max(0,Math.floor(b[1]-margin));
    const width=Math.min(tc.width,Math.ceil(b[2]+margin))-x;
    const height=Math.min(tc.height,Math.ceil(b[3]+margin))-y;
    if(width<=0||height<=0)throw Error('检测框不在源画面内，无法显示局部画面');
    // Enlarge a bounded region of the same PNG; never infer missing pixels.
    const scale=Math.min(4,420/height);
    tz.width=Math.max(1,Math.round(width*scale));tz.height=Math.max(1,Math.round(height*scale));
    tzctx.drawImage(img,x,y,width,height,0,0,tz.width,tz.height);
    tzctx.strokeStyle='#e24639';tzctx.lineWidth=2;
    tzctx.strokeRect((b[0]-x)*scale,(b[1]-y)*scale,(b[2]-b[0])*scale,(b[3]-b[1])*scale);
    tz.dataset.loaded='true';
    tc.dataset.loaded='true';tracePreview={result,img,index,task};
    const evidence=result.sample.detection.detection_kind==='single_frame'?'单帧检测线索（无连续轨迹，身份须核对）':`本路轨迹 ${result.sample.detection.local_track}`;
    $('tracePointInfo').textContent=`第 ${index+1}/${data.samples.length} 个采样点 · 文件内 ${result.actual_media_s.toFixed(6)} 秒 · 源帧 ${result.actual_frame_index}（从0计数） · ${evidence} · 检测置信度 ${Number(result.sample.detection.confidence).toFixed(3)}`;
    syncTraceActions();
  }catch(e){if(epoch===traceEpoch){traceFailure(e.message);toast(e.message);}}
}
guarded('openTrace',async()=>{
  const observation=traceObservation,task=selectedTask;
  if(!observation||!task)throw Error('先选择任务并回看片段');
  resetTrace(observation);const epoch=traceEpoch;
  $('traceReview').hidden=false;$('traceStatus').textContent='正在读取保存的采样记录…';
  try{
    const data=await api('/api/observation-trace?'+new URLSearchParams({task,observation:observation.id}));
    if(epoch!==traceEpoch||task!==selectedTask)return;
    traceData=data;
    $('traceStatus').textContent=data.sample_count
      ?`保存了 ${data.sample_count} 个实际检测点${data.superseded?'，本片段已被纠正，仅回看历史轨迹':''}。未采样帧没有补造记录。`
      :data.status==='manual_without_automatic_trace'
        ?'这是人工声明的片段，没有该片段自己的自动采样轨迹；请回看原录像。'
        :'旧索引未保存本片段的采样轨迹，或尚未完成索引；请回看原录像。';
    $('traceControls').hidden=!data.sample_count;
    $('tracePoint').innerHTML=data.samples.map((p,i)=>`<option value="${i}">${i+1} · ${p.media_s.toFixed(6)} 秒 · 源帧 ${p.frame_index}</option>`).join('');
    $('traceSlider').max=Math.max(0,data.samples.length-1);
    if(data.sample_count){await showTracePoint(0);await loadAppearanceHistory();}
  }catch(e){if(epoch===traceEpoch){$('traceStatus').textContent=e.message;throw e;}}
});
$('tracePoint').onchange=()=>showTracePoint(Number($('tracePoint').value));
$('traceSlider').onchange=()=>showTracePoint(Number($('traceSlider').value));
$('tracePrevious').onclick=()=>showTracePoint(Math.max(0,Number($('tracePoint').value)-1));
$('traceNext').onclick=()=>showTracePoint(Math.min(traceData.samples.length-1,Number($('tracePoint').value)+1));
function traceToEditor(replace){
  const p=tracePreview;
  if(!p||p.task!==selectedTask)throw Error('先加载当前任务的采样画面');
  const task=state.tasks.find(t=>t.id===selectedTask);
  if(task?.version!==traceData.task_version)throw Error('任务已变化，请重新打开本片段');
  if(replace&&traceData.superseded)throw Error('本片段已有纠正版本，只能回看历史轨迹');
  openObservation(replace?traceData.observation:null);
  const at=p.result.actual_media_s;
  $('observationMedia').value=traceData.observation.media_id;
  $('observationTime').value=at;
  if(!replace){$('observationStart').value=at;$('observationEnd').value=at;}
  $('observationBox').value=p.result.sample.detection.box.join(',');
  observationImage=p.img;oc.width=p.img.naturalWidth;oc.height=p.img.naturalHeight;
  observationLoaded={media:traceData.observation.media_id,at};drawObservation();
  $('observationMode').textContent+=` · 从采样源帧 ${p.result.actual_frame_index} 带入画面，请回看后填写实际范围和依据`;
}
guarded('traceCorrect',()=>traceToEditor(true));
guarded('traceAdd',()=>traceToEditor(false));
