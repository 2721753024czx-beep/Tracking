/* Display-only candidate browsing. API ranking and evidence remain unchanged. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CandidateBrowsing = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  function kind(row) {
    return row.detection_kind === 'single_frame' ? 'single_frame' :
      row.detection_kind === 'tracked' ? 'tracked' : 'other';
  }
  function contextKey(task, context, media, rows) {
    const keys = ['cameras','time_filter','from_epoch','to_epoch','query_group',
      'relative_from','relative_to','anchor','query_anchor','search_mode','search_direction',
      'reference_observations','reference_review_required','encoder'];
    return JSON.stringify({task:task.id,conditions:keys.map(k=>task[k]??null),
      scope:[context.mode,context.direction,context.anchor_id,context.topology_version,context.selected_cameras],
      media:media.map(m=>[m.id,m.time_version??1]), ranking:rows.map(o=>[o.id,o.score])});
  }
  class Browser {
    constructor(pageSize=24) {
      if (!Number.isInteger(pageSize) || pageSize<1) throw Error('Invalid page size');
      this.pageSize=pageSize;
      this.clear();
    }
    clear() {
      this.taskId=null; this.key=null; this.rows=[]; this.page=1;
      this.type='all'; this.status='active'; this.error=null; this.loading=false;
    }
    begin(taskId) {
      if (taskId!==this.taskId) {this.clear(); this.taskId=taskId;}
      this.loading=true;
    }
    update(taskId,key,rows,error=null) {
      if (this.taskId!==taskId || this.key!==key) {
        this.page=1; this.type='all'; this.status='active';
      }
      this.taskId=taskId; this.key=key; this.rows=rows; this.error=error; this.loading=false;
      return this.view();
    }
    filter(type,status) {
      if (!['all','single_frame','tracked','other'].includes(type) ||
          !['active','all','pending','confirmed','rejected'].includes(status)) throw Error('Invalid browsing filter');
      this.type=type; this.status=status; this.page=1;
      return this.view();
    }
    go(page) {
      if (!Number.isFinite(page)) throw Error('Invalid page');
      this.page=Math.floor(page);
      return this.view();
    }
    view() {
      const types={tracked:0,single_frame:0,other:0}, statuses={pending:0,confirmed:0,rejected:0};
      for (const row of this.rows) {
        types[kind(row)]++;
        statuses[row.status??'pending']=(statuses[row.status??'pending']??0)+1;
      }
      const filtered=this.rows.map((row,index)=>({row,rank:index+1})).filter(({row})=>
        (this.type==='all'||kind(row)===this.type) &&
        (this.status==='all'||(this.status==='active'?row.status!=='rejected':(row.status??'pending')===this.status)));
      const pages=Math.max(1,Math.ceil(filtered.length/this.pageSize));
      this.page=Math.min(pages,Math.max(1,this.page));
      const offset=(this.page-1)*this.pageSize;
      return {total:this.rows.length,types,statuses,matching:filtered.length,page:this.page,pages,
        from:filtered.length?offset+1:0,to:Math.min(offset+this.pageSize,filtered.length),
        items:filtered.slice(offset,offset+this.pageSize),error:this.error,loading:this.loading};
    }
  }
  return {Browser,kind,contextKey};
});
