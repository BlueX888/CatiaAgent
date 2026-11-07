"""Conventional FTC-08 nominal drawing; reference-side preparation only."""
import argparse,hashlib,json,math
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A3,landscape
from reportlab.lib.colors import Color,black

def generate(measurements,views,output,revision='G1',case='FTC-08'):
    if case not in ('FTC-08','STC-08'):raise ValueError('Unsupported case')
    output=output.resolve();provenance=output.with_suffix('.provenance.json')
    for target in (output,provenance):
        if target.exists():raise FileExistsError(target)
    data=json.loads(measurements.read_text());view_data=json.loads(views.read_text())
    plans=[]
    for name,p in data['profiles'].items():
        rows=[]
        for e in p['edges']:
            a,b=e['start'],e['end'];rows.append(f"E{e['id']:02d} {e['type']} {a[0]:.8f} {a[1]:.8f} {b[0]:.8f} {b[1]:.8f}")
            if e['type']=='CIRCLE':
                c,m=e['center'],e['mid'];rows.append(f"E{e['id']:02d} R {e['radius']:.8f} C {c[0]:.8f} {c[1]:.8f} M {m[0]:.8f} {m[1]:.8f}")
        for i in range(0,len(rows),34):plans.append((name,p,rows[i:i+34],i//34+1,math.ceil(len(rows)/34)))
    W,H=landscape(A3);pages=len(plans)+4
    output.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(output),pagesize=(W,H),invariant=1)
    c.setTitle(f'{case} derived nominal geometry {revision}')
    def text(x,y,value,size=9,bold=False):
        font='Helvetica-Bold' if bold else 'Helvetica'
        if c.stringWidth(value,font,size)>W-x-32:raise ValueError(f'Text too wide: {value}')
        c.setFont(font,size);c.drawString(x,y,value)
    def frame(page,title):
        c.setStrokeColor(black);c.setFillColor(black);c.setLineWidth(.7);c.rect(18,18,W-36,H-36)
        text(35,H-43,case+' | '+title,16,True)
        text(35,H-61,f'DERIVED NOMINAL GEOMETRY {revision} | UNITS MM | Common datum O=(0,0,0); Z up from the bottom face.',9)
        text(35,H-77,'X right and Y up in top view. Views individually scaled. Eight decimal coordinate places support numeric reconstruction, not manufacturing precision.',9)
        text(35,31,f'{revision} | SHEET {page} OF {pages} | NOMINAL SOLID ONLY - NOT A MANUFACTURING RELEASE',8)
    def plot(lines,rect,hidden=None):
        x,y,w,h=rect;all_points=[p for line in lines for p in line]
        xs,ys=zip(*all_points);lo,hi=min(xs),max(xs);bottom,top=min(ys),max(ys)
        scale=min(w/max(hi-lo,1),h/max(top-bottom,1));ox=x+(w-(hi-lo)*scale)/2;oy=y+(h-(top-bottom)*scale)/2
        tr=lambda p:(ox+(p[0]-lo)*scale,oy+(p[1]-bottom)*scale)
        for dashed,paths in [(True,hidden or []),(False,lines)]:
            c.setLineWidth(.3 if dashed else .65);c.setDash(3,2) if dashed else c.setDash()
            c.setStrokeColor(Color(.55,.55,.55) if dashed else black)
            for line in paths:
                path=c.beginPath();path.moveTo(*tr(line[0]))
                for p in line[1:]:path.lineTo(*tr(p))
                c.drawPath(path)
        c.setStrokeColor(black);c.setDash()
        return tr
    frame(1,'GENERAL ISOMETRIC VIEW')
    plot(view_data['ISOMETRIC']['visible'],(80,205,W-160,490))
    text(45,166,'OVERALL X 311.59450000  Y 222.69450000  Z 48.26000000. Bottom Z=0; symmetric overall X/Y extents about datum O.',11,True)
    text(45,144,'HEIGHT DATUMS: BOTTOM Z=0.00000000; BASE TOP Z=10.16000000; WEB UNDERSIDE Z=44.83100000; TOP Z=48.26000000.',10)
    text(45,122,'The base and upright frame enclose a large underside opening. Five upper apertures and twenty circular holes are individually dimensioned.',9)
    text(45,102,'The long top groove is blind; two side recesses reproduce the dimensioned marks. Exact curves replace generic text fonts.',9)
    text(45,82,'Source: NIST FTC-08 Rev C nominal dimensions'+('; verified against official STC-08 AP242 STEP.' if case=='STC-08' else '. The unchanged STEP supplied the nominal geometry.'),9)
    text(45,62,'Coordinate contours describe theoretical wall intersections. See the separate edge-transition sections and shared datum dimensions.',8)
    c.showPage()
    frame(2,'ORTHOGRAPHIC VIEWS')
    plot(view_data['TOP']['visible'],(55,270,650,425),view_data['TOP']['hidden']);text(65,245,'TOP: X RIGHT, Y UP. Dashed curves show hidden boundaries.',9)
    plot(view_data['FRONT']['visible'],(55,113,650,105),view_data['FRONT']['hidden']);text(65,90,'FRONT: X RIGHT, Z UP; viewed along +Y.',9)
    plot(view_data['RIGHT']['visible'],(760,355,365,245),view_data['RIGHT']['hidden']);text(760,325,'RIGHT: Y RIGHT, Z UP; viewed along -X.',9)
    text(760,285,'HEIGHT DATUMS ARE ON SHEET 1.',10,True)
    text(760,260,'Theoretical straight-wall contours follow.',10)
    text(760,237,'The final sheet specifies three edge radii.',10)
    text(760,214,'Circular holes have separate axis coordinates.',10)
    text(760,191,'All outlines share the same global datum.',10)
    c.showPage()
    for page,(name,p,rows,part,count) in enumerate(plans,3):
        frame(page,p['title'])
        text(40,698,f"PROFILE {name} | PLANE {p['plane']} | {p['location']}",10,True)
        text(40,680,f"GEOMETRIC EDGE DIMENSIONS | TABLE PART {part} OF {count} | {len(p['edges'])} EDGES",9)
        tr=plot([e['points'] for e in p['edges']],(45,145,475,475))
        for e in p['edges']:
            x,y=tr(e['mid']);c.setFillColor(Color(.05,.25,.65));c.setFont('Helvetica-Bold',7);c.drawString(x+3,y+3,str(e['id']))
        c.setFillColor(black)
        text(45,112,'Edge numbers locate the tabulated geometric dimensions.',9)
        text(565,650,'EDGE TYPE  START (first,second)  END (first,second)',9,True)
        text(565,634,'XY: first=X, second=Y. YZ: first=Y, second=Z.',9)
        text(565,618,'R = radius; C = center; M = point on the shown arc.',9)
        for i,row in enumerate(rows):
            if c.stringWidth(row,'Courier',8)>W-600:raise ValueError(row)
            c.setFont('Courier',8);c.drawString(565,593-i*14.5,row)
        c.showPage()
    frame(pages-1,'CIRCULAR HOLES AND RECESS DEPTHS')
    text(45,695,'CIRCULAR HOLES: axes parallel to Z, all twenty are through the dimensioned local web.',11,True)
    text(50,669,'HOLE  X / Y AXIS COORDINATES, DIAMETER AND AXIAL LIMITS',10,True)
    for i,h in enumerate(data['holes'],1):
        text(55,645-(i-1)*21,f"H{i:02d} X {h['x']:.8f} Y {h['y']:.8f} D {h['diameter']:.8f} Z {h['bottom']:.8f} TO {h['top']:.8f} THRU",10)
    text(45,190,'BLIND GROOVE: FLOOR Z=46.99000000; DEPTH 1.27000000.',11,True)
    text(45,165,'The groove opening contour is at top Z=48.26; its entire floor-to-wall transition is R0.762.',10)
    text(45,130,'SIDE MARKS: ENTRANCE X=146.01825000; FLOOR X=143.47825000; DEPTH 2.54000000.',11,True)
    text(45,105,'Both YZ contours open on the +X wall and have flat floors. The geometry is not a font substitution.',10)
    c.showPage()
    frame(pages,'EDGE TRANSITIONS AND LOCAL SECTIONS')
    text(45,697,'EDGE RADII: LARGE R3.04800000; FRAME ROOT R1.52400000; GROOVE ROOT R0.76200000.',11,True)
    details=[
        'A: FRAME UPPER OUTER RIM, Z=48.26 before rounding: R3.048 around the full outer contour.',
        'B: FOUR AXIS-ALIGNED BASE UPPER OUTER SPANS, Z=10.16: R3.048. The four short diagonal bevel spans are excluded.',
        'C: UNDERSIDE OPENING CEILING, Z=44.831: R3.048 around the full opening contour.',
        'D: FRAME-TO-BASE OUTER ROOT, Z=10.16: R1.524 around the full frame contour, including its curved corners.',
        'E: BLIND GROOVE FLOOR, Z=46.99: R0.762 around all eight floor edges, including the four corner arcs.']
    for i,line in enumerate(details):text(45,661-i*27,line,10)
    def section(x,y,r,kind,label):
        # These local radial sections define material side and tangent surfaces.
        k=23/r;path=c.beginPath()
        if kind=='convex':
            pts=[(-2*r,-r),(-2*r,0),(-r,0)]
            pts += [(-r+r*math.cos(math.pi/2*(1-j/40)),-r+r*math.sin(math.pi/2*(1-j/40))) for j in range(1,41)]
            pts += [(0,-2*r),(-2*r,-2*r)]
        else:
            pts=[(-2*r,0),(-r,0)]
            pts += [(-r+r*math.cos(-math.pi/2+math.pi/2*j/40),r+r*math.sin(-math.pi/2+math.pi/2*j/40)) for j in range(1,41)]
            pts += [(0,2*r),(r,2*r),(r,-r),(-2*r,-r)]
        path.moveTo(x+pts[0][0]*k,y+pts[0][1]*k)
        for a,b in pts[1:]:path.lineTo(x+a*k,y+b*k)
        path.close();c.setFillColor(Color(.87,.90,.93));c.drawPath(path,fill=1);c.setFillColor(black)
        text(x-70,y-83,label,9,True)
    section(165,362,3.048,'convex','A/B: convex R3.048')
    section(435,362,3.048,'concave','C: concave R3.048')
    section(700,362,1.524,'concave','D: concave R1.524')
    section(970,362,.762,'concave','E: concave R0.762')
    text(45,213,'Sections show the material side (shaded), tangent straight surfaces and circular transitions; each is normal to the indicated edge.',10)
    text(45,183,'All other edges remain as shown in the dimensioned contours. No edge radius is inferred from a generic rounded appearance.',10)
    text(45,153,'Theoretical contours and height datums locate the transitions before rounding. They do not prescribe feature creation order.',10)
    text(45,105,'Completeness: base, upright frame, underside opening, upper web, five upper apertures, twenty holes, blind groove, two side marks and edge transitions.',9)
    c.showPage();c.save()
    report={'schema':'nist-nominal-drawing/1','caseId':case,'revision':revision,
        'pdf':{'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'pages':pages},
        'measurements':{'path':str(measurements.resolve()),'sha256':hashlib.sha256(measurements.read_bytes()).hexdigest()},
        'views':{'path':str(views.resolve()),'sha256':hashlib.sha256(views.read_bytes()).hexdigest()},
        'reviewStatus':'pending_final_pdf_numeric_readback_and_visual_review','scope':'Derived nominal input; generation receives the PDF only.'}
    with provenance.open('x') as f:json.dump(report,f,indent=2)
    return report['pdf']

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--measurements',type=Path,required=True);p.add_argument('--views',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--case',choices=('FTC-08','STC-08'),default='FTC-08');a=p.parse_args()
    print(json.dumps(generate(a.measurements,a.views,a.out,case=a.case)),flush=True)
