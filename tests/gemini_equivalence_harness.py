# قارن مخرجات نسختين من gemini_helper: python tests/gemini_equivalence_harness.py <old|new> > out.json (old يتطلب نسخة قديمة بـ /tmp/gemini_helper_old.py)
import asyncio, os, sys, json, importlib.util, datetime, tempfile, types as pytypes
os.environ.update(BOT_TOKEN="123456:TEST",ADMIN_ID="1",SUPABASE_URL="https://example.supabase.co",SUPABASE_KEY="t")
os.environ.pop("GROQ_API_KEY",None); os.environ.pop("GEMINI_API_KEYS",None)
sys.path.insert(0,os.getcwd())
which=sys.argv[1]
import logging; logging.disable(logging.CRITICAL)
if which=="old":
    spec=importlib.util.spec_from_file_location("helpers_gemini_helper_old","/tmp/gemini_helper_old.py")
    G=importlib.util.module_from_spec(spec); sys.modules["helpers_gemini_helper_old"]=G; spec.loader.exec_module(G)
    C=S=P=G       # cascade / state / (patch targets) all one module
else:
    import helpers.gemini_helper as G
    import helpers.gemini_helper.cascade as C, helpers.gemini_helper.state as S
out={}
def rec(k,v): out[k]=v

# 1 json repair
samples=[r"\bar{d}",r"\text{x}\n",r"a\nb",r"\neq \nabla \nu",r"\frac{1}{2}\\sqrt",r"\s \alpha \p",r"plain",r'say \"hi\" \/ \u0041',"trail\\"]
rec("repair",[G._repair_json_backslashes(x) for x in samples])
# 2 mime
rec("mime",[G.get_safe_mime_type(x) for x in ["a.DOCX","b.pdf","c.jpeg","d.xyz","e.txt","f.csv","g"]])
# 3 splits
rec("split_imgs",[G.split_images_into_three_sync([f"p{i}" for i in range(n)]) for n in (1,2,3,8)])
import fitz
d=tempfile.mkdtemp(); pdf=os.path.join(d,"t.pdf"); doc=fitz.open()
for i in range(7): doc.new_page().insert_text((50,50),f"page {i}")
doc.save(pdf); doc.close()
chunks=G.split_pdf_into_three_sync(pdf)
rec("split_pdf",[len(fitz.open(c)) for c in chunks]); rec("pdf_pages",G.get_pdf_page_count_sync(pdf))
# 4 resolvers
rec("resolve",[G._resolve_difficulty_instruction(x) for x in (None,"easy","zzz")]+[G._resolve_question_type_instruction(a,b) for a,b in ((None,None),("general",None),("x"," custom "),("zz",None))])
# 5 schemas
rec("schema",G.QuizResponse.model_json_schema())
# 6 block logic
C.blocked_model_keys.clear()
now=datetime.datetime.now()
G._mark_model_key_failure(0,"m1",Exception("429 RESOURCE_EXHAUSTED quota"))
G._mark_model_key_failure(1,"m1",Exception("503 overloaded"))
G._mark_model_key_failure(2,"m1",Exception("weird"))
rec("blocked",{f"{k[0]}:{k[1]}":round((v-now).total_seconds()/3600,1) for k,v in sorted(C.blocked_model_keys.items())})
rec("is_blocked",[G._is_model_key_blocked(0,"m1"),G._is_model_key_blocked(0,"m2"),G._is_overload_error(Exception("503")),G._is_overload_error(Exception("ok"))])
C.blocked_model_keys.clear()
# 7 round robin / 8 cascade with fakes
G.API_KEYS[:]=["k0","k1","k2"]
rec("rr",[G._round_robin_key_order() for _ in range(5)])

class Usage: prompt_token_count=11; candidates_token_count=22; thoughts_token_count=3; total_token_count=36
class Cand: 
    def __init__(s,n): s.finish_reason=pytypes.SimpleNamespace(name=n)
class Resp:
    def __init__(s,text,fr="STOP"): s.text=text; s.usage_metadata=Usage(); s.candidates=[Cand(fr)]; s.parsed=None
CALLS=[]
class Models:
    def __init__(s,idx): s.idx=idx
    async def generate_content(s,model,contents,config=None):
        CALLS.append((s.idx,model,len(contents)))
        if s.idx==0 and model=="m-top": raise Exception("503 overloaded")
        q=[{"question":"S_{\\bar{d}} = \\text{x}","options":["a","b","c","d"],"correct_option_id":1,"hint":"h"}]
        if config is not None and getattr(config,"response_schema",None) is not None:
            return Resp('{"questions": '+json.dumps(q).replace("\\\\","\\")+'}')   # raw single backslashes (LaTeX leak)
        return Resp("hello world","MAX_TOKENS")
class Files:
    def upload(s,file): return pytypes.SimpleNamespace(name="files/x")
    def delete(s,name): CALLS.append(("del",name))
class FakeClient:
    def __init__(s,i): s.aio=pytypes.SimpleNamespace(models=Models(i)); s.files=Files()
G._GEMINI_CLIENTS[:]=[FakeClient(i) for i in range(3)]
async def fake_cascade(): return [{"provider":"gemini","model_name":"m-top"},{"provider":"gemini","model_name":"m-2"}]
C._get_models_cascade=fake_cascade
async def main():
    # blocked state after cascade, results, metadata
    C.blocked_model_keys.clear(); C.current_key_pointer=0; CALLS.clear()
    r=await G.generate_text_with_cascade(["x"],prompt_instruction="p")
    rec("text_cascade",[r,list(CALLS),sorted(f"{k[0]}:{k[1]}" for k in C.blocked_model_keys),S._last_model_used_var.get(),S._last_token_usage_var.get()])
    C.blocked_model_keys.clear(); C.current_key_pointer=0; CALLS.clear()
    r=await G.generate_structured_with_cascade(["x"],G.QuizResponse,"p")
    rec("struct_cascade",[r[0].model_dump(),r[1],list(CALLS)])
    # full public API: math text path (gemini only) + normal text (groq None -> gemini) + files regular + super pdf
    msgs=[]
    class Msg:
        async def edit_text(s,t): msgs.append(t)
    for label,kw in [("smart_math",dict(pure_text="t",count=1,is_math_mode=True)),
                     ("smart_text",dict(pure_text="t",count=1)),
                     ("smart_files",dict(file_paths=[os.path.join(d,"a.txt")],count=1,status_message=Msg())),
                     ("smart_super_pdf",dict(file_paths=[pdf],count=7))]:
        C.blocked_model_keys.clear(); C.current_key_pointer=0; CALLS.clear()
        if label=="smart_files": open(os.path.join(d,"a.txt"),"w").write("hello")
        if label=="smart_super_pdf":
            import constants; old_lim=constants.MAX_LIMIT_PAGES
            # force super path through the module that holds the name
            for m in {G, sys.modules.get("helpers.gemini_helper.quiz_generator")}:
                if m is not None and hasattr(m,"MAX_LIMIT_PAGES"): m.MAX_LIMIT_PAGES=3
        res=await G.generate_quiz_smart(**kw)
        meta=G.get_last_generation_metadata()
        if meta: meta=dict(meta); meta.pop("duration_seconds",None)
        rec(label,[res,meta,[c for c in CALLS if c[0]!="del"] if label!="smart_super_pdf" else len(CALLS)])
    rec("loading_msgs",msgs)
asyncio.run(main())
print(json.dumps(out,ensure_ascii=False,sort_keys=True,indent=1,default=str))
