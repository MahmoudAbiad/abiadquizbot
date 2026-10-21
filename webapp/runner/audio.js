// Local, original compressed assets; nothing plays until the Start gesture.
export class GameAudio {
  constructor(){this.music=localStorage.getItem('runner.music')!=='off';this.sfx=localStorage.getItem('runner.sfx')!=='off';this.buffers={};this.sources=new Set();this.mode='run';this.generation=0;}
  async start(){
    this.stop();const generation=++this.generation;
    const ctx=this.ctx=new (window.AudioContext||window.webkitAudioContext)();
    await ctx.resume();
    this.runGain=ctx.createGain();this.thinkGain=ctx.createGain();this.fxGain=ctx.createGain();
    [this.runGain,this.thinkGain,this.fxGain].forEach(g=>g.connect(ctx.destination));
    this.runGain.gain.value=0;this.thinkGain.gain.value=0;this.fxGain.gain.value=this.sfx?.45:0;
    await Promise.all(['run','think','coin','jump','dodge','correct','wrong','near','caught'].map(async name=>{
      try {if(!this.buffers[name]){const r=await fetch(`audio/${name}.mp3`);if(!r.ok)throw Error('audio');this.buffers[name]=await ctx.decodeAudioData(await r.arrayBuffer());}}
      catch(e){/* Gameplay remains usable when audio cannot load. */}
    }));
    if(generation!==this.generation||ctx.state==='closed')return;
    ['run','think'].forEach(name=>{if(!this.buffers[name])return;const s=ctx.createBufferSource();s.buffer=this.buffers[name];s.loop=true;s.connect(name==='run'?this.runGain:this.thinkGain);s.start();this.sources.add(s);});
    this.setMode(this.mode);
  }
  setMode(mode){this.mode=mode;if(!this.ctx||this.ctx.state==='closed')return;const now=this.ctx.currentTime;
    [[this.runGain,mode==='run'?.27:0],[this.thinkGain,mode==='think'?.19:0]].forEach(([g,v])=>{g.gain.cancelScheduledValues(now);g.gain.setValueAtTime(g.gain.value,now);g.gain.linearRampToValueAtTime(this.music?v:0,now+.65);});}
  play(name){if(!this.sfx||!this.ctx||this.ctx.state!=='running'||!this.buffers[name])return;const s=this.ctx.createBufferSource();s.buffer=this.buffers[name];s.connect(this.fxGain);this.sources.add(s);s.onended=()=>this.sources.delete(s);s.start();}
  toggleMusic(){this.music=!this.music;localStorage.setItem('runner.music',this.music?'on':'off');this.setMode(this.mode);return this.music;}
  toggleSfx(){this.sfx=!this.sfx;localStorage.setItem('runner.sfx',this.sfx?'on':'off');if(this.fxGain)this.fxGain.gain.value=this.sfx?.45:0;return this.sfx;}
  suspend(){this.ctx?.suspend().catch(()=>{});}
  resume(){this.ctx?.resume().catch(()=>{});}
  stop(){this.generation++;this.sources.forEach(s=>{try{s.stop();s.disconnect();}catch{}});this.sources.clear();if(this.ctx&&this.ctx.state!=='closed')this.ctx.close().catch(()=>{});this.ctx=null;}
}
