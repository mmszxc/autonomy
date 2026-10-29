import{element,setText}from'./dom.js';
export function startClock(){function tick(){const now=new Date;setText('clock',now.toLocaleTimeString('zh-CN',{hour12:false}));document.getElementById('clock').append(element('small',`${now.toLocaleDateString('zh-CN')} · LOCAL SYSTEM TIME`))}tick();return setInterval(tick,1000)}
