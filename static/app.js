'use strict';
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=(v,d=0)=>v===null||v===undefined?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d});
const pct=v=>v===null||v===undefined?'—':n(v,1)+'%';
const stateNames={open:'待处理',progress:'处理中',resolved:'已解决'};
const statusNames={delivered:'已送达',in_progress:'履约中',cancelled:'已取消'};
const issueNames={none:'无',stockout:'缺货',picking_error:'拣货差错',damaged:'货损',delivery_delay:'配送延误',other:'其他'};
const pages={overview:['运营概览','从订单流转到人员投入，找到下一步改进方向。'],people:['拣货人效','将拣货产出与实际工时关联，复核投入与效率。'],exceptions:['异常跟进','记录原因、明确负责人，保留每一次跟进。'],planning:['排班测算','用历史件量估算小时需求，提前发现用工缺口。'],data:['数据中心','导入原始记录，保留数据校验与备份。']};
let meta,report,plan,token='',importPayload=null,reportSequence=0,orderPage=1,casePage=1,activeQuery=null;
const pageSize=15;

async function api(path,data){
  const options=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:JSON.stringify(data)};
  const response=await fetch(path,options);
  const value=await response.json();
  if(!response.ok)throw new Error(value.error||'请求失败，请重试');
  return value;
}
async function busy(button,work){
  const label=button.textContent;
  button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='处理中…';
  try{return await work();}finally{button.disabled=false;button.removeAttribute('aria-busy');button.textContent=label;}
}
function error(message){$('#global-error').textContent=message;$('#global-error').hidden=!message;}
function query(){return new URLSearchParams(new FormData($('#filters')));}
function timestamp(v){return v?v.replace('T',' ').replace('+08:00',''):'—';}
function summary(values){return values.map(([label,value,note=''])=>'<div><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong><small>'+esc(note)+'</small></div>').join('');}
function table(headers,rows,empty='当前筛选无记录',cards=true){
  if(!rows.length)return '<div class="empty"><p>'+esc(empty)+'</p></div>';
  return '<div class="table-wrap"><table class="'+(cards?'mobile-cards':'')+'"><thead><tr>'+headers.map(h=>'<th scope="col">'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map((cell,i)=>'<td data-label="'+esc(headers[i])+'"><span>'+cell+'</span></td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
function badge(label,tone=''){return '<span class="badge '+tone+'">'+esc(label)+'</span>';}
function orderButton(row){return '<button class="row-link" data-order="'+esc(row.order_id)+'">'+esc(row.order_id)+'</button>';}
function chart(id,rows,value,second,label,{height=220,line=false}={}){
  const width=Math.max(230,$('#'+id).clientWidth||720),left=36,right=16,top=20,bottom=34,plotW=width-left-right,plotH=height-top-bottom;
  const max=Math.max(1,...rows.map(r=>Number(r[value])||0),...rows.map(r=>Number(r[second])||0));
  const slot=plotW/Math.max(1,rows.length),bar=Math.max(1,slot*.58);
  let svg='<svg role="img" aria-label="'+esc(label)+'" viewBox="0 0 '+width+' '+height+'"><title>'+esc(label)+'</title>';
  for(let i=0;i<4;i++){const y=top+plotH*i/3;svg+='<line class="axis" x1="'+left+'" x2="'+(width-right)+'" y1="'+y+'" y2="'+y+'"/><text x="'+(left-8)+'" y="'+(y+4)+'" text-anchor="end">'+n(max*(3-i)/3)+'</text>';}
  rows.forEach((r,i)=>{const x=left+slot*i+(slot-bar)/2,h=(r[value]||0)/max*plotH;
    const title=(r.date||String(r.hour).padStart(2,'0')+':00')+' · '+n(r[value],1)+(second?' / '+n(r[second],1):'');
    svg+='<rect class="bar" x="'+x+'" y="'+(top+plotH-h)+'" width="'+bar+'" height="'+h+'"><title>'+esc(title)+'</title></rect>';
    if(second&&!line){const h2=(r[second]||0)/max*plotH;svg+='<rect class="bar-secondary" x="'+(x+bar*.32)+'" y="'+(top+plotH-h2)+'" width="'+(bar*.36)+'" height="'+h2+'"><title>'+esc(title)+'</title></rect>';}
    const step=Math.max(1,Math.ceil(rows.length/Math.max(2,Math.floor(plotW/70))));
    if(i%step===0){svg+='<text x="'+(x+bar/2)+'" y="'+(height-10)+'" text-anchor="middle">'+esc(r.date?r.date.slice(5):String(r.hour).padStart(2,'0')+':00')+'</text>';}
  });
  if(second&&line)svg+='<polyline class="line" points="'+rows.map((r,i)=>(left+slot*(i+.5))+','+(top+plotH-(r[second]||0)/max*plotH)).join(' ')+'"/>';
  $('#'+id).innerHTML=svg+'</svg>';
}
function navigate(){
  const id=location.hash.slice(1)in pages?location.hash.slice(1):'overview';
  document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==id);
  document.querySelectorAll('nav a').forEach(a=>{if(a.hash==='#'+id)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  $('#page-title').textContent=pages[id][0];$('#page-desc').textContent=pages[id][1];
  $('#filters').hidden=id==='data'||id==='planning';
  $('#empty').hidden=!report||report.kpis.total>0||['data','planning'].includes(id);
  document.title=pages[id][0]+' · 仓衡';
  redrawCharts();
}
function redrawCharts(){
  if(report&&!$('#overview').hidden){
    chart('trend-chart',report.trend,'orders','delivered','每天有效订单与已送达订单');
    chart('hourly-chart',report.hourly,'orders',null,'按下单小时汇总的有效订单量',{height:165});
  }
  if(plan&&!$('#planning').hidden)chart('plan-chart',plan.hourly,'required','available','每小时需求人数与可用人数',{line:true});
}
async function loadMeta(first=false){
  meta=await api('/api/meta');token=meta.token;
  $('#mode').textContent={demo:'模拟数据工作区',mixed:'演示 + 导入数据',empty:'空白工作区',imported:'导入数据工作区'}[meta.mode];
  const station=$('#station').value,planStation=$('#plan-station').value;
  const options=meta.stations.map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('');
  $('#station').innerHTML='<option value="">全部站点</option>'+options;
  $('#plan-station').innerHTML='<option value="">全部站点合并测算</option>'+options;
  if(meta.stations.includes(station))$('#station').value=station;
  if(meta.stations.includes(planStation))$('#plan-station').value=planStation;
  if(first){
    $('#date-end').value=meta.last_day||meta.today;
    const last=new Date((meta.last_day||meta.today)+'T12:00:00+08:00');last.setUTCDate(last.getUTCDate()-6);
    $('#date-start').value=last.toISOString().slice(0,10);$('#plan-date').value=meta.today;
  }
  $('#database-summary').innerHTML='<div class="db-stat"><span>订单记录</span><b>'+n(meta.orders_count)+'</b></div><div class="db-stat"><span>工时记录</span><b>'+n(meta.shifts_count)+'</b></div><p class="caption">订单日期：'+esc(meta.first_day||'—')+' 至 '+esc(meta.last_day||'—')+'</p>';
  $('#import-history').innerHTML=table(['时间','文件','数据类型','新增','跳过'],meta.imports.map(r=>[esc(timestamp(r.created_at)),esc(r.filename),r.kind==='orders'?'订单':'工时',n(r.inserted),n(r.skipped)]),'暂无导入记录');
}
async function loadReport(){
  const seq=++reportSequence,q=query();
  const value=await api('/api/report?'+q);
  if(seq!==reportSequence)return;
  report=value;activeQuery=q;orderPage=casePage=1;error('');renderReport();navigate();
}
function renderReport(){
  const k=report.kpis;
  $('#on-time').textContent=pct(k.on_time_rate);$('#on-time-note').textContent=n(k.on_time)+' 单准时 / '+n(k.delivered)+' 单已送达';
  $('#orders-total').textContent=n(k.active);$('#cancel-note').textContent='全部 '+n(k.total)+' 单，剔除取消 '+n(k.cancelled)+' 单';
  $('#completed').textContent=pct(k.completion_rate);$('#efficiency').textContent=n(k.efficiency,1);$('#open-cases').textContent=n(k.open_exceptions);
  $('#overdue-note').textContent='其中 '+n(k.overdue)+' 单待履约超时';
  $('#range-label').textContent=(report.station||'全部站点')+' · '+report.start+' — '+report.end;
  $('#footer-status').textContent='统计截至 '+timestamp(report.as_of)+' · 本地 SQLite';
  for(const kind of ['orders','workers','exceptions','trend'])$('#export-'+kind).href='/api/export?'+activeQuery+'&kind='+kind;
  chart('trend-chart',report.trend,'orders','delivered','每天有效订单与已送达订单');
  chart('hourly-chart',report.hourly,'orders',null,'按下单小时汇总的有效订单量',{height:165});
  const max=Math.max(1,...report.stages.map(s=>s.p90||0));
  $('#stages').innerHTML=report.stages.map(s=>'<div class="stage"><div><span>'+esc(s.label)+'</span><b>'+n(s.mean,1)+' 分钟</b></div><progress aria-label="'+esc(s.label)+'平均用时" max="'+max+'" value="'+(s.mean||0)+'"></progress><div><small>'+n(s.samples)+' 个样本</small><small>P90 '+n(s.p90,1)+' 分钟</small></div></div>').join('');
  const slow=[...report.stages].filter(s=>s.mean!==null).sort((a,b)=>b.mean-a.mean)[0];
  const peak=[...report.hourly].sort((a,b)=>b.orders-a.orders)[0];
  $('#insights').innerHTML='<h3>本期观察</h3><p>'+(slow?'最长平均阶段为“'+esc(slow.label)+'”，'+n(slow.mean,1)+' 分钟；可结合订单详情排查具体原因。':'暂无完整阶段时间，先补齐订单记录。')+'</p><p>'+((peak?.orders)?String(peak.hour).padStart(2,'0')+':00 时段汇总 '+n(peak.orders)+' 单，为本期下单高峰。':'当前筛选没有有效订单。')+'</p><p>'+n(k.unmatched_orders)+' 单拣货记录缺少匹配工时，已从效率分子排除。</p>';
  $('#people-summary').innerHTML=summary([['拣货人效',n(k.efficiency,2),'件 / 人时'],['有效拣货工时',n(k.hours,1),'人时'],['匹配产出',n(k.matched_items),'件'],['缺少工时',n(k.unmatched_orders),'单']]);
  $('#people-quality').hidden=!k.unmatched_orders;$('#people-quality').textContent=k.unmatched_orders+' 单未匹配同日同站点工号的拣货工时，请补录后重新核对。';
  $('#people-table').innerHTML=table(['站点 / 工号','有效工时','匹配订单','匹配件数','件 / 人时','缺工时订单'],report.workers.map(w=>[esc(w.station)+'<small class="sub">'+esc(w.employee_id)+'</small>',n(w.hours,2),n(w.orders),n(w.items),'<b>'+n(w.efficiency,2)+'</b>',w.unmatched_orders?badge(w.unmatched_orders+' 单','bad'):'—']),'暂无拣货与工时记录，请在数据中心导入');
  const cases=report.exceptions;
  $('#exception-summary').innerHTML=summary([['异常订单',n(cases.length)],['待处理',n(cases.filter(r=>r.followup.state==='open').length)],['处理中',n(cases.filter(r=>r.followup.state==='progress').length)],['已解决',n(cases.filter(r=>r.followup.state==='resolved').length)]]);
  renderOrders();renderCases();
}
function pager(id,count,page,type){
  const last=Math.max(1,Math.ceil(count/pageSize));
  $('#'+id).innerHTML='<span>'+n(count)+' 条 · 第 '+page+' / '+last+' 页</span><button class="secondary" data-page="'+type+'" data-step="-1" '+(page===1?'disabled':'')+'>上一页</button><button class="secondary" data-page="'+type+'" data-step="1" '+(page===last?'disabled':'')+'>下一页</button>';
}
function renderOrders(){
  if(!report)return;
  const search=$('#order-search').value.trim().toLowerCase(),status=$('#order-status').value;
  const rows=report.orders.filter(r=>(!status||r.status===status)&&(!search||(r.order_id+' '+r.picker_id+' '+r.station).toLowerCase().includes(search)));
  orderPage=Math.min(orderPage,Math.max(1,Math.ceil(rows.length/pageSize)));
  $('#order-table').innerHTML=table(['订单编号','站点 / 工号','下单时间','件数','状态','履约情况'],rows.slice((orderPage-1)*pageSize,orderPage*pageSize).map(r=>[orderButton(r),esc(r.station)+'<small class="sub">'+esc(r.picker_id||'未分配')+'</small>',esc(timestamp(r.created_at).slice(5,16)),n(r.items),badge(statusNames[r.status],r.status==='delivered'?'good':''),r.reasons.length?badge(r.reasons[0],'bad'):'—']),'没有匹配订单，试试其他关键词或筛选条件');
  pager('order-pages',rows.length,orderPage,'orders');
}
function renderCases(){
  if(!report)return;
  const search=$('#exception-search').value.trim().toLowerCase(),state=$('#exception-state').value;
  const rows=report.exceptions.filter(r=>(!state||(state==='unfinished'?r.followup.state!=='resolved':r.followup.state===state))&&(!search||(r.order_id+' '+r.reasons.join(' ')+' '+r.followup.owner).toLowerCase().includes(search)));
  casePage=Math.min(casePage,Math.max(1,Math.ceil(rows.length/pageSize)));
  $('#exception-table').innerHTML=table(['订单编号 / 站点','异常原因','超时分钟','跟进状态','负责人'],rows.slice((casePage-1)*pageSize,casePage*pageSize).map(r=>[orderButton(r)+'<small class="sub">'+esc(r.station)+'</small>',esc(r.reasons.join(' / ')),n(r.late_minutes,1),badge(stateNames[r.followup.state],r.followup.state==='resolved'?'good':'bad'),esc(r.followup.owner||'未指定')]),'当前没有符合条件的异常，可切换“全部”查看已解决记录');
  pager('exception-pages',rows.length,casePage,'cases');
}
async function openOrder(id){
  const row=report.orders.find(r=>r.order_id===id);if(!row)return;
  const exception=report.exceptions.find(r=>r.order_id===id);
  $('#dialog-title').textContent=row.order_id;
  const pairs=[['站点',row.station],['拣货员工号',row.picker_id||'未分配'],['商品件数',n(row.items)],['订单状态',statusNames[row.status]],['登记问题',issueNames[row.issue]],['当前异常',row.reasons.join(' / ')||'无']];
  $('#order-detail').innerHTML='<dl class="detail-grid">'+pairs.map(([k,v])=>'<div><dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd></div>').join('')+'</dl><div class="timeline">'+[['下单','created_at'],['开始拣货','pick_started_at'],['拣货完成','pick_finished_at'],['包装完成 / 出仓','packed_at'],['送达','delivered_at'],['承诺送达','promised_at']].map(([k,v])=>'<p>'+esc(k)+'<small>'+esc(timestamp(row[v]))+'</small></p>').join('')+'</div>';
  $('#followup-form').hidden=!exception;$('#followup-message').textContent='';$('#case-history').innerHTML='';
  if(exception){const f=$('#followup-form');for(const key of ['state','owner','note','version'])f.elements[key].value=exception.followup[key];f.elements.order_id.value=row.order_id;}
  if(!$('#order-dialog').open)$('#order-dialog').showModal();
  if(exception){
    try{const history=await api('/api/history?order_id='+encodeURIComponent(id));if($('#dialog-title').textContent!==id)return;
      $('#case-history').innerHTML='<h3>跟进历史</h3>'+(history.length?history.map(r=>'<div class="history-item"><b>'+esc(stateNames[r.state])+' · '+esc(r.owner||'未指定')+'</b><p>'+esc(r.note||'无补充说明')+'</p><small>'+esc(timestamp(r.created_at))+'</small></div>').join(''):'<p class="caption">暂无跟进记录</p>');
    }catch(e){$('#case-history').textContent=e.message;}
  }
}
function renderPlan(){
  $('#plan-empty').hidden=true;$('#plan-result').hidden=false;
  $('#plan-summary').innerHTML=summary([['峰值需求',n(plan.peak_required)+' 人'],['合计需求',n(plan.required_hours)+' 人时'],['待补用工',n(plan.gap_hours)+' 人时']]);
  $('#plan-method').textContent=plan.method+' · '+plan.sample_days.length+' 个样本日 · '+(plan.station||'全部站点')+' · '+plan.date;
  chart('plan-chart',plan.hourly,'required','available','每小时需求人数与可用人数',{line:true});
  $('#plan-table').innerHTML=table(['时段','基准件量','预测件量','需求人数','可用人数','缺口'],plan.hourly.map(r=>[String(r.hour).padStart(2,'0')+':00',n(r.baseline_items,1),n(r.forecast_items,1),n(r.required),n(r.available),r.gap?badge(r.gap+' 人','bad'):'—']));
}
function updateImportKind(){
  importPayload=null;$('#commit-import').disabled=true;$('#import-preview').innerHTML='';$('#import-message').textContent='';
  const kind=$('#import-kind').value;$('#download-template').href='/api/template?kind='+kind;$('#download-sample').href='/api/sample?kind='+kind;
  $('#field-help').innerHTML=kind==='orders'?'<p><code>order_id</code> 唯一订单编号；<code>station</code> 站点；<code>picker_id</code> 拣货工号；<code>items</code> 正整数件数。</p><p><code>created_at / promised_at / pick_started_at / pick_finished_at / packed_at / delivered_at</code> 分别为下单、承诺送达、开拣、拣完、包装完成出仓、送达。格式 YYYY-MM-DD HH:MM:SS，可带时区；无时区按北京时间。</p><p><code>status</code>：delivered / in_progress / cancelled。<code>issue</code>：none / stockout / picking_error / damaged / delivery_delay / other。</p><p>实际作业时间不可在未来，不可倒序或缺少前序时间。已送达必须填写所有阶段时间。</p>':'<p><code>shift_id</code> 唯一工时记录；<code>employee_id</code> 员工工号；<code>station</code> 站点；<code>start_at / end_at</code> 班次起止时间；<code>break_minutes</code> 休息分钟；<code>role</code> 为 picker / packer。</p><p>同一员工所有站点、岗位的时间不得重叠。单条最多 16 小时，跨日班次请分成两条同日记录。休息时长必须小于班次时长。</p>';
  $('#field-help').innerHTML+='<p>预检不写入；提交时重新校验。相同业务编号与相同内容跳过；相同编号但不同内容整批拒绝。CSV 列顺序可调整，但必须保留所有模板列。</p>';
}
function importResult(result){
  const message=$('#import-message');
  message.className='form-message '+(result.valid?'success':'error');
  message.textContent=result.committed?'已导入 '+result.inserted+' 条，跳过 '+result.skipped+' 条重复记录。':result.valid?'预检通过：可新增 '+result.inserted+' 条，重复 '+result.skipped+' 条。请点击确认导入。':'发现 '+result.error_count+' 项错误，整批未写入。';
  $('#import-preview').innerHTML=result.errors.length?table(['CSV 行号','错误原因'],result.errors.map(r=>[n(r.line),esc(r.message)])):(result.sample.length?'<p class="caption">前 5 条有效记录</p>'+table(['业务编号','站点','日期'],result.sample.map(r=>[esc(r.order_id||r.shift_id),esc(r.station),esc(timestamp(r.created_at||r.start_at))])):'');
  $('#commit-import').disabled=!result.valid||result.committed||!result.inserted;
}
function downloadPlan(){
  if(!plan)return;
  const columns=['plan_date','station','method','sample_days','growth_pct','productivity','utilization_pct','hour','baseline_items','forecast_items','required','available','gap'];
  const cell=v=>'"'+String(v??'').replace(/^[=+@-]/,"'$&").replace(/"/g,'""')+'"';
  const rows=plan.hourly.map(r=>[plan.date,plan.station,plan.method,plan.sample_days.join('|'),plan.growth,plan.productivity,plan.utilization,r.hour,r.baseline_items,r.forecast_items,r.required,r.available,r.gap]);
  const csv=String.fromCharCode(65279)+[columns,...rows].map(row=>row.map(cell).join(',')).join(String.fromCharCode(13,10));
  const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='staffing-'+plan.date+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
window.addEventListener('hashchange',navigate);
let resizeTimer;
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(redrawCharts,150);});
$('#filters').addEventListener('submit',async e=>{e.preventDefault();try{await busy(e.submitter,loadReport);}catch(ex){error(ex.message);}});
$('#refresh').addEventListener('click',async e=>{try{await busy(e.currentTarget,async()=>{await loadMeta();await loadReport();});}catch(ex){error(ex.message);}});
for(const selector of ['#order-search','#order-status'])$(selector).addEventListener('input',()=>{orderPage=1;renderOrders();});
for(const selector of ['#exception-search','#exception-state'])$(selector).addEventListener('input',()=>{casePage=1;renderCases();});
document.addEventListener('click',e=>{const order=e.target.closest('[data-order]');if(order)openOrder(order.dataset.order);const page=e.target.closest('[data-page]');if(page){const step=Number(page.dataset.step);if(page.dataset.page==='orders'){orderPage+=step;renderOrders();}else{casePage+=step;renderCases();}}});
$('#followup-form').addEventListener('submit',async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.currentTarget));try{await busy(e.submitter,async()=>{await api('/api/followup',data);await loadReport();await openOrder(data.order_id);});}catch(ex){$('#followup-message').textContent=ex.message;}});
$('#plan-form').addEventListener('submit',async e=>{e.preventDefault();$('#plan-error').textContent='';try{await busy(e.submitter,async()=>{plan=await api('/api/staffing',Object.fromEntries(new FormData(e.currentTarget)));renderPlan();});}catch(ex){$('#plan-error').textContent=ex.message;$('#plan-result').hidden=true;$('#plan-empty').hidden=false;}});
$('#plan-form').addEventListener('input',()=>{if(plan){$('#plan-result').hidden=true;$('#plan-empty').hidden=false;plan=null;}});
$('#export-plan').addEventListener('click',downloadPlan);
$('#import-kind').addEventListener('change',updateImportKind);
$('#import-file').addEventListener('change',()=>{importPayload=null;$('#commit-import').disabled=true;$('#import-preview').innerHTML='';$('#import-message').textContent='';});
$('#import-form').addEventListener('submit',async e=>{e.preventDefault();importPayload=null;$('#commit-import').disabled=true;try{await busy(e.submitter,async()=>{
  const file=$('#import-file').files[0];if(!file||file.size>4000000)throw new Error('请选择小于 4 MB 的 UTF-8 CSV 文件');
  let content;try{content=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());}catch{throw new Error('文件不是 UTF-8 编码，请在 Excel 中另存为 CSV UTF-8');}
  const payload={kind:$('#import-kind').value,filename:file.name,content};const result=await api('/api/import/preview',payload);if(result.valid)importPayload=payload;importResult(result);
});}catch(ex){$('#import-message').className='form-message error';$('#import-message').textContent=ex.message;}});
$('#commit-import').addEventListener('click',async e=>{if(!importPayload)return;const button=e.currentTarget;try{await busy(button,async()=>{const result=await api('/api/import/commit',importPayload);importResult(result);if(result.committed){importPayload=null;await loadMeta();await loadReport();}});}catch(ex){$('#import-message').className='form-message error';$('#import-message').textContent=ex.message;}finally{button.disabled=!importPayload;}});
$('#order-dialog').addEventListener('click',e=>{if(e.target===$('#order-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
(async()=>{navigate();updateImportKind();try{await loadMeta(true);await loadReport();}catch(ex){error('无法加载数据：'+ex.message);$('#mode').textContent='连接失败';}})();
