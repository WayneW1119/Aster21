const $ = id => document.getElementById(id);
const APP_BASE = new URL('./', location.href);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {online:'在线',offline:'已暂停',starting:'启动中',stopping:'暂停中',error:'需要处理',archived:'已归档'};
window.name = 'aster-console';
let token = sessionStorage.getItem('squad-token') || '', fleet = null, detail = null;
let browserSession = false, securityInfo = null;
let selectedSquad = sessionStorage.getItem('squad-selected') || '', selectedAgent = null;
const mobileLayout = matchMedia('(max-width:700px)');
const isMobile = () => mobileLayout.matches;
// Mobile sheets share the scrim's parent, outside the content scroller's
// stacking context. Desktop keeps the same panel in the workbench grid.
const detailPanel = $('detailPanel'), detailHome = detailPanel.parentElement;
function placeDetailPanel() {
  (isMobile() ? $('app') : detailHome).append(detailPanel);
}
mobileLayout.addEventListener('change', placeDetailPanel);
placeDetailPanel();
let view = location.hash === '#domains' ? 'domains' : 'squads', layout = isMobile() ? 'list' : 'tree', zoom = 1, busy = 0, refreshing = false, generation = 0;
let editingAgent = null, editingResource = null, creatingKind = 'squad', terminalAgent = null;

if (location.hash.startsWith('#token=')) {
  token = decodeURIComponent(location.hash.slice(7));
  history.replaceState(null, '', location.pathname);
}
async function api(path, body) {
  const response = await fetch(new URL('api/' + path, APP_BASE), {method:body === undefined ? 'GET':'POST', credentials:'same-origin', headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '请求失败');
  return data;
}
function resource() { return fleet?.resources.find(r => r.id === selectedSquad); }
function allAgents() { return detail?.agents || []; }
function selected() { return allAgents().find(a => a.id === selectedAgent); }
function activeAgents() { return allAgents().filter(a => !a.archived); }
function squadApi(path, body, id = selectedSquad) { return api('squads/'+id+'/'+path, body); }
function notify(text, error = false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); $('notice').classList.toggle('hidden', !text); }
function avatarClass(a) { return 'c' + ([...a.handle].reduce((v,c)=>v+c.charCodeAt(0),0)%4); }
function initials(a) { return a.handle.slice(0,2).toUpperCase(); }
const phases = {starting:'准备中',processing:'处理中',thinking:'模型响应中',tools:'执行工具',waiting:'等待事件',retrying:'重试中'};
function status(a) {
  const phase=a.status==='online'?a.telemetry?.phase:null;
  return `<i class="status-dot ${esc(a.status)} ${esc(phase||'')}"></i>${phases[phase]||labels[a.status]||esc(a.status)}`;
}
const tokens = n => Number.isInteger(n)&&n>=0 ? n.toLocaleString() : '未上报';
const compactTokens = n => Number.isInteger(n)&&n>=0 ? (n>=1000?(n/1000).toFixed(1)+'k':String(n)) : '—';
const percent = n => typeof n==='number'&&Number.isFinite(n) ? (n*100).toFixed(1)+'%' : '未上报';
function lastCache(a) {const u=a.telemetry?.latest;return u?.input_tokens>0&&u.cached_tokens!==null ? u.cached_tokens/u.input_tokens : null;}
function metricStrip(a) {
  const t=a.telemetry||{}, u=t.latest;
  const hint=t.context_compacted?'已压缩，等待下次测量':'最近请求输入 token，包含指令、历史、记忆和工具；不是实时上下文估计';
  return `<div class="agent-metrics" aria-label="运行用量"><span title="${esc(hint)}">上下文 <b>${compactTokens(u?.input_tokens)}${t.context_compacted?'*':''}</b></span><span title="最近一次请求的缓存 token / 输入 token；缺失不等于零">缓存 <b>${lastCache(a)===null?'—':percent(lastCache(a))}</b></span></div>`;
}
function runtimeDetail(a) {
  const t=a.telemetry||{},u=t.latest,w=t.cache_window;
  const measured=u?new Date(u.at*1000).toLocaleString():'暂无模型用量数据';
  const model=u&&u.model!==a.config.model?' · 来自上次模型 '+u.model:'';
  return `<section class="runtime-metrics" aria-label="上下文与缓存"><p class="section-label">上下文与缓存</p><div class="metric-pair"><div><small>最近请求输入</small><strong>${tokens(u?.input_tokens)}</strong><small>token · 含缓存部分</small></div><div><small>最近缓存命中</small><strong>${percent(lastCache(a))}</strong><small>${tokens(u?.cached_tokens)} / ${tokens(u?.input_tokens)} token</small></div></div><p class="metric-note">${esc(measured+model)}${t.context_compacted?' · 已压缩，等待下次请求重新测量':''}</p><div class="detail-row"><label>最近输出</label><span>${tokens(u?.output_tokens)} token</span></div><div class="detail-row"><label>近 ${w?.requests||0} 次加权缓存命中</label><span>${percent(w?.hit_rate)}</span></div><p class="metric-note">${w?.reported||0} 次请求上报缓存数据；以这些请求的缓存 token 总数 / 输入 token 总数计算。缺失数据不按零计算。</p><p class="metric-note">输入包含指令、记忆、历史及工具定义，以服务端最近一次用量为准；不含其后的输出和工具结果，不代表模型最大窗口。</p>${a.status==='online'&&t.phase==='tools'&&t.tool?'<p class="metric-note">当前工具：'+esc(t.tool)+'</p>':''}</section>`;
}

