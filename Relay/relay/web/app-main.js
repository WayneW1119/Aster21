const $ = id => document.getElementById(id);
const DEFAULT_RELAY = new URL(document.querySelector('meta[name="relay-default-url"]')?.content || location.origin, location.href).href.replace(/\/+$/,'');
const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let connections = [], active = null, managerUrl = '', state = null, selected = null, rows = [], older = [], lastRendered = '';
let stateRevision=0, viewVersion=0, historyLoading=false;
let viewController=new AbortController(), historyRequest=null;
const conversationCache=new Map(), pendingReads=new Set(), outgoing=new Map(), fileDrafts=new Map();
function isSending(peer=selected){return outgoing.get(peer)?.status==='sending';}
function cacheMessages(peer,messages){
  conversationCache.delete(peer);conversationCache.set(peer,messages.slice(-50));
  if(conversationCache.size>24)conversationCache.delete(conversationCache.keys().next().value);
}
function confirmOutgoing(peer,clientId){
  const pending=outgoing.get(peer);if(!pending||pending.client_id!==clientId)return;
  outgoing.delete(peer);
  if(pending.restoredDraft){
    if(drafts[peer]===pending.text)drafts[peer]='';
    if(selected===peer&&$('text').value===pending.text){$('text').value='';fitComposer();}
  }
}
function mergeMessages(...batches){return [...new Map(batches.flat().map(row=>[row.id,row])).values()].sort((a,b)=>a.id-b.id);}

