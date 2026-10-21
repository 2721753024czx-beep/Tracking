// Suggestions are review aids. Preparing a draft is not saving a correction.
let appearanceEpoch=0, appearanceItems=[], appearanceCurrent=null, appearanceBusy=false;
function resetAppearance(){
  appearanceEpoch++;appearanceItems=[];appearanceCurrent=null;appearanceBusy=false;
  $('appearanceReview').hidden=true;$('appearanceHistory').innerHTML='';
  $('appearanceSegments').innerHTML='';$('appearanceWarnings').innerHTML='';
  $('appearanceStatus').textContent='';$('appearanceReason').value='';
  syncAppearanceActions();
}
function syncAppearanceActions(){
  const task=state.tasks.find(t=>t.id===selectedTask);
  const active=task?.status==='active'&&task.version===traceData?.task_version&&!traceData?.superseded;
  $('generateAppearance').disabled=!active||appearanceBusy;
  $('prepareAppearance').disabled=!active||appearanceBusy||!appearanceCurrent?.valid;
  $('dismissAppearance').disabled=!active||appearanceBusy||!appearanceCurrent?.valid;
}
function renderAppearance(){
  const r=appearanceCurrent;
  $('appearanceHistory').innerHTML=appearanceItems.map((item,i)=>`<option value="${i}">${new Date(item.created_at*1000).toLocaleString()} · 阈值 ${item.threshold} · 核对版 ${item.revision}</option>`).join('');
  if(r)$('appearanceHistory').value=String(appearanceItems.findIndex(item=>item.id===r.id));
  $('appearanceSegments').innerHTML=r?r.segments.map((s,i)=>`<option value="${i}">段 ${i+1} · ${s.start.toFixed(6)}–${s.end.toFixed(6)} 秒 · ${s.indices.length}点 · ${r.reviews[String(i)]?.status==='dismissed'?'已忽略':r.reviews[String(i)]?.status==='draft_prepared'?'曾准备草稿':'待核对'}</option>`).join(''):'';
  $('appearanceWarnings').innerHTML=r?r.warnings.map(w=>`<button type="button" class="secondary" data-appearance-point="${w.index}">待核对点 ${w.index+1} · ${r.samples[w.index].media_s.toFixed(6)} 秒</button>`).join(''):'';
  document.querySelectorAll('[data-appearance-point]').forEach(b=>b.onclick=()=>showTracePoint(Number(b.dataset.appearancePoint)));
  $('appearanceStatus').textContent=r?`阈值 ${r.threshold} · ${r.segments.length}段 · ${r.cuts.length}个变化边界 · ${r.warnings.length}个保留异常点。${r.valid?'建议与当前源及采样记录一致。':'采样记录已变化，此历史建议不能用于新草稿。'}身份仍未确认。`: '尚无建议；显式填写阈值后按需生成。';
  syncAppearanceActions();
}
async function loadAppearanceHistory(){
  if(!traceData?.sample_count)return;
  const epoch=++appearanceEpoch,task=selectedTask,observation=traceData.observation.id;
  const result=await api('/api/appearance-reviews?'+new URLSearchParams({task,observation}));
  if(epoch!==appearanceEpoch||task!==selectedTask||observation!==traceData?.observation.id)return;
  if(result.task_version!==traceData.task_version)throw Error('任务已变化，请重新打开轨迹');
  $('appearanceReview').hidden=false;appearanceItems=result.items;
  $('appearanceHistory').innerHTML=appearanceItems.map((r,i)=>`<option value="${i}">${new Date(r.created_at*1000).toLocaleString()} · 阈值 ${r.threshold} · 核对版 ${r.revision}</option>`).join('');
  appearanceCurrent=appearanceItems[0]??null;renderAppearance();
}
$('appearanceHistory').onchange=()=>{appearanceCurrent=appearanceItems[Number($('appearanceHistory').value)]??null;renderAppearance();};
$('appearanceSegments').onchange=()=>{
  const r=appearanceCurrent,s=r?.segments[Number($('appearanceSegments').value)];
  if(s)showTracePoint(s.representative_index);
};
guarded('generateAppearance',async()=>{
  if(!traceData)throw Error('先打开逐点轨迹');
  const raw=$('appearanceThreshold').value.trim();
  if(!raw)throw Error('请显式填写阈值；当前没有通用默认值');
  const threshold=Number(raw),epoch=appearanceEpoch,task=selectedTask;
  if(!Number.isFinite(threshold)||threshold<=0||threshold>=1)throw Error('阈值须位于0与1之间');
  appearanceBusy=true;syncAppearanceActions();$('appearanceStatus').textContent='正在逐点核对源帧并提取外观…';
  try{
    await api('/api/appearance-proposal',{task_id:task,observation_id:traceData.observation.id,
      expected_task_version:traceData.task_version,threshold});
    if(epoch!==appearanceEpoch||task!==selectedTask)return;
    await loadAppearanceHistory();
  }catch(e){if(epoch===appearanceEpoch)$('appearanceStatus').textContent=e.message;throw e;}
  finally{if(task===selectedTask){appearanceBusy=false;syncAppearanceActions();}}
});
async function reviewAppearance(status){
  const r=appearanceCurrent,segment=Number($('appearanceSegments').value);
  const reason=$('appearanceReason').value.trim(),task=selectedTask,epoch=appearanceEpoch;
  if(!r?.valid||!reason)throw Error('先选择有效建议段，并填写核对说明');
  appearanceBusy=true;syncAppearanceActions();
  try{
    if(status==='draft_prepared'){
      const s=r.segments[segment];
      await showTracePoint(s.representative_index);
      if(epoch!==appearanceEpoch||task!==selectedTask||tracePreview?.index!==s.representative_index)throw Error('画面或任务已变化，请重新核对建议');
      traceToEditor(true);
      $('observationStart').value=s.start;$('observationEnd').value=s.end;
      $('observationMode').textContent+=' · 外观建议仅含采样范围，请回看修订后保存';
      $('observationEvidence').value=`外观建议 ${r.id} 段 ${segment+1}（阈值 ${r.threshold}），人工核对说明：${reason}`;
    }
    const changed=await api('/api/appearance-review',{task_id:task,review_id:r.id,segment,status,reason,
      expected_task_version:traceData.task_version,expected_review_revision:r.revision});
    if(epoch!==appearanceEpoch||task!==selectedTask)return;
    appearanceCurrent={...changed,valid:true};
    appearanceItems=appearanceItems.map(item=>item.id===changed.id?appearanceCurrent:item);
    renderAppearance();$('appearanceSegments').value=String(segment);
    toast(status==='dismissed'?'已保存忽略原因；原轨迹保留':'已准备纠正草稿；尚未保存片段或确认身份');
  }catch(e){if(epoch===appearanceEpoch)$('appearanceStatus').textContent=e.message;throw e;}
  finally{if(epoch===appearanceEpoch){appearanceBusy=false;syncAppearanceActions();}}
}
guarded('prepareAppearance',()=>reviewAppearance('draft_prepared'));
guarded('dismissAppearance',()=>reviewAppearance('dismissed'));