async function login() {
  try {
    fleet = await api('state');
    if(!selectedSquad||(fleet.resources.find(r=>r.id===selectedSquad)?.mode==='fake'&&fleet.resources.find(r=>r.id===selectedSquad)?.status==='stopped'))selectedSquad=fleet.resources.find(r=>r.kind==='squad'&&r.mode==='real'&&r.status==='running')?.id||'studio';
    if(token)sessionStorage.setItem('squad-token', token);else sessionStorage.removeItem('squad-token');
    $('login').classList.add('hidden'); $('app').classList.remove('hidden');
    if (!fleet.resources.some(r=>r.kind==='squad'&&r.id===selectedSquad)) selectedSquad = fleet.resources.find(r=>r.kind==='squad')?.id;
    await selectSquad(selectedSquad, false);
    showView(view);
  } catch (error) { $('loginError').textContent = error.message; }
}
async function refresh() {
  if ((!token && !browserSession) || !fleet || busy || refreshing) return;
  const epoch = generation, id = selectedSquad;
  refreshing = true;
  try {
    const [next, nextDetail] = await Promise.all([api('state'), id ? squadApi('state', undefined, id) : Promise.resolve(null)]);
    if (epoch !== generation) return;
    fleet = next; detail = nextDetail;
    render();
  } catch (error) { if (epoch === generation) notify(error.message, true); }
  finally { refreshing = false; }
}
async function selectSquad(id, changeView = true) {
  selectedSquad = id; selectedAgent = null; generation++;
  sessionStorage.setItem('squad-selected', id || '');
  const epoch = generation;
  if (changeView) view = 'squads';
  setNavigation(false);
  try {
    const next = id ? await squadApi('state', undefined, id) : null;
    if (epoch !== generation) return;
    detail = next;
    selectedAgent = isMobile() ? null : activeAgents()[0]?.id || null;
    notify(''); render(); showView(view);
  } catch (error) { notify(error.message, true); }
}
function showView(next, closeNav = true) {
  view = next;
  if(view==='domains'){selectedAgent=null;$('app').classList.remove('has-selection');}
  $('squadView').classList.toggle('hidden', view !== 'squads');
  $('domainsView').classList.toggle('hidden', view !== 'domains');
  $('squadsNav').classList.toggle('active', view === 'squads');
  $('domainsNav').classList.toggle('active', view === 'domains');
  $('breadcrumb').textContent = view === 'domains' ? 'Relay 聊天域' : resource()?.name || 'Squads';
  if(closeNav)setNavigation(false);
  $('mobileSpaces').classList.toggle('active', view === 'squads');
  $('mobileDomains').classList.toggle('active', view === 'domains');
  if (view === 'domains') renderDomains();
}
function render() {
  const squads = fleet.resources.filter(r=>r.kind==='squad');
  $('squadCount').textContent = squads.length; $('domainCount').textContent = fleet.resources.length;
  $('squadList').innerHTML = squads.map(r=>`<button class="squad-item ${r.id===selectedSquad&&view==='squads'?'selected':''}" data-squad="${esc(r.id)}"><span class="squad-icon">${esc(r.name.slice(0,1))}</span><div><strong>${esc(r.name)}</strong><small>${r.agents} 位成员 · ${({running:'运行中',starting:'启动中',stopping:'安全暂停中'})[r.status]||'已暂停'}</small></div></button>`).join('');
  $('squadList').querySelectorAll('[data-squad]').forEach(b=>b.onclick=()=>selectSquad(b.dataset.squad));
  const r = resource();
  $('squadTitle').textContent = r?.name || '我的工作空间';
  $('squadDescription').textContent = r?.description || '每个成员独立工作，在同一个目标下协作。';
  $('modeTag').textContent = r?.mode === 'real' ? '真实模型' : '离线演示';
  $('newAgent').disabled = !!r && r.status !== 'running';
  $('newAgent').textContent=r?'＋ 新增成员':'＋ 新建 Squad';
  $('resourceSettings').disabled=!r;
  $('stoppedBanner').classList.toggle('hidden', !r || r.status === 'running');
  const stopping=r?.status==='stopping';$('stoppedBanner').querySelector('b').textContent=stopping?'这个 Squad 正在安全暂停':'这个 Squad 已暂停';$('stoppedBanner').querySelector('span').textContent=stopping?'正在等待当前步骤保存检查点，不会自动强制结束。':'成员和工作记录已保留。启动后可继续管理。';$('startResource').disabled=stopping;
  const agents = activeAgents(), online = agents.filter(a=>a.status==='online').length, archived = allAgents().filter(a=>a.archived).length;
  const stats = [['▦',agents.length,'位成员'],['◉',online,'位成员在线'],['⑂',agents.filter(a=>!a.parent).length,'条独立分支'],['▧',archived,'位成员已归档']];
  $('stats').innerHTML = stats.map(([icon,value,label])=>`<div class="stat"><span class="stat-icon">${icon}</span><div><strong>${value}</strong><small>${label}</small></div></div>`).join('');
  renderOrganization(); renderDetail(); showView(view, false);
}
function visibleAgents() {
  const candidates = allAgents().filter(a=>!a.archived || $('showArchived').checked), query = $('search').value.trim().toLowerCase();
  if (!query) return candidates;
  const keep = new Set(candidates.filter(a=>(a.handle+' '+a.config.instruction).toLowerCase().includes(query)).map(a=>a.id));
  for (const id of [...keep]) {
    let current = candidates.find(a=>a.id===id), seen = new Set();
    while (current?.parent && !seen.has(current.parent)) {seen.add(current.parent);keep.add(current.parent);current=candidates.find(a=>a.id===current.parent);}
  }
  return candidates.filter(a=>keep.has(a.id));
}
function renderOrganization() {
  for(const mode of ['tree','list'])$(mode+'View').classList.toggle('on',layout===mode);
  const agents = visibleAgents(), ids = new Set(agents.map(a=>a.id)), roots = agents.filter(a=>!a.parent||!ids.has(a.parent));
  function node(a, ancestry = new Set()) {
    if (ancestry.has(a.id)) return '';
    const next = new Set([...ancestry,a.id]), children = agents.filter(child=>child.parent===a.id);
    const parent = allAgents().find(p=>p.id===a.parent);
    return `<li><article class="node ${a.id===selectedAgent?'selected':''} ${a.archived?'archived':''}" data-agent="${a.id}" tabindex="0" role="button" aria-label="查看 ${esc(a.handle)}"><div class="node-head"><span class="avatar ${avatarClass(a)}">${initials(a)}</span><div><h3>${esc(a.handle)}</h3><span class="lineage">${parent?'直属 '+esc(parent.handle):'独立成员'}</span></div>${!a.parent?'<span class="node-badge">ROOT</span>':''}</div><p class="node-role">${esc(a.config.instruction)}</p>${metricStrip(a)}<div class="node-footer">${status(a)}<span class="spacer"></span><span class="model">${esc(a.config.model)}</span></div>${!a.archived&&resource()?.status==='running'?`<button class="add-child" data-parent="${a.id}" aria-label="在 ${esc(a.handle)} 下新增成员">＋</button>`:''}</article>${children.length?'<ul>'+children.map(c=>node(c,next)).join('')+'</ul>':''}</li>`;
  }
  $('tree').innerHTML = roots.length ? '<ul>'+roots.map(a=>node(a)).join('')+'</ul>' : `<div class="empty-canvas"><span>✳</span><h3>${$('search').value?'没有匹配的成员':'从第一位成员开始'}</h3><p>${$('search').value?'试试其他名称或职责关键词。':'给它一个身份和目标。随后可以沿着分支，逐步组建团队。'}</p>${!$('search').value?'<button class="button primary" id="emptyCreate">＋ 新增第一位成员</button>':''}</div>`;
  if ($('emptyCreate')) $('emptyCreate').onclick=()=>resource()?openAgent():openResource('squad');
  $('tree').style.transform = `scale(${zoom})`; $('zoomLabel').textContent = Math.round(zoom*100)+'%';
  $('treeCaption').textContent = `${agents.length} 位成员 · ${roots.length} 条分支`;
  $('treeCanvas').classList.toggle('hidden', layout !== 'tree'); $('memberTable').classList.toggle('hidden', layout !== 'list');
  $('memberTable').innerHTML = '<table><thead><tr><th>成员</th><th>直属上级</th><th>模型</th><th>状态</th><th>上下文（输入）</th><th>缓存命中</th></tr></thead><tbody>'+agents.map(a=>`<tr data-agent="${a.id}" class="${a.id===selectedAgent?'selected':''}"><td><span class="member-name"><span class="avatar ${avatarClass(a)}">${initials(a)}</span>${esc(a.handle)}</span></td><td>${esc(allAgents().find(p=>p.id===a.parent)?.handle||'独立成员')}</td><td>${esc(a.config.model)}</td><td>${status(a)}</td><td class="table-metric" title="最近请求输入 token">${compactTokens(a.telemetry?.latest?.input_tokens)}${a.telemetry?.context_compacted?'*':''}</td><td class="table-metric">${lastCache(a)===null?'未上报':percent(lastCache(a))}</td></tr>`).join('')+'</tbody></table>';
  for (const container of [$('tree'),$('memberTable')]) container.querySelectorAll('[data-agent]').forEach(el=>{el.onclick=()=>{selectedAgent=el.dataset.agent;renderOrganization();renderDetail();};el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click();}};});
  $('tree').querySelectorAll('[data-parent]').forEach(b=>b.onclick=e=>{e.stopPropagation();openAgent(null,b.dataset.parent);});
}
function renderDetail() {
  const advancedOpen=$('detailPanel').querySelector('.advanced')?.open;
  const a = selected();
  $('app').classList.toggle('has-selection',!!a);
  const panelScroll=$('detailPanel').scrollTop;
  if (!a) { $('detailPanel').innerHTML='<div class="detail-empty"><span>◎</span><h3>认识你的成员</h3><p>选择分支中的一位成员，查看职责、配置与工作记录。</p></div>'; return; }
  const parent = allAgents().find(p=>p.id===a.parent), canRun = resource()?.status === 'running';
  $('detailPanel').innerHTML = `<header class="detail-header"><span class="avatar ${avatarClass(a)}">${initials(a)}</span><div><h2>${esc(a.handle)}</h2><span class="account">Relay · @${esc(a.relay_username)}</span></div><span class="spacer"></span><button id="closeDetail" class="icon-button" aria-label="关闭成员详情">×</button></header><div class="detail-content"><p class="section-label">成员概览</p><div class="detail-row"><label>运行状态</label><span>${status(a)}</span></div><div class="detail-row"><label>模型</label><span>${esc(a.config.model)}</span></div><div class="detail-row"><label>运行身份</label><span>${a.config.run_as_root?'root':a.config.windows_admin?'Windows 管理员':'宿主普通用户'}</span></div><div class="detail-row"><label>沙箱</label><span>${detail.mode==='fake'?'演示模式':a.config.run_as_root?'root 身份 · 路径规则已保留':a.config.windows_admin?'管理员身份 · 路径规则已保留':a.sandbox_enforced?'已开启':'已关闭 · 路径规则已保留'}</span></div><div class="detail-row"><label>直属上级</label><span>${esc(parent?.handle||'用户 · 独立成员')}</span></div><div class="detail-row"><label>直接下级</label><span>${activeAgents().filter(c=>c.parent===a.id).length} 位成员</span></div><div class="detail-separator"></div>${runtimeDetail(a)}<div class="detail-separator"></div><p class="section-label">身份与职责</p><p class="role-text">${esc(a.config.instruction)}</p><button id="viewInstruction" class="text-button">查看完整 instruction ↗</button><div class="detail-separator"></div><p class="section-label">已配置能力</p><div class="skill-chips"><span>◈ Squad · MCP + Skill</span><span>◎ Relay · MCP + Skill</span>${(a.mcp_services||[]).map(id=>'<span>'+esc((detail.mcp_services||[]).find(s=>s.id===id)?.name||id)+' · MCP</span>').join('')}${a.config.local_ipc===true?'<span>本机进程通信 · 已授权</span>':''}${a.config.host_systemd===true?'<span>宿主用户级 systemd · 已授权</span>':''}</div>${a.permission_update?.pending?'<p class="permission-status" role="status">'+(a.status==='online'?'权限变更已保存，等待当前步骤结束后生效。':'权限变更已保存，成员恢复运行时应用。')+(a.permission_update.error?' '+esc(a.permission_update.error):'')+'</p>':a.permission_update?.status==='failed'?'<p class="agent-error">权限更新失败，原权限已保留：'+esc(a.permission_update.error||'请检查路径后重试')+'</p>':''}${a.permission_update?.unavailable_mcp?.length?'<p class="agent-error" role="status">MCP 配置已保存，但这些服务未连接：'+a.permission_update.unavailable_mcp.map(esc).join('、')+'。检查服务后，取消勾选并保存，再重新启用以重试。</p>':''}${a.error?'<p class="agent-error">'+esc(a.error)+'</p>':''}<div class="detail-separator"></div><div class="detail-actions">${a.archived?'<button id="restoreAgent" class="button primary wide">恢复成员</button>':`<button id="editAgent" class="button" ${!canRun?'disabled':''}>编辑配置</button><button id="agentChat" class="button primary" ${!canRun?'disabled':''}>打开聊天 ↗</button><button id="agentRun" class="button" ${!canRun?'disabled':''}>${a.status==='online'||a.status==='starting'?'正常暂停':'启动成员'}</button><button id="addChild" class="button" ${!canRun?'disabled':''}>＋ 新增下级</button>`}</div><div class="detail-separator"></div><button id="viewTerminal" class="text-button" ${!canRun?'disabled':''}>基础终端 ↗</button>${!a.archived?'<button id="repairChat" class="text-button">修复聊天连接</button><button id="archiveAgent" class="text-button danger">归档此成员</button>':''}<details class="advanced"><summary>技术详情与记录</summary><label>工作区</label><code>${esc(a.workspace)}</code><label>Agent ID</label><code>${esc(a.id)}</code><label>Aster Thread</label><code>${esc(a.thread||'尚未创建')}</code>${(detail.audit||[]).filter(row=>row.target===a.id).slice(0,8).map(row=>'<div>'+new Date(row.created*1000).toLocaleString()+' · '+esc(row.action)+'</div>').join('')}</details></div>`;
  if(advancedOpen)$('detailPanel').querySelector('.advanced').open=true;
  const actions=$('detailPanel').querySelector('.detail-actions'),firstSeparator=$('detailPanel').querySelector('.detail-separator');
  if(actions.previousElementSibling?.classList.contains('detail-separator'))actions.previousElementSibling.remove();
  firstSeparator.before(actions);
  $('closeDetail').onclick=()=>{selectedAgent=null;renderOrganization();renderDetail();};
  $('viewInstruction').onclick=()=>{$('instructionTitle').textContent=a.handle+' · 完整 instruction';$('instructionText').textContent=a.instruction;$('instructionDialog').showModal();};
  if ($('editAgent')) $('editAgent').onclick=()=>openAgent(a);
  if ($('agentChat')) $('agentChat').onclick=()=>openRelay(selectedSquad);
  if ($('addChild')) $('addChild').onclick=()=>openAgent(null,a.id);
  if ($('agentRun') && a.status==='stopping') {$('agentRun').disabled=true;$('agentRun').textContent='等待安全暂停';}
  if ($('agentRun')) $('agentRun').onclick=()=>agentAction(a, a.status==='online'||a.status==='starting'?'stop':'start');
  if ($('restoreAgent')) $('restoreAgent').onclick=()=>agentAction(a,'restore');
  if ($('repairChat')) $('repairChat').onclick=()=>agentAction(a,'reconnect');
  if ($('archiveAgent')) $('archiveAgent').onclick=()=>{if(confirm(`归档 ${a.handle}？它会正常暂停，对应聊天账号会停用。Thread、工作区和消息均保留。`))agentAction(a,'archive');};
  for(const id of ['restoreAgent','repairChat','archiveAgent'])if($(id))$(id).disabled=!canRun;
  $('viewTerminal').onclick=()=>openTerminal(a);
  $('detailPanel').scrollTop=panelScroll;
}
async function agentAction(a, action) {
  const id = selectedSquad; busy++;
  try { await squadApi('agents/'+a.id+'/'+action,{},id); notify(action==='restore'?'成员已恢复，可以重新启动。':'操作完成。'); }
  catch(error) {notify(error.message,true);}
  finally {busy--;await refresh();}
}
const permissionTypes = [
  {access:'read',title:'只读',hint:'可以查看文件，不能修改。未匹配任何规则的路径禁止访问；/ 表示整个文件系统，可编辑或删除。'},
  {access:'write',title:'可读写',hint:'可以读取、创建、修改和删除。尚不存在的路径按目录处理，保存时自动创建，只授权该目录。'},
  {access:'deny',title:'禁止访问',hint:'不能读取或写入。路径越具体越优先，可在禁止的父目录内开放指定子目录。系统私有数据不能在这里解除保护。'}
];
function renderPermissions(a) {
  const protectedPaths=a?.filesystem?.protected_paths||[];
  const openings=a?.filesystem?.platform_openings||[];
  $('grantRows').innerHTML=permissionTypes.map(({access,title,hint})=>`<section class="permission-group permission-${access}" aria-label="${title}路径"><header><h3>${title}<span id="grantCount-${access}" class="permission-count">0</span></h3><button type="button" class="button compact" data-add-access="${access}" aria-label="添加${title}路径">＋ 添加路径</button></header><p class="permission-hint">${hint}</p>${access==='read'?`<details class="permission-protected"><summary>系统运行文件 · 只读上限</summary><p class="permission-hint">系统为命令执行保留这些运行文件；更具体的禁止规则仍生效，平台私有数据始终受保护。</p>${(a?.filesystem?.runtime_read_paths||[]).map(path=>`<code>${esc(path)}</code>`).join('')}</details>`:access==='write'?`<div class="permission-default"><span>自己的工作区 · 默认可写</span><code>${esc(a?.workspace||'创建成员时自动分配')}</code></div>`:protectedPaths.length?`<details class="permission-protected"><summary>系统保护路径 · ${protectedPaths.length} 项</summary><p class="permission-hint">这些数据区默认隐藏，系统仅开放自己的工作区、连接配置和必要运行数据。普通授权不能开放其他成员的私有数据。</p>${protectedPaths.map(path=>`<code>${esc(path)}</code>`).join('')}<p class="permission-hint">系统预设的访问例外</p>${openings.map(g=>`<code>${esc(g.access==='write'?'可读写':'只读')} · ${esc(g.path)}</code>`).join('')}</details>`:'<p class="permission-hint">平台私有数据与其他成员的私有目录由系统保护。</p>'}<div id="grantList-${access}" class="grant-rows" data-empty="尚未添加自定义${title}路径"></div></section>`).join('');
  if(detail.mode==='fake')$('grantRows').insertAdjacentHTML('afterbegin','<p class="permission-demo">当前是离线演示模式：可以管理规则，但不启用文件沙箱。</p>');
  $('grantRows').querySelectorAll('[data-add-access]').forEach(button=>button.onclick=()=>{
    const row=addGrant({path:'',access:button.dataset.addAccess});row.querySelector('textarea').focus({preventScroll:true});
  });
  (a?.config.grants||( $('agentParent').value ? [] : [{path:'/',access:'read'}])).forEach(addGrant);
}
function renderSandboxHint(){
  $('agentSandboxEnabled').disabled=detail.sandbox_toggle_available!==true;
  $('rootStateHint').textContent=detail.mode==='fake'?'演示空间不运行 root 成员。':detail.root_available!==true?'宿主尚未准备好 root 启动能力。':$('agentRunAsRoot').checked?'root 身份运行期间保留沙箱设置，路径规则暂不生效。':'普通宿主用户身份；按沙箱设置执行。';
  if($('agentRunAsRoot').checked){$('sandboxStateHint').textContent='设置已保留；root 身份期间不应用路径沙箱。';return;}
  const admin=$('agentWindowsAdmin');
  if(!admin.disabled&&admin.checked){
    $('sandboxStateHint').textContent='管理员模式期间不应用路径沙箱；这里保存的开关和规则将在退出管理员模式后恢复。';
    return;
  }
  $('sandboxStateHint').textContent=detail.mode==='fake'?'演示空间不运行真实模型或启用操作系统沙箱。':detail.sandbox_toggle_available!==true?'该空间需重新启动后才能使用沙箱开关；现有规则保持不变。':$('agentSandboxEnabled').checked?'启用后按下方保存的路径规则隔离。':'路径规则继续保存，关闭期间不生效；再次开启后恢复原配置。';
}
function resizeGrantInput(input){input.style.height='auto';input.style.height=Math.min(240,Math.max(84,input.scrollHeight+2))+'px';input.scrollTop=0;}
function addGrant(value={path:'',access:'read'}) {
  const group=permissionTypes.find(type=>type.access===value.access);
  if(!group)return;
  const row=document.createElement('div');row.className='grant-row';row.dataset.access=value.access;
  row.innerHTML=`<textarea aria-label="${group.title}路径" placeholder="输入宿主的绝对路径，例如 /home/yourname/projects/new-project" rows="2" maxlength="4096" autocapitalize="none" autocorrect="off" spellcheck="false" required></textarea><button class="button danger" type="button" aria-label="删除${group.title}路径">删除</button>`;
  const input=row.querySelector('textarea');input.value=value.path;input.oninput=()=>{resizeGrantInput(input);input.removeAttribute('aria-invalid');$('agentFormError').textContent='';};
  const list=$('grantList-'+value.access),count=$('grantCount-'+value.access);
  row.querySelector('button').onclick=()=>{row.remove();count.textContent=list.children.length;};
  list.append(row);count.textContent=list.children.length;if($('agentDialog').open)resizeGrantInput(input);return row;
}
function openAgent(a=null,parent=null) {
  if(resource()?.status!=='running'){notify('请先启动这个 Squad。',true);return;}
  editingAgent=a?.id||null; $('agentForm').reset();for(const field of $('agentForm').elements)field.removeAttribute('aria-invalid');$('grantRows').innerHTML='';$('agentFormError').textContent='';
  $('agentDialogTitle').textContent=a?'编辑 '+a.handle:'新增成员';$('saveAgent').textContent=a?'保存配置':'创建并启动';
  $('agentHandle').disabled=!!a;$('agentHandle').value=a?.handle||'';
  $('agentParent').innerHTML='<option value="">独立成员 · 由用户管理</option>'+activeAgents().filter(p=>p.id!==a?.id).map(p=>`<option value="${p.id}">在 ${esc(p.handle)} 下创建 child</option>`).join('');
  $('agentParent').value=a?.parent||parent||'';$('agentParent').disabled=!!a;
  $('agentModel').innerHTML=(detail.models||['fake-echo']).map(m=>`<option>${esc(m)}</option>`).join('');
  $('agentModel').value=a?.config.model||allAgents().find(p=>p.id===parent)?.config.model||detail.models?.[0]||'fake-echo';
  $('agentInstruction').value=a?.config.instruction||'';renderPermissions(a);
  const services=detail.mcp_services||[];
  const selected=a?(a.mcp_services||a.config.mcp_services||services.filter(s=>s.default).map(s=>s.id)):services.filter(s=>s.default).map(s=>s.id);
  const enrolled=!a||!!a.config.relay_namespace;
  $('mcpServicesField').classList.toggle('hidden',!enrolled&&services.length===0);
  $('squadBaseSuite').hidden=!enrolled;
  $('serviceSectionTitle').textContent=enrolled?'能力套件':'MCP 服务';
  $('mcpServiceChoices').innerHTML=services.map(s=>`<label class="mcp-service-option"><input type="checkbox" value="${esc(s.id)}" ${selected.includes(s.id)?'checked':''}><span>${esc(s.name)}<small>${esc(s.id)}</small></span></label>`).join('');

  $('agentRunAsRoot').checked=a?.config.run_as_root===true;
  $('agentLinuxRootField').classList.toggle('hidden','windows_admin_available' in detail);
  $('agentRunAsRoot').disabled=detail.mode==='fake'||(detail.root_available!==true&&!$('agentRunAsRoot').checked);
  $('agentRunAsRoot').onchange=renderSandboxHint;
  $('agentHostSystemd').checked=a?.config.host_systemd===true;
  $('agentHostSystemd').disabled=!a||detail.host_systemd_available!==true;
  $('connectMcpField').hidden=!!a&&!a.config.relay_namespace;
  $('agentConnectMcp').checked=a?.config.allow_connect===true;
  $('agentLocalIpc').checked=a?.config.local_ipc===true;
  $('agentLocalIpc').disabled=detail.local_ipc_available!==true;
  $('agentLocalIpcField').classList.toggle('hidden','windows_admin_available' in detail);
  $('agentGpuAccess').checked=a?.config.gpu_access===true;
  $('agentGpuAccess').disabled=detail.mode==='fake'||(!detail.gpu_available&&!$('agentGpuAccess').checked);
  $('gpuAccessHint').textContent=detail.gpu_available?'授权后沙箱可使用本机 GPU。目前支持 WSL2 /dev/dxg。':'当前空间未提供受支持的 GPU 设备。';
  $('agentSandboxEnabled').checked=a?.config.sandbox_enabled!==false;
  $('agentWindowsAdminField').classList.toggle('hidden',detail.windows_admin_available!==true);
  $('agentWindowsAdmin').disabled=detail.windows_admin_available!==true;
  $('agentWindowsAdmin').checked=a?.config.windows_admin===true;
  $('agentWindowsAdmin').onchange=renderSandboxHint;
  $('agentSandboxEnabled').onchange=renderSandboxHint;renderSandboxHint();
  $('agentDialog').showModal();$('grantRows').querySelectorAll('textarea').forEach(resizeGrantInput);
}
async function saveAgent(event) {
  event.preventDefault();
  if($('saveAgent').disabled)return;
  const form=$('agentForm'), handle=$('agentHandle');
  handle.value=handle.value.trim();$('agentInstruction').value=$('agentInstruction').value.trim();
  for(const field of form.elements)field.removeAttribute('aria-invalid');
  const invalid=[...form.elements].find(field=>!field.disabled&&field.validity&&!field.validity.valid);
  if(invalid){
    const message=invalid===handle?'请填写成员用户名：以小写字母开头，最多 24 位，可含数字、下划线和减号。':invalid===$('agentInstruction')?'请填写这位成员的身份与职责。':invalid===$('agentModel')?'请选择可用模型。':'请填写完整的授权路径，或移除空白路径。';
    $('agentFormError').textContent=message;invalid.setAttribute('aria-invalid','true');invalid.focus();return;
  }
  if(handle.value==='user'){$('agentFormError').textContent='user 是保留用户名，请换一个名称。';handle.focus();return;}
  const id=selectedSquad, editing=editingAgent;busy++;$('saveAgent').disabled=true;$('saveAgent').textContent=editing?'正在保存…':'正在创建并启动…';form.setAttribute('aria-busy','true');$('agentFormError').textContent='';
  try {
    const grants=[...$('grantRows').querySelectorAll('.grant-row')].map(row=>({path:row.querySelector('textarea').value.trim(),access:row.dataset.access}));
    const body={instruction:$('agentInstruction').value.trim(),model:$('agentModel').value,grants};
    if((detail.mcp_services||[]).length)body.mcp_services=[...$('mcpServiceChoices').querySelectorAll('input:checked')].map(input=>input.value);
    if(editing&&!$('agentHostSystemd').disabled)body.host_systemd=$('agentHostSystemd').checked;
    if(!$('agentRunAsRoot').disabled)body.run_as_root=$('agentRunAsRoot').checked;
    if(!$('agentWindowsAdmin').disabled)body.windows_admin=$('agentWindowsAdmin').checked;
    if(!$('agentSandboxEnabled').disabled)body.sandbox_enabled=$('agentSandboxEnabled').checked;
    if(!$('connectMcpField').hidden)body.allow_connect=$('agentConnectMcp').checked;
    if(!$('agentLocalIpc').disabled)body.local_ipc=$('agentLocalIpc').checked;
    if(!$('agentGpuAccess').disabled)body.gpu_access=$('agentGpuAccess').checked;
    if(!editing){body.handle=$('agentHandle').value.trim();body.parent=$('agentParent').value||null;}
    const result=await squadApi(editing?'agents/'+editing+'/configure':'agents',body,id);
    if(id===selectedSquad)selectedAgent=result.id;
    if(result.startup_warning){
      $('agentDialog').close();
      notify(result.startup_warning+(result.configuration_update==='rolled_back'?'；已按原配置恢复运行。':'；成员已保存，稍后可启动。'));
      return;
    }
    $('agentDialog').close();notify(editing?(result.permission_update?.pending?(result.status==='online'?'配置变更已保存，等待当前步骤结束后生效。':'配置已保存，成员恢复后应用。'):result.configuration_update==='live'?(result.permission_update?.kind==='mcp'?(result.permission_update.unavailable_mcp?.length?'配置已保存，部分 MCP 服务未连接：'+result.permission_update.unavailable_mcp.join('、'):'MCP 配置已生效，成员继续运行。'):'沙箱权限已生效，成员继续运行。'):result.status==='error'?'配置已保存；成员原有运行错误仍保留，需处理后启动。':'配置已保存，成员保留原来的 Thread。'):'新成员已创建并启动。');
  } catch(error){$('agentFormError').textContent=error.message;}
  finally {busy--;$('saveAgent').disabled=false;$('saveAgent').textContent=editing?'保存配置':'创建并启动';$('agentForm').removeAttribute('aria-busy');await refresh();}
}
function openResource(kind='squad',existing=null) {
  creatingKind=kind;editingResource=existing?.id||null;$('resourceForm').reset();$('resourceFormError').textContent='';
  $('resourceDialogTitle').textContent=existing?'空间设置':kind==='squad'?'创建 Squad':'新建 Relay 聊天域';
  $('resourceHelp').textContent=kind==='squad'?'独立的成员、工作区和默认聊天域。从一个清晰的目标开始。':'一个独立运行的聊天空间，不需要关联 Squad。';
  $('resourceName').value=existing?.name||'';$('resourceDescription').value=existing?.description||'';
  $('resourceRestoreField').classList.toggle('hidden',kind!=='squad');
  $('resourceRestoreMembers').checked=existing?.restore_members===true;
  $('resourceModeField').classList.toggle('hidden',!!existing||kind==='domain');
  $('resourceMode').querySelector('[value="real"]').disabled=!fleet.models.length;
  if(!existing)$('resourceMode').value=fleet.models.length?'real':'fake';
  $('modelHelp').textContent=fleet.models.length?'可用真实模型：'+fleet.models.join('、'):'当前未配置真实模型，可以先使用离线演示。';
  $('resourceLifecycle').classList.toggle('hidden',!existing);
  $('resourceRemoval').classList.toggle('hidden',!existing);
  $('deleteResource').disabled=false;
  $('deleteResource').onclick=()=>removeResource(existing);
  if(existing){$('resourceLifecycleLabel').textContent=existing.status==='stopping'?'空间正在安全暂停':existing.status==='running'?'空间正在运行':'空间已暂停';$('resourceToggle').disabled=existing.status==='stopping';$('resourceToggle').textContent=existing.status==='stopping'?'等待完成':existing.status==='running'?'暂停空间':'启动空间';$('resourceToggle').onclick=async()=>{await resourceAction(existing,existing.status==='running'?'stop':'start');$('resourceDialog').close();};}
  $('saveResource').textContent=existing?'保存设置':kind==='squad'?'创建 Squad':'创建聊天域';$('resourceDialog').showModal();
}
async function saveResource(event) {
  event.preventDefault();busy++;$('saveResource').disabled=true;$('resourceFormError').textContent='';
  try {
    const body={name:$('resourceName').value.trim(),description:$('resourceDescription').value.trim(),mode:$('resourceMode').value};
    if(creatingKind==='squad')body.restore_members=$('resourceRestoreMembers').checked;
    const result=await api(editingResource?'resources/'+editingResource+'/settings':creatingKind==='squad'?'squads':'domains',body);
    fleet=await api('state');$('resourceDialog').close();notify(editingResource?'空间设置已保存。':'新空间已创建。');
    if(!editingResource&&creatingKind==='squad') await selectSquad(result.resource.id);
    else render();
  } catch(error){$('resourceFormError').textContent=error.message;}
  finally {busy--;$('saveResource').disabled=false;await refresh();}
}
async function removeResource(r) {
  if(!r||!confirm(`删除空间「${r.name}」？将安全暂停成员和聊天域，并从工作台移除。本机成员记录、聊天和文件保留，不会自动擦除。`))return;
  busy++;$('deleteResource').disabled=true;$('resourceFormError').textContent='';
  try{
    await api('resources/'+r.id+'/delete',{});
    generation++;
    fleet=await api('state');$('resourceDialog').close();
    if(selectedSquad===r.id)await selectSquad(fleet.resources.find(item=>item.kind==='squad')?.id||null);
    else render();
    notify('空间已从工作台移除，本机数据已保留。');
  }catch(error){$('resourceFormError').textContent=error.message;}
  finally{busy--;$('deleteResource').disabled=false;}
}
async function resourceAction(r,action) {
  if(action==='stop'&&!confirm(`暂停 ${r.name}？其成员会正常暂停，聊天域也会暂时停止服务，数据保留。`))return;
  busy++;
  try{await api('resources/'+r.id+'/'+action,{});notify(action==='start'?'空间已启动，成员可按需恢复。':'空间已暂停。');}
  catch(error){notify(error.message,true);}
  finally{busy--;await refresh();}
}
function renderDomains() {
  $('domainCards').innerHTML=fleet.resources.map(r=>`<article class="domain-card"><div class="domain-head"><span class="domain-icon">◎</span><div><h3>${esc(r.name)}</h3><span class="domain-kind">${r.kind==='squad'?'关联 Squad · 默认域':'独立 Relay 域'}</span></div><span class="spacer"></span><button class="icon-button" data-domain-settings="${r.id}" aria-label="设置 ${esc(r.name)}">•••</button></div><p>${esc(r.description||'一个独立的聊天空间，人和 agent 都以自己的账号交流。')}</p><div class="domain-url">${esc(r.relay_url)}</div><footer><span class="status-dot ${r.status==='running'?'online':'offline'}"></span><span class="small muted">${({running:'运行中',starting:'启动中',stopping:'安全暂停中'})[r.status]||'已暂停'}</span><span class="spacer"></span><button class="button ${r.status==='running'?'primary':''}" data-domain-open="${r.id}">${r.status==='running'?'进入聊天 ↗':'启动域'}</button></footer></article>`).join('');
  $('domainCards').querySelectorAll('[data-domain-open]').forEach(b=>b.onclick=()=>{const r=fleet.resources.find(r=>r.id===b.dataset.domainOpen);if(r.status==='running')openRelay(r.id);else resourceAction(r,'start');});
  $('domainCards').querySelectorAll('[data-domain-settings]').forEach(b=>b.onclick=()=>{const r=fleet.resources.find(r=>r.id===b.dataset.domainSettings);openResource(r.kind,r);});
}
async function openRelay(id=selectedSquad) {
  if (window.pywebview?.api?.open_relay) {
    try {
      const result = await window.pywebview.api.open_relay(id || '');
      if (!result.ok) throw Error(result.error);
    } catch (error) {
      notify(error.message, true);
    }
    return;
  }
  if (window.pywebview) {
    try {
      const result = await api('relay-connections?selected=' + encodeURIComponent(id || ''));
      const destination = new URL(result.url);
      if (destination.origin !== location.origin || destination.pathname !== '/relay/') {
        throw Error('Relay 地址与当前工作台不匹配。');
      }
      location.assign(destination.href);
    } catch (error) {
      notify(error.message, true);
    }
    return;
  }
  const popup=window.open('about:blank','relay');
  try{const result=await api('relay-connections?selected='+encodeURIComponent(id||''));if(popup)popup.location=result.url;else notify('请允许浏览器打开 Relay 窗口。',true);}
  catch(error){popup?.close();notify(error.message,true);}
}
async function openTerminal(a) {terminalAgent=a.id;$('terminalTitle').textContent=a.handle+' · 基础终端';$('terminalError').textContent='';$('terminalInput').value='';$('terminalDialog').showModal();await loadTerminal();}
async function loadTerminal(){try{const data=await squadApi('agents/'+terminalAgent+'/terminal');$('terminalMessages').innerHTML=data.messages.map(m=>`<div class="terminal-row"><small>${m.direction==='in'?'用户':'agent'} · ${new Date(m.created*1000).toLocaleTimeString()}</small><div>${esc(m.text)}</div></div>`).join('')||'<p class="dialog-description">终端还没有消息。日常对话请使用 Relay。</p>';}catch(error){$('terminalError').textContent=error.message;}}

