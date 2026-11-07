"""Independently reconstruct the visible CTC-01 G1 dimension tables only."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import build123d as bd
import pymupdf

N=r'-?\d+(?:\.\d+)?'
NAMES={'MAIN','FOOT1','FOOT2','FOOT3','FOOT4','SLOT1','SLOT2','MARK1','MARK2','HEX'}


def match_one(pattern,text):
    rows=re.findall(pattern,text,re.M)
    if len(rows)!=1:raise ValueError(f'Expected one dimension match: {pattern}, got {len(rows)}')
    return rows[0]


def read_pdf(pdf):
    with pymupdf.open(pdf) as doc: pages=[p.get_text() for p in doc]
    profiles={}; active=None
    for page in pages:
        for line in page.splitlines():
            header=re.fullmatch(r'PROFILE (\w+) \| PLANE (XY|YZ) \| (.+)',line.strip())
            if header:
                name,plane,location=header.groups(); active=name
                item=profiles.setdefault(name,{'plane':plane,'location':location,'edges':{}})
                if (item['plane'],item['location'])!=(plane,location):raise ValueError('Contradictory profile headers')
            row=re.fullmatch(r'E(\d+) (LINE|CIRCLE) ('+N+r') ('+N+r') ('+N+r') ('+N+r')',line.strip())
            if row:
                if active is None:raise ValueError('Unlocated geometric edge')
                i,kind,*values=row.groups();values=list(map(float,values));i=int(i)
                if i in profiles[active]['edges']:raise ValueError('Duplicate geometric edge')
                profiles[active]['edges'][i]={'id':i,'type':kind,'start':values[:2],'end':values[2:]}
            arc=re.fullmatch(r'E(\d+) R ('+N+r') C ('+N+r') ('+N+r') M ('+N+r') ('+N+r')',line.strip())
            if arc:
                i,*values=arc.groups();i=int(i);values=list(map(float,values))
                row=profiles[active]['edges'].get(i)
                if row is None or row['type']!='CIRCLE' or 'radius' in row:raise ValueError('Arc dimensions missing or duplicated')
                row.update(radius=values[0],center=values[1:3],mid=values[3:5])
    if set(profiles)!=NAMES:raise ValueError('Incomplete profile set')
    for name,p in profiles.items():
        rows=p['edges'];expected=list(range(1,len(rows)+1))
        if sorted(rows)!=expected:raise ValueError('Gap in edge dimension sequence')
        p['edges']=[rows[i] for i in expected]
        for e,next_e in zip(p['edges'],p['edges'][1:]+p['edges'][:1]):
            if math.dist(e['end'],next_e['start'])>.0002:raise ValueError(f'Open dimensioned contour: {name}')
            if e['type']=='CIRCLE':
                if 'mid' not in e or any(abs(math.dist(e[k],e['center'])-e['radius'])>.0002 for k in ('start','mid','end')):
                    raise ValueError(f'Arc dimensions disagree: {name}')
    text='\n'.join(pages)
    holes=[]
    for row in re.findall(r'^H(\d+) X ('+N+r') Y ('+N+r') D ('+N+r') Z ('+N+r') TO ('+N+r') THRU$',text,re.M):
        i,x,y,d,bottom,top=row
        holes.append({'id':int(i),'x':float(x),'y':float(y),'diameter':float(d),'bottom':float(bottom),'top':float(top)})
    if [h['id'] for h in holes]!=list(range(1,9)):raise ValueError('Incomplete vertical hole table')
    bores=[]
    for row in re.findall(r'^B(\d+) X ('+N+r') Z ('+N+r') D ('+N+r') ENTRY Y ('+N+r') CYL END Y ('+N+r') TIP Y ('+N+r')$',text,re.M):
        i,x,z,d,entry,end,tip=row
        bores.append(dict(zip(('id','x','z','diameter','entry','end','tip'),(int(i),*map(float,(x,z,d,entry,end,tip))))))
    if [b['id'] for b in bores]!=[1,2]:raise ValueError('Incomplete blind-bore dimensions')
    angle=float(match_one(r'INCLUDED TIP ANGLE ('+N+r') DEG',text))
    for b in bores:
        if not math.isclose(b['tip']-b['end'],b['diameter']/2/math.tan(math.radians(angle/2)),abs_tol=.00002):
            raise ValueError('Drill-tip dimensions conflict')
    root_radius=float(match_one(r'UNDERSIDE TRANSITION: R('+N+r') on all eight',text))
    root=tuple(map(float,match_one(r'ROOT LOCATIONS BEFORE ROUNDING: Z=('+N+r'); Y=\+/-('+N+r') at X=('+N+r') TO ('+N+r')',text)))
    wall=tuple(map(float,match_one(r'and X=\+/-('+N+r') at Y=('+N+r') TO ('+N+r')',text)))
    main_band=tuple(map(float,re.search(r'Z=('+N+r') TO ('+N+r')',profiles['MAIN']['location']).groups()))
    foot_bands={name:tuple(map(float,re.search(r'Z=('+N+r') TO ('+N+r')',p['location']).groups())) for name,p in profiles.items() if name.startswith('FOOT')}
    if any(not math.isclose(b[1]+root_radius,main_band[0],abs_tol=.0001) for b in foot_bands.values()):
        raise ValueError('Foot top transition conflicts with underside root radius')
    root_hex,hex_radius=map(float,match_one(r'ROOT Z=('+N+r'); R('+N+r') TO STRAIGHT SIDE AT Z=',text))
    top_hex,top_flats=map(float,match_one(r'TOP Z=('+N+r'); TOP ACROSS FLATS ('+N+r')',text))
    bevel,bevel_angle=map(float,match_one(r'TOP BEVEL ('+N+r') X ('+N+r') DEG',text))
    main_flats=float(match_one(r'STRAIGHT-WALL ACROSS FLATS ('+N+r')',text))
    if bevel_angle!=45 or not math.isclose(main_flats-2*bevel,top_flats,abs_tol=.0001):raise ValueError('Hexagon bevel dimensions conflict')
    floor,depth=map(float,match_one(r'flat floor X=('+N+r'), depth ('+N+r')\.',text))
    if not math.isclose(floor+depth,float(match_one(r'SIDE MARKS: both dimensioned YZ contours open on the \+X face X=('+N+r');',text)),abs_tol=.0001):
        raise ValueError('Side-mark depth conflicts')
    return {'profiles':profiles,'holes':holes,'bores':bores,'mainBand':main_band,'footBands':foot_bands,
        'root':{'radius':root_radius,'locations':root,'wall':wall},
        'hex':{'bottom':root_hex,'top':top_hex,'rootRadius':hex_radius,'bevel':bevel,'flats':main_flats},
        'marks':{'floor':floor,'depth':depth}}


def profile_wire(profile,offset):
    point=(lambda p:(p[0],p[1],offset)) if profile['plane']=='XY' else (lambda p:(offset,p[0],p[1]))
    edges=[bd.Edge.make_line(point(e['start']),point(e['end'])) if e['type']=='LINE' else
           bd.Edge.make_three_point_arc(point(e['start']),point(e['mid']),point(e['end'])) for e in profile['edges']]
    return bd.Wire(edges)


def profile_prism(profile,z,depth):
    # Inner contours have the opposite winding; the dimensioned range owns Z.
    result=bd.extrude(bd.Face(profile_wire(profile,z)),amount=depth,dir=(0,0,1))
    solids=result.solids()
    if len(solids)!=1 or not solids[0].is_valid:raise ValueError('A dimensioned prism did not produce one valid solid')
    box=solids[0].bounding_box()
    if abs(box.min.Z-z)>.0002 or abs(box.max.Z-z-depth)>.0002:raise ValueError('Prism does not occupy its dimensioned Z range')
    return solids[0]


def reconstruct(spec):
    p=spec['profiles']; low,high=spec['mainBand']
    def single(shape):
        solids=shape.solids()
        if len(solids)!=1 or not solids[0].is_valid:raise ValueError('A readback construction stage did not produce one valid solid')
        return solids[0]
    prism=lambda name,z,depth:profile_prism(p[name],z,depth)
    body=prism('MAIN',low,high-low)
    for name,band in spec['footBands'].items():body=single(body.fuse(prism(name,band[0],low-band[0])))
    body=body.clean();r=spec['root'];z,inner_y,xlo,xhi=r['locations'];inner_x,ylo,yhi=r['wall']
    edges=[]
    for e in body.edges():
        b=e.bounding_box();m=e.position_at(.5)
        if (str(e.geom_type).endswith('LINE') and abs(b.min.Z-z)<.0002 and abs(b.max.Z-z)<.0002 and
            ((abs(abs(m.Y)-inner_y)<.0002 and xlo<abs(m.X)<xhi) or
             (abs(abs(m.X)-inner_x)<.0002 and ylo<abs(m.Y)<yhi))):edges.append(e)
    if len(edges)!=8:raise ValueError('Eight dimensioned underside roots not found')
    body=single(body.fillet(r['radius'],edges))
    h=spec['hex'];body=single(body.fuse(prism('HEX',h['bottom'],h['top']-h['bottom']))).clean()
    box=profile_wire(p['HEX'],0).bounding_box()
    edges=[e for e in body.edges() if abs(e.bounding_box().min.Z-h['bottom'])<.0002 and abs(e.bounding_box().max.Z-h['bottom'])<.0002
           and box.min.X-.001<e.center().X<box.max.X+.001 and box.min.Y-.001<e.center().Y<box.max.Y+.001]
    if len(edges)!=6:raise ValueError('Six hexagon roots not found')
    body=single(body.fillet(h['rootRadius'],edges))
    edges=[e for e in body.edges() if abs(e.bounding_box().min.Z-h['top'])<.0002 and abs(e.bounding_box().max.Z-h['top'])<.0002]
    if len(edges)!=6:raise ValueError('Six top bevel edges not found')
    body=single(body.chamfer(h['bevel'],None,edges))
    cuts=[prism(name,low,high-low) for name in ('SLOT1','SLOT2')]
    for h in spec['holes']:
        cuts.append(bd.Solid.make_cylinder(h['diameter']/2,h['top']-h['bottom'],plane=bd.Plane(origin=(h['x'],h['y'],h['bottom']))))
    for b in spec['bores']:
        plane=lambda y:bd.Plane(origin=(b['x'],y,b['z']),x_dir=(1,0,0),z_dir=(0,1,0))
        cuts.append(bd.Solid.make_cylinder(b['diameter']/2,b['end']-b['entry'],plane=plane(b['entry'])))
        cuts.append(bd.Solid.make_cone(b['diameter']/2,0,b['tip']-b['end'],plane=plane(b['end'])))
    for name in ('MARK1','MARK2'):
        cuts.append(bd.extrude(bd.Face(profile_wire(p[name],spec['marks']['floor'])),amount=spec['marks']['depth'],dir=(1,0,0)))
    body=body.cut(*cuts)
    if len(body.solids())!=1 or not body.solids()[0].is_valid:raise ValueError('PDF did not reconstruct one valid solid')
    return body.solids()[0]


def main():
    p=argparse.ArgumentParser();p.add_argument('--pdf',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True)
    a=p.parse_args();a.out_dir.mkdir(parents=True,exist_ok=False)
    spec=read_pdf(a.pdf);body=reconstruct(spec);step=a.out_dir/'pdf-readback.step';bd.export_step(body,step)
    from catia_mcp.cad_support import compare_steps
    try:
        comparison=compare_steps({'actual':str(step),'reference':str(a.reference),'lengthToleranceMm':.005,'volumeToleranceMm3':7321.4})
    except Exception as exc:
        comparison={'verdict':'INCONCLUSIVE','checks':[],'error':f'{type(exc).__name__}: {exc}'}
    report={'pdf':str(a.pdf.resolve()),'pdfSha256':hashlib.sha256(a.pdf.read_bytes()).hexdigest(),'readback':spec,'comparison':comparison,
        'scope':'Independent visible PDF numeric readback; not Agent generation, CATIA editability, manufacturing or PMI verification.'}
    (a.out_dir/'review.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'verdict':comparison['verdict'],'checks':comparison['checks'],'out':str(a.out_dir)}),flush=True)
    return 0 if comparison['verdict']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
