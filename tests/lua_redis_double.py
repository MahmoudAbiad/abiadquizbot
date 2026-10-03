"""Run the *production Lua scripts* against a deterministic Redis command double.

Local harness uses the system Lua library. CI additionally uses a real local Redis.
No production credentials/network calls.
"""
import ctypes
import ctypes.util

REDIS_COMMANDS = r'''
db = {}; ttl = {}; NOW = 0
redis = {}
local function expire(k)
 if ttl[k] and ttl[k] <= NOW then db[k]=nil; ttl[k]=nil end
end
function redis.call(cmd, k, ...)
 expire(k)
 local a={...}; cmd=string.upper(cmd)
 if cmd=='EXISTS' then return db[k] and 1 or 0 end
 if cmd=='GET' then return db[k] or false end
 if cmd=='SET' then
  for i=2,#a do if a[i]=='NX' and db[k] then return false end end
  db[k]=a[1]; ttl[k]=nil
  for i=2,#a do if a[i]=='EX' then ttl[k]=NOW+tonumber(a[i+1]) end end
  return 'OK'
 end
 if cmd=='DEL' then local exists=db[k]~=nil; db[k]=nil; ttl[k]=nil; return exists and 1 or 0 end
 if cmd=='EXPIRE' then if not db[k] then return 0 end; ttl[k]=NOW+tonumber(a[1]); return 1 end
 if cmd=='HGET' then return db[k] and db[k][a[1]] or false end
 if cmd=='HEXISTS' then return db[k] and db[k][a[1]] and 1 or 0 end
 if cmd=='HSET' then db[k]=db[k] or {}; db[k][a[1]]=a[2]; return 1 end
 if cmd=='HDEL' then if db[k] then db[k][a[1]]=nil end; return 1 end
 if cmd=='HINCRBY' then db[k]=db[k] or {}; db[k][a[1]]=tonumber(db[k][a[1]] or 0)+tonumber(a[2]); return db[k][a[1]] end
 if cmd=='ZADD' then
  db[k]=db[k] or {}; local i=1
  if a[1]=='XX' then i=2; if not db[k][a[3]] then return 0 end end
  -- Real Redis: reply counts only NEWLY ADDED members; updating an existing score returns 0.
  local existed=db[k][a[i+1]]~=nil
  db[k][a[i+1]]=tonumber(a[i]); return existed and 0 or 1
 end
 if cmd=='ZSCORE' then return db[k] and db[k][a[1]] or false end
 if cmd=='ZREM' then if db[k] then db[k][a[1]]=nil end; return 1 end
 if cmd=='ZRANGEBYSCORE' then
  local min=a[1]=='-inf' and -math.huge or tonumber(a[1]);local max=tonumber(a[2]);local r={}
  for id,score in pairs(db[k] or {}) do if score>=min and score<=max then table.insert(r,{id,score}) end end
  table.sort(r,function(x,y) return x[2]<y[2] or (x[2]==y[2] and x[1]<y[1]) end)
  local out={};for i=1,math.min(#r,tonumber(a[5])) do table.insert(out,r[i][1]) end;return out
 end
 if cmd=='LPUSH' then db[k]=db[k] or {};table.insert(db[k],1,a[1]);return #db[k] end
 if cmd=='LTRIM' then local r={};for i=tonumber(a[1])+1,math.min(#db[k],tonumber(a[2])+1) do table.insert(r,db[k][i]) end;db[k]=r;return 'OK' end
 error('Unexpected Redis command '..cmd)
end
'''


class LuaRedis:
    def __init__(self):
        name=ctypes.util.find_library('lua5.4')
        if not name:raise RuntimeError('System Lua 5.4 unavailable')
        self.lib=ctypes.CDLL(name)
        specs={'luaL_newstate':([],ctypes.c_void_p),'luaL_openlibs':([ctypes.c_void_p],None),
               'luaL_loadstring':([ctypes.c_void_p,ctypes.c_char_p],ctypes.c_int),
               'lua_pcallk':([ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_longlong,ctypes.c_void_p],ctypes.c_int),
               'lua_type':([ctypes.c_void_p,ctypes.c_int],ctypes.c_int),
               'lua_tolstring':([ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_size_t)],ctypes.c_void_p),
               'lua_toboolean':([ctypes.c_void_p,ctypes.c_int],ctypes.c_int),
               'lua_tonumberx':([ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p],ctypes.c_double),
               'lua_rawlen':([ctypes.c_void_p,ctypes.c_int],ctypes.c_size_t),
               'lua_rawgeti':([ctypes.c_void_p,ctypes.c_int,ctypes.c_longlong],ctypes.c_int),
               'lua_gettop':([ctypes.c_void_p],ctypes.c_int),'lua_settop':([ctypes.c_void_p,ctypes.c_int],None),
               'lua_close':([ctypes.c_void_p],None)}
        for fn,(args,result) in specs.items():
            method=getattr(self.lib,fn);method.argtypes=args;method.restype=result
        self.state=self.lib.luaL_newstate();self.lib.luaL_openlibs(self.state)
        self.clock=lambda:0
        self.fail=False
        self.execute(REDIS_COMMANDS)

    def close(self):
        if self.state:self.lib.lua_close(self.state);self.state=None

    @staticmethod
    def literal(value):
        raw=str(value).encode()
        return '"'+''.join('\\'+f'{byte:03}' for byte in raw)+'"'

    def value(self,index):
        kind=self.lib.lua_type(self.state,index)
        if kind==0:return None
        if kind==1:return bool(self.lib.lua_toboolean(self.state,index))
        if kind==3:return self.lib.lua_tonumberx(self.state,index,None)
        if kind==4:
            size=ctypes.c_size_t();pointer=self.lib.lua_tolstring(self.state,index,ctypes.byref(size));return ctypes.string_at(pointer,size.value).decode()
        if kind==5:
            out=[];absolute=index if index>0 else self.lib.lua_gettop(self.state)+index+1
            for i in range(1,self.lib.lua_rawlen(self.state,absolute)+1):
                self.lib.lua_rawgeti(self.state,absolute,i);out.append(self.value(-1));self.lib.lua_settop(self.state,-2)
            return out
        raise AssertionError(kind)

    def execute(self,code):
        self.lib.lua_settop(self.state,0)
        result=self.lib.luaL_loadstring(self.state,code.encode())
        if not result:result=self.lib.lua_pcallk(self.state,0,1,0,0,None)
        if result:raise RuntimeError(self.value(-1))
        return self.value(-1)

    async def eval(self,script,count,*args):
        if self.fail:raise ConnectionError('Redis unavailable')
        keys='{'+','.join(map(self.literal,args[:count]))+'}'
        argv='{'+','.join(map(self.literal,args[count:]))+'}'
        return self.execute(f'KEYS={keys}; ARGV={argv}; NOW={self.clock()}; '+script)

    async def set(self,key,value,nx=False,ex=None):
        args=['SET',key,value]+(['NX'] if nx else [])+(['EX',ex] if ex else [])
        return self.execute(f'NOW={self.clock()}; return redis.call('+','.join(map(self.literal,args))+')')