$('loginForm').onsubmit=async event=>{event.preventDefault();token=$('token').value.trim();await login();};
$('logout').onclick=async()=>{try{await authApi('logout',{});sessionStorage.removeItem('squad-token');location.reload();}catch(error){notify(error.message,true);}};
function setNavigation(open){$('app').classList.toggle('nav-open',open);$('mobileNav').setAttribute('aria-expanded',String(open));}
$('mobileNav').onclick=()=>setNavigation(!$('app').classList.contains('nav-open'));
$('navBackdrop').onclick=()=>setNavigation(false);
$('mobileSpaces').onclick=()=>{showView('squads');setNavigation(true);};
$('mobileDomains').onclick=()=>showView('domains');
$('mobileRelay').onclick=()=>openRelay();
$('mobileSettings').onclick=()=>$('securitySettings').click();
$('detailBackdrop').onclick=()=>{selectedAgent=null;renderDetail();renderOrganization();};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!document.querySelector('dialog[open]')){setNavigation(false);selectedAgent=null;renderDetail();}});
$('squadsNav').onclick=()=>showView('squads');$('domainsNav').onclick=()=>showView('domains');
$('newSquadSide').onclick=()=>openResource('squad');$('newDomain').onclick=()=>openResource('domain');
$('resourceSettings').onclick=()=>openResource('squad',resource());$('startResource').onclick=()=>resourceAction(resource(),'start');
$('openRelay').onclick=()=>openRelay();$('newAgent').onclick=()=>resource()?openAgent():openResource('squad');
$('agentForm').onsubmit=saveAgent;$('resourceForm').onsubmit=saveResource;
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
$('search').oninput=renderOrganization;$('showArchived').onchange=()=>{renderOrganization();$('bulkDropdown').classList.add('hidden');};
for(const mode of ['tree','list'])$(mode+'View').onclick=()=>{layout=mode;for(const m of ['tree','list'])$(m+'View').classList.toggle('on',m===mode);renderOrganization();};
$('zoomIn').onclick=()=>{zoom=Math.min(1.4,zoom+.1);renderOrganization();};$('zoomOut').onclick=()=>{zoom=Math.max(.6,zoom-.1);renderOrganization();};$('zoomReset').onclick=()=>{zoom=1;renderOrganization();$('treeCanvas').scrollLeft=0;};
$('bulkMenu').onclick=()=>$('bulkDropdown').classList.toggle('hidden');
document.querySelectorAll('[data-bulk]').forEach(b=>b.onclick=async()=>{const action=b.dataset.bulk;$('bulkDropdown').classList.add('hidden');if(action==='stop'&&!confirm('正常暂停这个 Squad 中的全部成员？Thread 和工作区会保留。'))return;busy++;try{const result=await squadApi('bulk',{action});notify(`${result.succeeded.length} 位成员操作成功。`+(result.failed.length?'\n'+result.failed.map(f=>f.handle+'：'+f.error).join('\n'):''),!!result.failed.length);}catch(error){notify(error.message,true);}finally{busy--;await refresh();}});
$('terminalForm').onsubmit=async event=>{event.preventDefault();try{await squadApi('agents/'+terminalAgent+'/terminal',{text:$('terminalInput').value});$('terminalInput').value='';await loadTerminal();}catch(error){$('terminalError').textContent=error.message;}};
setInterval(()=>{refresh();if($('terminalDialog').open&&!busy)loadTerminal();},2500);
initBrowserLogin();

