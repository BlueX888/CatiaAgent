"""Verification-side reconstruction from the final FTC-08 PDF text only."""
import argparse,hashlib,json,math,re
from pathlib import Path
import build123d as bd
import pymupdf
from readback_ctc01_nominal import profile_wire,profile_prism

N=r'[-+]?\d+(?:\.\d+)?'
NAMES={'BASE','FRAME','UNDERSIDE','GROOVE','MARK1','MARK2',*[f'APERTURE{i}' for i in range(1,6)]}

def one(pattern,text):
    rows=re.findall(pattern,text,re.M)
    if len(rows)!=1:raise ValueError(f'Expected one dimension: {pattern}')
    return rows[0]

def read_pdf(path):
    with pymupdf.open(path) as doc:pages=[p.get_text() for p in doc]
    profiles={}
    for text in pages:
        header=re.search(r'^PROFILE (\w+) \| PLANE (XY|YZ) \| (.+)$',text,re.M)
        if not header:continue
        name,plane,location=header.groups()
        if name not in NAMES:raise ValueError('Unknown contour')
        p=profiles.setdefault(name,{'plane':plane,'location':location,'edges':{}})
        if p['plane']!=plane or p['location']!=location:raise ValueError('Conflicting contour header')
        for line in text.splitlines():
            m=re.fullmatch(r'E(\d+) (LINE|CIRCLE) ('+N+r') ('+N+r') ('+N+r') ('+N+r')',line.strip())
            if m:
                i,kind,*values=m.groups();i=int(i);v=list(map(float,values))
                if i in p['edges']:raise ValueError('Duplicate contour edge')
                p['edges'][i]={'id':i,'type':kind,'start':v[:2],'end':v[2:]}
            m=re.fullmatch(r'E(\d+) R ('+N+r') C ('+N+r') ('+N+r') M ('+N+r') ('+N+r')',line.strip())
            if m:
                i,*values=m.groups();i=int(i);v=list(map(float,values));e=p['edges'].get(i)
                if not e or e['type']!='CIRCLE' or 'mid' in e:raise ValueError('Unmatched arc dimensions')
                e.update(radius=v[0],center=v[1:3],mid=v[3:])
    if set(profiles)!=NAMES:raise ValueError('Incomplete contour set')
    for name,p in profiles.items():
        rows=p['edges']
        if sorted(rows)!=list(range(1,len(rows)+1)):raise ValueError('Edge sequence gap')
        p['edges']=[rows[i] for i in range(1,len(rows)+1)]
        for e,f in zip(p['edges'],p['edges'][1:]+p['edges'][:1]):
            if math.dist(e['end'],f['start'])>1e-5:raise ValueError(f'Open contour {name}')
            if e['type']=='CIRCLE' and ('mid' not in e or any(abs(math.dist(e[k],e['center'])-e['radius'])>.0002 for k in ('start','mid','end'))):
                raise ValueError('Inconsistent arc dimensions')
    text='\n'.join(pages)
    levels=tuple(map(float,one(r'HEIGHT DATUMS: BOTTOM Z=('+N+r'); BASE TOP Z=('+N+r'); WEB UNDERSIDE Z=('+N+r'); TOP Z=('+N+r')\.',text)))
    floor,depth=map(float,one(r'BLIND GROOVE: FLOOR Z=('+N+r'); DEPTH ('+N+r')\.',text))
    mark_face,mark_floor,mark_depth=map(float,one(r'SIDE MARKS: ENTRANCE X=('+N+r'); FLOOR X=('+N+r'); DEPTH ('+N+r')\.',text))
    large,root,groove=map(float,one(r'EDGE RADII: LARGE R('+N+r'); FRAME ROOT R('+N+r'); GROOVE ROOT R('+N+r')\.',text))
    if not math.isclose(floor+depth,levels[3],abs_tol=1e-6) or not math.isclose(mark_floor+mark_depth,mark_face,abs_tol=1e-6):raise ValueError('Conflicting recess depth')
    holes=[]
    for row in re.findall(r'^H(\d+) X ('+N+r') Y ('+N+r') D ('+N+r') Z ('+N+r') TO ('+N+r') THRU$',text,re.M):
        i,x,y,d,z0,z1=row;holes.append({'id':int(i),'x':float(x),'y':float(y),'diameter':float(d),'bottom':float(z0),'top':float(z1)})
    if [h['id'] for h in holes]!=list(range(1,21)):raise ValueError('Incomplete circular hole table')
    return {'profiles':profiles,'levels':levels,'holes':holes,'groove':{'floor':floor,'depth':depth},
        'marks':{'face':mark_face,'floor':mark_floor,'depth':mark_depth},'radii':{'large':large,'root':root,'groove':groove}}

