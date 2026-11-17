import {trials} from '../api/trials.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {chart} from '../components/common/chart.js';
import {TrialStatus} from '../constants/enums.js';
import {esc,fmt,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const writable=()=>['admin','analyst'].includes(session.user.role);
const isAdmin=()=>session.user.role==='admin';

function statusPill(status){
 return '<span class="status '+(status==='closed'?'completed':'running')+'">'+esc(TrialStatus[status]||status)+'</span>';
}

function trialList(current,selectedId){
 return '<section class="panel"><div class="section-title"><h2>盲态试验</h2><span>开验即锁定分层、等比分组与保密种子</span></div>'+
  '<div class="batch-list">'+current.map(t=>'<button class="batch-item'+(String(t.id)===String(selectedId)?' active':'')+'" data-trial="'+t.id+'"><strong>'+esc(t.name)+' <small>'+esc(t.key)+'</small></strong><span>'+statusPill(t.status)+' · '+t.strata+' 层 · 已入组 '+t.enrolled+'/'+t.planned+' 件</span><small>随访登记 '+t.follow_up+' 件 · 待登记 '+t.missing_observations+' 件</small></button>').join('')+
  (current.length?'':'<p class="empty">尚无盲态试验。开验后即可逐件入组。</p>')+'</div></section>';
}

function openForm(){
 if(!writable())return '<p class="hint">观察员只能查看试验，不能开验。</p>';
 return '<form id="trial-form"><label>试验名称<input name="name" required minlength="2" value="两种检修方案盲态对比"></label>'+
  '<div class="parameter-grid"><label>区组长度<select name="block_size"><option value="4">4 件 / 区组</option><option value="6">6 件 / 区组</option><option value="8">8 件 / 区组</option></select></label>'+
  '<label>分组比例<select name="group_ratio"><option value="1:1">1 : 1 等比</option></select></label></div>'+
  '<div class="parameter-grid"><label>方案甲名称<input name="label_A" value="方案甲" maxlength="40"></label><label>方案乙名称<input name="label_B" value="方案乙" maxlength="40"></label></div>'+
  '<label>锁定分层（每行“站点, 型号”，至少一行）<textarea name="strata" rows="3" required>东港站, AC-8\n东港站, AC-12</textarea></label>'+
  '<label>待入组清单（可选；每行“站点, 型号, 资产号, 部件编号”，用于查待入组）<textarea name="parts" rows="4" placeholder="东港站, AC-8, AC-8-101, SN-101"></textarea></label>'+
  '<button type="submit" class="primary">开验并锁定分配 ↗</button><p class="hint">开验后分层、区组与种子不可修改；分配按层内顺序逐段生成，未使用的分配不入库、不可查。</p></form>';
}

function parseParts(text){
 const parts=[];
 for(const line of String(text||'').split(/\r?\n/)){
  const cells=line.split(',').map(v=>v.trim());
  if(!cells.some(Boolean))continue;
  if(cells.length<3)throw new Error('待入组清单每行至少需要“站点, 型号, 资产号”');
  parts.push({site:cells[0],model:cells[1],asset:cells[2],serial:cells[3]||null,batch_ref:cells[4]||null});
 }
 return parts;
}

export async function trialPage(id){
 const list=await trials.list();
 workspace.trials=list;
 const selected=list.find(t=>String(t.id)===String(id))||list[0]||null;
 const detail=selected?await trials.get(selected.id):null;
 workspace.selectedTrial=selected?.id||null;
 let body='';
 if(!detail){
  body='<section class="panel"><div class="section-title"><h2>还没有盲态试验</h2></div><p class="empty">开验锁定站点型号分层、等比分组和保密种子，随后逐件入组。未使用的分配不会生成，也无法查询。</p></section>';
 }else{
  const t=detail.trial;
  const pendingRows=detail.pending.map(p=>({...p,observation:'待入组'}));
  const followRows=detail.follow_up.map(f=>({asset:f.asset,site:f.site,model:f.model,group:f.group||'盲态保密',arm:f.arm,
   km:f.observation?.km,status:f.observation?('已登记 v'+f.observation.version):'未登记',failed:f.observation?f.observation.failed:''}));
  const strataRows=detail.strata.map(s=>({site:s.site,model:s.model,planned:s.planned,enrolled:s.enrolled,pending:s.pending}));
  body='<div class="two-columns"><section class="panel"><div class="section-title"><h2>试验概览</h2>'+statusPill(t.status)+'</div>'+
   '<div class="metrics">'+[['已入组',t.enrolled],['待入组',t.pending],['随访中',t.follow_up],['未登记',t.missing_observations]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div>'+
   '<p class="hint">'+esc(t.name)+' · '+esc(t.key)+' · 区组 '+t.block_size+' 件 · 分组比例 '+esc(t.group_ratio)+' · 方案甲 '+esc(t.group_labels.A)+' / 方案乙 '+esc(t.group_labels.B)+'</p>'+
   '<div class="trial-filter"><button class="button small" data-filter="all">全部</button>'+
   (t.status==='open'?'<button class="button small" data-filter="pending">待入组</button><button class="button small" data-filter="follow">随访中</button>':'<button class="button small" data-filter="unblind">已揭盲</button>')+'</div>'+
   (t.status==='closed'?'<p class="warning">已结案冻结观测并整案揭盲。补报与入组均已停止，冻结快照可供按组估计。</p>':'<p class="hint">盲态进行中：已入组部件仅在各自入组后揭示该件方案；未使用的分配不生成、不可查，盲态期间不能按组估计。</p>')+
   '</section>'+
   '<section class="panel"><div class="section-title"><h2>分层与平衡</h2><span>逐层分段</span></div>'+table([{key:'site',label:'站点'},{key:'model',label:'型号'},{key:'planned',label:'计划'},{key:'enrolled',label:'已入组'},{key:'pending',label:'待入组'}],strataRows)+'</section></div>'+
   (t.status==='open'?'<section class="panel"><div class="section-title"><h2>入组</h2><span>同一部件跨批次仅入组一次</span></div>'+enrollForm(detail)+(writable()?'':"<p class='hint'>观察员不能入组。</p>")+'</section>':'');

  if(t.status==='open'){
   body+='<section class="panel" data-section="pending"><div class="section-title"><h2>待入组清单</h2><span>'+detail.pending.length+' 件</span></div>'+
    table([{key:'site',label:'站点'},{key:'model',label:'型号'},{key:'asset',label:'资产号'},{key:'serial',label:'部件编号'},{key:'batch_ref',label:'批次'}],pendingRows,'该分层已无待入组部件。')+'</section>'+
    '<section class="panel" data-section="follow"><div class="section-title"><h2>随访中</h2><span>登记里程与失效后进入估计样本</span></div>'+
    table([{key:'asset',label:'资产号'},{key:'site',label:'站点'},{key:'model',label:'型号'},{key:'group',label:'该件方案'},{key:'km',label:'里程'},{key:'failed',label:'失效'},{key:'status',label:'观测'}],followRows)+
    '<form id="observe-form"><label>补报观测（每行“资产号, 里程, 失效1/0”，可多行）<textarea name="rows" rows="4" required>'+esc(detail.follow_up.filter(f=>f.observation).slice(0,2).map(f=>f.asset+', '+Math.round(f.observation.km)+', '+f.observation.failed).join('\n'))+'</textarea></label>'+
    '<button type="submit" class="primary" '+(writable()?'':'disabled')+'>登记随访观测</button><p class="hint">同一部件重复补报会生成新版本，不覆盖历史；结案前须每件都有观测。</p></form></section>'+
    '<section class="panel" data-section="close"><div class="section-title"><h2>结案与揭盲</h2><span>负责人执行</span></div>'+
    '<p>结案将冻结当前观测版本，整案揭盲并生成两组样本供估计。观测未齐或盲态期间均拒绝按组估计。</p>'+
    '<button class="primary" id="close-trial" '+(isAdmin()&&detail.can_close?'':'disabled')+'>结案、冻结并揭盲</button>'+
    (isAdmin()?'':'<p class="hint">仅业务负责人可结案。</p>')+(detail.can_close?'':'<p class="hint">仍有待入组部件或未登记观测，暂不能结案。</p>')+
    '</section>';
  }else{
   body+='<section class="panel" data-section="unblind"><div class="section-title"><h2>揭盲样本</h2><span>两组各成一批数据</span></div>'+
    table([{key:'group',label:'组别'},{key:'dataset_name',label:'数据集'},{key:'rows',label:'样本数'}],detail.groups.map(g=>({group:g.group,dataset_name:g.dataset_name,rows:g.rows})))+
    '<p class="hint">两组样本已作为独立数据批次生成，可在分析工作台分别拟合；下方为按同一型号、同一可靠度的两组估计。</p>'+
    '<div id="estimate-panel">'+estimatePanel(detail)+'</div></section>';
  }
 }
 return heading('05 / BLINDED TRIAL','盲态试验与揭盲估计','开验锁定站点型号分层、等比分组和保密种子；逐层分段平衡分配，入组后才揭示该件方案。','<button class="button" id="refresh-trials">刷新试验 ↻</button>')+
  '<div class="report-grid"><section><div class="section-title"><h2>开验</h2></div>'+openForm()+'</section><section>'+trialList(list,selected?.id)+body+'</section></div>';
}

function enrollForm(detail){
 const strata=detail.strata;
 return '<form id="enroll-form"><label>站点型号分层<select name="stratum">'+strata.map(s=>'<option value="'+esc(s.site)+'|'+esc(s.model)+'">'+esc(s.site)+' · '+esc(s.model)+'</option>').join('')+'</select></label>'+
  '<label>部件资产号<input name="asset" required maxlength="160" placeholder="例如 AC-8-101"></label>'+
  '<div class="parameter-grid"><label>部件编号<input name="serial" maxlength="160" placeholder="用于拒绝同号换件"></label><label>来源批次<input name="batch_ref" maxlength="160" placeholder="例如 11 月复检"></label></div>'+
  '<button type="submit" class="primary" '+(writable()?'':'disabled')+'>入组该件并揭示方案</button>'+
  '<p class="hint">并发入组按层内顺序分配；同一部件重复提交会返回原分组，换用同号新件会被拒绝。</p></form>'+
  '<div id="enroll-result"></div>';
}

function estimatePanel(detail){
 const models=[...new Set(detail.strata.map(s=>s.model))];
 const groups=detail.groups||[];
 return '<div class="result-selector"><label>型号<select id="estimate-model">'+models.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join('')+'</select></label>'+
  '<label>目标可靠度<input id="estimate-reliability" type="number" step="0.01" min="0.6" max="0.99" value="0.9"></label>'+
  '<button class="button" id="run-estimate">按组估计</button></div>'+
  '<div id="estimate-output">'+groups.map(g=>'<p class="hint">'+esc(g.group)+' · '+g.rows+' 件样本已就绪，点击“按组估计”生成估计。</p>').join('')+'</div>';
}

function renderEstimates(payload){
 const arms=payload.arms||[];
 if(!arms.length)return '<p class="empty">暂无揭盲样本。</p>';
 return '<div class="two-columns">'+arms.map(a=>'<section class="panel"><div class="section-title"><h2>'+esc(a.group)+'</h2><span>'+a.rows+' 件 · '+esc(payload.model||'')+'</span></div>'+
  (a.error?'<p class="warning">'+esc(a.error)+'</p>':'<p class="eyebrow">'+esc(a.result.summary)+'</p><div class="metrics">'+(a.result.metrics||[]).map(m=>'<div><span>'+esc(m.label)+'</span><strong>'+esc(fmt(m.value))+'<small>'+esc(m.unit||'')+'</small></strong></div>').join('')+'</div>'+chart(a.result.chart)+(a.result.warnings||[]).map(w=>'<p class="warning">'+esc(w)+'</p>').join(''))+
  '</section>').join('')+'</div>';
}

export function bindTrials(refresh,id){
 const trialsNode=document.querySelector('#refresh-trials');
 if(trialsNode)trialsNode.onclick=refresh;
 document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{
  const view=button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('primary',b===button));
  document.querySelectorAll('[data-section]').forEach(section=>{
   const key=section.dataset.section;
   section.classList.toggle('is-hidden',view!=='all'&&!(key===view||(view==='follow'&&key==='close')));
  });
 });
 document.querySelectorAll('[data-trial]').forEach(button=>button.onclick=()=>{location.hash='#/trials/'+button.dataset.trial;});
 const open=document.querySelector('#trial-form');
 if(open)submit(open,async f=>{
  const parts=parseParts(f.get('parts'));
  const strataText=String(f.get('strata')||'').split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
  if(!strataText.length)throw new Error('请至少锁定一个站点型号分层');
  const strata=strataText.map(line=>{
   const cells=line.split(',').map(v=>v.trim());
   if(cells.length<2)throw new Error('分层须为“站点, 型号”');
   return {site:cells[0],model:cells[1],parts:parts.filter(p=>p.site===cells[0]&&p.model===cells[1])};
  });
  const t=await trials.open({name:f.get('name'),block_size:Number(f.get('block_size')),group_ratio:f.get('group_ratio'),
   group_labels:{A:f.get('label_A'),B:f.get('label_B')},strata});
  notify('试验已开验，分层、等比分组与保密种子已锁定');
  location.hash='#/trials/'+t.id;await refresh();
 });
 const enroll=document.querySelector('#enroll-form');
 if(enroll)submit(enroll,async f=>{
  const t=workspace.selectedTrial;
  const [site,model]=String(f.get('stratum')).split('|');
  const payload=await trials.enroll(t,{asset:f.get('asset'),site,model,
   serial:f.get('serial')||null,batch_ref:f.get('batch_ref')||null,request_key:'ui-'+crypto.randomUUID()});
  document.querySelector('#enroll-result').innerHTML='<p class="warning">'+esc(payload.asset)+' 已入组，该件方案：'+esc(payload.group)+'（'+esc(payload.arm)+'）'+(payload.replayed?' · 重试返回原分组':'')+'。此方案仅对该件揭示。</p>';
  notify('已入组并揭示该件方案');await refresh();
 });
 const observe=document.querySelector('#observe-form');
 if(observe)submit(observe,async f=>{
  const t=workspace.selectedTrial;
  const observations=String(f.get('rows')||'').split(/\r?\n/).map(v=>v.trim()).filter(Boolean).map(line=>{
   const [asset,km,failed]=line.split(',').map(v=>v.trim());
   if(!asset||km===undefined)throw new Error('每行须为“资产号, 里程, 失效1/0”');
   return {asset,km:Number(km),failed:Number(failed)};
  });
  const result=await trials.observe(t,{observations,request_key:'ui-'+crypto.randomUUID()});
  notify('已登记 '+result.applied.length+' 件观测'+(result.skipped.length?('，重试跳过 '+result.skipped.length+' 件'):''));
  await refresh();
 });
 const close=document.querySelector('#close-trial');
 if(close)close.onclick=async()=>{
  const t=workspace.selectedTrial;
  if(!confirm('结案后冻结观测并整案揭盲，且不再接收补报。确认结案？'))return;
  try{close.disabled=true;const result=await trials.close(t);notify('已结案、冻结观测并揭盲');await refresh();}
  catch(e){close.disabled=false;notify(e.message,true);}
 };
 const run=document.querySelector('#run-estimate');
 if(run)run.onclick=async()=>{
  const t=workspace.selectedTrial;
  const model=document.querySelector('#estimate-model').value;
  const reliability=document.querySelector('#estimate-reliability').value;
  try{const payload=await trials.estimates(t,'?model='+encodeURIComponent(model)+'&reliability='+encodeURIComponent(reliability));document.querySelector('#estimate-output').innerHTML=renderEstimates(payload);}
  catch(e){notify(e.message,true);}
 };
}
