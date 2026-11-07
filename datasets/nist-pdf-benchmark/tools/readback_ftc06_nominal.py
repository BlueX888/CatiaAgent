"""Read a conventional FTC-06 PDF without measurement files or a model plan."""
import argparse,hashlib,json,math,re
from pathlib import Path
import build123d as bd
import pymupdf
N=r'[-+]?\d+(?:\.\d+)?'
NAMES={'REAR','FRONT','RIB','BALL','CONE','MARK1','MARK2'}
def one(pattern,text):
    rows=re.findall(pattern,text,re.M)
    if len(rows)!=1:raise ValueError(f'Expected one dimension: {pattern}')
    return rows[0]
def read_pdf(path):
    with pymupdf.open(path) as doc:pages=[p.get_text() for p in doc]
    profiles={}
    for text in pages:
        header=re.search(r'^PROFILE (\w+) \| PLANE (XY|YZ|RY) \| (.+)$',text,re.M)
        if not header:continue
        name,plane,location=header.groups()
        if name not in NAMES:raise ValueError('Unknown contour')
        p=profiles.setdefault(name,{'plane':plane,'location':location,'edges':{}})
        if p['plane']!=plane or p['location']!=location:raise ValueError('Conflicting contour header')
        for line in text.splitlines():
            m=re.fullmatch(r'E(\d+) (LINE|CIRCLE) ('+N+r') ('+N+r') ('+N+r') ('+N+r')',line.strip())
            if m:
                i,kind,*v=m.groups();i=int(i);v=list(map(float,v))
                if i in p['edges']:raise ValueError('Duplicate edge dimensions')
                p['edges'][i]={'type':kind,'start':v[:2],'end':v[2:]}
            m=re.fullmatch(r'E(\d+) R ('+N+r') C ('+N+r') ('+N+r') M ('+N+r') ('+N+r')',line.strip())
            if m:
                i,*v=m.groups();i=int(i);v=list(map(float,v));e=p['edges'].get(i)
                if not e or e['type']!='CIRCLE' or 'mid' in e:raise ValueError('Unmatched arc dimensions')
                e.update(radius=v[0],center=v[1:3],mid=v[3:])
    if set(profiles)!=NAMES:raise ValueError('Incomplete contour set')
    for name,p in profiles.items():
        rows=p['edges']
        if sorted(rows)!=list(range(1,len(rows)+1)):raise ValueError('Missing edge sequence')
        p['edges']=[rows[i] for i in range(1,len(rows)+1)]
        for a,b in zip(p['edges'],p['edges'][1:]+p['edges'][:1]):
            if math.dist(a['end'],b['start'])>1e-5:raise ValueError('Open contour')
            if a['type']=='CIRCLE' and ('mid' not in a or any(abs(math.dist(a[k],a['center'])-a['radius'])>.0002 for k in ('start','mid','end'))):raise ValueError('Inconsistent circular edge')
    text='\n'.join(pages)
    numbers=lambda pattern:tuple(map(float,one(pattern,text)))
    base=numbers(r'BASE DATUMS: X=('+N+r') TO ('+N+r'); Y=('+N+r') TO ('+N+r'); Z=('+N+r') TO ('+N+r')\.')
    rear=numbers(r'REAR WALL: Z=('+N+r') TO ('+N+r')\.')
    front=numbers(r'FRONT BODY: Z=('+N+r') TO ('+N+r')\.')
    rib=numbers(r'RIB: X=('+N+r') TO ('+N+r')\.')
    noses=numbers(r'LUG NOSES: X=('+N+r') ('+N+r') ('+N+r') ('+N+r'); Y=('+N+r'); R('+N+r'); Z=('+N+r') TO ('+N+r')\.')
    ball=numbers(r'BALL AXES: X=\+/-('+N+r'); Z=('+N+r'); FULL CIRCUMFERENCE ('+N+r') DEG\.')
    cone=numbers(r'TAPERED AXES: X=\+/-('+N+r'); Z=('+N+r'); FULL CIRCUMFERENCE ('+N+r') DEG\.')
    cup=numbers(r'SPHERICAL RECESS: CENTER ('+N+r') ('+N+r') ('+N+r'); R('+N+r'); OPEN Y=('+N+r'); LOWEST Y=('+N+r')\.')
    marks=numbers(r'FRONT MARKS: ENTRANCE Z=('+N+r'); FLOOR Z=('+N+r'); DEPTH ('+N+r')\.')
    radius=float(one(r'STRAIGHT ROOT RADII: R('+N+r') ON ALL EIGHT INDICATED SPANS\.',text))
    if ball[2]!=360 or cone[2]!=360 or not math.isclose(cup[1],cup[4],abs_tol=1e-6) or not math.isclose(cup[1]-cup[3],cup[5],abs_tol=1e-6) or not math.isclose(marks[0]+marks[2],marks[1],abs_tol=1e-6):raise ValueError('Conflicting axial or recess dimensions')
    holes=[]
    for row in re.findall(r'^H(\d+) AXIS ([XYZ]) POINT ('+N+r') ('+N+r') ('+N+r') D ('+N+r') FROM ('+N+r') TO ('+N+r')$',text,re.M):
        i,axis,*v=row;v=list(map(float,v));holes.append({'id':int(i),'axis':axis,'center':v[:3],'diameter':v[3],'bottom':v[4],'top':v[5]})
    roots=[]
    for row in re.findall(r'^R(\d+) FROM ('+N+r') ('+N+r') ('+N+r') TO ('+N+r') ('+N+r') ('+N+r')$',text,re.M):
        i,*v=row;v=list(map(float,v));roots.append({'id':int(i),'start':v[:3],'end':v[3:]})
    if [h['id'] for h in holes]!=list(range(1,21)) or [r['id'] for r in roots]!=list(range(1,9)):raise ValueError('Incomplete bore or root table')
    return dict(profiles=profiles,base=base,rear=rear,front=front,rib=rib,noses=noses,ball=ball,cone=cone,cup=cup,marks=marks,rootRadius=radius,holes=holes,roots=roots)