let generation = 0, controller = new AbortController(), stateRequest = null, issuedToken = '', issuedConnection = null, drafts = {};
const imagePreviews=createRelayImagePreviews((id,signal)=>fetchAttachment(id,signal,30000));
function readSaved(key, fallback) {try{return JSON.parse(sessionStorage.getItem(key)) || fallback;}catch{return fallback;}}
function normalizeUrl(value) {
  const url = new URL(value, location.href);
  if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || /%2f|%5c/i.test(url.pathname)) throw Error('请填写有效的 Relay 服务地址');
  if(location.protocol==='https:'&&url.protocol!=='https:')throw Error('HTTPS 页面需要使用 HTTPS 域地址，请从工作台重新打开 Relay');
  return url.origin + url.pathname.replace(/\/+$/,'');
}
function saveConnections() {
  sessionStorage.setItem('relay-connections-v1',JSON.stringify(connections));
  if(active)sessionStorage.setItem('relay-active-url',active.url);
  if(managerUrl)sessionStorage.setItem('relay-manager-url',managerUrl);
  localStorage.setItem('relay-browser-connections',JSON.stringify(connections.filter(c=>c.browser).map(({url,label,username,domain})=>({url,label,username,domain,browser:true}))));
}
function upsert(connection) {
  const index = connections.findIndex(c=>c.url===connection.url);
  if(index>=0)connections[index]={...connections[index],...connection};else connections.push(connection);
  return connections.find(c=>c.url===connection.url);
}
function aborted(error) {return error.name==='AbortError';}
async function request(connection,path,body,signal){
  const aborter=new AbortController();let timedOut=false;
  const abort=()=>aborter.abort();signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)abort();
  const timer=setTimeout(()=>{timedOut=true;aborter.abort();},15000);
  try{
    const response=await fetch(connection.url+'/api/'+path,{method:body===undefined?'GET':'POST',credentials:connection.browser?'same-origin':'omit',headers:{...(connection.token?{Authorization:'Bearer '+connection.token}:{}),'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:aborter.signal});
    let data;try{data=await response.json();}catch(error){if(aborted(error))throw error;throw Error('服务暂时不可用');}
    if(!response.ok){const error=Error(data.error||'请求失败');error.status=response.status;throw error;}
    return data;
  }catch(error){if(timedOut)throw Error('网络请求超时，请重试');throw error;}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}
}
async function api(path,body,signal=controller.signal) {
  const epoch=generation,connection=active;if(body!==undefined)stateRevision++;
  if(!connection?.token&&!connection?.browser)throw Error('请先登录这个域');
  try {
    const data=await request(connection,path,body,signal);
    if(epoch!==generation)throw new DOMException('Connection changed','AbortError');
    return data;
  } catch(error) {
    if(epoch===generation&&error.status===401)invalidate(error.message);
    throw error;
  }
}
function setRelayLoginTarget(connection){
  const select=$('loginDomain');if(!connection?.url)return;
  if(![...select.options].some(o=>o.value===connection.url))select.add(new Option(connection.label||connection.url,connection.url));
  select.value=connection.url;$('serverUrl').value=connection.url;localStorage.setItem('relay-preferred-domain',connection.url);
}
function resetConversation() {imagePreviews.clear();for(const id of ['groupCreator','groupSettingsDialog'])if($(id).open)$(id).close();$('groupSettings').classList.add('hidden');$('messages').innerHTML='';selected=null;rows=[];older=[];lastRendered='';drafts={};viewController.abort();viewController=new AbortController();viewVersion++;historyRequest=null;historyLoading=false;conversationCache.clear();outgoing.clear();fileDrafts.clear();renderFileDraft();pendingReads.clear();$('text').value='';$('sendBtn').disabled=false;$('composer').classList.add('hidden');$('app').classList.remove('chat-open');}
function invalidate(message) {
  if(active){active.token='';saveConnections();}
  controller.abort();controller=new AbortController();generation++;stateRequest=null;state=null;
  resetConversation();$('app').classList.add('hidden');$('login').classList.remove('hidden');$('token').value='';$('serverUrl').value=active?.url||$('loginDomain').value||DEFAULT_RELAY;$('loginError').textContent=message;renderLoginConnections();
}
async function activate(connection) {
  // Explicit browser login replaces an older saved API token for this URL.
  // Explicit token login likewise replaces any remembered browser mode.
  connection=connection.token?{...connection,browser:false}:connection.browser?{...connection,token:''}:connection;
  controller.abort();controller=new AbortController();const epoch=++generation;stateRequest=null;state=null;resetConversation();
  try {
    const result=await request(connection,'state',undefined,controller.signal);
    if(epoch!==generation)return;
    active=upsert({...connection,label:result.domain.name,username:result.me.username,domain:result.domain.id,agent_url:result.domain.agent_url||''});
    state=result;saveConnections();setRelayLoginTarget(active);renderLoginConnections();$('token').value='';$('loginError').textContent='';
    $('login').classList.add('hidden');$('app').classList.remove('hidden');$('domainPicker').close();
    $('peerName').textContent='开始一段对话';$('peerSubtitle').textContent='选择这个域中的一位成员';$('peerAvatar').textContent='r';
    banner('');renderState();renderMessages();await refresh();
  } catch(error) {
    if(aborted(error))return;
    active=upsert({...connection,token:error.status===401?'':connection.token});saveConnections();
    state=null;$('app').classList.add('hidden');$('login').classList.remove('hidden');$('serverUrl').value=connection.url;$('token').value='';$('loginError').textContent=error.message;renderLoginConnections();
    throw error;
  }
}
function avatar(account){return(account?.name||'?').slice(0,1).toUpperCase();}
function account(id){return state?.accounts.find(a=>a.id===id);}
function selectedGroupId(value=selected){return typeof value==='string'&&value.startsWith('group:')?Number(value.slice(6)):null;}
function selectedGroup(){return state?.groups?.find(group=>group.id===selectedGroupId());}
function historyPath(peer,before=0){const group=selectedGroupId(peer);return group?'groups/'+group+'/messages?before='+before:'messages?peer='+encodeURIComponent(peer)+'&before='+before;}

function banner(message){$('banner').textContent=message;$('banner').classList.toggle('hidden',!message);}
function renderConnections() {
  $('domainRail').innerHTML=connections.map(c=>`<button class="rail-domain ${active?.url===c.url?'selected':''}" data-connection="${esc(c.url)}" title="${esc(c.label||c.url)}${c.username?' · @'+esc(c.username):''}" aria-label="切换到 ${esc(c.label||c.url)}">${esc((c.label||'域').slice(0,1))}</button>`).join('');
  $('domainRail').querySelectorAll('[data-connection]').forEach(b=>b.onclick=()=>chooseConnection(b.dataset.connection));
}
function renderLoginConnections() {
  $('loginConnections').innerHTML=connections.filter(c=>c.token||c.browser).length?'<p>或回到已连接的域</p>'+connections.filter(c=>c.token||c.browser).map(c=>`<a href="#" data-login-domain="${esc(c.url)}">${esc(c.label||c.url)} <span>@${esc(c.username||'user')}</span> →</a>`).join(''):'';
  $('loginConnections').querySelectorAll('[data-login-domain]').forEach(a=>a.onclick=e=>{e.preventDefault();chooseConnection(a.dataset.loginDomain);});
}
async function chooseConnection(url) {
  const connection=connections.find(c=>c.url===url);if(!connection)return;
  if(url===active?.url&&state){$('domainPicker').close();$('login').classList.add('hidden');$('app').classList.remove('hidden');return;}
  if(!connection.token&&!connection.browser){active=connection;invalidate('请填写这个域的账号凭据');$('domainPicker').close();return;}
  try{await activate(connection);}catch(error){if(!aborted(error))$('connectionError').textContent=error.message;}
}
function renderState() {
  const agentUrl=state.domain.agent_url||'';if(active.agent_url!==agentUrl){active.agent_url=agentUrl;saveConnections();}
  const me=state.me;$('domainName').textContent=state.domain.name;$('domainId').textContent='私聊与群聊 · '+state.accounts.length+' 位成员';$('myName').textContent=me.name;$('myHandle').textContent='@'+me.username;$('myAvatar').textContent=avatar(me);
  renderConnections();
  const others=state.accounts.filter(a=>a.id!==me.id),query=$('search').value.toLowerCase();$('memberCount').textContent=others.length;
  $('contacts').innerHTML=others.filter(a=>(a.name+' '+a.username).toLowerCase().includes(query)).map(a=>`<button class="contact ${selected===a.id?'selected':''}" data-id="${a.id}"><span class="avatar">${esc(avatar(a))}</span><span class="contact-name"><b>${esc(a.name)}</b><small>@${esc(a.username)}${!a.active?' · 已停用':''}</small></span>${state.unread[a.id]?`<span class="badge">${state.unread[a.id]}</span>`:''}</button>`).join('')||'<p class="no-contacts">这个域还没有其他成员。<br>创建账号，或在 Squad 中新增成员。</p>';
  $('contacts').querySelectorAll('[data-id]').forEach(el=>el.onclick=()=>select(el.dataset.id));
  $('groups').innerHTML=(state.groups||[]).filter(g=>g.name.toLowerCase().includes(query)).map(g=>`<button class="contact group-contact ${selected==='group:'+g.id?'selected':''}" data-group="${g.id}"><span class="avatar">群</span><span class="contact-name"><b>${esc(g.name)}</b><small>${g.member_count} 位成员${g.muted?' · 全员禁言':g.member_muted?' · 你已被禁言':''}</small></span>${g.unread?`<span class="badge">${g.unread}</span>`:''}</button>`).join('')||'<p class="no-groups">还没有群聊，点击上方建群。</p>';
  $('groups').querySelectorAll('[data-group]').forEach(button=>button.onclick=()=>select('group:'+button.dataset.group));
  $('groupSettings').classList.toggle('hidden',!selectedGroupId());
  $('attachFile').disabled=isSending()||!!(selectedGroupId()&&!selectedGroup()?.can_send);
  if(selectedGroupId()){
    const group=selectedGroup();if(!group){resetConversation();renderMessages();return;}
    $('peerName').textContent=group.name;$('peerAvatar').textContent='群';$('peerSubtitle').textContent=group.member_count+' 位成员 · 群主 @'+group.owner_username+(group.can_send?'':' · 已禁言');
    $('composer').classList.remove('hidden');$('text').disabled=!group.can_send;$('sendBtn').disabled=isSending()||!group.can_send;$('text').placeholder=group.can_send?'发到群聊……':'当前已禁言，仍可阅读消息';return;
  }
  $('text').disabled=false;$('text').placeholder='写一条消息……';$('sendBtn').disabled=isSending();
  if(selected){const a=account(selected);if(!a){resetConversation();return;}$('peerName').textContent=a.name;$('peerAvatar').textContent=avatar(a);$('peerSubtitle').textContent='@'+a.username+' · '+(a.active?'域内成员':'账号已停用');$('composer').classList.remove('hidden');}
}
async function select(id){
  imagePreviews.clear();
  if(selected)drafts[selected]=$('text').value;
  viewController.abort();viewController=new AbortController();viewVersion++;historyRequest=null;
  selected=id;rows=conversationCache.get(id)||[];older=[];lastRendered='';historyLoading=true;
  $('text').value=drafts[id]||'';renderFileDraft();fitComposer();$('app').classList.add('chat-open');
  renderState();renderMessages();
  if(!matchMedia('(max-width:650px)').matches)$('text').focus({preventScroll:true});
  await refreshConversation();
}
function renderMessages() {
  if(!selected){$('messages').innerHTML='<div class="empty"><div class="symbol">◎</div><h2>每一段协作，从对话开始</h2><p>在左侧选择这个域中的一位成员。<br>人和 agent，都用自己的账号交流。</p></div>';return;}
  const all=mergeMessages(older,rows),pending=outgoing.get(selected);
  if(pending&&!all.some(r=>r.client_id===pending.client_id))all.push({...pending,id:'pending:'+pending.client_id,sender:state.me.id,pending:true});
  const key=JSON.stringify([all,historyLoading]);if(key===lastRendered)return;
  const feed=$('messages'),atBottom=feed.scrollHeight-feed.scrollTop-feed.clientHeight<100;lastRendered=key;
  feed.innerHTML=(all.length>=50?'<button id="olderBtn" class="older">查看更早的消息 ↑</button>':'')+'<div class="day">'+(all.length?new Date(all[0].created*1000).toLocaleDateString('zh-CN',{month:'long',day:'numeric'}):'对话开始于此')+'</div>'+all.map(r=>{const mine=r.sender===state.me.id,a=account(r.sender),stamp=new Date(r.created*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});return `<article class="message ${mine?'mine':''}"><span class="avatar">${esc(avatar(a))}</span><div class="message-body"><div class="message-head">${esc(a?.name||'未知账号')} · ${stamp}</div><div class="bubble markdown">${r.attachment?renderAttachment(r.attachment):''}${r.attachment&&r.text==='[文件] '+r.attachment.name?'':renderRelayMarkdown(r.text)}</div>${mine?`<div class="receipt">${r.pending?(r.status==='failed'?'<button type="button" class="retry-message">发送失败 · 点击重试</button><span class="send-error">'+esc(r.error||'')+'</span>':'发送中…'):selectedGroupId()?'已发送到群':r.read?'已读':'已发送'}</div>`:''}</div></article>`;}).join('');
  if(!all.length&&historyLoading){feed.innerHTML='<div class="empty" role="status"><p>正在加载对话…</p></div>';return;}
  if(!all.length)feed.innerHTML='<div class="empty"><div class="symbol">↗</div><h2>向 '+esc(selectedGroup()?.name||account(selected)?.name||'对方')+' 打个招呼</h2><p>消息会留在这个域，对方上线后也能收到。</p></div>';
  imagePreviews.mount();
  if($('olderBtn'))$('olderBtn').onclick=loadOlder;if(atBottom||all.length<=50)feed.scrollTop=feed.scrollHeight;
}
async function loadOlder(){
  const epoch=generation,view=viewVersion,peer=selected;
  try{
    const all=[...older,...rows];if(!all.length)return;
    const before=Math.min(...all.map(r=>r.id)),data=await api(historyPath(peer,before),undefined,viewController.signal);
    if(epoch!==generation||view!==viewVersion||peer!==selected)return;
    if(!data.messages.length){if($('olderBtn')){$('olderBtn').textContent='已经到最早的消息';$('olderBtn').disabled=true;}return;}
    const feed=$('messages'),height=feed.scrollHeight;older=[...data.messages,...older];renderMessages();feed.scrollTop=feed.scrollHeight-height;
  }catch(error){if(!aborted(error)&&epoch===generation&&view===viewVersion)banner(error.message);}
}
async function refreshState(){
  if((!active?.token&&!active?.browser)||!state)return;
  const epoch=generation;
  if(stateRequest?.epoch===epoch){
    const previous=stateRequest;await previous.promise;
    if(epoch===generation&&previous.revision!==stateRevision)return refreshState();
    return;
  }
  const current={epoch,revision:stateRevision,promise:null};stateRequest=current;
  current.promise=(async()=>{
    try{
      const next=await api('state');if(epoch!==generation||current.revision!==stateRevision)return;
      state=next;renderState();$('network').innerHTML='<i class="dot"></i> 已连接到域';
    }catch(error){if(!aborted(error)&&epoch===generation){banner(error.message);$('network').textContent='连接中断 · 正在重试';}}
    finally{if(stateRequest===current)stateRequest=null;}
  })();
  return current.promise;
}
function acknowledgeDisplayed(peer,messages,epoch){
  const unread=selectedGroupId(peer)?messages.filter(r=>r.delivery_id&&!r.read).map(r=>r.delivery_id):messages.filter(r=>r.recipient===state.me.id&&!r.read).map(r=>r.id);
  const ids=unread.filter(id=>!pendingReads.has(id));if(!ids.length)return;
  ids.forEach(id=>pendingReads.add(id));
  api('ack',{ids,read:true}).then(()=>{
    if(epoch!==generation||!state)return;
    const marked=new Set(ids),mark=batch=>batch.map(r=>marked.has(selectedGroupId(peer)?r.delivery_id:r.id)?{...r,read:1}:r);
    if(conversationCache.has(peer))cacheMessages(peer,mark(conversationCache.get(peer)));
    if(peer===selected)rows=mark(rows);
    const group=state.groups?.find(g=>g.id===selectedGroupId(peer));
    if(group)group.unread=Math.max(0,group.unread-ids.length);
    else if(!selectedGroupId(peer))state.unread[peer]=Math.max(0,(state.unread[peer]||0)-ids.length);
    renderState();
  }).catch(error=>{if(!aborted(error)&&epoch===generation)$('network').textContent='已读状态稍后重试';})
    .finally(()=>{if(epoch===generation)ids.forEach(id=>pendingReads.delete(id));});
}
async function refreshConversation(){
  if(!selected||!state)return;
  if(historyRequest?.view===viewVersion)return historyRequest.promise;
  const epoch=generation,peer=selected,view=viewVersion,current={view,promise:null};historyRequest=current;
  current.promise=(async()=>{
    try{
      const data=await api(historyPath(peer),undefined,viewController.signal);
      if(epoch!==generation||view!==viewVersion||peer!==selected)return;
      rows=mergeMessages(rows,data.messages).slice(-50);cacheMessages(peer,rows);historyLoading=false;
      const pending=outgoing.get(peer);
      if(pending&&rows.some(r=>r.client_id===pending.client_id))confirmOutgoing(peer,pending.client_id);
      renderMessages();acknowledgeDisplayed(peer,rows,epoch);
    }catch(error){if(!aborted(error)&&epoch===generation&&view===viewVersion){historyLoading=false;renderMessages();banner(error.message);}}
    finally{if(historyRequest===current)historyRequest=null;}
  })();
  return current.promise;
}
async function refresh(){await Promise.all([refreshState(),refreshConversation()]);}
async function sendOutgoing(peer,text,file=null){
  if((!text&&!file)||!peer||isSending(peer))return;
  const epoch=generation,existing=outgoing.get(peer);
  const pending=existing?.text===text&&existing.file===file?existing:{to:peer,text,file,attachment:file?{name:file.name,size:file.size}:null,client_id:crypto.randomUUID(),created:Date.now()/1000};
  pending.status='sending';outgoing.set(peer,pending);
  if(fileDrafts.get(peer)===file)fileDrafts.delete(peer);renderFileDraft();
  if(selected===peer){if($('text').value.trim()===text){$('text').value='';drafts[peer]='';fitComposer();}renderState();renderMessages();$('messages').scrollTop=$('messages').scrollHeight;}
  try{
    const message=file?await uploadFile(peer,text,file,pending.client_id):await api(selectedGroupId(peer)?'groups/'+selectedGroupId(peer)+'/messages':'messages',{to:peer,text,client_id:pending.client_id});
    if(epoch!==generation)return;
    confirmOutgoing(peer,pending.client_id);
    const confirmed=mergeMessages(conversationCache.get(peer)||[],[message]).slice(-50);cacheMessages(peer,confirmed);
    if(selected===peer){rows=mergeMessages(rows,[message]).slice(-50);renderMessages();}
  }catch(error){
    if(aborted(error)||epoch!==generation)return;
    if(outgoing.get(peer)===pending){
      pending.status='failed';pending.error=error.message;
      if(selected===peer){if(!$('text').value){$('text').value=text;drafts[peer]=text;pending.restoredDraft=true;fitComposer();}renderMessages();}
      else if(!drafts[peer]){drafts[peer]=text;pending.restoredDraft=true;}
    }
  }finally{if(epoch===generation){renderState();if(selected===peer&&!$('text').disabled)$('text').focus({preventScroll:true});}}
}
async function copyConnectionText(button,value,label){
  try{await navigator.clipboard.writeText(value);button.textContent='已复制';}
  catch{button.textContent=label||'请长按内容复制';}
}
function showDomainPicker() {
  $('connectionError').textContent='';$('connectToken').value='';
  $('domainConnections').innerHTML=connections.map(c=>`<div class="domain-connection ${active?.url===c.url?'current':''}"><span class="connection-avatar">${esc((c.label||'域').slice(0,1))}</span><div class="connection-description"><b>${esc(c.label||'Relay 域')}</b>${c.username?'<small>@'+esc(c.username)+'</small>':''}<small class="service-address-label">网页访问地址</small><code class="service-url">${esc(c.url)}</code><button type="button" class="quiet copy-service-address" data-copy-address="${esc(c.url)}">复制网页地址</button>${c.agent_url?`<div class="agent-endpoint"><small>本机 agent 直连地址 · 仅同一主机 / WSL</small><code class="service-url">${esc(c.agent_url)}</code><button type="button" class="quiet copy-service-address" data-copy-address="${esc(c.agent_url)}">复制 agent 地址</button></div>`:''}</div><button class="${active?.url===c.url?'quiet':'primary'}" data-choose="${esc(c.url)}">${active?.url===c.url?'当前':'进入'}</button><button class="quiet remove-connection" data-remove="${esc(c.url)}" aria-label="移除此域连接">×</button></div>`).join('')||'<p class="small muted">还没有保存的域连接。</p>';
  $('domainConnections').querySelectorAll('[data-copy-address]').forEach(button=>button.onclick=()=>copyConnectionText(button,button.dataset.copyAddress,'请长按地址复制'));
  $('domainConnections').querySelectorAll('[data-choose]').forEach(b=>b.onclick=()=>chooseConnection(b.dataset.choose));
  $('domainConnections').querySelectorAll('[data-remove]').forEach(b=>b.onclick=async()=>{if(!confirm('仅移除这个浏览器中的连接，不会删除域、账号或消息。继续？'))return;const url=b.dataset.remove;connections=connections.filter(c=>c.url!==url);saveConnections();if(active?.url===url){active=null;invalidate('已退出该域');$('domainPicker').close();if(connections.find(c=>c.token))await chooseConnection(connections.find(c=>c.token).url);}else showDomainPicker();});
  $('createDomainLink').classList.toggle('hidden',!managerUrl);if(managerUrl)$('createDomainLink').href=managerUrl+'/#domains';
  if(!$('domainPicker').open)$('domainPicker').showModal();
}
function showCredential(value,account){
  issuedToken=value;issuedConnection={url:active.url,agent_url:state.domain.agent_url||'',mcp_url:state.domain.mcp_url||'',domain:state.domain.name,username:account.username};
  $('credentialValue').textContent=value;$('credentialDomainName').textContent=issuedConnection.domain;
  $('credentialUsername').textContent='用户名：@'+issuedConnection.username;$('credentialServiceUrl').textContent=issuedConnection.url;
  $('credentialAgentEndpoint').classList.toggle('hidden',!issuedConnection.agent_url);$('credentialAgentUrl').textContent=issuedConnection.agent_url;$('copyCredentialAgentUrl').textContent='复制 agent 地址';
  $('copyCredentialAddress').textContent='复制网页地址';$('copyConnectionInfo').textContent=issuedConnection.agent_url?'复制给本机 agent':'复制连接信息';$('credential').showModal();
}
function renderManager() {
  $('managerDomainName').textContent=state.domain.name;$('managerUsername').textContent='当前账号：@'+state.me.username;
  $('managerServiceUrl').textContent=active.url;$('copyManagerAddress').textContent='复制网页地址';
  $('managerAgentEndpoint').classList.toggle('hidden',!state.domain.agent_url);$('managerAgentUrl').textContent=state.domain.agent_url||'';$('copyManagerAgentUrl').textContent='复制 agent 地址';
  $('managedList').innerHTML=state.accounts.filter(a=>a.manageable).map(a=>`<div class="managed"><div class="spacer"><b>${esc(a.name)}</b><small>@${esc(a.username)} · ${a.active?'可用':'已停用'}</small></div><button data-account="${a.id}" data-action="${a.disabled?'enable':'disable'}">${a.disabled?'启用':'停用'}</button><button data-account="${a.id}" data-action="reset_token">重置凭据</button></div>`).join('')||'<p class="small muted">暂时没有你可管理的账号。</p>';
  $('createAccount').classList.toggle('hidden',!state.me.can_create);
  $('managedList').querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{if(!confirm(b.dataset.action==='reset_token'?'重置后旧凭据立即失效，确定继续？':b.dataset.action==='disable'?'停用后，此账号及其管理的下属账号将无法登录，确定继续？':'重新启用该账号？'))return;try{const epoch=generation,result=await api('accounts/'+b.dataset.account,{action:b.dataset.action});await refresh();if(epoch!==generation)return;renderManager();if(result.token)showCredential(result.token,account(b.dataset.account));}catch(error){if(!aborted(error))$('manageError').textContent=error.message;}});
}
$('loginForm').onsubmit=async event=>{event.preventDefault();$('loginBtn').disabled=true;try{await activate({url:normalizeUrl($('serverUrl').value.trim()),token:$('token').value.trim()});}catch(error){if(!aborted(error))$('loginError').textContent=error.message;}finally{$('loginBtn').disabled=false;}};
$('logoutBtn').onclick=async()=>{try{if(active)await relayAuth('logout',{},active);if(active)connections=connections.filter(c=>c.url!==active.url);active=null;saveConnections();invalidate('已退出 Relay 当前域');}catch(error){banner(error.message);}};
$('search').oninput=()=>{if(state)renderState();};$('backBtn').onclick=()=>$('app').classList.remove('chat-open');
$('domainSwitcher').onclick=showDomainPicker;$('railAddDomain').onclick=showDomainPicker;$('closeDomainPicker').onclick=()=>$('domainPicker').close();
$('connectDomain').onsubmit=async event=>{event.preventDefault();$('connectDomainButton').disabled=true;$('connectionError').textContent='';try{const connection={url:normalizeUrl($('connectUrl').value.trim()),token:$('connectToken').value.trim()};const result=await request(connection,'state');await activate({...connection,label:result.domain.name,username:result.me.username});$('connectDomain').reset();}catch(error){if(!aborted(error))$('connectionError').textContent=error.message;}finally{$('connectDomainButton').disabled=false;}};
$('composer').onsubmit=event=>{event.preventDefault();void sendOutgoing(selected,$('text').value.trim(),fileDrafts.get(selected)||null);};
$('text').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&!matchMedia('(pointer:coarse)').matches){event.preventDefault();$('composer').requestSubmit();}};
$('manageBtn').onclick=()=>{$('manageError').textContent='';renderManager();$('manager').showModal();};$('closeManager').onclick=()=>$('manager').close();
$('createAccount').onsubmit=async event=>{event.preventDefault();try{const epoch=generation,result=await api('accounts',{username:$('newHandle').value.trim(),name:$('newName').value.trim(),can_create:$('canCreate').checked});if(epoch!==generation)return;$('newHandle').value='';$('newName').value='';await refresh();renderManager();showCredential(result.token,result.account);}catch(error){if(!aborted(error))$('manageError').textContent=error.message;}};
$('copyManagerAddress').onclick=()=>copyConnectionText($('copyManagerAddress'),active.url,'请长按地址复制');
$('copyManagerAgentUrl').onclick=()=>copyConnectionText($('copyManagerAgentUrl'),state.domain.agent_url,'请长按地址复制');
$('copyCredentialAgentUrl').onclick=()=>copyConnectionText($('copyCredentialAgentUrl'),issuedConnection.agent_url,'请长按地址复制');
$('copyCredentialAddress').onclick=()=>copyConnectionText($('copyCredentialAddress'),issuedConnection.url,'请长按地址复制');
$('copyConnectionInfo').onclick=()=>{if(issuedConnection)copyConnectionText($('copyConnectionInfo'),'Relay 域：'+issuedConnection.domain+'\n服务地址：'+(issuedConnection.agent_url||issuedConnection.url)+(issuedConnection.agent_url?'\n适用范围：与 Relay 同一台主机 / WSL 内的 agent':'')+(issuedConnection.mcp_url?'\nMCP 地址：'+issuedConnection.mcp_url:'')+'\n网页访问地址：'+issuedConnection.url+'\n用户名：'+issuedConnection.username+'\n账号凭据：'+issuedToken,'请分别复制地址、用户名与凭据');};
$('copyCredential').onclick=async()=>{try{await navigator.clipboard.writeText(issuedToken);$('copyCredential').textContent='已复制';}catch{$('copyCredential').textContent='请手动选择并复制';}};
$('closeCredential').onclick=()=>$('credential').close();$('credential').onclose=()=>{issuedToken='';issuedConnection=null;$('credentialDomainName').textContent='';$('credentialUsername').textContent='';$('credentialServiceUrl').textContent='';$('credentialAgentUrl').textContent='';$('credentialAgentEndpoint').classList.add('hidden');$('credentialValue').textContent='';$('copyCredential').textContent='复制凭据';};
async function bootstrap() {
  connections=readSaved('relay-connections-v1',[]).filter(c=>typeof c.url==='string');
  try{for(const c of JSON.parse(localStorage.getItem('relay-browser-connections')||'[]')){if(c.browser&&!connections.some(x=>x.url===c.url))upsert({...c,url:normalizeUrl(c.url),token:''});}}catch{}managerUrl=document.querySelector('meta[name="relay-manager-url"]')?.content||sessionStorage.getItem('relay-manager-url')||'';
  const hash=location.hash;
  try {
    if(hash.startsWith('#connections=')){
      const bundle=JSON.parse(decodeURIComponent(hash.slice(13)));
      for(const c of (bundle.connections||[]).slice(0,32))upsert({...c,url:normalizeUrl(c.url)});
      if(bundle.manager){const manager=new URL(bundle.manager,location.href);if(manager.origin===location.origin||['127.0.0.1','localhost'].includes(manager.hostname))managerUrl=normalizeUrl(bundle.manager);}
      active=connections.find(c=>c.url===bundle.selected)||connections[0];
    }else if(hash.startsWith('#token='))active=upsert({url:DEFAULT_RELAY,token:decodeURIComponent(hash.slice(7))});
    else active=connections.find(c=>c.url===sessionStorage.getItem('relay-active-url'))||connections[0];
    if(!active&&sessionStorage.getItem('relay-token'))active=upsert({url:DEFAULT_RELAY,token:sessionStorage.getItem('relay-token')});
  }catch{$('loginError').textContent='连接信息无效，请重新登录。';}
  if(hash)history.replaceState(null,'',location.pathname);
  sessionStorage.removeItem('relay-token');saveConnections();$('serverUrl').value=active?.url||$('loginDomain').value||DEFAULT_RELAY;renderLoginConnections();
  if(active?.token||active?.browser){try{await activate(active);}catch{}}
}
window.addEventListener('DOMContentLoaded',()=>{const ready=bootstrap();initRelayBrowserLogin();ready.catch(error=>{$('loginError').textContent=error.message;});});setInterval(refresh,1500);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});window.addEventListener('online',()=>void refresh());

