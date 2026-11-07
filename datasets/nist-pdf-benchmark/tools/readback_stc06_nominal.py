"""STC-06 PDF readback in its own Z-up datum; no reference data is read here."""
import argparse,hashlib,json,math,re
from pathlib import Path
import build123d as bd
import pymupdf
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Trsf
import readback_ftc06_nominal as common
N=common.N;NAMES=common.NAMES
def read_pdf(path):
    with pymupdf.open(path) as doc:pages=[p.get_text() for p in doc]
    profiles={}
    for text in pages:
        header=re.search(r'^PROFILE (\w+) \| PLANE (YZ|XZ|RZ) \| (.+)$',text,re.M)
        if not header:continue
        name,plane,location=header.groups()
        if name not in NAMES:raise ValueError('Unknown STC-06 contour')
        p=profiles.setdefault(name,{'plane':plane,'location':location,'edges':{}})
        if (p['plane'],p['location'])!=(plane,location):raise ValueError('Conflicting contour header')
        for line in text.splitlines():
            m=re.fullmatch(r'E(\d+) (LINE|CIRCLE) ('+N+r') ('+N+r') ('+N+r') ('+N+r')',line.strip())
            if m:
                i,kind,*v=m.groups();i=int(i);v=list(map(float,v))
                if i in p['edges']:raise ValueError('Duplicate contour edge')
                p['edges'][i]={'type':kind,'start':v[:2],'end':v[2:]}
            m=re.fullmatch(r'E(\d+) R ('+N+r') C ('+N+r') ('+N+r') M ('+N+r') ('+N+r')',line.strip())
            if m:
                i,*v=m.groups();e=p['edges'].get(int(i));v=list(map(float,v))
                if not e or e['type']!='CIRCLE' or 'mid' in e:raise ValueError('Unmatched arc dimensions')
                e.update(radius=v[0],center=v[1:3],mid=v[3:])
    if set(profiles)!=NAMES:raise ValueError('Incomplete contour set')
    for p in profiles.values():
        es=p['edges']
        if sorted(es)!=list(range(1,len(es)+1)):raise ValueError('Missing contour edge')
        p['edges']=[es[i] for i in range(1,len(es)+1)]
        for a,b in zip(p['edges'],p['edges'][1:]+p['edges'][:1]):
            if math.dist(a['end'],b['start'])>1e-5:raise ValueError('Open contour')
            if a['type']=='CIRCLE' and ('mid' not in a or any(abs(math.dist(a[k],a['center'])-a['radius'])>.0002 for k in ('start','mid','end'))):raise ValueError('Inconsistent arc dimensions')
    text='\n'.join(pages);numbers=lambda pattern:tuple(map(float,common.one(pattern,text)))
    base=numbers(r'BASE DATUMS: X=('+N+r') TO ('+N+r'); Y=('+N+r') TO ('+N+r'); Z=('+N+r') TO ('+N+r')\.')
    rear=numbers(r'REAR WALL: X=('+N+r') TO ('+N+r')\.')
    front=numbers(r'FRONT BODY: X=('+N+r') TO ('+N+r')\.')
    rib=numbers(r'RIB: Y=('+N+r') TO ('+N+r')\.')
    noses=numbers(r'LUG NOSES: Y=('+N+r') ('+N+r') ('+N+r') ('+N+r'); Z=('+N+r'); R('+N+r'); X=('+N+r') TO ('+N+r')\.')
    ba=numbers(r'BALL AXES: X=('+N+r'); Y=('+N+r') AND ('+N+r'); FULL CIRCUMFERENCE ('+N+r') DEG\.')
    co=numbers(r'TAPERED AXES: X=('+N+r'); Y=('+N+r') AND ('+N+r'); FULL CIRCUMFERENCE ('+N+r') DEG\.')
    if abs(ba[1]+ba[2])>1e-8 or abs(co[1]+co[2])>1e-8:raise ValueError('The paired axes are not symmetric')
    ball=(ba[0],ba[2],ba[3]);cone=(co[0],co[2],co[3])
    cup=numbers(r'SPHERICAL RECESS: CENTER ('+N+r') ('+N+r') ('+N+r'); R('+N+r'); OPEN Z=('+N+r'); LOWEST Z=('+N+r')\.')
    marks=numbers(r'FRONT MARKS: ENTRANCE X=('+N+r'); FLOOR X=('+N+r'); DEPTH ('+N+r')\.')
    radius=float(common.one(r'STRAIGHT ROOT RADII: R('+N+r') ON ALL EIGHT INDICATED SPANS\.',text))
    if ball[2]!=360 or cone[2]!=360 or not math.isclose(cup[2],cup[4],abs_tol=1e-6) or not math.isclose(cup[2]-cup[3],cup[5],abs_tol=1e-6) or not math.isclose(marks[0]-marks[2],marks[1],abs_tol=1e-6):raise ValueError('Conflicting recess or axial dimensions')
    holes=[]
    for row in re.findall(r'^H(\d+) AXIS ([XYZ]) POINT ('+N+r') ('+N+r') ('+N+r') D ('+N+r') FROM ('+N+r') TO ('+N+r')$',text,re.M):
        i,axis,*v=row;v=list(map(float,v));holes.append({'id':int(i),'axis':axis,'center':v[:3],'diameter':v[3],'bottom':v[4],'top':v[5]})
    roots=[]
    for row in re.findall(r'^R(\d+) FROM ('+N+r') ('+N+r') ('+N+r') TO ('+N+r') ('+N+r') ('+N+r')$',text,re.M):
        i,*v=row;v=list(map(float,v));roots.append({'id':int(i),'start':v[:3],'end':v[3:]})
    if [h['id'] for h in holes]!=list(range(1,21)) or [r['id'] for r in roots]!=list(range(1,9)):raise ValueError('Incomplete bore or root table')
    return dict(profiles=profiles,base=base,rear=rear,front=front,rib=rib,noses=noses,ball=ball,cone=cone,cup=cup,marks=marks,rootRadius=radius,holes=holes,roots=roots)
