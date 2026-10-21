"""Generate original CC0 game music/SFX; requires ffmpeg. No third-party samples."""
import math, wave, struct, subprocess, tempfile
from pathlib import Path
RATE=22050
OUT=Path(__file__).resolve().parents[1]/'webapp/runner/audio'

def tone(buf,start,duration,freq,volume=.15):
    first=int(start*RATE)
    for j in range(int(duration*RATE)):
        i=first+j
        if i>=len(buf):break
        t=j/RATE
        env=min(1,t/.012)*max(0,1-t/duration)**1.6
        buf[i]+=volume*env*(math.sin(2*math.pi*freq*t)+.2*math.sin(4*math.pi*freq*t))

def save(name,buf):
    with tempfile.NamedTemporaryFile(suffix='.wav') as f:
        with wave.open(f.name,'wb') as w:
            w.setparams((1,2,RATE,0,'NONE','not compressed'))
            w.writeframes(b''.join(struct.pack('<h',int(max(-.95,min(.95,x))*32767)) for x in buf))
        subprocess.run(['ffmpeg','-loglevel','error','-y','-i',f.name,'-codec:a','libmp3lame','-b:a','48k',str(OUT/(name+'.mp3'))],check=True)

def music(name,quiet=False):
    beat=.4;buf=[0.] * int(RATE*beat*32)
    melody=[0,7,12,10,7,3,5,7,0,3,7,14,12,10,7,5]
    for n in range(32):
        freq=220*2**(melody[n%16]/12)
        if not quiet or n%2==0:tone(buf,n*beat,beat*(1.8 if quiet else .65),freq,.065 if quiet else .12)
        if n%2==0:tone(buf,n*beat,beat*1.8,110*2**([0,0,3,5][n//8]/12),.09)
        if not quiet:
            tone(buf,n*beat,.1,58,.2)
            if n%2:tone(buf,n*beat,.045,2400,.018)
    save(name,buf)

OUT.mkdir(exist_ok=True,parents=True)
music('run');music('think',True)
for name,notes in {'coin':[880,1320],'jump':[220,330,440],'dodge':[660,880], 'correct':[440,554,660,880], 'wrong':[330,220,165], 'near':[146,155,146], 'caught':[220,196,146,110]}.items():
    buf=[0.] * int(RATE*(len(notes)*.09+.18))
    for n,f in enumerate(notes):tone(buf,n*.09,.17,f,.22)
    save(name,buf)
