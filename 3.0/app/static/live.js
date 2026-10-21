/* Credentials are sent once to the local server, never stored in the browser. */
'use strict';
const cards = [...document.querySelectorAll('.camera-card')];
const notice = document.getElementById('notice');
let latest = {cameras: [], captures: []};
let polling = false;
const busy = new Set();
const metadataDirty = new Set();
const metadataSaving = new Set();
const active = job => job && ['recording', 'stopping'].includes(job.status);
const text = (node, value) => { if (node.textContent !== value) node.textContent = value; };
const role = (card, name) => card.querySelector(`[data-role="${name}"]`);
function announce(message, error = false) { text(notice, message); notice.classList.toggle('error', error); }

async function api(path, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST',
      headers: data === undefined ? {} : {'Content-Type': 'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store', signal: controller.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `请求失败（${response.status}）`);
    return result;
  } finally { clearTimeout(timer); }
}
function recording(camera) { return latest.captures.find(job => job.camera_id === camera && active(job)); }
function session(camera) { return latest.cameras.find(item => item.camera_id === camera); }
function metadataValues(card) {
  const form = role(card, 'metadata');
  if (!form.reportValidity()) throw new Error('请检查显示名称和相对位置');
  return Object.fromEntries(new FormData(form));
}
async function saveMetadata(card) {
  const id = card.dataset.camera, data = metadataValues(card);
  metadataSaving.add(id); render();
  try {
    const saved = await api('/api/live-metadata', {camera_id: id, ...data});
    latest.camera_metadata = [...(latest.camera_metadata || []).filter(item => item.id !== id), saved];
    metadataDirty.delete(id);
    announce('机位信息已保存。下一段录像使用新说明，已开始的录像保留原说明。');
    await poll();
  } finally { metadataSaving.delete(id); render(); }
}
function options() {
  const input = document.getElementById('duration');
  if (!input.reportValidity()) throw new Error('请填写1–86400秒的录制上限');
  return {duration: Number(input.value), codec: document.getElementById('codec').value};
}
async function connect(card) {
  const id = card.dataset.camera;
  const existing = session(id);
  if (existing && !['stopped', 'failed'].includes(existing.status)) return;
  const form = card.querySelector('.connection-form');
  if (!form.reportValidity()) throw new Error(`请填写${card.querySelector('h2').textContent}的连接信息`);
  const data = {...Object.fromEntries(new FormData(form)), ...metadataValues(card)};
  busy.add(id); render();
  try {
    await api('/api/live-connect', data);
    metadataDirty.delete(id);
    form.elements.password.value = '';
    card.querySelector('.connection').open = false;
    const image = role(card, 'image');
    image.removeAttribute('src'); delete image.dataset.failed;
    role(card, 'message').textContent = '正在连接摄像头，等待第一帧画面…';
    announce('已发起连接，请查看各路画面状态。');
    await poll();
  } finally { busy.delete(id); render(); }
}
async function startRecord(card) {
  const id = card.dataset.camera;
  busy.add(id); render();
  try {
    if (metadataDirty.has(id)) await saveMetadata(card);
    await api('/api/live-record', {camera_id: id, ...options()}); await poll();
  }
  finally { busy.delete(id); render(); }
}
async function stopRecord(card) {
  const job = recording(card.dataset.camera);
  if (job && job.status === 'recording') { await api('/api/capture-stop', {id: job.id}); await poll(); }
}
async function act(action) {
  try { await action(); } catch (error) { announce(error.name === 'AbortError' ? '连接请求超时，请检查摄像头状态后重试。' : error.message, true); }
}
async function group(action, success) {
  const results = await Promise.allSettled(cards.map(card => action(card)));
  const failures = results.flatMap((item, i) => item.status === 'rejected' ? [`摄像头${i + 1}：${item.reason.message}`] : []);
  announce(failures.length ? failures.join('；') + '。已启动的另一路会继续运行，请查看各路状态。' : success, !!failures.length);
  await poll();
}
function render() {
  for (const card of cards) {
    const id = card.dataset.camera, view = session(id), job = recording(id), pending = busy.has(id) || metadataSaving.has(id);
    const connected = view && !['failed', 'stopped'].includes(view.status);
    const live = view && view.status === 'live';
    const state = role(card, 'state');
    const labels = {connecting: '连接中', live: '实时画面', stale: '画面暂时中断', failed: '连接失败', stopped: '已断开'};
    text(state, pending && !view ? '连接中' : labels[view?.status] || '未连接');
    state.className = 'state ' + (view?.status || '');
    for (const input of card.querySelectorAll('.connection-form input')) input.disabled = !!connected || pending;
    const metadataForm = role(card, 'metadata');
    const saved = (latest.camera_metadata || []).find(item => item.id === id) || view;
    for (const key of ['label', 'location']) {
      const field = metadataForm.elements[key];
      field.disabled = metadataSaving.has(id);
      if (saved && !metadataDirty.has(id) && !metadataSaving.has(id) && document.activeElement !== field) field.value = saved[key] || '';
    }
    metadataForm.querySelector('button').disabled = metadataSaving.has(id);
    text(role(card, 'metadata-status'), metadataSaving.has(id) ? '正在保存…' : metadataDirty.has(id) ? '有修改，尚未保存' : saved ? '已保存 · 用于下一段录像' : '连接后也可随时修改');
    card.querySelector('.connect-one').disabled = !!connected || pending;
    card.querySelector('[data-action="disconnect"]').disabled = !connected || pending;
    card.querySelector('[data-action="record"]').disabled = !live || !!job || pending;
    card.querySelector('[data-action="stop-record"]').disabled = !job || job.status === 'stopping';
    role(card, 'recording-badge').hidden = !job;
    if (view) {
      text(card.querySelector('h2'), view.label || (id === 'cam_a' ? '摄像头1' : '摄像头2'));
      text(card.querySelector('.ip'), new URL(view.preview_url).hostname);
      for (const key of ['preview_url', 'record_url']) {
        const field = card.querySelector('.connection-form').elements[key];
        if (document.activeElement !== field && connected) field.value = view[key] || '';
      }
      const message = view.error || (job ? (job.status === 'stopping' ? '正在停止并保存录像…' : `正在录制 · ${Math.max(0, Math.floor(Date.now() / 1000 - job.started_at))} 秒`) :
        view.status === 'live' ? '实时预览已连接。录制采用主码流，保存到本机。' :
        view.status === 'stale' ? '最近没有新画面，请检查摄像头连接；不要把静止旧画面当成实时。' :
        view.status === 'connecting' ? '正在连接摄像头，等待第一帧画面…' : '预览已断开。可重新填写密码连接。');
      text(role(card, 'message'), message);
    }
    const image = role(card, 'image');
    if (connected) {
      if (!image.getAttribute('src') || (image.dataset.failed && Date.now() - Number(image.dataset.failed) > 5000)) {
        delete image.dataset.failed;
        image.src = '/api/live.mjpg?camera_id=' + encodeURIComponent(id) + '&v=' + Date.now();
      }
      image.hidden = !view.frames;
      role(card, 'empty').hidden = !!view.frames;
      text(role(card, 'frame-info'), view.frames ? `预览 ${view.frames} 帧 · 更新于 ${new Date(view.last_frame_at * 1000).toLocaleTimeString('zh-CN', {hour12: false})}` : '');
    } else {
      image.removeAttribute('src'); image.hidden = true; role(card, 'empty').hidden = false;
      text(role(card, 'frame-info'), '');
    }
  }
  document.getElementById('connectAll').disabled = cards.every(card => {
    const view = session(card.dataset.camera); return busy.has(card.dataset.camera) || (view && !['failed', 'stopped'].includes(view.status));
  });
  document.getElementById('recordAll').disabled = cards.some(card => session(card.dataset.camera)?.status !== 'live' || recording(card.dataset.camera) || busy.has(card.dataset.camera) || metadataSaving.has(card.dataset.camera));
  document.getElementById('stopAll').disabled = !cards.some(card => recording(card.dataset.camera)?.status === 'recording');
  if (latest.workspace) text(document.getElementById('savePath'), '保存位置：' + latest.workspace + '/captures');
  renderRecordings();
}
let recordingSignature = '';
function renderRecordings() {
  const jobs = latest.captures.filter(job => cards.some(card => card.dataset.camera === job.camera_id))
    .sort((a, b) => b.started_at - a.started_at).slice(0, 6);
  const signature = JSON.stringify(jobs);
  if (signature === recordingSignature) return;
  recordingSignature = signature;
  const list = document.getElementById('recordingsList'); list.replaceChildren();
  if (!jobs.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = '还没有本次摄像头的录制记录。'; list.append(p); }
  const names = {recording: '录制中', stopping: '正在保存', complete: '已保存', failed: '录制失败'};
  for (const job of jobs) {
    const row = document.createElement('div'); row.className = 'recording-row';
    const title = document.createElement('div'); title.className = 'rec-title';
    const strong = document.createElement('strong'); strong.textContent = `${job.camera_id} · ${new Date(job.started_at * 1000).toLocaleString('zh-CN', {hour12: false})}`;
    const detail = document.createElement('small'); detail.textContent = job.error || `${job.recorded_duration ? job.recorded_duration.toFixed(2) + ' 秒 · ' : ''}${job.codec === 'h264' ? 'H.264' : '原流复制'} · ${job.location || '未填机位'} · ${job.path}`;
    title.append(strong, detail); row.append(title);
    const status = document.createElement('span'); status.className = 'rec-state ' + job.status; status.textContent = names[job.status] || job.status; row.append(status);
    if (job.status === 'complete') {
      for (const label of ['回放', '下载']) {
        const a = document.createElement('a'); a.href = '/asset?path=' + encodeURIComponent(job.path); a.textContent = label;
        if (label === '下载') a.download = job.camera_id + '-' + job.id + '.mp4'; else { a.target = '_blank'; a.rel = 'noopener'; }
        row.append(a);
      }
    }
    list.append(row);
  }
}
async function poll() {
  if (polling) return;
  polling = true;
  try { latest = await api('/api/live-status'); render(); }
  catch (error) { announce('工作台连接中断，请确认本地服务仍在运行。', true); }
  finally { polling = false; }
}
for (const card of cards) {
  card.querySelector('.connection-form').addEventListener('submit', event => { event.preventDefault(); act(() => connect(card)); });
  role(card, 'metadata').addEventListener('input', () => { metadataDirty.add(card.dataset.camera); render(); });
  role(card, 'metadata').addEventListener('submit', event => { event.preventDefault(); act(() => saveMetadata(card)); });
  card.querySelector('[data-action="record"]').addEventListener('click', () => act(() => startRecord(card)));
  card.querySelector('[data-action="stop-record"]').addEventListener('click', () => act(() => stopRecord(card)));
  card.querySelector('[data-action="disconnect"]').addEventListener('click', () => act(async () => {
    await api('/api/live-disconnect', {camera_id: card.dataset.camera});
    card.querySelector('.connection').open = true; await poll();
  }));
  role(card, 'image').addEventListener('error', () => { role(card, 'image').dataset.failed = String(Date.now()); });
}
document.getElementById('connectAll').addEventListener('click', () => act(() => group(connect, '已发起两路连接，请查看画面状态。')));
document.getElementById('recordAll').addEventListener('click', () => act(() => group(startRecord, '已发起双路录制。')));
document.getElementById('stopAll').addEventListener('click', () => act(() => group(stopRecord, '已请求停止录制，正在保存文件。')));
poll(); setInterval(poll, 1000);
