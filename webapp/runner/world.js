// Pure bounded world rules; Runner currency never affects knowledge scores.
export const PHASES=Object.freeze(['RUN','APPROACH','DECIDE','RESOLVE']);
export function phaseSpeed(phase,elapsed=0,reduced=false){
 if(phase==='RUN')return 1;
 if(phase==='APPROACH'){const t=Math.min(1,elapsed/1.5);return reduced?.35:1-.65*(1-(1-t)**3);}
 return phase==='RESOLVE'?1.6:.12;
}
export function stepObjects(objects,dt,lane,jump,flying=false){
 let coins=0,hits=0,dodges=0;const effects=[];
 for(const o of objects){o.z+=dt*.31;if(!o.done&&o.z>=.79){o.done=true;
  if(Math.abs(o.lane-lane)<.42){if(o.kind==='coin'){o.taken=true;coins++;effects.push({...o,kind:'coin'});}
   else if(flying||jump>.09)dodges++;else{o.broken=true;hits++;effects.push({...o,kind:'barrier'});}}}}
 return {objects:objects.filter(o=>o.z<1.1),coins,hits,dodges,effects};
}
export function visibleObject(o){return !o.taken&&!o.broken;}
export function objectAlpha(o){return Math.max(0,Math.min(1,(1.03-o.z)/.18));}
export function useClassic(options,images,height,width){
 return options.some(o=>o.text.length>60)||images.some(img=>img.naturalWidth&&img.naturalHeight/img.naturalWidth*width*.22>height*.25);
}
