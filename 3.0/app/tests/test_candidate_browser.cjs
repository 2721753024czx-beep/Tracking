const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Browser,kind,contextKey}=require('../static/candidates.js');
const rows=Array.from({length:356},(_,i)=>({id:String(i),score:1-i/1000,
  detection_kind:i<88?'tracked':'single_frame',status:'pending'}));

test('356 ranked candidates remain reachable exactly once across 15 batches',()=>{
  const b=new Browser(); b.update('A','scope',rows);
  const ids=[];
  for(let p=1;p<=15;p++) {
    const v=b.go(p);assert.ok(v.items.length<=24);
    ids.push(...v.items.map(i=>i.row.id));
  }
  assert.deepEqual(ids,rows.map(o=>o.id));assert.equal(b.view().items.length,20);
  assert.equal(b.view().total,356);
});
test('type filtering preserves global order/rank, does not mutate evidence',()=>{
  const saved=JSON.stringify(rows),b=new Browser();b.update('A','scope',rows);b.go(14);
  const v=b.filter('single_frame','active');assert.equal(v.page,1);assert.equal(v.matching,268);
  assert.equal(v.items[0].rank,89);assert.deepEqual(v.types,{tracked:88,single_frame:268,other:0});
  assert.equal(JSON.stringify(rows),saved);
});
test('excluded results can be viewed and an empty display is distinct from total zero',()=>{
  const b=new Browser();b.update('A','scope',[{id:'p',status:'pending'},{id:'r',status:'rejected'}]);
  assert.equal(b.view().matching,1);assert.equal(b.view().total,2);
  assert.equal(b.filter('all','rejected').items[0].row.id,'r');
  assert.equal(b.filter('single_frame','all').matching,0);assert.equal(b.view().total,2);
  assert.equal(b.filter('all','all').matching,2);
});
test('task or scope changes reset page and filters; ordinary decisions preserve browsing',()=>{
  const b=new Browser();b.update('A','scope',rows);b.filter('single_frame','all');b.go(4);
  const changed=rows.map(r=>({...r,status:r.id==='100'?'confirmed':r.status}));
  b.update('A','scope',changed);assert.equal(b.page,4);assert.equal(b.type,'single_frame');
  b.update('A','new-scope',changed);assert.equal(b.page,1);assert.equal(b.type,'all');assert.equal(b.status,'active');
  b.go(10);b.begin('B');assert.equal(b.rows.length,0);assert.equal(b.page,1);assert.equal(b.loading,true);
});
test('shrinking the filtered result clamps a last batch without losing access',()=>{
  const b=new Browser();b.update('A','scope',rows);b.go(15);
  b.update('A','scope',rows.map((r,i)=>({...r,status:i>=24?'rejected':'pending'})));
  assert.equal(b.page,1);assert.equal(b.view().matching,24);assert.equal(b.view().total,356);
  assert.equal(b.filter('all','all').matching,356);
});
test('manual and legacy fragments stay visible without inventing a tracked identity',()=>{
  assert.equal(kind({source:'manual'}),'other');assert.equal(kind({local_track:'7'}),'other');
  const b=new Browser();b.update('A','scope',[{id:'m',source:'manual'},{id:'old',local_track:'7'}]);
  assert.equal(b.filter('other','all').matching,2);
});
test('context resets on conditions/reference/time/topology/ranking, not a status version',()=>{
  const t={id:'A',cameras:['A'],search_direction:'both',version:1},c={mode:'all',direction:'both',selected_cameras:['A'],topology_version:0},m=[{id:'m',time_version:1}];
  const original=contextKey(t,c,m,rows);
  assert.equal(contextKey({...t,version:2,status:'paused'},c,m,rows),original);
  assert.notEqual(contextKey({...t,cameras:['B']},c,m,rows),original);
  assert.notEqual(contextKey({...t,relative_from:5},c,m,rows),original);
  assert.notEqual(contextKey({...t,reference_observations:['r']},c,m,rows),original);
  assert.notEqual(contextKey(t,{...c,topology_version:1},m,rows),original);
  assert.notEqual(contextKey(t,c,[{id:'m',time_version:2}],rows),original);
  assert.notEqual(contextKey(t,c,m,rows.slice(1)),original);
});
test('explicit jump and Enter commit the requested batch; refresh keeps that batch',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const elements=new Map(),toasts=[];
  const element=id=>{if(!elements.has(id))elements.set(id,{value:''});return elements.get(id);};
  const sandbox={CandidateBrowsing:{Browser},$:element,toast:s=>toasts.push(s),renderCandidatePage:()=>{}};
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../static/candidate-ui.js'),'utf8'),sandbox);
  vm.runInContext('renderCandidatePage=()=>{}; installCandidateControls();',sandbox);
  sandbox.rows=rows;
  vm.runInContext("candidateBrowser.update('A','scope',rows)",sandbox);
  element('candidatePage').value='6';element('candidateJump').onclick();
  assert.equal(vm.runInContext('candidateBrowser.view().page',sandbox),6);
  vm.runInContext("candidateBrowser.begin('A');candidateBrowser.update('A','scope',rows)",sandbox);
  assert.equal(vm.runInContext('candidateBrowser.view().items[0].row.id',sandbox),'120');
  let prevented=false;element('candidatePage').value='3';
  element('candidatePage').onkeydown({key:'Enter',preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,true);assert.equal(vm.runInContext('candidateBrowser.page',sandbox),3);
  element('candidatePage').value='0';element('candidateJump').onclick();
  assert.equal(toasts.length,1);assert.equal(vm.runInContext('candidateBrowser.page',sandbox),3);
});
