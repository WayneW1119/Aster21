/* One visible-viewport owner for browser chrome, keyboards and modal sheets. */
(()=>{
 const viewport=window.visualViewport, root=document.documentElement;
 let frame=0, width=innerWidth, baseline=viewport?.height||innerHeight;
 let previous={height:0,top:0},settleTimers=[];
 const editable=()=>document.activeElement?.matches('input:not([type="checkbox"]):not([type="radio"]),textarea,select,[contenteditable="true"]');
 function revealInput(){
  const field=document.activeElement;
  if(!editable())return;
  // Move only the nearest content scroller; never scroll the document or header.
  for(let parent=field.parentElement;parent&&parent!==document.body;parent=parent.parentElement){
   if(!/(auto|scroll)/.test(getComputedStyle(parent).overflowY)||parent.scrollHeight<=parent.clientHeight)continue;
   const box=parent.getBoundingClientRect(), rect=field.getBoundingClientRect();
   let top=Math.max(box.top,previous.top)+12, bottom=Math.min(box.bottom,previous.top+previous.height)-16;
   const header=parent.querySelector('.dialog-header'), footer=parent.querySelector('.dialog-footer');
   if(header&&getComputedStyle(header).position==='sticky')top=Math.max(top,header.getBoundingClientRect().bottom+8);
   if(footer&&getComputedStyle(footer).position==='sticky')bottom=Math.min(bottom,footer.getBoundingClientRect().top-8);
   if(rect.height>bottom-top){if(rect.top<top||rect.top>=bottom)parent.scrollTop+=rect.top-top;}
   else if(rect.bottom>bottom)parent.scrollTop+=rect.bottom-bottom;
   else if(rect.top<top)parent.scrollTop+=rect.top-top;
   break;
  }
 }
 function update(){
  frame=0;
  // Preserve browser pinch zoom: it must not relayout the app at each zoom step.
  if(viewport&&Math.abs(viewport.scale-1)>.01)return;
  const height=Math.round(viewport?.height||innerHeight), top=Math.max(0,Math.round(viewport?.offsetTop||0));
  if(width!==innerWidth){width=innerWidth;baseline=height;}
  baseline=Math.max(baseline,height);
  root.classList.toggle('keyboard-open',!!editable()&&baseline-height>120);
  if(height===previous.height&&top===previous.top)return;
  const detail={height,top,previousHeight:previous.height,previousTop:previous.top};
  window.dispatchEvent(new CustomEvent('appviewportwillchange',{detail}));
  root.style.setProperty('--app-height',height+'px');
  root.style.setProperty('--app-top',top+'px');
  const shrinking=previous.height&&height<previous.height;
  previous={height,top};
  window.dispatchEvent(new CustomEvent('appviewportchange',{detail}));
  if(shrinking)requestAnimationFrame(revealInput);
 }
 function schedule(){if(!frame)frame=requestAnimationFrame(update);}
 viewport?.addEventListener('resize',settle);viewport?.addEventListener('scroll',schedule);
 window.addEventListener('resize',settle);window.addEventListener('pageshow',schedule);
 // Safari may expose its final keyboard geometry after the last resize event.
 function settle(){
  settleTimers.forEach(clearTimeout);
  schedule();settleTimers=[80,250,500,800].map(delay=>setTimeout(schedule,delay));
 }
 viewport?.addEventListener('scrollend',schedule);
 document.addEventListener('focusin',()=>{settle();requestAnimationFrame(revealInput);});
 document.addEventListener('focusout',settle);window.addEventListener('orientationchange',settle);update();
 const manifest=document.querySelector('link[rel="manifest"]');
 if(manifest&&'serviceWorker' in navigator&&window.isSecureContext){
  navigator.serviceWorker.register(new URL('sw.js',manifest.href),{scope:new URL('./',manifest.href).pathname}).catch(()=>{});
 }
})();
