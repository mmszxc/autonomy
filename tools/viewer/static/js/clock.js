import{element,setText}from'./dom.js';
export function startClock(){
  let timer=0;
  function tick(){const now=new Date;setText('clock',now.toLocaleTimeString('zh-CN',{hour12:false}));document.getElementById('clock').append(element('small',`${now.toLocaleDateString('zh-CN')} · LOCAL SYSTEM TIME`))}
  function run(paused=false){clearInterval(timer);if(paused)return;tick();timer=setInterval(tick,1000)}
  document.addEventListener('viewer:pause',event=>run(event.detail.paused));run();return()=>clearInterval(timer)
}
