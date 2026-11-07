"""Reconstruct the folded bracket from the final PDF alone, with no nominal defaults."""
import argparse,hashlib,json,math,re
from pathlib import Path
import build123d as bd
import pymupdf
N=r'[-+]?\d+(?:\.\d+)?';ID=r'[A-Z][A-Z0-9_]*'

def read_pdf(path):
    with pymupdf.open(path) as doc:pages=[p.get_text() for p in doc]
    text='\n'.join(pages)
    if set(re.findall(r'\bUNITS (\w+)\b',text))!={'MM'}:raise ValueError('Missing or conflicting units')
    def one(pattern):
        result=re.findall(pattern,text,re.M)
        if len(result)!=1:raise ValueError('Missing or conflicting dimension: '+pattern)
        return result[0]
    counts=dict(zip(('frames','profiles','walls','bends','holes','openings','recesses'),map(int,one(
        r'CONTENTS: FRAMES (\d+) PROFILES (\d+) WALLS (\d+) BENDS (\d+) HOLES (\d+) OPENINGS (\d+) RECESSES (\d+)\.'))))
    overall=list(map(float,one(r'OVERALL X ('+N+r')  Y ('+N+r')  Z ('+N+r')\.')))
    limits=list(map(float,one(r'GLOBAL LIMITS: X=('+N+r') TO ('+N+r'); Y=('+N+r') TO ('+N+r'); Z=('+N+r') TO ('+N+r')\.')))
    if any(size<=0 or abs(limits[2*i+1]-limits[2*i]-size)>.005 for i,size in enumerate(overall)):raise ValueError('Overall dimension conflicts')
    frames={}
    vector=r'('+N+r') ('+N+r') ('+N+r')'
    for row in re.findall(r'^FRAME (F\d+) H '+vector+' V '+vector+' N '+vector+'$',text,re.M):
        name,*values=row
        if name in frames:raise ValueError('Duplicate datum frame')
        values=list(map(float,values));h,v,n=[values[i:i+3] for i in (0,3,6)]
        cross=[h[1]*v[2]-h[2]*v[1],h[2]*v[0]-h[0]*v[2],h[0]*v[1]-h[1]*v[0]]
        if any(abs(sum(x*x for x in a)-1)>1e-8 for a in (h,v,n)) or math.dist(cross,n)>1e-8:raise ValueError('Datum frame is not right-handed orthonormal')
        frames[name]={key:[x/math.sqrt(sum(y*y for y in a)) for x in a] for key,a in [('h',h),('v',v),('n',n)]}
    profiles={}
    for page in pages:
        header=re.search(r'^PROFILE ('+ID+r') FRAME (F\d+) \| (.+)$',page,re.M)
        if not header:continue
        name,fid,title=header.groups();p=profiles.setdefault(name,{'frame':fid,'title':title,'edges':{}})
        if (p['frame'],p['title'])!=(fid,title):raise ValueError('Conflicting profile header')
        for line in page.splitlines():
            m=re.fullmatch(r'E(\d+) (LINE|CIRCLE) ('+N+r') ('+N+r') ('+N+r') ('+N+r')',line.strip())
            if m:
                i,kind,*numbers=m.groups();i=int(i);numbers=list(map(float,numbers))
                if i in p['edges']:raise ValueError('Duplicate contour edge')
                p['edges'][i]={'type':kind,'start':numbers[:2],'end':numbers[2:]}
            m=re.fullmatch(r'E(\d+) R ('+N+r') C ('+N+r') ('+N+r') M ('+N+r') ('+N+r')',line.strip())
            if m:
                i,*numbers=m.groups();edge=p['edges'].get(int(i));numbers=list(map(float,numbers))
                if not edge or edge['type']!='CIRCLE' or 'mid' in edge:raise ValueError('Unmatched arc dimensions')
                edge.update(radius=numbers[0],center=numbers[1:3],mid=numbers[3:])
    for p in profiles.values():
        edges=p['edges']
        if not edges or sorted(edges)!=list(range(1,len(edges)+1)):raise ValueError('Incomplete contour')
        p['edges']=[edges[i] for i in range(1,len(edges)+1)]
        for a,b in zip(p['edges'],p['edges'][1:]+p['edges'][:1]):
            if math.dist(a['end'],b['start'])>1e-5:raise ValueError('Open contour')
            if a['type']=='CIRCLE' and ('mid' not in a or a['radius']<=0 or any(abs(math.dist(a[k],a['center'])-a['radius'])>.0002 for k in ('start','mid','end'))):raise ValueError('Inconsistent arc dimensions')
    walls=[];bends=[]
    for kind,target in [('WALL',walls),('BEND',bends)]:
        for name,fid,profile,low,high in re.findall(r'^'+kind+r' ('+ID+r') FRAME (F\d+) PROFILE ('+ID+r') D ('+N+r') TO ('+N+r')$',text,re.M):
            target.append({'name':name,'frame':fid,'profile':profile,'limits':[float(low),float(high)]})
    openings=[{'profile':name,'panel':panel,'limits':[float(low),float(high)]} for name,panel,low,high in re.findall(
        r'^OPENING ('+ID+r') PANEL ('+ID+r') D ('+N+r') TO ('+N+r')$',text,re.M)]
    recesses=[{'profile':name,'panel':panel,'entry':float(entry),'floor':float(floor),'depth':float(depth)} for name,panel,entry,floor,depth in re.findall(
        r'^RECESS ('+ID+r') PANEL ('+ID+r') ENTRY ('+N+r') FLOOR ('+N+r') DEPTH ('+N+r')$',text,re.M)]
    holes=[{'id':int(i),'panel':panel,'center':[float(u),float(v)],'diameter':float(diameter),'limits':[float(low),float(high)]} for i,panel,u,v,diameter,low,high in re.findall(
        r'^H(\d+) PANEL ('+ID+r') U ('+N+r') V ('+N+r') DIA ('+N+r') D ('+N+r') TO ('+N+r') THRU$',text,re.M)]
    collections=dict(frames=frames,profiles=profiles,walls=walls,bends=bends,holes=holes,openings=openings,recesses=recesses)
    if any(len(collections[k])!=count for k,count in counts.items()):raise ValueError('Incomplete dimension tables')
    if [h['id'] for h in holes]!=list(range(1,len(holes)+1)):raise ValueError('Duplicate or missing hole id')
    panel_map={w['name']:w for w in walls}
    if len(panel_map)!=len(walls):raise ValueError('Duplicate panel')
    attached=[]
    for item in walls+bends:
        if item['frame'] not in frames or item['profile'] not in profiles or profiles[item['profile']]['frame']!=item['frame'] or not item['limits'][0]<item['limits'][1]:raise ValueError('Conflicting surface dimensions')
        attached.append(item['profile'])
    for item in holes+openings+recesses:
        panel=panel_map.get(item['panel'])
        if panel is None:raise ValueError('Unknown opening panel')
        if 'limits' in item and any(abs(a-b)>1e-8 for a,b in zip(item['limits'],panel['limits'])):raise ValueError('Opening surface limits conflict')
        if 'profile' in item:
            attached.append(item['profile'])
            if item['profile'] not in profiles or profiles[item['profile']]['frame']!=panel['frame']:raise ValueError('Opening frame conflicts')
        if 'diameter' in item and item['diameter']<=0:raise ValueError('Nonpositive hole diameter')
        if 'depth' in item:
            lo,hi=panel['limits']
            if item['depth']<=0 or abs(abs(item['entry']-item['floor'])-item['depth'])>1e-8 or min(abs(item['entry']-lo),abs(item['entry']-hi))>1e-8 or not lo<item['floor']<hi:raise ValueError('Recess depth conflicts')
    if len(set(attached))!=len(attached) or set(attached)!=set(profiles):raise ValueError('Unassociated or duplicate profile')
    if 'OBLIQUE' in panel_map:
        angle=float(one(r'OBLIQUE PANEL INCLINATION: ('+N+r') DEG\.'))
        n=frames[panel_map['OBLIQUE']['frame']]['n'];measured=math.degrees(math.acos(min(1,abs(n[2]))))
        if abs(angle-measured)>.0001:raise ValueError('Inclination conflicts with datum frame')
    collections.update(overall=overall,globalLimits=limits)
    return collections

