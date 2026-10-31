import{startClock}from'./clock.js';
import{startRefresh}from'./refresh.js';
import{bindSeeds,loadSeeds}from'./seed-map.js';
import{startStarfield}from'./starfield.js';
import{loadStatus}from'./status.js';
import{loadNotesToTeam}from'./notes.js';
import{loadModelRoute}from'./model-route.js';
import{startTabs}from'./tabs.js';
import{loadThoughts}from'./thought-stream.js';
let active='seeds',refreshing=false;
async function refresh(){if(refreshing)return;refreshing=true;document.dispatchEvent(new CustomEvent('viewer:refreshing',{detail:{active:true}}));try{const jobs=[loadStatus(),loadNotesToTeam(),loadModelRoute()];if(active==='seeds')jobs.push(loadSeeds());else if(active==='thoughts')jobs.push(loadThoughts());await Promise.all(jobs);document.dispatchEvent(new CustomEvent('viewer:updated',{detail:{at:Date.now()}}))}finally{refreshing=false;document.dispatchEvent(new CustomEvent('viewer:refreshing',{detail:{active:false}}))}}
startClock();startStarfield();bindSeeds();startRefresh();document.addEventListener('viewer:refresh',()=>refresh().catch(()=>{}));startTabs(name=>{active=name;refresh().catch(()=>{})});