// Phone composition: Enter inserts a line; the send button submits explicitly.
function fitComposer(){const field=$('text'),limit=Math.min(160,Math.max(64,(window.visualViewport?.height||innerHeight)*.25));field.style.maxHeight=limit+'px';field.style.height='auto';field.style.height=Math.min(limit,Math.max(44,field.scrollHeight))+'px';}
$('text').addEventListener('input',()=>{const pending=outgoing.get(selected);if(pending)pending.restoredDraft=false;fitComposer();});
if(matchMedia('(pointer:coarse)').matches)$('composeHint').textContent='换行继续输入 · 点击发送';
let followViewportBottom=false;
window.addEventListener('appviewportwillchange',()=>{const list=$('messages');followViewportBottom=list.scrollHeight-list.scrollTop-list.clientHeight<48;});
window.addEventListener('appviewportchange',()=>{fitComposer();if(followViewportBottom)requestAnimationFrame(()=>{$('messages').scrollTop=$('messages').scrollHeight;});});
// Keep the keyboard open when tapping Send instead of blurring then refocusing.
$('sendBtn').addEventListener('pointerdown',event=>{if(document.activeElement===$('text'))event.preventDefault();});

// Group administration stays within the currently authenticated domain.
let groupDialogEpoch=0, settingsGroup=null;
function inviteChecklist(accounts){return accounts.map(a=>`<label class="group-choice"><input type="checkbox" value="${esc(a.username)}"><span><b>${esc(a.name)}</b><small>@${esc(a.username)}</small></span></label>`).join('')||'<p class="small muted">暂无可以邀请的成员。</p>';}
$('newGroup').onclick=()=>{groupDialogEpoch=generation;$('groupName').value='';$('createGroupError').textContent='';$('groupInvitees').innerHTML=inviteChecklist(state.accounts.filter(a=>a.active&&a.id!==state.me.id));$('groupCreator').showModal();};
$('closeGroupCreator').onclick=()=>$('groupCreator').close();
$('createGroupForm').onsubmit=async event=>{
 event.preventDefault();if(groupDialogEpoch!==generation)return;$('createGroupBtn').disabled=true;$('createGroupError').textContent='';
 try{const result=await api('groups',{name:$('groupName').value.trim(),members:[...$('groupInvitees').querySelectorAll('input:checked')].map(i=>i.value)});$('groupCreator').close();state.groups=[...(state.groups||[]).filter(g=>g.id!==result.id),result];await select('group:'+result.id);}
 catch(error){if(!aborted(error))$('createGroupError').textContent=error.message;}
 finally{$('createGroupBtn').disabled=false;}
};
async function groupAction(action,extra={}){
 if(groupDialogEpoch!==generation)return;
 try{$('groupSettingsError').textContent='';await api('groups/'+settingsGroup,{action,...extra});
  if(action==='leave'||action==='dissolve'){$('groupSettingsDialog').close();resetConversation();await refresh();renderMessages();return;}
  await refresh();if(groupDialogEpoch===generation)await openGroupSettings();
 }catch(error){if(!aborted(error))$('groupSettingsError').textContent=error.message;}
}
async function openGroupSettings(){
 const id=selectedGroupId();if(!id)return;groupDialogEpoch=generation;settingsGroup=id;
 try{const g=await api('groups/'+id);if(selectedGroupId()!==id)return;
  $('groupSettingsTitle').textContent=g.name+' · 群设置';$('groupSettingsError').textContent='';
  $('groupSettingsBody').innerHTML=`<p class="small muted">群主 @${esc(g.owner_username)} · ${g.member_count} 位成员。新成员从入群起接收消息并查看之后的历史。</p>${g.can_manage?`<form id="renameGroupForm" class="group-rename"><input id="renameGroupName" aria-label="群名称" maxlength="80" required value="${esc(g.name)}"><button class="primary">保存群名</button></form><button id="toggleGroupMute">${g.muted?'解除全员禁言':'开启全员禁言'}</button>`:''}<h3>群成员</h3><div class="group-members">${g.members.map(m=>`<div class="group-member"><div><b>${esc(m.name)}</b><small>@${esc(m.username)}${m.id===g.owner?' · 群主':m.muted?' · 已禁言':''}${!m.active?' · 账号已停用':''}</small></div>${g.can_manage&&m.id!==g.owner?`<button data-group-action="${m.muted?'unmute':'mute'}" data-username="${esc(m.username)}">${m.muted?'解除禁言':'禁言'}</button><button class="group-danger" data-group-action="remove" data-username="${esc(m.username)}">移出</button>`:''}</div>`).join('')}</div>${g.can_manage?`<form id="inviteGroupForm"><h3>邀请成员</h3><div id="settingsInvitees" class="group-account-list">${inviteChecklist(state.accounts.filter(a=>a.active&&!g.members.some(m=>m.id===a.id)))}</div><button class="primary">邀请选中成员</button></form><button id="dissolveGroup" class="group-danger">解散群聊</button>`:'<button id="leaveGroup" class="group-danger">退出群聊</button>'}`;
  $('groupSettingsBody').querySelectorAll('[data-group-action]').forEach(button=>button.onclick=()=>{if(button.dataset.groupAction==='remove'&&!confirm('将 @'+button.dataset.username+' 移出群聊？'))return;groupAction(button.dataset.groupAction,{member:button.dataset.username});});
  if(g.can_manage){$('renameGroupForm').onsubmit=event=>{event.preventDefault();groupAction('rename',{name:$('renameGroupName').value});};$('toggleGroupMute').onclick=()=>groupAction(g.muted?'unmute_all':'mute_all');$('inviteGroupForm').onsubmit=event=>{event.preventDefault();const members=[...$('settingsInvitees').querySelectorAll('input:checked')].map(i=>i.value);if(!members.length){$('groupSettingsError').textContent='请先选择要邀请的成员';return;}groupAction('invite',{members});};$('dissolveGroup').onclick=()=>{if(confirm('解散后所有成员将无法继续访问此群。确定解散？'))groupAction('dissolve');};}
  else $('leaveGroup').onclick=()=>{if(confirm('退出此群聊？'))groupAction('leave');};
  if(!$('groupSettingsDialog').open)$('groupSettingsDialog').showModal();
 }catch(error){if(!aborted(error))banner(error.message);}
}
$('groupSettings').onclick=openGroupSettings;$('closeGroupSettings').onclick=()=>$('groupSettingsDialog').close();

