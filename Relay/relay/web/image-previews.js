/* Raster previews use authenticated file bytes, never external image URLs. */
window.createRelayImagePreviews = function(fetchFile) {
  const cache=new Map(),failed=new Set(),pending=new Set();
  let queue=[],running=0,bytes=0,epoch=0,aborter=new AbortController(),wanted=null,draft=null,draftUrl='',draftVersion=0;
  const viewer=document.getElementById('imageViewer'),large=document.getElementById('imageViewerImage');
  document.getElementById('closeImageViewer').onclick=()=>viewer.close();
  viewer.addEventListener('close',()=>{wanted=null;large.removeAttribute('src');});
  function candidate(file){return /\.(png|jpe?g|gif|webp)$/i.test(file.name||'');}
  async function raster(blob){
    const b=new Uint8Array(await blob.slice(0,16).arrayBuffer()),ascii=(start,end)=>String.fromCharCode(...b.slice(start,end));
    const type=b.length>=8&&b[0]===137&&ascii(1,4)==='PNG'&&b[4]===13&&b[5]===10&&b[6]===26&&b[7]===10?'image/png'
      :b[0]===255&&b[1]===216&&b[2]===255?'image/jpeg'
      :['GIF87a','GIF89a'].includes(ascii(0,6))?'image/gif'
      :ascii(0,4)==='RIFF'&&ascii(8,12)==='WEBP'?'image/webp':null;
    if(!type)throw Error('此文件无法预览，可下载后查看');
    return blob.slice(0,blob.size,type);
  }
  function buttons(id){return [...document.querySelectorAll('[data-image-preview]')].filter(b=>b.dataset.imagePreview===id);}
  function paint(button,item){
    const image=document.createElement('img');image.src=item.url;image.alt=button.dataset.imageName||'图片';image.decoding='async';
    image.onerror=()=>{button.textContent='此图片无法显示 · 可下载查看';};
    button.replaceChildren(image);button.setAttribute('aria-label','查看大图：'+image.alt);
  }
  function show(id){
    const item=cache.get(id);if(!item||wanted!==id)return;
    large.src=item.url;large.alt=item.name;document.getElementById('imageViewerName').textContent=item.name;
    if(!viewer.open)viewer.showModal();
  }
  function remember(id,blob,name){
    const item={url:URL.createObjectURL(blob),size:blob.size,name};cache.set(id,item);bytes+=item.size;
    while(cache.size>8||bytes>32*1024*1024){
      const key=cache.keys().next().value,old=cache.get(key);cache.delete(key);bytes-=old.size;URL.revokeObjectURL(old.url);
      for(const button of buttons(key))button.textContent='点击预览图片';
    }
    return item;
  }
  function enqueue(id,name){
    if(cache.has(id)){for(const button of buttons(id))paint(button,cache.get(id));show(id);return;}
    if(pending.has(id)||failed.has(id))return;
    pending.add(id);queue.push({id,name,epoch,signal:aborter.signal});pump();
  }
  function pump(){
    while(running<2&&queue.length){
      const job=queue.shift();running++;
      (async()=>{
        try{
          const blob=await raster(await fetchFile(job.id,job.signal));
          if(job.epoch!==epoch)return;
          const item=remember(job.id,blob,job.name);for(const button of buttons(job.id))paint(button,item);show(job.id);
        }catch(error){
          if(job.epoch!==epoch||error.name==='AbortError')return;
          failed.add(job.id);for(const button of buttons(job.id))button.textContent='点击重试预览 · 也可下载';
        }finally{running--;if(job.epoch===epoch)pending.delete(job.id);pump();}
      })();
    }
  }
  const observer=new IntersectionObserver(entries=>{
    for(const entry of entries)if(entry.isIntersecting){const b=entry.target;observer.unobserve(b);enqueue(b.dataset.imagePreview,b.dataset.imageName);}
  },{root:document.getElementById('messages'),rootMargin:'100px'});
  function mount(){
    observer.disconnect();
    for(const button of document.querySelectorAll('[data-image-preview]')){
      const id=button.dataset.imagePreview;
      button.onclick=()=>{wanted=id;failed.delete(id);enqueue(id,button.dataset.imageName);};
      if(cache.has(id))paint(button,cache.get(id));else if(!failed.has(id))observer.observe(button);
      else button.textContent='点击重试预览 · 也可下载';
    }
  }
  async function draftPreview(file,element){
    const version=++draftVersion;
    if(draftUrl){URL.revokeObjectURL(draftUrl);draftUrl='';}draft=file;
    if(!file||(!candidate(file)&&!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)))return;
    try{
      const blob=await raster(file);if(version!==draftVersion||draft!==file||!element.isConnected)return;
      draftUrl=URL.createObjectURL(blob);const image=document.createElement('img');image.src=draftUrl;image.alt='待发送图片预览';image.className='draft-image';
      image.onerror=()=>image.remove();element.prepend(image);
    }catch{ /* Non-raster files retain their ordinary file card. */ }
  }
  function clear(){
    epoch++;aborter.abort();aborter=new AbortController();observer.disconnect();queue=[];pending.clear();failed.clear();
    if(viewer.open)viewer.close();wanted=null;large.removeAttribute('src');
    for(const item of cache.values())URL.revokeObjectURL(item.url);cache.clear();bytes=0;
    draftVersion++;draft=null;if(draftUrl)URL.revokeObjectURL(draftUrl);draftUrl='';
  }
  return {candidate,mount,draftPreview,clear};
};
