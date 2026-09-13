import sys, json, hashlib, subprocess, tempfile
sys.dont_write_bytecode = True
from pathlib import Path
from xml.etree import ElementTree as ET
BASE=Path(__file__).resolve().parents[3]
AUDIT=Path(tempfile.mkdtemp(prefix="svf-review-20260913-"))
WORK=Path(tempfile.mkdtemp(prefix="fixtures-",dir=AUDIT))
sys.path[:0]=[str(BASE/'src'),str(BASE/'tests')]
from test_core import SAMPLE_XML, make_profile
from svf_reproducer.xmlcore import extract_ir, compile_xml, parse_xml, validate_xml
from svf_reproducer.annotations import AnnotationSet
from svf_reproducer.models import Job, MappingConfig, MappingField, RuntimeResult
from svf_reproducer.jobs import runtime_input_hash, validate_job
from svf_reproducer.config import write_json, stable_json_hash
from svf_reproducer.service import generate_form, execute_job
from svf_reproducer.storage import JobLedger, load_current
from svf_reproducer.validation import structural_report, create_evidence_manifest, record_check, accepted
from svf_reproducer.preview import render_svg
from svf_reproducer.proposals import apply_proposal

results=[]
def case(id,fn):
    try: value=fn()
    except Exception as e: value={'exception':type(e).__name__,'message':str(e)}
    results.append({'case':id,'observed':value})
def fixture(name='base',data=SAMPLE_XML):
    path=WORK/(name+'.xml');path.write_bytes(data);return path,make_profile(path)
def errors(items):return [i.code for i in items if i.severity=='error']
baseline={str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(BASE.rglob('*')) if p.is_file() and (p.suffix in {'.py','.docx'} or p.name in {'few-shot.txt','IMPLEMENTATION_STATUS.md','README.md'})}
(AUDIT/'baseline.json').write_text(json.dumps(baseline,ensure_ascii=False,indent=2))

def wrongtarget():
    p,prof=fixture('id',b'<FormData><Line y1="10"/><Line y1="20"/><Line y1="30"/></FormData>')
    prof.compatibility['Line']='edit_supported';prof.editable_attributes['Line']=['y1'];prof.element_templates['line']='<Line y1="40"/>'
    ir=extract_ir(p,prof);root=ir.elements[0];lines=[e for e in ir.elements if e.kind=='Line']
    out=compile_xml(p,[{'op':'remove','target_id':lines[0].id},{'op':'add','target_id':root.id,'value':{'template':'line'}},{'op':'set','target_id':lines[1].id,'property':'y1','value':555}],prof)
    return {'initial':[10,20,30],'requested':'original 20 -> 555','actual':[e.get('y1') for e in parse_xml(out).findall('Line')]}
case('R01',wrongtarget)
def nofcntl():
    code="import sys; sys.path.insert(0,"+repr(str(BASE/'src'))+"); sys.modules['fcntl']=None; import svf_reproducer.cli"
    r=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
    return {'simulation':'fcntl unavailable, not native Windows','returncode':r.returncode,'last_error':r.stderr.splitlines()[-1]}
case('R02',nofcntl)
case('R03',lambda:parse_xml('<?xml version="1.0" encoding="Shift_JIS"?><FormData><Text strText="日本語"/></FormData>'.encode('shift_jis')).tag)
def lexical():
    p=BASE/'few-shot.txt';prof=make_profile(p);ir=extract_ir(p,prof);r=next(e for e in ir.elements if e.kind=='Record')
    before=p.read_bytes();after=compile_xml(p,[{'op':'set','target_id':r.id,'property':'displayLineCount','value':12}],prof)
    return {'before_crlf':before.count(b'\r\n'),'after_crlf':after.count(b'\r\n'),'before_declaration':before.splitlines()[0].decode(),'after_declaration':after.splitlines()[0].decode(),'before_final_lf':before.endswith(b'\n'),'after_final_lf':after.endswith(b'\n')}
case('R04',lexical)
def dpi():
    p,prof=fixture('dpi');a=extract_ir(p,prof);prof.coordinate_dpi=600;b=extract_ir(p,prof)
    return {'400_x_mm':next(e for e in a.elements if e.name=='CODE').geometry_mm['x'],'600_x_mm':next(e for e in b.elements if e.name=='CODE').geometry_mm['x'],'error_codes':errors(validate_xml(p,prof))}
