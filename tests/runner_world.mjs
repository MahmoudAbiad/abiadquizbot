import assert from 'node:assert/strict';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../webapp/runner/world.js',import.meta.url),'utf8');
const {stepObjects,visibleObject,objectAlpha,objectLift,phaseSpeed,useClassic,PHASES,roadZ,roadMarkZ,WORLD_SPEED,projectRoad}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
for(const z of [.02,.2,.5,.81,1]){
 const p=projectRoad(1,z,390,844),edgeWidth=390*(.18+.92*(p.y/844-.34)/.66);
 assert.ok(Math.abs(p.road-edgeWidth)<1e-9);
 assert.ok(projectRoad(0,z,390,844).x>195-edgeWidth/2);
 assert.ok(projectRoad(2,z,390,844).x<195+edgeWidth/2);
}
const anchored={kind:'coin',lane:0,anchor:2,z:0};
stepObjects([anchored],1,1,0,false,false,2.3);
assert.equal(anchored.z,roadZ(2.3,2));
const before=anchored.z,mark=roadMarkZ(0,2.3);
stepObjects([anchored],10,1,0,false,false,2.3);assert.equal(anchored.z,before);
stepObjects([anchored],.1,1,0,false,false,2.3+WORLD_SPEED*.1);
assert.ok(Math.abs(anchored.z-before-(roadMarkZ(0,2.3+WORLD_SPEED*.1)-mark))<1e-12);
assert.ok(roadZ(0,8*WORLD_SPEED-.60)<0); // Far barrier approaches from the horizon.
assert.ok(Math.abs(roadZ(8*WORLD_SPEED,8*WORLD_SPEED-.60)-.62)<1e-12);
const coin={kind:'coin',lane:1,z:.78,done:false};
const first=stepObjects([coin],.05,1,0);assert.equal(first.coins,1);assert.equal(visibleObject(coin),false);
assert.equal(stepObjects(first.objects,.05,1,0).coins,0);
const barrier={kind:'barrier',lane:1,z:.78};assert.equal(stepObjects([barrier],.05,1,0).hits,1);assert.equal(visibleObject(barrier),false);
const jumped={kind:'barrier',lane:1,z:.78};assert.equal(stepObjects([jumped],.05,1,.5).dodges,1);assert.equal(visibleObject(jumped),true);
const airborne=stepObjects([{kind:'barrier',lane:1,z:.78}],.05,1,0,true);assert.equal(airborne.hits,0);assert.equal(airborne.dodges,1);
assert.equal(stepObjects([{kind:'coin',lane:1,z:.78}],.05,1,0,true).coins,1);
assert.equal(stepObjects([{kind:'coin',lane:0,z:.78}],.05,1,0).coins,0);
assert.equal(stepObjects([{z:1.09,done:true}],1,1,0).objects.length,0);
assert.ok(objectAlpha({z:.95})<objectAlpha({z:.85}));
assert.deepEqual(PHASES,['RUN','APPROACH','DECIDE','RESOLVE']);assert.equal(phaseSpeed('RUN'),1);assert.equal(phaseSpeed('APPROACH',0),1);
assert.ok(Math.abs(phaseSpeed('APPROACH',1.5)-.35)<.001);assert.equal(phaseSpeed('DECIDE'),0);assert.equal(phaseSpeed('RESOLVE'),0);
assert.ok(useClassic([{text:'x'.repeat(61)}],[],844,390));assert.ok(useClassic([{text:'x'}],[{naturalHeight:1000,naturalWidth:50}],844,390));assert.equal(useClassic([{text:'قصير'}],[],844,390),false);
console.log('Runner world: coins, collision, jump, fade, four phases, long/math fallback PASS');

assert.equal(objectLift({kind:'barrier',z:.81},844,true),0);assert.equal(objectLift({kind:'coin',z:.81},844,false),0);assert.equal(objectLift({kind:'coin',z:.81},844,true),844*.16);
assert.equal(stepObjects([{kind:'coin',lane:0,z:.78}],.05,1,0,false,true).coins,1);
assert.equal(stepObjects([{kind:'barrier',lane:0,z:.78}],.05,1,0,false,true).hits,0);