def reconstruct(s):
    p=s['profiles'];z0,z1,z2,z3=s['levels']
    def single(obj):
        solids=obj.solids()
        if len(solids)!=1 or not solids[0].is_valid:raise ValueError('Invalid or multiple readback solids')
        return solids[0]
    prism=lambda name,z,h:profile_prism(p[name],z,h)
    # The section contours give physical wall surfaces, without a supplied plan.
    body=single(prism('BASE',z0,z1-z0).fuse(prism('FRAME',z1,z3-z1))).clean()
    body=single(body.cut(prism('UNDERSIDE',z0,z2-z0)))
    for i in range(1,6):body=single(body.cut(prism(f'APERTURE{i}',z2,z3-z2)))
    for h in s['holes']:
        cylinder=bd.Solid.make_cylinder(h['diameter']/2,h['top']-h['bottom'],plane=bd.Plane(origin=(h['x'],h['y'],h['bottom'])))
        body=single(body.cut(cylinder))
    body=single(body.cut(prism('GROOVE',s['groove']['floor'],s['groove']['depth'])))
    for name in ('MARK1','MARK2'):
        tool=bd.extrude(bd.Face(profile_wire(p[name],s['marks']['floor'])),amount=s['marks']['depth'],dir=(1,0,0))
        body=single(body.cut(tool))
    body=body.clean()
    def resolve(name,z,line_only=False,axial_only=False):
        source=profile_wire(p[name],z);selected=[]
        for e in source.edges():
            if line_only and not str(e.geom_type).endswith('LINE'):continue
            if axial_only and min(abs(e.tangent_at(.5).X),abs(e.tangent_at(.5).Y))>1e-6:continue
            matches=[g for g in body.edges() if abs(e.length-g.length)<.0002 and (e.position_at(.5)-g.position_at(.5)).length<.0002]
            if len(matches)!=1:raise ValueError(f'A dimensioned transition edge is not unique: {name}')
            selected.append(matches[0])
        return selected
    edges=resolve('FRAME',z3,True)+resolve('BASE',z1,True,True)+resolve('UNDERSIDE',z2,True)
    if len(edges)!=20:raise ValueError('Large-radius transition census failed')
    body=single(body.fillet(s['radii']['large'],edges))
    edges=resolve('FRAME',z1)
    if len(edges)!=16:raise ValueError('Frame-root census failed')
    body=single(body.fillet(s['radii']['root'],edges))
    edges=resolve('GROOVE',s['groove']['floor'])
    if len(edges)!=8:raise ValueError('Groove-root census failed')
    return single(body.fillet(s['radii']['groove'],edges))

def main():
    p=argparse.ArgumentParser();p.add_argument('--pdf',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True);a=p.parse_args()
    a.out_dir.mkdir(parents=True,exist_ok=False);spec=read_pdf(a.pdf);body=reconstruct(spec)
    step=a.out_dir/'pdf-readback.step';bd.export_step(body,step)
    from catia_mcp.cad_support import compare_steps
    try:r=compare_steps({'actual':str(step),'reference':str(a.reference),'lengthToleranceMm':.005,'volumeToleranceMm3':100.7})
    except Exception as exc:r={'verdict':'INCONCLUSIVE','error':str(exc)}
    report={'pdf':str(a.pdf.resolve()),'pdfSha256':hashlib.sha256(a.pdf.read_bytes()).hexdigest(),'comparison':r,
        'profileEdgeCounts':{k:len(v['edges']) for k,v in spec['profiles'].items()},'holes':len(spec['holes'])}
    with (a.out_dir/'review.json').open('x') as f:json.dump(report,f,indent=2)
    print(json.dumps(report),flush=True)
    if r['verdict']!='PASS':raise SystemExit(1)

if __name__=='__main__':main()
