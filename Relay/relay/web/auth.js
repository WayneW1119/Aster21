/* Relay owns its browser login. No Squad password, passkey or session is read. */
let relaySecurityInfo=null;
function loginConnection(){return {url:normalizeUrl($('serverUrl').value.trim()||DEFAULT_RELAY),browser:true};}
async function relayAuth(path,body,connection=active||loginConnection()){
  if(new URL(connection.url).origin!==location.origin)throw Error('请直接打开这个域的 Relay 网页设置登录。');
  const login=path.startsWith('password/login')||path.startsWith('passkeys/login')||path==='logout';
  const response=await fetch(connection.url+'/api/auth/'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(!login&&connection.token?{Authorization:'Bearer '+connection.token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data=await response.json();if(!response.ok)throw Error(data.error||'登录请求失败');return data;
}
function relayBytes(value){return Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4)),c=>c.charCodeAt(0));}
function relayBase64(value){return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function relayCredential(credential){const response={};for(const key of ['clientDataJSON','attestationObject','authenticatorData','signature','userHandle'])if(credential.response[key])response[key]=relayBase64(credential.response[key]);if(credential.response.getTransports)response.transports=credential.response.getTransports();return {id:credential.id,rawId:relayBase64(credential.rawId),type:credential.type,response,clientExtensionResults:credential.getClientExtensionResults(),...(credential.authenticatorAttachment?{authenticatorAttachment:credential.authenticatorAttachment}:{})};}
function relayLoginError(error){return error.name==='NotAllowedError'?'已取消或设备未完成验证，请重试。':error.message;}
async function refreshRelaySecurity(){
  relaySecurityInfo=await relayAuth('status');$('securityUsername').value=relaySecurityInfo.username;
  $('relayCurrentPasswordLabel').classList.toggle('hidden',!relaySecurityInfo.configured);
  $('registerRelayPasskey').disabled=!relaySecurityInfo.configured||!relaySecurityInfo.passkeys_supported;
  $('relayPasskeyList').innerHTML=relaySecurityInfo.passkeys.map(k=>`<div class="managed"><span class="spacer">${esc(k.name||'我的设备')}</span><button data-remove-relay-key="${esc(k.id)}">移除</button></div>`).join('');
  $('relayPasskeyList').querySelectorAll('[data-remove-relay-key]').forEach(button=>button.onclick=async()=>{if(!confirm('移除此通行密钥？此域的旧网页登录会话将退出。'))return;try{await relayAuth('passkeys/remove',{id:button.dataset.removeRelayKey,current_password:$('relayCurrentPassword').value});await refreshRelaySecurity();$('relaySecurityMessage').textContent='通行密钥已移除。';}catch(e){$('relaySecurityMessage').textContent=e.message;}});
}
async function initRelayBrowserLogin(){
  let available=[];try{available=JSON.parse(document.querySelector('meta[name="relay-domains"]')?.content||'[]');}catch{}
  for(const c of connections)if(!available.some(x=>x.url===c.url))available.push(c);
  if(!available.length)available.push({url:DEFAULT_RELAY,label:'当前 Relay 域'});
  $('loginAnotherDomain').onclick=()=>{renderLoginConnections();$('domainPicker').close();$('app').classList.add('hidden');$('login').classList.remove('hidden');$('loginDomain').focus();};
  $('loginDomain').innerHTML=available.map(c=>`<option value="${esc(c.url)}">${esc(c.label||c.url)}</option>`).join('');
  const preferred=active?.url||localStorage.getItem('relay-preferred-domain');
  if(preferred&&available.some(c=>c.url===preferred))$('loginDomain').value=preferred;
  const chooseLoginDomain=()=>{$('serverUrl').value=$('loginDomain').value;$('loginError').textContent='';localStorage.setItem('relay-preferred-domain',$('loginDomain').value);};
  chooseLoginDomain();$('loginDomain').onchange=chooseLoginDomain;
  $('connectFromWorkspace').classList.toggle('hidden',!managerUrl||new URL(managerUrl,location.href).origin!==location.origin);
  $('connectFromWorkspace').onclick=async()=>{
    const button=$('connectFromWorkspace');button.disabled=true;$('loginError').textContent='';
    try{
      const target=loginConnection(),selected=new URL(target.url).pathname.split('/').filter(Boolean).pop();
      const response=await fetch(managerUrl+'/api/relay-connections?selected='+encodeURIComponent(selected),{credentials:'same-origin'});
      if(!response.ok)throw Error('当前浏览器还没有登录工作台。可以先在另一个页面登录，或直接使用域账号凭据连接。');
      const data=await response.json(),bundle=JSON.parse(decodeURIComponent(new URL(data.url).hash.slice(13)));
      for(const c of bundle.connections||[])upsert({...c,url:normalizeUrl(c.url)});
      const connection=connections.find(c=>c.url===target.url)||connections.find(c=>c.url===bundle.selected);
      if(!connection)throw Error('没有可连接的聊天账号');
      await activate(connection);$('loginSecurity').click();
    }catch(e){$('loginError').textContent=e.message;}finally{button.disabled=false;}
  };
  $('passwordLoginForm').onsubmit=async event=>{
    event.preventDefault();$('passwordLoginBtn').disabled=true;$('loginError').textContent='';
    try{const connection=loginConnection();await relayAuth('password/login',{username:$('loginUsername').value.trim(),password:$('loginPassword').value},connection);$('loginPassword').value='';await activate(connection);}catch(e){$('loginError').textContent=relayLoginError(e);}finally{$('passwordLoginBtn').disabled=false;}
  };
  $('passkeyLogin').disabled=!window.PublicKeyCredential||!window.isSecureContext;
  $('passkeyLogin').onclick=async()=>{
    $('passkeyLogin').disabled=true;$('loginError').textContent='';
    try{const connection=loginConnection(),options=await relayAuth('passkeys/login/options',{},connection);options.challenge=relayBytes(options.challenge);options.allowCredentials=(options.allowCredentials||[]).map(c=>({...c,id:relayBytes(c.id)}));const credential=await navigator.credentials.get({publicKey:options});if(!credential)throw Error('设备未完成验证');await relayAuth('passkeys/login/verify',{credential:relayCredential(credential)},connection);await activate(connection);}catch(e){$('loginError').textContent=relayLoginError(e);}finally{$('passkeyLogin').disabled=false;}
  };
  $('loginSecurity').onclick=async()=>{$('relaySecurityMessage').textContent='';$('relayCurrentPassword').value='';$('relayNewPassword').value='';$('relayConfirmPassword').value='';$('loginSecurityDialog').showModal();try{await refreshRelaySecurity();}catch(e){$('relaySecurityMessage').textContent=e.message;}};
  $('closeLoginSecurity').onclick=()=>$('loginSecurityDialog').close();
  $('passwordSettingsForm').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try{if($('relayNewPassword').value!==$('relayConfirmPassword').value)throw Error('两次输入的密码不一致');await relayAuth('password/set',{password:$('relayNewPassword').value,current_password:$('relayCurrentPassword').value});active.browser=true;active.token='';saveConnections();$('relayCurrentPassword').value='';$('relayNewPassword').value='';$('relayConfirmPassword').value='';await refreshRelaySecurity();$('relaySecurityMessage').textContent='Relay 密码已保存。现在可以绑定 Face ID / 通行密钥。';}catch(e){$('relaySecurityMessage').textContent=e.message;}finally{button.disabled=false;}
  };
  $('registerRelayPasskey').onclick=async()=>{
    $('registerRelayPasskey').disabled=true;$('relaySecurityMessage').textContent='请在系统提示中绑定设备。';
    try{const options=await relayAuth('passkeys/register/options',{current_password:$('relayCurrentPassword').value});options.challenge=relayBytes(options.challenge);options.user.id=relayBytes(options.user.id);options.excludeCredentials=(options.excludeCredentials||[]).map(c=>({...c,id:relayBytes(c.id)}));const credential=await navigator.credentials.create({publicKey:options});if(!credential)throw Error('设备未创建通行密钥');await relayAuth('passkeys/register/verify',{credential:relayCredential(credential),name:$('relayPasskeyName').value.trim()||'我的设备'});await refreshRelaySecurity();$('relaySecurityMessage').textContent='绑定成功。下次直接打开 Relay，用 Face ID / 通行密钥登录。';}catch(e){$('relaySecurityMessage').textContent=relayLoginError(e);}finally{$('registerRelayPasskey').disabled=false;}
  };
}
