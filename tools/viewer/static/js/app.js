import{startClock}from'./clock.js';import{bindSeeds,loadSeeds}from'./seed-map.js';import{startStarfield}from'./starfield.js';import{loadStatus}from'./status.js';import{startTabs}from'./tabs.js';import{loadThoughts}from'./thought-stream.js';
let active='seeds',contentTimer=0,statusTimer=0;
async function content(){if(document.hidden)return;if(active==='seeds')await loadSeeds();else if(active==='thoughts')await loadThoughts()}
function schedule(){clearInterval(contentTimer);clearInterval(statusTimer);if(document.hidden)return;content().catch(()=>{});loadStatus().catch(()=>{});contentTimer=setInterval(()=>content().catch(()=>{}),15000);statusTimer=setInterval(()=>loadStatus().catch(()=>{}),15000)}
startClock();startStarfield();bindSeeds();startTabs(name=>{active=name;schedule()});document.addEventListener('visibilitychange',schedule);
