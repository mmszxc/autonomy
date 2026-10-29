export function startStarfield(){
  const canvas=document.getElementById('starfield'),ctx=canvas.getContext('2d'),reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let stars=[],frame=0,last=0,paused=false;
  function resize(){canvas.width=innerWidth;canvas.height=innerHeight;stars=Array.from({length:44},()=>({x:Math.random()*canvas.width,y:Math.random()*canvas.height,r:Math.random()*1.2+.25,v:Math.random()*.08+.015}))}
  function draw(time){frame=0;if(paused||document.hidden||reduced.matches)return;if(time-last<50){frame=requestAnimationFrame(draw);return}last=time;ctx.clearRect(0,0,canvas.width,canvas.height);for(const star of stars){star.y+=star.v;if(star.y>canvas.height)star.y=0;ctx.beginPath();ctx.arc(star.x,star.y,star.r,0,Math.PI*2);ctx.fillStyle=`rgba(0,229,255,${.22+star.r/3})`;ctx.fill()}frame=requestAnimationFrame(draw)}
  function run(){cancelAnimationFrame(frame);frame=0;if(!paused&&!document.hidden&&!reduced.matches)frame=requestAnimationFrame(draw)}
  document.addEventListener('viewer:pause',event=>{paused=event.detail.paused;run()});document.addEventListener('visibilitychange',run);reduced.addEventListener?.('change',run);addEventListener('resize',resize);resize();run()
}