def reconstruct(spec):
    def wire(profile,level):
        p=spec['profiles'][profile];f=spec['frames'][p['frame']];h,v,n=[bd.Vector(f[k]) for k in ('h','v','n')]
        point=lambda q:n*level+h*q[0]+v*q[1]
        return bd.Wire([bd.Edge.make_line(point(e['start']),point(e['end'])) if e['type']=='LINE' else
            bd.Edge.make_three_point_arc(point(e['start']),point(e['mid']),point(e['end'])) for e in p['edges']])
    panels={};regions=[]
    for wall in spec['walls']:
        low,high=wall['limits'];n=bd.Vector(spec['frames'][wall['frame']]['n'])
        stock=bd.Solid.extrude(bd.Face(wire(wall['profile'],low)),n*(high-low))
        for hole in spec['holes']:
            if hole['panel']!=wall['name']:continue
            f=spec['frames'][wall['frame']];h,v=[bd.Vector(f[k]) for k in ('h','v')];u,w=hole['center']
            stock=stock.cut(bd.Solid.make_cylinder(hole['diameter']/2,high-low,plane=bd.Plane(origin=n*low+h*u+v*w,x_dir=h,z_dir=n)))
        for opening in spec['openings']:
            if opening['panel']==wall['name']:stock=stock.cut(bd.Solid.extrude(bd.Face(wire(opening['profile'],low)),n*(high-low)))
        for recess in spec['recesses']:
            if recess['panel']==wall['name']:stock=stock.cut(bd.Solid.extrude(bd.Face(wire(recess['profile'],recess['entry'])),n*(recess['floor']-recess['entry'])))
        if len(stock.solids())!=1 or not stock.is_valid:raise ValueError('Invalid wall reconstruction')
        panels[wall['name']]=stock.solids()[0];regions.append(stock.solids()[0])
    for bend in spec['bends']:
        low,high=bend['limits'];n=bd.Vector(spec['frames'][bend['frame']]['n'])
        stock=bd.Solid.extrude(bd.Face(wire(bend['profile'],low)),n*(high-low))
        if len(stock.solids())!=1 or not stock.is_valid:raise ValueError('Invalid bend reconstruction')
        regions.append(stock.solids()[0])
    # Numeric joining is bounded below the source vertex tolerance and remains
    # subject to unchanged whole-solid material and coordinate acceptance.
    body=regions[0].fuse(*regions[1:],tol=1e-5)
    if len(body.solids())!=1 or not body.is_valid:raise ValueError('PDF reconstruction is not one valid solid')
    box=body.bounding_box();bounds=spec['globalLimits']
    if any(abs(actual-expected)>.005 for i in range(3) for actual,expected in [(list(box.min)[i],bounds[2*i]),(list(box.max)[i],bounds[2*i+1])]):raise ValueError('Overall dimensions disagree with the reconstructed contours')
    return body.solids()[0]

def main():
    p=argparse.ArgumentParser();p.add_argument('--pdf',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True);a=p.parse_args();a.out_dir.mkdir(parents=True,exist_ok=False)
    spec=read_pdf(a.pdf);body=reconstruct(spec);step=a.out_dir/'pdf-readback.step';bd.export_step(body,step)
    from catia_mcp.cad_support import compare_steps
    try:comparison=compare_steps({'actual':str(step),'reference':str(a.reference),'lengthToleranceMm':.005,'volumeToleranceMm3':1.0})
    except Exception as exc:comparison={'verdict':'INCONCLUSIVE','error':str(exc)}
    report={'pdf':str(a.pdf.resolve()),'pdfSha256':hashlib.sha256(a.pdf.read_bytes()).hexdigest(),'comparison':comparison,
        'counts':{k:len(v) for k,v in spec.items() if k not in ('overall','globalLimits')},'numericalJoinFuzzyMm':1e-5}
    with (a.out_dir/'review.json').open('x') as f:json.dump(report,f,indent=2)
    print(json.dumps({'verdict':comparison['verdict'],'counts':report['counts']}),flush=True)
    if comparison['verdict']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