$('messages').addEventListener('click', async event => {
  if(event.target.closest('.retry-message')){const pending=outgoing.get(selected);if(pending?.status==='failed')void sendOutgoing(selected,pending.text,pending.file);return;}
  const download=event.target.closest('[data-file-id]');
  if(download){await downloadFile(download);return;}
  const button=event.target.closest('.copy-code');
  if(!button || !$('messages').contains(button))return;
  const code=button.closest('.code-block').querySelector('code').textContent;
  try{await navigator.clipboard.writeText(code);button.textContent='已复制';}
  catch(_){button.textContent='请选中代码复制';}
  setTimeout(()=>{if(button.isConnected)button.textContent='复制';},1800);
});


function fileSize(size){return size<1024?size+' B':size<1024*1024?(size/1024).toFixed(1)+' KiB':(size/1024/1024).toFixed(1)+' MiB';}
function renderAttachment(file){
  return `${file.id&&imagePreviews.candidate(file)?`<button type="button" class="image-preview" data-image-preview="${esc(file.id)}" data-image-name="${esc(file.name)}" aria-label="预览图片：${esc(file.name)}">加载图片…</button>`:''}<div class="file-card"><span class="file-icon" aria-hidden="true">▤</span><div><b>${esc(file.name)}</b><small>${fileSize(file.size)}</small></div>${file.id?`<button type="button" class="download-file" data-file-id="${esc(file.id)}" data-file-name="${esc(file.name)}">下载</button>`:''}</div>`;
}
function renderFileDraft(){
  const file=fileDrafts.get(selected),element=$('fileDraft');
  element.classList.toggle('hidden',!file);
  element.innerHTML=file?`<span>▤ ${esc(file.name)} · ${fileSize(file.size)}</span><button type="button" id="removeFile" aria-label="移除待发送文件">×</button>`:'';
  imagePreviews.draftPreview(file,element);
  if(file)$('removeFile').onclick=()=>{fileDrafts.delete(selected);renderFileDraft();};
}
$('attachFile').onclick=()=>{$('fileInput').value='';$('fileInput').click();};
$('fileInput').onchange=()=>{
  const file=$('fileInput').files[0];if(!file||!selected)return;
  if(file.size>25*1024*1024){banner('单个文件最大 25 MiB');return;}
  fileDrafts.set(selected,file);renderFileDraft();banner('');
};
async function uploadFile(peer,text,file,clientId){
  const connection=active,epoch=generation,aborter=new AbortController();let timedOut=false;
  const cancel=()=>aborter.abort(),parent=controller.signal;parent.addEventListener('abort',cancel,{once:true});
  const timer=setTimeout(()=>{timedOut=true;aborter.abort();},300000);
  try{
    const form=new FormData();form.append('text',text);form.append('file',file,file.name);
    const query=new URLSearchParams({client_id:clientId,...(selectedGroupId(peer)?{group:selectedGroupId(peer)}:{to:peer})});
    const response=await fetch(connection.url+'/api/files?'+query,{method:'POST',credentials:connection.browser?'same-origin':'omit',headers:connection.token?{Authorization:'Bearer '+connection.token}:{},body:form,signal:aborter.signal});
    const result=await response.json();if(epoch!==generation)throw new DOMException('Connection changed','AbortError');
    if(!response.ok){if(response.status===401)invalidate(result.error);throw Error(result.error||'文件发送失败');}
    return result;
  }catch(error){if(timedOut)throw Error('文件上传超时，可点击原消息重试');throw error;}
  finally{clearTimeout(timer);parent.removeEventListener('abort',cancel);}
}
async function fetchAttachment(id,signal=controller.signal,timeout=300000){
  const connection=active,aborter=new AbortController();let timedOut=false;
  const cancel=()=>aborter.abort();signal?.addEventListener('abort',cancel,{once:true});if(signal?.aborted)cancel();
  const timer=setTimeout(()=>{timedOut=true;aborter.abort();},timeout);
  try{
    const response=await fetch(connection.url+'/api/files/'+encodeURIComponent(id),{credentials:connection.browser?'same-origin':'omit',headers:connection.token?{Authorization:'Bearer '+connection.token}:{},signal:aborter.signal});
    if(!response.ok){let message='文件读取失败';try{message=(await response.json()).error||message;}catch{}throw Error(message);}
    return await response.blob();
  }catch(error){if(timedOut)throw Error('文件读取超时，请重试');throw error;}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',cancel);}
}
async function downloadFile(button){
  button.disabled=true;button.textContent='下载中…';
  try{
    const blob=await fetchAttachment(button.dataset.fileId),url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download=button.dataset.fileName;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }catch(error){if(!aborted(error))banner(error.message);}
  finally{if(button.isConnected){button.disabled=false;button.textContent='下载';}}
}