def reconstruct(s):
    def single(obj):
        solids=obj.solids()
        if len(solids)!=1 or not solids[0].is_valid:raise ValueError('Readback construction is not one valid solid')
        return solids[0]
    def wire(name,offset=0,axis_x=0):
        p=s['profiles'][name]
        def point(q):return (q[0]+axis_x,q[1],offset) if p['plane'] in ('XY','RY') else (offset,q[0],q[1])
        return bd.Wire([bd.Edge.make_line(point(e['start']),point(e['end'])) if e['type']=='LINE' else bd.Edge.make_three_point_arc(point(e['start']),point(e['mid']),point(e['end'])) for e in p['edges']])
    x0,x1,y0,y1,z0,z1=s['base'];body=bd.Solid.make_box(x1-x0,y1-y0,z1-z0,plane=bd.Plane(origin=(x0,y0,z0)))
    for name,(low,high) in [('REAR',s['rear']),('FRONT',s['front'])]:body=single(body.fuse(bd.extrude(bd.Face(wire(name,low)),amount=high-low,dir=(0,0,1))))
    low,high=s['rib'];body=single(body.fuse(bd.extrude(bd.Face(wire('RIB',low)),amount=high-low,dir=(1,0,0))))
    a,b,c,d,y,r,low,high=s['noses']
    for x in (a,b,c,d):body=single(body.fuse(bd.Solid.make_cylinder(r,high-low,plane=bd.Plane(origin=(x,y,low)))))
    body=body.clean();edges=[]
    for root in s['roots']:
        mid=bd.Vector(*[(a+b)/2 for a,b in zip(root['start'],root['end'])]);length=math.dist(root['start'],root['end'])
        matches=[e for e in body.edges() if str(e.geom_type).endswith('LINE') and abs(e.length-length)<.0002 and (e.position_at(.5)-mid).length<.0002]
        if len(matches)!=1:raise ValueError('Root span is not unique')
        edges.append(matches[0])
    body=single(body.fillet(s['rootRadius'],edges))
    for name,key in [('BALL','ball'),('CONE','cone')]:
        x,z,angle=s[key]
        for x_axis in (-x,x):body=single(body.fuse(bd.revolve(bd.Face(wire(name,z,x_axis)),axis=bd.Axis((x_axis,0,z),(0,1,0)),revolution_arc=angle)))
    x,y,z,r,_,_=s['cup'];body=single(body.cut(bd.Solid.make_sphere(r,plane=bd.Plane(origin=(x,y,z)))))
    for h in s['holes']:
        axis='XYZ'.index(h['axis']);origin=h['center'][:];origin[axis]=h['bottom'];normal=[0,0,0];normal[axis]=1
        tool=bd.Solid.make_cylinder(h['diameter']/2,h['top']-h['bottom'],plane=bd.Plane(origin=origin,x_dir=(0,1,0) if axis==0 else (1,0,0),z_dir=normal))
        body=single(body.cut(tool))
    entrance,floor,depth=s['marks']
    for name in ('MARK1','MARK2'):body=single(body.cut(bd.extrude(bd.Face(wire(name,entrance)),amount=depth,dir=(0,0,1))))
    return body
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