def reconstruct(s):
    # Reuse the tested construction in an orthogonal intermediate frame. Every
    # coordinate is transformed from the final PDF; no nominal defaults enter.
    inverse=lambda p:[-p[1],p[2],-p[0]]
    profiles={}
    for name,p in s['profiles'].items():
        cv=(lambda q:[-q[0],q[1]]) if p['plane']=='YZ' else ((lambda q:[q[1],-q[0]]) if p['plane']=='XZ' else (lambda q:q[:]))
        rows=[]
        for e in p['edges']:
            row={k:(cv(v) if k in ('start','end','mid','center') else v) for k,v in e.items()};rows.append(row)
        profiles[name]={'plane':{'YZ':'XY','XZ':'YZ','RZ':'RY'}[p['plane']],'edges':rows}
    x0,x1,y0,y1,z0,z1=s['base'];a,b,c,d,z,r,lo,hi=s['noses']
    holes=[]
    for h in s['holes']:
        if h['axis']=='X':axis='Z';lo_h,hi_h=-h['top'],-h['bottom']
        elif h['axis']=='Z':axis='Y';lo_h,hi_h=h['bottom'],h['top']
        else:raise ValueError('Unexpected bore axis')
        holes.append({**h,'axis':axis,'center':inverse(h['center']),'bottom':lo_h,'top':hi_h})
    cx,cy,cz,cr,opening,lowest=s['cup'];entry,floor,depth=s['marks']
    old={'profiles':profiles,'base':(-y1,-y0,z0,z1,-x1,-x0),
        'rear':(-s['rear'][1],-s['rear'][0]),'front':(-s['front'][1],-s['front'][0]),'rib':(-s['rib'][1],-s['rib'][0]),
        'noses':(-a,-b,-c,-d,z,r,-hi,-lo),'ball':(s['ball'][1],-s['ball'][0],s['ball'][2]),
        'cone':(s['cone'][1],-s['cone'][0],s['cone'][2]),'cup':(-cy,cz,-cx,cr,opening,lowest),
        'marks':(-entry,-floor,depth),'rootRadius':s['rootRadius'],'holes':holes,
        'roots':[{'start':inverse(r['start']),'end':inverse(r['end'])} for r in s['roots']]}
    body=common.reconstruct(old);t=gp_Trsf();t.SetValues(0,0,-1,0,-1,0,0,0,0,1,0,0)
    result=bd.Shape.cast(BRepBuilderAPI_Transform(body.wrapped,t,True).Shape())
    if len(result.solids())!=1 or not result.is_valid:raise ValueError('Invalid reconstructed STC-06 solid')
    return result.solids()[0]
def main():
    p=argparse.ArgumentParser();p.add_argument('--pdf',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True);a=p.parse_args();a.out_dir.mkdir(parents=True,exist_ok=False)
    spec=read_pdf(a.pdf);body=reconstruct(spec);step=a.out_dir/'pdf-readback.step';bd.export_step(body,step)
    from catia_mcp.cad_support import compare_steps
    try:r=compare_steps({'actual':str(step),'reference':str(a.reference),'lengthToleranceMm':.005,'volumeToleranceMm3':1.0})
    except Exception as exc:r={'verdict':'INCONCLUSIVE','error':str(exc)}
    report={'pdf':str(a.pdf.resolve()),'pdfSha256':hashlib.sha256(a.pdf.read_bytes()).hexdigest(),'comparison':r,'profileEdgeCounts':{k:len(v['edges']) for k,v in spec['profiles'].items()}}
    with (a.out_dir/'review.json').open('x') as f:json.dump(report,f,indent=2)
    print(json.dumps(report),flush=True)
    if r['verdict']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
