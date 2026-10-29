const PAUSE_KEY='xiaozhi.viewer.paused';
const INTERVAL_KEY='xiaozhi.viewer.interval';
const allowed=new Set(['15','30','60','manual']);
const read=(key,fallback)=>{try{return localStorage.getItem(key)??fallback}catch{return fallback}};
const save=(key,value)=>{try{localStorage.setItem(key,value)}catch{}};
const event=(name,detail={})=>document.dispatchEvent(new CustomEvent(name,{detail}));
function interacting(){
  const active=document.activeElement;
  return active?.matches?.('#search')||document.getElementById('drawer')?.classList.contains('open')||Boolean(document.querySelector('#thoughts details[open]'));
}
export function startPoller(){
  const toggle=document.getElementById('pauseToggle'),select=document.getElementById('pollInterval'),manual=document.getElementById('refreshNow');
  let paused=read(PAUSE_KEY,'false')==='true',interval=read(INTERVAL_KEY,'30'),timer=0;
  if(!allowed.has(interval))interval='30';select.value=interval;
  function paint(){
    document.documentElement.dataset.viewerPaused=String(paused);
    toggle.textContent=paused?'▶ 继续':'⏸ 暂停';toggle.setAttribute('aria-pressed',String(paused));
    const badge=document.getElementById('liveState');badge.textContent=paused?'PAUSED':'LIVE';badge.classList.toggle('paused',paused);
  }
  function schedule(){clearTimeout(timer);if(paused||interval==='manual')return;timer=setTimeout(()=>request(false),Number(interval)*1000)}
  function request(force){
    if(paused||document.hidden||(!force&&interacting())){schedule();return}
    event('viewer:refresh',{force});schedule();
  }
  function setPaused(value){paused=value;save(PAUSE_KEY,String(value));paint();event('viewer:pause',{paused});if(!paused)request(true);else clearTimeout(timer)}
  toggle.addEventListener('click',()=>setPaused(!paused));
  select.addEventListener('change',()=>{interval=select.value;save(INTERVAL_KEY,interval);schedule()});
  manual.addEventListener('click',()=>request(true));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else if(!paused)request(false)});
  document.addEventListener('viewer:updated',({detail})=>{const value=document.getElementById('lastUpdated');value.textContent=detail?.at?new Date(detail.at).toLocaleTimeString('zh-CN',{hour12:false}):'--:--:--'});
  paint();event('viewer:pause',{paused});if(!paused)request(true);
  return{request,setPaused};
}