window.addEventListener('hashchange',()=>{if(fleet&&location.hash==='#domains')showView('domains');});

async function authApi(path, body) {
  const response=await fetch('/squad/auth/'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data=await response.json();if(!response.ok)throw Error(data.error||'登录请求失败');return data;
}
async function initBrowserLogin() {
  try {
    securityInfo=await authApi('status');browserSession=securityInfo.authenticated;
    $('browserLogin').classList.toggle('hidden',!securityInfo.configured);
    $('firstSetupHint').classList.toggle('hidden',securityInfo.configured);
    $('tokenLoginDetails').open=!securityInfo.configured;
    $('passkeyLogin').disabled=!securityInfo.passkeys_available||!window.PublicKeyCredential||!isSecureContext;
  } catch(error) { /* Local token-only pages continue to work. */ }
  if(token||browserSession)await login();
}
function securityMessage(text,ok=false){$('securityMessage').textContent=text;$('securityMessage').classList.toggle('security-success',ok);}
async function loadSecurity() {
  securityInfo=await authApi('status');$('securityUsername').value=securityInfo.username||'owner';
  $('currentPasswordLabel').classList.toggle('hidden',!securityInfo.configured||!!token);
  $('registerPasskey').disabled=!securityInfo.configured||!securityInfo.passkeys_supported||!window.PublicKeyCredential||!isSecureContext;
  $('passkeyList').innerHTML=securityInfo.passkeys.map(k=>'<div><span>'+esc(k.name)+'</span><button type="button" class="button compact" data-remove-key="'+esc(k.id)+'">移除</button></div>').join('')||'<p class="muted small">尚未绑定通行密钥。</p>';
  $('passkeyList').querySelectorAll('[data-remove-key]').forEach(b=>b.onclick=async()=>{
    if(!confirm('移除此通行密钥并使之前的网页登录会话退出？'))return;
    try{await authApi('passkeys/remove',{id:b.dataset.removeKey,current_password:$('currentPassword').value});browserSession=true;await loadSecurity();securityMessage('通行密钥已移除，其他网页登录会话已退出。',true);}catch(e){securityMessage(e.message);}
  });
}
async function loadStartup(){
  $('startupMessage').textContent='';$('consoleAutoStart').disabled=true;$('saveStartup').disabled=true;
  try{const data=await api('startup');$('consoleAutoStart').checked=data.enabled===true;$('consoleAutoStart').disabled=!data.available;$('saveStartup').disabled=!data.available;$('consoleStartupHint').textContent=data.description;}
  catch(error){$('consoleStartupHint').textContent=error.message;}
}
$('saveStartup').onclick=async()=>{
  $('saveStartup').disabled=true;$('startupMessage').textContent='';
  try{const data=await api('startup',{enabled:$('consoleAutoStart').checked});$('consoleAutoStart').checked=data.enabled===true;$('startupMessage').textContent='已保存；当前工作台继续运行。';}
  catch(error){$('startupMessage').textContent=error.message;}
  finally{$('saveStartup').disabled=false;}
};
$('securitySettings').onclick=async()=>{securityMessage('');$('currentPassword').value='';$('newPassword').value='';$('confirmPassword').value='';$('securityDialog').showModal();loadStartup();try{await loadSecurity();}catch(e){securityMessage(e.message);}};
$('passwordLoginForm').onsubmit=async event=>{
  event.preventDefault();$('loginError').textContent='';const button=event.submitter;button.disabled=true;
  try{await authApi('password/login',{username:$('loginUsername').value.trim(),password:$('loginPassword').value});token='';browserSession=true;sessionStorage.removeItem('squad-token');$('loginPassword').value='';await login();}catch(e){$('loginError').textContent=e.message;}finally{button.disabled=false;}
};
$('passwordSettingsForm').onsubmit=async event=>{
  event.preventDefault();securityMessage('');if($('newPassword').value!==$('confirmPassword').value){securityMessage('两次输入的新密码不一致');return;}
  const button=event.submitter;button.disabled=true;
  try{await authApi('password/set',{username:$('securityUsername').value.trim(),password:$('newPassword').value,current_password:$('currentPassword').value});token='';browserSession=true;sessionStorage.removeItem('squad-token');$('currentPassword').value='';$('newPassword').value='';$('confirmPassword').value='';await loadSecurity();securityMessage('账号密码已保存。以后可以直接登录，也可以继续绑定通行密钥。',true);}catch(e){securityMessage(e.message);}finally{button.disabled=false;}
};
function bytesFromBase64url(value){return Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4)),c=>c.charCodeAt(0));}
function base64urlFromBytes(value){return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function credentialJson(credential){
  const response={};for(const key of ['clientDataJSON','attestationObject','authenticatorData','signature','userHandle'])if(credential.response[key])response[key]=base64urlFromBytes(credential.response[key]);
  if(credential.response.getTransports)response.transports=credential.response.getTransports();
  return {id:credential.id,rawId:base64urlFromBytes(credential.rawId),type:credential.type,response,clientExtensionResults:credential.getClientExtensionResults(),...(credential.authenticatorAttachment?{authenticatorAttachment:credential.authenticatorAttachment}:{})};
}
function passkeyError(e){return e.name==='NotAllowedError'?'已取消或设备未完成验证，请重试。':e.message;}
$('registerPasskey').onclick=async()=>{
  $('registerPasskey').disabled=true;securityMessage('请在系统提示中保存通行密钥。');
  try{const options=await authApi('passkeys/register/options',{current_password:$('currentPassword').value});options.challenge=bytesFromBase64url(options.challenge);options.user.id=bytesFromBase64url(options.user.id);options.excludeCredentials=(options.excludeCredentials||[]).map(k=>({...k,id:bytesFromBase64url(k.id)}));const credential=await navigator.credentials.create({publicKey:options});if(!credential)throw Error('未创建通行密钥');await authApi('passkeys/register/verify',{credential:credentialJson(credential),name:$('passkeyName').value.trim()||'我的通行密钥'});await loadSecurity();securityMessage('通行密钥已绑定，下次可以使用 Face ID、指纹或设备解锁登录。',true);}catch(e){securityMessage(passkeyError(e));}finally{$('registerPasskey').disabled=false;}
};
$('passkeyLogin').onclick=async()=>{
  $('passkeyLogin').disabled=true;$('loginError').textContent='';
  try{const options=await authApi('passkeys/login/options',{});options.challenge=bytesFromBase64url(options.challenge);options.allowCredentials=(options.allowCredentials||[]).map(k=>({...k,id:bytesFromBase64url(k.id)}));const credential=await navigator.credentials.get({publicKey:options});if(!credential)throw Error('未完成验证');await authApi('passkeys/login/verify',{credential:credentialJson(credential)});token='';browserSession=true;sessionStorage.removeItem('squad-token');await login();}catch(e){$('loginError').textContent=passkeyError(e);}finally{$('passkeyLogin').disabled=false;}
};
