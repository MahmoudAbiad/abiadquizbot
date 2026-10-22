// Pure bounded world rules; Runner currency never affects knowledge scores.
export const PHASES=Object.freeze(['RUN','APPROACH','DECIDE','RESOLVE']);
// Road markings, pickups and barriers use the same continuous travel offset.
export const WORLD_SPEED=.31;
export function roadZ(distance,anchor){return .02+distance-anchor;}
export function roadMarkZ(index,distance){return (index/15+distance)%1;}
export function previewLines(text,measure,width,maxLines=4){
 const original=Array.from(String(text).replace(/\s+/g,' ').trim()),chars=original.slice(0,320),lines=[];
 while(chars.length&&lines.length<maxLines){
  let count=1;while(count<chars.length&&measure(chars.slice(0,count+1).join(''))<=width)count++;
  if(count<chars.length){const space=chars.slice(0,count).lastIndexOf(' ');if(space>0)count=space;}
  let line=chars.splice(0,count).join('').trim();while(chars[0]===' ')chars.shift();
  if(lines.length===maxLines-1&&(chars.length||original.length>320)){
   while(line&&measure(line+'…')>width)line=Array.from(line).slice(0,-1).join('');line+='…';
  }
  lines.push(line);
 }
 return lines;
}
export function projectRoad(lane,z,width,height){
 const t=Math.max(0,z),depth=t*t,y=height*(.34+.72*depth);
 // Match the asphalt's straight edges at this screen height.
 const road=width*(.18+.92*(y/height-.34)/.66);
 return {x:width/2+(lane-1)*road/3,y,scale:.15+.95*depth,road};
}
export function phaseSpeed(phase,elapsed=0,reduced=false){
 if(phase==='RUN')return 1;
 if(phase==='APPROACH'){const t=Math.min(1,elapsed/1.5);return reduced?.35:1-.65*(1-(1-t)**3);}
 return 0; // Answering, feedback and resolving never move the street.
}
export function stepObjects(objects,dt,lane,jump,flying=false,magnet=false,distance){
 let coins=0,hits=0,dodges=0;const effects=[];
 for(const o of objects){o.z=Number.isFinite(o.anchor)&&Number.isFinite(distance)?roadZ(distance,o.anchor):o.z+dt*WORLD_SPEED;if(!o.done&&o.z>=.79){o.done=true;
  if(Math.abs(o.lane-lane)<.42||(magnet&&o.kind==='coin')){if(o.kind==='coin'){o.taken=true;coins++;effects.push({...o,kind:'coin'});}
   else if(flying||jump>.09)dodges++;else{o.broken=true;hits++;effects.push({...o,kind:'barrier'});}}}}
 return {objects:objects.filter(o=>o.z<1.1),coins,hits,dodges,effects};
}
export function visibleObject(o){return !o.taken&&!o.broken;}
// Perspective height follows the airborne player; barriers remain on the road.
export function objectLift(o,height,flying){return flying&&o.kind==='coin'?height*.16*(Math.max(0,o.z)/.81)**2:0;}
export function objectAlpha(o){return Math.max(0,Math.min(1,(1.03-o.z)/.18));}
export function useClassic(options,images,height,width){
 return options.some(o=>o.text.length>60)||images.some(img=>img.naturalWidth&&img.naturalHeight/img.naturalWidth*width*.22>height*.25);
}
