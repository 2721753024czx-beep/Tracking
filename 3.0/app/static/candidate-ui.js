const candidateBrowser = new CandidateBrowsing.Browser(24);
let candidateRequest = 0;

function installCandidateControls() {
  const filter=()=>{candidateBrowser.filter($('candidateType').value,$('candidateStatus').value);renderCandidatePage();};
  $('candidateType').onchange=filter; $('candidateStatus').onchange=filter;
  $('candidateShowAll').onclick=()=>{candidateBrowser.filter('all','all');renderCandidatePage();};
  for (const id of ['candidatePrevious','candidatePreviousBottom']) $(id).onclick=()=>{candidateBrowser.go(candidateBrowser.page-1);renderCandidatePage();};
  for (const id of ['candidateNext','candidateNextBottom']) $(id).onclick=()=>{candidateBrowser.go(candidateBrowser.page+1);renderCandidatePage();};
  $('candidateFirst').onclick=()=>{candidateBrowser.go(1);renderCandidatePage();};
  $('candidateLast').onclick=()=>{candidateBrowser.go(candidateBrowser.view().pages);renderCandidatePage();};
  const jump=()=>{
    const page=Number($('candidatePage').value);
    if (Number.isInteger(page)&&page>=1) candidateBrowser.go(page);
    else toast('批次须为正整数');
    renderCandidatePage();
  };
  $('candidateJump').onclick=jump;
  $('candidatePage').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();jump();}};
}

function clearCandidates() {
  candidateRequest++; candidateBrowser.clear();
  $('candidateControls').hidden=true; $('candidateBottomNavigation').hidden=true;
  $('candidates').textContent='';
}

function beginCandidateLoad(taskId) {
  const changed=candidateBrowser.taskId!==taskId;
  candidateBrowser.begin(taskId);
  if (changed) renderCandidatePage();
}

function renderCandidatePage() {
  const v=candidateBrowser.view(), controls=$('candidateControls');
  controls.hidden=!candidateBrowser.taskId;
  $('candidateBottomNavigation').hidden=!candidateBrowser.taskId;
  $('candidateType').value=candidateBrowser.type; $('candidateStatus').value=candidateBrowser.status;
  const options={all:`全部类型（${v.total}）`,tracked:`跟踪片段（${v.types.tracked}）`,
    single_frame:`单帧线索（${v.types.single_frame}）`,other:`其他片段：人工／旧索引（${v.types.other}）`};
  for (const option of $('candidateType').options) option.textContent=options[option.value];
  const unavailable=v.loading||Boolean(v.error);
  $('candidateCounts').textContent=v.loading?'正在读取当前任务，数量待加载。':v.error?
    '当前查询未完成，候选数量未知；不能据此判断没有目标。':
    `查询范围共 ${v.total} 条：待确认 ${v.statuses.pending}、已确认 ${v.statuses.confirmed}、已排除 ${v.statuses.rejected}。当前浏览筛选 ${v.matching} 条，展示 ${v.from}–${v.to}，每批最多 ${candidateBrowser.pageSize} 条。`;
  $('candidatePage').value=v.page; $('candidatePage').max=v.pages;
  $('candidatePages').textContent=`/ ${v.pages} 批`;
  $('candidateBottomInfo').textContent=unavailable?'':`第 ${v.page} / ${v.pages} 批 · 展示 ${v.from}–${v.to} / ${v.matching}`;
  for (const id of ['candidateFirst','candidatePrevious','candidatePreviousBottom']) $(id).disabled=unavailable||v.page<=1;
  for (const id of ['candidateLast','candidateNext','candidateNextBottom']) $(id).disabled=unavailable||v.page>=v.pages;
  for (const id of ['candidateType','candidateStatus','candidatePage','candidateJump','candidateShowAll']) $(id).disabled=unavailable;
  if (v.loading) {$('candidates').innerHTML='<p>正在读取当前任务候选…</p>';return;}
  if (v.error) {$('candidates').innerHTML=`<p>当前查询无法完成：${escapeHtml(v.error)}；请修订后重新查找，不能据此判断没有目标。</p>`;return;}
  const labels={confirmed:'已确认',rejected:'已排除',pending:'待确认'};
  $('candidates').innerHTML=v.items.map(({row:o,rank})=>`<div class="card ${escapeHtml(o.status)}" data-candidate-id="${escapeHtml(o.id)}"><div class="candidateRank">查询排序第 ${rank} 位</div><img loading="lazy" decoding="async" src="${asset(o.crop)}" alt="候选目标"><strong>${escapeHtml(o.camera_id)} · ${escapeHtml(o.location)}</strong><p>${escapeHtml(timeText(o))}<br>${o.source_integrity&&!o.source_integrity.valid?'源证据失效／待核实 · ':''}${o.detection_kind==='single_frame'?'单帧检测线索（无连续轨迹） · ':o.detection_kind==='tracked'?'跟踪片段 · ':'其他片段（人工／旧索引） · '}排序分数 ${escapeHtml(o.score)}${o.query_time_status==='unknown'?' · 查询时间待核对':''} · ${labels[o.status]||'待确认'}</p><button data-review="${escapeHtml(o.id)}">回看</button><button data-correct="${escapeHtml(o.id)}">纠正片段</button><button data-decision="${escapeHtml(o.id)}" data-status="confirmed" ${o.source_integrity&&!o.source_integrity.valid?'disabled':''}>确认</button><button data-decision="${escapeHtml(o.id)}" data-status="rejected">排除</button><button data-decision="${escapeHtml(o.id)}" data-status="pending">撤销</button></div>`).join('') || (v.total?
    '<p>当前浏览筛选没有匹配项。可选择“显示全部类型与状态”查看查询范围内其他候选。</p>':
    '<p>当前查询暂无候选，请检查处理覆盖、查询范围或调整目标图片；采样未命中不代表没有目标。</p>');
  const list=candidateBrowser.rows;
  $('candidates').querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>review(list.find(o=>o.id===b.dataset.review)));
  bindCorrections(list); bindDecisions();
  setTaskReadOnly(state.tasks.find(t=>t.id===selectedTask));
}
