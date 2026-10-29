export function startRefresh(){
  const button=document.getElementById('refreshNow'),updated=document.getElementById('lastUpdated');
  button.addEventListener('click',()=>document.dispatchEvent(new CustomEvent('viewer:refresh',{detail:{manual:true}})));
  document.addEventListener('viewer:refreshing',event=>{button.disabled=event.detail.active;button.textContent=event.detail.active?'刷新中…':'↻ 刷新'});
  document.addEventListener('viewer:updated',event=>{updated.textContent=new Date(event.detail.at).toLocaleTimeString('zh-CN',{hour12:false})});
}
