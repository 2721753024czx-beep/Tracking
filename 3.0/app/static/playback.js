/* Display-only playback of recorded samples. No identity decisions are written. */
(function (root, factory) {
  'use strict';
  const logic = factory();
  if (typeof module === 'object' && module.exports) module.exports = logic;
  else { root.TargetPlayback = logic; if (typeof document !== 'undefined') startPlayback(logic); }

  function startPlayback(model) {
    const $ = id => document.getElementById(id);
    const video = $('playbackVideo'), overlay = $('targetOverlay'), ctx = overlay.getContext('2d');
    const timeline = $('coverageTimeline'), tctx = timeline.getContext('2d');
    const query = new URLSearchParams(location.search);
    const taskId = query.get('task') || '';
    let observationId = query.get('observation') || '';
    let data = null, samples = [], epoch = 0, displayedTime = 0, currentIndex = -1;
    let picking = false, frameRequest = null, currentPath = '', portraitDrawn = false;
    let ready = false, metadataHandler = null, loadingTimer = null;
    const canFrameCallback = typeof video.requestVideoFrameCallback === 'function';
    const controls = ['playButton','leadInButton','replayButton','playbackSpeed','pickButton','showTrail'];
    const asset = path => '/asset?path=' + encodeURIComponent(path);
    const identityLabels = {confirmed:'身份已确认',pending:'身份待核对 · 只读回放',rejected:'身份已排除 · 只读回放'};
    const statuses = {waiting:'等待片段开始',detected:'已检出 · 框随目标移动',missing:'暂未检出',reappeared:'重新检出',ended:'此片段结束'};

    async function api(path, body) {
      let response;
      try {
        response = await fetch(path, body === undefined ? {} : {
          method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
        });
      } catch (_) {
        const startup = location.port === '8782' ? '“启动图书馆L01回放.command”' : '对应录像工作区的启动程序';
        throw Error(`连接不到本机回放服务。请先打开${startup}并保留终端窗口，启动成功后再点“重试”。`);
      }
      let result;
      try { result = await response.json(); } catch (_) { throw Error('回放服务返回异常，请稍后重试。'); }
      if (!response.ok || result.error) throw Error(result.error || '回放读取失败');
      return result;
    }
    function notice(message) {
      $('notice').textContent = message; $('notice').hidden = false;
      clearTimeout(notice.timer); notice.timer = setTimeout(() => { $('notice').hidden = true; }, 3600);
    }
    function loading(title, message) {
      ready = false; controls.forEach(id => { $(id).disabled = true; });
      $('errorBox').hidden = true; $('stageMessage').hidden = false;
      $('stageMessage').querySelector('.spinner').hidden = false;
      $('loadTitle').textContent = title; $('loadMessage').textContent = message;
      $('targetState').textContent = '准备回放'; $('stateDot').dataset.tone = 'waiting';
      ctx.clearRect(0,0,overlay.width,overlay.height);
    }
    function failure(message) {
      ready = false; video.pause(); clearInterval(loadingTimer);
      controls.forEach(id => { $(id).disabled = true; });
      $('stageMessage').hidden = false; $('stageMessage').querySelector('.spinner').hidden = true;
      $('loadTitle').textContent = '回放暂不可用'; $('loadMessage').textContent = '可以保留当前任务与目标后重试。';
      $('errorBox').hidden = false; $('errorMessage').textContent = message;
      $('targetState').textContent = '回放未就绪'; $('stateDot').dataset.tone = 'waiting';
    }
    function setPicking(value) {
      picking = !!value && ready;
      if (picking) video.pause();
      overlay.classList.toggle('picking', picking); $('pickHint').hidden = !picking;
      $('pickButton').setAttribute('aria-pressed', String(picking));
      $('pickButton').textContent = picking ? '退出选人' : '画面选人';
      if (ready) render(displayedTime);
    }
    function prepareDetails(result) {
      const observation = result.observation, media = result.media;
      $('taskName').textContent = result.task.name || '当前查找任务';
      $('mediaName').textContent = media.name || media.camera_id;
      $('targetName').textContent = `${observation.camera_id} · 本路 ID ${observation.local_track}`;
      $('targetRange').textContent = `本片段 ${model.formatTime(observation.start)}–${model.formatTime(observation.end)} · ${observation.id.slice(0,8)}`;
      $('identityStatus').textContent = identityLabels[observation.status] || '身份尚未核对 · 只读回放';
      $('identityStatus').dataset.status = observation.status || 'pending';
      $('sampleNote').textContent = `本片段不与其他 ID 自动连接。`;
      $('videoFrame').style.setProperty('--media-ratio', `${media.width || 16}/${media.height || 9}`);
      overlay.width = media.width || 1280; overlay.height = media.height || 720;
      const detected = samples.filter(sample => model.targetDetection(sample,observation.id)).length;
      const missing = samples.filter(sample => sample.media_s >= observation.start - model.EPSILON &&
        sample.media_s <= observation.end + model.EPSILON && !model.targetDetection(sample,observation.id)).length;
      $('coverageInfo').textContent = `${detected} 个检出采样点${missing ? ` · ${missing} 个间断采样点` : ''}`;
      $('timelineMiddle').textContent = model.formatTime(media.duration / 2, false);
      $('timelineEnd').textContent = model.formatTime(media.duration, false);
      timeline.setAttribute('aria-valuemax', String(media.duration));
      portraitDrawn = false; $('targetPortrait').hidden = true; $('targetPortraitCanvas').hidden = true;
      $('portraitPlaceholder').hidden = false;
      if (observation.crop) {
        $('targetPortrait').onload = () => { $('targetPortrait').hidden = false; $('portraitPlaceholder').hidden = true; };
        $('targetPortrait').onerror = () => { $('targetPortrait').hidden = true; };
        $('targetPortrait').src = asset(observation.crop);
      } else $('targetPortrait').removeAttribute('src');
    }
    async function loadTarget(nextId, preserveTime = null) {
      const requestEpoch = ++epoch;
      observationId = nextId; video.pause(); setPicking(false);
      clearInterval(loadingTimer);
      if (metadataHandler) { video.removeEventListener('loadedmetadata',metadataHandler); metadataHandler = null; }
      loading('正在读取回放记录', '读取已保存的目标片段和采样画面…');
      try {
        let result = await api('/api/target-playback?' + new URLSearchParams({task:taskId,observation:nextId}));
        if (requestEpoch !== epoch) return;
        if (!result.ready) {
          $('loadTitle').textContent = '正在准备采样回放';
          $('loadMessage').textContent = '首次打开需要生成播放副本，请稍候…';
          let seconds = 0;
          loadingTimer = setInterval(() => {
            seconds += 1;
            $('loadMessage').textContent = `正在生成播放副本 · 已等待 ${seconds} 秒`;
          },1000);
          result = await api('/api/target-playback-prepare', {task_id:taskId,observation_id:nextId});
          if (requestEpoch !== epoch) return;
        }
        clearInterval(loadingTimer);
        if (!result.ready || !result.preview?.path) throw Error(result.explanation || '播放副本还未就绪，请重试。');
        data = result; samples = (result.samples || []).slice().sort((a,b) => a.media_s - b.media_s);
        if (!samples.length) throw Error('此片段没有可用的采样画面，请返回工作台查看原录像。');
        prepareDetails(result);
        const nextQuery = new URLSearchParams(location.search); nextQuery.set('task',taskId); nextQuery.set('observation',nextId);
        history.replaceState(null,'',location.pathname + '?' + nextQuery);
        document.title = `本路 ID ${result.observation.local_track} · 目标跟踪回放`;
        const initial = preserveTime == null ? model.representativeTime(samples,result.observation) : preserveTime;
        const onReady = () => {
          if (requestEpoch !== epoch) return;
          metadataHandler = null; ready = true;
          controls.forEach(id => { $(id).disabled = false; });
          $('stageMessage').hidden = true;
          video.playbackRate = Number($('playbackSpeed').value);
          seek(initial,false);
          scheduleVideoFrame();
        };
        if (currentPath !== result.preview.path || !video.readyState) {
          currentPath = result.preview.path;
          metadataHandler = onReady; video.addEventListener('loadedmetadata',onReady,{once:true});
          video.src = asset(currentPath); video.load();
        } else onReady();
      } catch (error) { if (requestEpoch === epoch) failure(error.message); }
    }
    function scheduleVideoFrame() {
      if (!canFrameCallback || frameRequest !== null) return;
      frameRequest = video.requestVideoFrameCallback((_now, metadata) => {
        frameRequest = null;
        if (ready) render(metadata.mediaTime);
        scheduleVideoFrame();
      });
    }
    function seek(second, play) {
      if (!ready) return;
      const at = Math.max(0,Math.min(Number(second) || 0,Math.max(0,data.media.duration - 0.001)));
      video.currentTime = at;
      // Clear immediately while decoding the new frame so a previous target box cannot linger.
      ctx.clearRect(0,0,overlay.width,overlay.height);
      if (!canFrameCallback) render(at);
      drawTimeline(at);
      if (play) video.play().catch(error => notice(error.message || '请点击播放按钮继续。'));
    }
    function drawLabel(text,x,y,color,subtle=false) {
      const scale = overlay.width / 1280, fontSize = Math.max(16,18 * scale), pad = 7 * scale;
      ctx.font = `600 ${fontSize}px -apple-system,BlinkMacSystemFont,sans-serif`;
      const width = ctx.measureText(text).width + pad*2, height = fontSize + pad*1.6;
      x = Math.max(0,Math.min(overlay.width-width,x)); y = Math.max(0,y-height-3*scale);
      ctx.fillStyle = subtle ? '#183a35bb' : color; ctx.fillRect(x,y,width,height);
      ctx.fillStyle = '#fff'; ctx.textBaseline = 'top'; ctx.fillText(text,x+pad,y+pad*.6);
    }
    function scaledBox(detection,sample) {
      const sx = overlay.width / (sample.width || data.media.width || overlay.width);
      const sy = overlay.height / (sample.height || data.media.height || overlay.height);
      return detection.box.map((n,i) => n * (i % 2 ? sy : sx));
    }
    function drawDetection(detection,sample,selected) {
      if (!model.validBox(detection.box)) return;
      const b = scaledBox(detection,sample), scale = overlay.width/1280;
      ctx.strokeStyle = selected ? '#5cf2bc' : '#d8f1e5b8'; ctx.lineWidth = Math.max(2,selected ? 3*scale : 1.5*scale);
      ctx.setLineDash(selected ? [] : [8*scale,5*scale]);
      ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]); ctx.setLineDash([]);
      drawLabel(`${selected ? '选中目标 · ' : ''}本路 ID ${detection.local_track}`,b[0],b[1],selected ? '#087556' : '#2f5b4e',!selected);
    }
    function drawTrail(index) {
      if (!$('showTrail').checked || index < 0) return;
      const paths = model.trailSegments(samples,index,data.observation.id,4);
      ctx.strokeStyle = '#64f0c1bb'; ctx.lineWidth = Math.max(2,overlay.width/600);
      for (const path of paths) {
        if (path.length < 2) continue;
        ctx.beginPath();
        path.forEach(({sample,detection},i) => {
          const b = scaledBox(detection,sample), x=(b[0]+b[2])/2,y=b[3];
          if (i) ctx.lineTo(x,y); else ctx.moveTo(x,y);
        }); ctx.stroke();
      }
    }
    function drawPortrait(detection,sample) {
      if (portraitDrawn || data.observation.crop || !detection || video.readyState < 2) return;
      if (!model.validBox(detection.box)) return;
      const b=scaledBox(detection,sample), width=b[2]-b[0],height=b[3]-b[1];
      const portrait=$('targetPortraitCanvas'), pctx=portrait.getContext('2d');
      portrait.width=Math.max(1,Math.round(width)); portrait.height=Math.max(1,Math.round(height));
      try {
        pctx.drawImage(video,b[0]*video.videoWidth/overlay.width,b[1]*video.videoHeight/overlay.height,
          width*video.videoWidth/overlay.width,height*video.videoHeight/overlay.height,0,0,portrait.width,portrait.height);
        portrait.hidden=false; $('portraitPlaceholder').hidden=true; portraitDrawn=true;
      } catch (_) { /* The video frame may still be decoding; retry on the next frame. */ }
    }
    function render(second) {
      if (!ready || !data) return;
      displayedTime = Math.max(0,Number(second)||0);
      currentIndex = model.sampleIndex(samples,displayedTime);
      const sample = samples[currentIndex], detection = model.targetDetection(sample,data.observation.id);
      const status = video.ended ? 'ended' : model.sampleStatus(samples,currentIndex,data.observation);
      $('targetState').textContent = statuses[status]; $('stateDot').dataset.tone = status;
      $('clock').replaceChildren(document.createTextNode(model.formatTime(displayedTime)+' '));
      const total=document.createElement('span');total.textContent='/ '+model.formatTime(data.media.duration);$('clock').append(total);
      $('playButton').innerHTML=video.paused?'<span aria-hidden="true">▶</span> 播放':'<span aria-hidden="true">Ⅱ</span> 暂停';
      ctx.clearRect(0,0,overlay.width,overlay.height);
      if (sample) {
        if (detection && status !== 'ended' && status !== 'waiting') drawTrail(currentIndex);
        if (picking) for (const other of sample.detections || []) {
          if (model.isTracked(other) && other.observation_id !== data.observation.id) drawDetection(other,sample,false);
        }
        if (detection && status !== 'ended' && status !== 'waiting') {
          drawDetection(detection,sample,true); drawPortrait(detection,sample);
        }
      }
      drawTimeline(displayedTime);
    }
    function drawTimeline(second) {
      const width=timeline.width,height=timeline.height,duration=data?.media.duration||1;
      tctx.clearRect(0,0,width,height);
      tctx.fillStyle='#d6e1dc';tctx.fillRect(0,8,width,height-16);
      if (data) for (let i=0;i<samples.length;i++) {
        const status=model.sampleStatus(samples,i,data.observation);
        if (status==='waiting'||status==='ended') continue;
        const left=samples[i].media_s/duration*width;
        const right=(samples[i+1]?.media_s??duration)/duration*width;
        tctx.fillStyle=status==='missing'?'#e3ad53':'#31a484';
        tctx.fillRect(left,8,Math.max(1,right-left),height-16);
      }
      const x=Math.max(2,Math.min(width-2,second/duration*width));
      tctx.fillStyle='#133c32';tctx.fillRect(x-2,1,4,height-2);
      tctx.beginPath();tctx.moveTo(x-6,0);tctx.lineTo(x+6,0);tctx.lineTo(x,7);tctx.fill();
      timeline.setAttribute('aria-valuenow',String(second.toFixed(3)));
      timeline.setAttribute('aria-valuetext',`${model.formatTime(second)}，${$('targetState').textContent}`);
    }
    function canvasPoint(event) {
      const rect=overlay.getBoundingClientRect(),scale=Math.min(rect.width/overlay.width,rect.height/overlay.height);
      const left=rect.left+(rect.width-overlay.width*scale)/2,top=rect.top+(rect.height-overlay.height*scale)/2;
      return [(event.clientX-left)/scale,(event.clientY-top)/scale];
    }
    overlay.addEventListener('click',event => {
      if (!picking || !samples[currentIndex]) return;
      const sample=samples[currentIndex],point=canvasPoint(event);
      const matches=(sample.detections||[]).filter(detection => {
        if (!model.isTracked(detection)||!model.validBox(detection.box)) return false;
        const b=scaledBox(detection,sample);return point[0]>=b[0]&&point[0]<=b[2]&&point[1]>=b[1]&&point[1]<=b[3];
      }).sort((a,b)=>(a.box[2]-a.box[0])*(a.box[3]-a.box[1])-(b.box[2]-b.box[0])*(b.box[3]-b.box[1]));
      if (!matches.length) { notice('请点击浅色框内的人物。'); return; }
      const chosen=matches[0];
      if (chosen.observation_id===observationId) {setPicking(false);return;}
      loadTarget(chosen.observation_id,displayedTime);
    });
    $('playButton').onclick=() => {
      if (!ready) return;
      setPicking(false);
      if (video.paused) video.play().catch(error=>notice(error.message)); else video.pause();
    };
    $('leadInButton').onclick=() => {setPicking(false);seek(data.observation.start-2,true);};
    $('replayButton').onclick=() => {setPicking(false);seek(data.observation.start,true);};
    $('pickButton').onclick=() => setPicking(!picking);
    $('showTrail').onchange=() => render(displayedTime);
    $('playbackSpeed').onchange=() => {video.playbackRate=Number($('playbackSpeed').value);};
    $('retryButton').onclick=() => loadTarget(observationId);
    timeline.onclick=event => {
      if (!ready) return;
      const rect=timeline.getBoundingClientRect();seek((event.clientX-rect.left)/rect.width*data.media.duration,false);
    };
    timeline.onkeydown=event => {
      if (!ready) return;
      const delta=event.shiftKey?5:1;
      const next={ArrowLeft:video.currentTime-delta,ArrowRight:video.currentTime+delta,Home:0,End:data.media.duration}[event.key];
      if (next==null) return;event.preventDefault();seek(next,false);
    };
    video.addEventListener('seeked',() => {if (!canFrameCallback || video.paused) render(video.currentTime);});
    video.addEventListener('seeking',() => {ctx.clearRect(0,0,overlay.width,overlay.height);});
    video.addEventListener('timeupdate',() => {if (!canFrameCallback) render(video.currentTime);});
    video.addEventListener('play',() => {if (picking) setPicking(false);render(displayedTime);scheduleVideoFrame();});
    video.addEventListener('pause',() => {if (ready) render(displayedTime);});
    video.addEventListener('ended',() => {if (ready) render(video.currentTime);});
    video.addEventListener('error',() => {if (currentPath) failure('播放副本读取失败。请确认本机回放服务仍在运行，再点“重试”；也可返回工作台查看原帧。');});
    if (!taskId || !observationId) {
      failure('请从核对工作台回看一个跟踪片段，再点击“目标跟踪回放”。');
      $('taskName').textContent='尚未选择任务';$('retryButton').hidden=true;
    } else loadTarget(observationId);
  }
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const EPSILON=0.0001;
  function isTracked(detection) {
    return !!detection?.observation_id && detection.detection_kind !== 'single_frame' &&
      detection.local_track != null && !String(detection.local_track).startsWith('frame:');
  }
  function targetDetection(sample,id) {
    return (sample?.detections||[]).find(detection => detection.observation_id===id && isTracked(detection)) || null;
  }
  function sampleIndex(samples,second) {
    let left=0,right=samples.length-1,result=-1;
    while(left<=right) {const middle=(left+right)>>1;if(samples[middle].media_s<=second+EPSILON){result=middle;left=middle+1;}else right=middle-1;}
    return result;
  }
  function sampleStatus(samples,index,observation) {
    const sample=samples[index];
    if(!sample || sample.media_s<observation.start-EPSILON)return 'waiting';
    if(sample.media_s>observation.end+EPSILON)return 'ended';
    if(!targetDetection(sample,observation.id))return 'missing';
    const previous=samples[index-1];
    return previous && previous.media_s>=observation.start-EPSILON && !targetDetection(previous,observation.id) ? 'reappeared':'detected';
  }
  function representativeTime(samples,observation) {
    const target=samples.filter(sample=>targetDetection(sample,observation.id));
    if(!target.length)return observation.start;
    return target.reduce((a,b)=>Math.abs(a.media_s-observation.representative)<=Math.abs(b.media_s-observation.representative)?a:b).media_s;
  }
  function trailSegments(samples,index,id,seconds) {
    const paths=[];let path=[];const start=(samples[index]?.media_s||0)-seconds;
    for(let i=0;i<=index;i++) {
      if(samples[i].media_s<start)continue;
      const detection=targetDetection(samples[i],id);
      if(detection&&validBox(detection.box))path.push({sample:samples[i],detection});
      else if(path.length){paths.push(path);path=[];}
    }
    if(path.length)paths.push(path);
    return paths;
  }
  function validBox(box) {return Array.isArray(box)&&box.length===4&&box.every(Number.isFinite)&&box[2]>box[0]&&box[3]>box[1];}
  function formatTime(value,precise=true) {
    const units=precise?100:1,total=Math.round(Math.max(0,Number(value)||0)*units);
    const minutes=Math.floor(total/(60*units)),rest=(total-minutes*60*units)/units;
    return `${String(minutes).padStart(2,'0')}:${precise?rest.toFixed(2).padStart(5,'0'):String(rest).padStart(2,'0')}`;
  }
  return {EPSILON,isTracked,targetDetection,sampleIndex,sampleStatus,representativeTime,trailSegments,validBox,formatTime};
});