case('R05',dpi)
def stale():
    p,prof=fixture('stale');rp=WORK/'stale-report.json';write_json(rp,structural_report(p,prof));a=WORK/'designer.txt';a.write_text('test observation only')
    ep=WORK/'stale-evidence.json';create_evidence_manifest(rp,'designer',[a],ep)
    old=hashlib.sha256(p.read_bytes()).hexdigest();p.write_bytes(p.read_bytes().replace(b'displayLineCount="4"',b'displayLineCount="99"'))
    report=record_check(rp,'designer','pass',[ep]);return {'xml_changed':old!=hashlib.sha256(p.read_bytes()).hexdigest(),'accepted_designer':accepted(report,{'designer'})}
case('R06',stale)
def empty_evidence():
    p,prof=fixture('empty-evidence');rp=WORK/'empty-report.json';report=structural_report(p,prof);write_json(rp,report)
    ep=WORK/'empty-manifest.json';write_json(ep,{'schema':'svf-evidence/1.0','kind':'runtime','input_hashes':report['checks'][0]['input_hashes'],'artifacts':[]})
    return {'accepted_runtime_without_artifacts':accepted(record_check(rp,'runtime','pass',[ep]),{'runtime'})}
case('R07',empty_evidence)
def jobfixture(name):
    p,prof=fixture(name);csv=WORK/(name+'.csv');csv.write_bytes(b'code\r\nA\r\n')
    m=MappingConfig('m',[MappingField('code','detail','CODE',required=True)])
    j=Job(name,'not-verified-revision',stable_json_hash(m.to_dict()),{},str(csv),1,prof.profile_id)
    return p,prof,csv,m,j
def csvhash():
    p,prof,csv,m,j=jobfixture('csvhash');h1=runtime_input_hash(p,j,m,prof);csv.write_bytes(b'code\r\nB\r\n');h2=runtime_input_hash(p,j,m,prof)
    ledger=JobLedger(WORK/'csv-ledger');ledger.claim(j.job_id,h1);ledger.record(j.job_id,h1,RuntimeResult('SUCCEEDED','old-A-result',1))
    prior=ledger.claim(j.job_id,h2);return {'hash_unchanged':h1==h2,'cached_result':prior.to_dict()}
case('R08',csvhash)
def timeout():
    p,prof,csv,m,j=jobfixture('timeout');prof.runtime_kind='command';prof.runtime_timeout_seconds=.2
    prof.runtime_command=[sys.executable,'-c','import time; print("started",flush=True); time.sleep(1)']
    pp=WORK/'timeout-profile.json';mp=WORK/'timeout-mapping.json';jp=WORK/'timeout-job.json'
    for path,value in [(pp,prof.to_dict()),(mp,m.to_dict()),(jp,j.to_dict())]:write_json(path,value)
    try: result=execute_job(p,jp,mp,pp,WORK/'timeout-workspace',WORK/'timeout-output');err=result.to_dict()
    except Exception as e:err={'exception':type(e).__name__,'message':str(e)}
    return {'result':err,'ledger':json.loads((WORK/'timeout-workspace/runtime-ledger.json').read_text())}
case('R09',timeout)
def overwrite():
    p,prof=fixture('overwrite');prof.compatibility['SubForm']='edit_supported';prof.editable_attributes['SubForm']=['x2'];pp=WORK/'overwrite-profile.json';write_json(pp,prof.to_dict())
    line=next(e for e in extract_ir(p,prof).elements if e.kind=='SubForm');out=WORK/'existing.xml';out.write_bytes(b'previous-good-output')
    report=generate_form(p,pp,[{'op':'set','target_id':line.id,'property':'x2','value':-999}],out,workspace=WORK/'overwrite-workspace',revision='bad-generation')
    return {'structural_status':report['checks'][0]['status'],'old_output_replaced':out.read_bytes()!=b'previous-good-output','current_revision':load_current(WORK/'overwrite-workspace').name}
case('R10',overwrite)
def sourceoverwrite():
    p,prof=fixture('samepath');pp=WORK/'samepath-profile.json';write_json(pp,prof.to_dict());r=next(e for e in extract_ir(p,prof).elements if e.kind=='Record');old=p.read_bytes()
    report=generate_form(p,pp,[{'op':'set','target_id':r.id,'property':'displayLineCount','value':12}],p,workspace=WORK/'samepath-workspace',revision='samepath')
    return {'original_overwritten':p.read_bytes()!=old,'saved_source_equals_generated':(Path(report['revision_path'])/'source.xml').read_bytes()==p.read_bytes()}
