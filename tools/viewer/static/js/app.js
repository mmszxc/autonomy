import{startClock}from'./clock.js';
import{startPoller}from'./poller.js';
import{bindSeeds,loadSeeds}from'./seed-map.js';
import{startStarfield}from'./starfield.js';
import{loadStatus}from'./status.js';
import{startTabs}from'./tabs.js';
import{loadThoughts}from'./thought-stream.js';
let active='seeds',refreshing=false;
async function refresh(){if(refreshing||document.hidden)return;refreshing=true;try{const jobs=[loadStatus()];if(active==='seeds')jobs.push(loadSeeds());else if(active==='thoughts')jobs.push(loadThoughts());await Promise.all(jobs);document.dispatchEvent(new CustomEvent('viewer:updated',{detail:{at:Date.now()}}))}finally{refreshing=false}}
startClock();startStarfield();bindSeeds();startTabs(name=>{active=name;document.dispatchEvent(new CustomEvent('viewer:refresh',{detail:{force:true}}))});document.addEventListener('viewer:refresh',()=>refresh().catch(()=>{}));startPoller();
