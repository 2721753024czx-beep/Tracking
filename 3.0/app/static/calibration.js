let calibrationDraft=null, calibrationPreview=null;
function syncCalibrationMedia(){
  for(const id of ['calibrationReference','calibrationTarget']){
    const previous=$(id).value;
    $(id).innerHTML=state.media.map(m=>`<option value="${m.id}">${escapeHtml(m.camera_id+' / '+m.name)}</option>`).join('');
    if(state.media.some(m=>m.id===previous))$(id).value=previous;
    else if(id==='calibrationTarget'&&state.media.length>1)$(id).value=state.media[1].id;
  }
}
function invalidateCalibrationPreview(){
  calibrationPreview=null;$('calibrationMatched').checked=false;
  for(const id of ['calibrationRefImage','calibrationTargetImage'])$(id).removeAttribute('src');
  $('calibrationFrameInfo').textContent='录像或定位时刻已变化，请重新加载两份原画面。';
}
for(const id of ['calibrationReference','calibrationTarget'])$(id).onchange=invalidateCalibrationPreview;
for(const id of ['calibrationRefSecond','calibrationTargetSecond'])$(id).oninput=invalidateCalibrationPreview;
function calibrationSelection(){return [ $('calibrationReference').value,$('calibrationTarget').value,Number($('calibrationRefSecond').value),Number($('calibrationTargetSecond').value) ];}
guarded('loadCalibrationFrames',async()=>{
  const selection=calibrationSelection(),[reference,target,refSecond,targetSecond]=selection;
  if(!reference||!target||reference===target)throw Error('请选择两份不同录像');
  if(calibrationDraft?.points.length&&(calibrationDraft.reference_media_id!==reference||calibrationDraft.target_media_id!==target))throw Error('两份录像已变化，请先清空旧事件再核对新组合');
  invalidateCalibrationPreview();
  const media=id=>state.media.find(m=>m.id===id);
  const frames=await Promise.all([
    api('/api/calibration-frame',{media_id:reference,fingerprint:media(reference).fingerprint,second:refSecond}),
    api('/api/calibration-frame',{media_id:target,fingerprint:media(target).fingerprint,second:targetSecond})
  ]);
  if(JSON.stringify(selection)!==JSON.stringify(calibrationSelection()))throw Error('加载期间定位已变化，请重新加载');
  calibrationPreview={selection,reference:frames[0],target:frames[1]};
  $('calibrationRefImage').src=asset(frames[0].path);$('calibrationTargetImage').src=asset(frames[1].path);
  $('calibrationFrameInfo').textContent=`实际解码：参考 ${frames[0].actual_media_s.toFixed(6)}秒（帧${frames[0].frame_index}）／待校 ${frames[1].actual_media_s.toFixed(6)}秒（帧${frames[1].frame_index}）。请核对同一事件的同一瞬间；加入事件使用这些实际时刻。`;
});
function renderCalibrationEvents(){
  const points=calibrationDraft?.points||[];
  $('calibrationEvents').innerHTML=points.length?points.map((p,i)=>`<div class="calibrationEvent"><strong>${escapeHtml(p.event_id)}</strong> · 参考 ${p.reference_second.toFixed(6)}秒／待校 ${p.target_second.toFixed(6)}秒<br>定位误差：参考±${p.reference_tolerance??'未知'}秒／待校±${p.target_tolerance??'未知'}秒<br>${escapeHtml(p.evidence)}<br><button class="secondary" data-remove-calibration="${i}">移除此事件</button></div>`).join(''):'暂无共同事件。';
  document.querySelectorAll('[data-remove-calibration]').forEach(button=>button.onclick=()=>{calibrationDraft.points.splice(Number(button.dataset.removeCalibration),1);renderCalibrationEvents();});
}
guarded('addCalibrationEvent',async()=>{
  if(!calibrationPreview||JSON.stringify(calibrationPreview.selection)!==JSON.stringify(calibrationSelection()))throw Error('请先重新加载两份原画面');
  if(!$('calibrationMatched').checked)throw Error('请核对两张画面后勾选同一事件的同一时刻');
  const p=calibrationPreview,ident=$('calibrationEventId').value.trim(),evidence=$('calibrationEvidence').value.trim();
  if(!ident||!evidence)throw Error('请填写事件编号和共同事件定位依据');
  const pair={reference_media_id:p.reference.media_id,target_media_id:p.target.media_id,reference_fingerprint:p.reference.fingerprint,target_fingerprint:p.target.fingerprint};
  if(calibrationDraft?.points.length&&Object.keys(pair).some(key=>calibrationDraft[key]!==pair[key]))throw Error('录像或指纹已变化，请先清空旧事件');
  if(!calibrationDraft?.points.length)calibrationDraft={...pair,points:[]};
  if(calibrationDraft.points.some(point=>point.event_id===ident))throw Error('事件编号重复，请选择另一个事件');
  if(calibrationDraft.points.length>=100)throw Error('最多保留100个共同事件');
  const bound=id=>$(id).value===''?null:Number($(id).value);
  calibrationDraft.points.push({event_id:ident,reference_second:p.reference.actual_media_s,target_second:p.target.actual_media_s,reference_tolerance:bound('calibrationRefTolerance'),target_tolerance:bound('calibrationTargetTolerance'),evidence,human_matching_confirmed:true});
  renderCalibrationEvents();$('calibrationMatched').checked=false;$('calibrationEventId').value='E'+(calibrationDraft.points.length+1);
  toast('已加入核对事件；建议至少3个分布在录像前中后');
});
guarded('clearCalibrationEvents',async()=>{calibrationDraft=null;invalidateCalibrationPreview();renderCalibrationEvents();});
function renderCalibrationResult(result){
  $('calibrationResult').innerHTML=`<h3>${escapeHtml(result.label)}</h3><p>已保存诊断：参考 ${escapeHtml(result.sources.reference.camera_id)} / ${escapeHtml(result.sources.reference.name)} ↔ 待校 ${escapeHtml(result.sources.target.camera_id)} / ${escapeHtml(result.sources.target.name)}。<br>已保存 ${result.point_count} 个事件。${result.candidate_offset_to_reference_s==null?'没有可核对偏移候选。':`可核对偏移 ${Number(result.candidate_offset_to_reference_s).toFixed(6)} 秒（参考秒数 ≈ 待校秒数 + 偏移）。`}<br>未修改任何时间配置；未采样区间或实际拍摄时钟仍须独立核对。</p><p><button id="downloadCalibrationBundle" class="secondary">下载完整诊断包（含原帧）</button></p><p><a href="${asset(result.report_path)}" download>仅下载 Markdown</a> · <a href="${asset(result.result_path)}" download>下载诊断 JSON</a></p>`+result.decoded_frames.map(row=>`<div class="calibrationEvent"><strong>${escapeHtml(row.event_id)}</strong><div class="pair">${['reference','target'].map(role=>`<div><p>${role==='reference'?'参考':'待校'}实际帧 ${row[role].actual_media_s.toFixed(6)}秒</p><img class="calibrationImage" alt="${escapeHtml(row.event_id)}${role==='reference'?'参考':'待校'}保存原帧" src="${asset(result.frame_base+'/'+row[role].image)}"></div>`).join('')}</div></div>`).join('');
  guarded('downloadCalibrationBundle',async()=>{const packageResult=await api('/api/calibration-bundle',{id:result.id});const link=document.createElement('a');link.href=asset(packageResult.path);link.download=packageResult.path.split('/').pop();link.click();toast('完整诊断包已生成并发起下载，含报告和原帧，不含源录像');});
}
async function renderCalibrationReports(){
  const rows=await api('/api/calibration-reports');
  $('calibrationReports').innerHTML=rows.map(row=>`<div class="calibrationEvent"><strong>${escapeHtml(row.sources.reference.camera_id)} ↔ ${escapeHtml(row.sources.target.camera_id)}</strong><br>${escapeHtml(row.label)} · ${row.point_count}事件<br>保存于 ${escapeHtml(new Date(row.created_at*1000).toLocaleString())}<br><button class="secondary" data-view-calibration="${row.id}">查看已保存诊断</button></div>`).join('')||'<p>暂无已保存的校时诊断。</p>';
  document.querySelectorAll('[data-view-calibration]').forEach(button=>button.onclick=async()=>{try{renderCalibrationResult(await api('/api/calibration-report?id='+button.dataset.viewCalibration));}catch(error){toast(error.message);}});
}
guarded('saveCalibrationReport',async()=>{if(!calibrationDraft?.points.length)throw Error('请先加入至少一个共同事件');if(calibrationDraft.reference_media_id!==$('calibrationReference').value||calibrationDraft.target_media_id!==$('calibrationTarget').value)throw Error('当前选择与本次事件的录像不同，请恢复原组合或清空事件');const result=await api('/api/calibration-report',calibrationDraft);renderCalibrationResult(result);await renderCalibrationReports();toast('只读诊断已保存，时间配置保持原状');});
guarded('refreshCalibrationReports',renderCalibrationReports);
syncCalibrationMedia();renderCalibrationReports().catch(error=>toast(error.message));