case('R11',sourceoverwrite)
def permissive():
    out={}
    for name,data in [('wrong-root',b'<NotForm/>'),('nan',SAMPLE_XML.replace(b'x="120"',b'x="NaN"')),('link-cycle',SAMPLE_XML.replace(b'name="OVERFLOW" linkName=""',b'name="OVERFLOW" linkName="DETAIL"'))]:
        p,prof=fixture(name,data);out[name]=errors(validate_xml(p,prof))
    return out
case('R12',permissive)
def badtypes():
    p,prof=fixture('types');r=next(e for e in extract_ir(p,prof).elements if e.kind=='Record');out={}
    for v in [10.7,True]:
        xml=compile_xml(p,[{'op':'set','target_id':r.id,'property':'displayLineCount','value':v}],prof);out[str(v)]=parse_xml(xml).find('.//Record').get('displayLineCount')
    prof.coordinate_dpi='400'
    try:prof.readiness_issues()
    except Exception as e:out['profile']='%s: %s'%(type(e).__name__,e)
    return out
case('R13',badtypes)
def dtd():
    data='<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE FormData [<!ENTITY tiny "EXPANDED">]><FormData><Text>&tiny;</Text></FormData>'.encode('utf-16')
    return {'internal_entity_result':parse_xml(data).find('Text').text}
case('R14',dtd)
def annotation():
    a=AnnotationSet();a.add(['record'],'表示行数を10行から12行に変更','hash');return a.display_line_changes()[0]
case('R15',annotation)
def missingcell():
    p,prof,csv,m,j=jobfixture('missingcell');m.fields.append(MappingField('required','detail','REQUIRED',required=True));j.mapping_hash=stable_json_hash(m.to_dict());csv.write_bytes(b'code,required\r\nA\r\n')
    rows,issues=validate_job(j,m,prof);return {'rows':rows,'errors':errors(issues)}
case('R16',missingcell)
def preview():
    p,prof=fixture('preview',SAMPLE_XML.replace(b'<Field name="CODE"',b'<Text x="120" y="420" strText="row-label"/><Field name="CODE"'));ir=extract_ir(p,prof)
    zero,_=render_svg(ir,prof,mode='data',data_count=0);design,_=render_svg(ir,prof)
    return {'zero_data_record_instances':zero.count('data-instance='),'D4_record_instances':design.count('data-instance='),'D4_child_text_instances':design.count('row-label'),'field_rendered':'data-object-id="'+next(e.id for e in ir.elements if e.name=='CODE')+'"' in design}
case('R17',preview)
def scope():
    p,prof=fixture('scope',SAMPLE_XML.replace(b'<Field name="HEADER"',b'<Record name="R2" displayLineCount="2"/><Field name="HEADER"'));ir=extract_ir(p,prof);a=next(e for e in ir.elements if e.name=='R1');b=next(e for e in ir.elements if e.name=='R2');notes=AnnotationSet();n=notes.add([a.id],'表示行数を8行',ir.source_hash)
    proposal={'source_hash':ir.source_hash,'base_revision':1,'annotation_revisions':notes.active_revisions(),'changes':[{'target_id':b.id,'op':'set','property':'displayLineCount','value':8,'note_ids':[n.id],'example_ids':[]}],'xml_fragments':[],'unresolved':[]}
    out=apply_proposal(p,proposal,source_hash=ir.source_hash,revision=1,annotations=notes,profile=prof)
    return {'note_target':'R1','modified_R2':parse_xml(out).find('./Record').get('displayLineCount')}
case('R18',scope)
def artifact():
    p,prof,csv,m,j=jobfixture('invalidpdf');prof.runtime_kind='command';prof.runtime_command=[sys.executable,'-c','from pathlib import Path; import sys; Path(sys.argv[1],"empty.pdf").write_bytes(b"")','{output_dir}']
    from svf_reproducer.jobs import run_runtime
    result=run_runtime(p,j,m,prof,WORK/'invalidpdf-output');return {'status':result.status,'artifact_sizes':[Path(x).stat().st_size for x in result.artifact_paths]}
case('R19',artifact)
def independentjobs():
    l=JobLedger(WORK/'parallel-ledger');return {'first_claim':l.claim('a','hash-a'),'second_claim':l.claim('b','hash-b'),'states':l._read()['jobs']}
case('R20',independentjobs)

(AUDIT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
for x in results: print(json.dumps(x,ensure_ascii=False))

print("Evidence directory:", AUDIT)
