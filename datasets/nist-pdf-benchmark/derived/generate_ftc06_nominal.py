"""Conventional FTC-06 contour and radial-section candidate, no model recipe."""
import argparse,hashlib,json,math
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A3,landscape
from reportlab.lib.colors import Color,black

def generate(measurements,views,output,revision='G1.3'):
    output=output.resolve();provenance=output.with_suffix('.provenance.json')
    for p in (output,provenance):
        if p.exists():raise FileExistsError(p)
    data=json.loads(measurements.read_text());view_data=json.loads(views.read_text());plans=[]
    for name,p in data['profiles'].items():
        rows=[]
        for e in p['edges']:
            a,b=e['start'],e['end'];rows.append(f"E{e['id']:02d} {e['type']} {a[0]:.10f} {a[1]:.10f} {b[0]:.10f} {b[1]:.10f}")
            if e['type']=='CIRCLE':
                a,b=e['center'],e['mid'];rows.append(f"E{e['id']:02d} R {e['radius']:.10f} C {a[0]:.10f} {a[1]:.10f} M {b[0]:.10f} {b[1]:.10f}")
        for i in range(0,len(rows),34):plans.append((name,p,rows[i:i+34],i//34+1,math.ceil(len(rows)/34)))
    W,H=landscape(A3);pages=len(plans)+4;output.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(output),pagesize=(W,H),invariant=1);c.setTitle('FTC-06 derived nominal geometry '+revision)
    def text(x,y,s,size=9,bold=False):
        font='Helvetica-Bold' if bold else 'Helvetica'
        if c.stringWidth(s,font,size)>W-x-32:raise ValueError(f'Text too wide: {s}')
        c.setFont(font,size);c.drawString(x,y,s)
    def frame(page,title):
        c.setFillColor(black);c.setStrokeColor(black);c.setLineWidth(.7);c.rect(18,18,W-36,H-36)
        text(35,H-43,'FTC-06 | '+title,16,True)
        text(35,H-61,f'DERIVED NOMINAL GEOMETRY {revision} | UNITS MM | Y UP; common datum O=(0,0,0) at the rear bottom center.',9)
        text(35,H-77,'Views individually scaled. XY contours use X/Y; YZ uses Y/Z; radial RY uses radius/Y. Ten decimal coordinate places are numerical, not manufacturing precision.',9)
        text(35,31,f'{revision} | SHEET {page} OF {pages} | NOMINAL SOLID ONLY - NOT A MANUFACTURING RELEASE',8)
    def plot(lines,rect,hidden=None):
        x,y,w,h=rect;points=[p for line in lines for p in line];xs,ys=zip(*points);lo,hi,bottom,top=min(xs),max(xs),min(ys),max(ys)
        scale=min(w/max(hi-lo,1),h/max(top-bottom,1));ox=x+(w-(hi-lo)*scale)/2;oy=y+(h-(top-bottom)*scale)/2
        tr=lambda p:(ox+(p[0]-lo)*scale,oy+(p[1]-bottom)*scale)
        for dashed,paths in [(True,hidden or []),(False,lines)]:
            c.setStrokeColor(Color(.55,.55,.55) if dashed else black);c.setLineWidth(.3 if dashed else .65);c.setDash(3,2) if dashed else c.setDash()
            for line in paths:
                path=c.beginPath();path.moveTo(*tr(line[0]))
                for p in line[1:]:path.lineTo(*tr(p))
                c.drawPath(path)
        c.setDash();c.setStrokeColor(black);return tr
    frame(1,'GENERAL ISOMETRIC VIEW')
    plot(view_data['ISOMETRIC']['visible'],(85,235,W-170,455))
    text(45,199,'OVERALL X 304.80000000  Y 97.79000000  Z 247.65000000. The overall Z extent is -247.65 TO 0.',11,True)
    text(45,176,'BASE DATUMS: X=-152.40000000 TO 152.40000000; Y=0.00000000 TO 31.75000000; Z=-234.95000000 TO 0.00000000.',10)
    text(45,154,'REAR WALL: Z=-31.75000000 TO 0.00000000.',10)
    text(45,133,'FRONT BODY: Z=-234.95000000 TO -209.55000000.',10)
    text(45,112,'RIB: X=-12.70000000 TO 12.70000000.',10)
    text(45,91,'Rear and front wall contours, four circular lug noses, central rib, spherical heads, tapered bosses, stepped bores and marks are dimensioned.',9)
    text(45,69,'Source: unchanged NIST FTC-06 Rev D STEP. Theoretical wall intersections and local radial sections define nominal geometry.',9)
    c.showPage()
    frame(2,'ORTHOGRAPHIC VIEWS')
    plot(view_data['TOP']['visible'],(50,190,620,505),view_data['TOP']['hidden']);text(50,164,'TOP: X RIGHT, -Z UP; viewed along -Y.',9)
    plot(view_data['REAR']['visible'],(725,437,405,215),view_data['REAR']['hidden']);text(725,411,'REAR: X RIGHT, Y UP; viewed along -Z.',9)
    plot(view_data['RIGHT']['visible'],(725,131,405,215),view_data['RIGHT']['hidden']);text(725,105,'RIGHT: Z LEFT, Y UP; viewed along -X.',9)
    c.showPage()
    for page,(name,p,rows,part,count) in enumerate(plans,3):
        frame(page,p['title']);plane='RY' if name in ('BALL','CONE') else p['plane']
        text(40,698,f"PROFILE {name} | PLANE {plane} | {p['location']}",10,True)
        text(40,680,f"GEOMETRIC EDGE DIMENSIONS | TABLE PART {part} OF {count} | {len(p['edges'])} EDGES",9)
        tr=plot([e['points'] for e in p['edges']],(45,145,475,475))
        for e in p['edges']:
            x,y=tr(e['mid']);c.setFillColor(Color(.05,.25,.65));c.setFont('Helvetica-Bold',7);c.drawString(x+3,y+3,str(e['id']))
        c.setFillColor(black);text(45,112,'Edge numbers locate the tabulated dimensions.',9)
        text(565,650,'EDGE TYPE  START (first,second)  END (first,second)',9,True)
        text(565,634,'RY: first=radius from the Y axis, second=global Y.',9)
        text(565,618,'R = arc radius; C = center; M = point on shown arc.',9)
        for i,row in enumerate(rows):
            if c.stringWidth(row,'Courier',8)>W-600:raise ValueError(row)
            c.setFont('Courier',8);c.drawString(565,593-i*14.5,row)
        c.showPage()
    frame(pages-1,'STEPPED BORES AND AXIS LOCATIONS')
    text(45,697,'TEN STEPPED BORES: twenty cylindrical portions. The axis point and scalar axial limits use the global datum.',11,True)
    for i,h in enumerate(data['holes'],1):
        x,y,z=h['center'];text(50,669-(i-1)*20,f"H{i:02d} AXIS {h['axis']} POINT {x:.8f} {y:.8f} {z:.8f} D {h['diameter']:.8f} FROM {h['bottom']:.8f} TO {h['top']:.8f}",9)
    text(45,239,'BALL AXES: X=+/-114.30000000; Z=-107.95000000; FULL CIRCUMFERENCE 360.00000000 DEG.',10,True)
    text(45,215,'TAPERED AXES: X=+/-76.20000000; Z=-158.75000000; FULL CIRCUMFERENCE 360.00000000 DEG.',10,True)
    text(45,191,'Both radial profiles are axisymmetric about the indicated Y axes. Their displayed R=0 line is the axis.',10)
    text(45,168,'The tapered-boss section includes its cylindrical blind bore and conical drill tip (118 DEG included).',10)
    text(45,134,'LUG NOSES: X=-108.58500000 -36.19500000 36.19500000 108.58500000; Y=50.80000000; R22.22500000; Z=-247.65000000 TO -234.95000000.',9,True)
    text(45,108,'The four nose portions are full circular cylinders; their rear portions merge into the FRONT contour.',10)
    text(45,83,'FRONT MARKS: ENTRANCE Z=-234.95000000; FLOOR Z=-232.41000000; DEPTH 2.54000000.',10,True)
    c.showPage()
    frame(pages,'ROOT TRANSITIONS AND SPHERICAL RECESS')
    text(45,697,'STRAIGHT ROOT RADII: R3.17500000 ON ALL EIGHT INDICATED SPANS.',11,True)
    text(45,674,'Coordinates locate theoretical straight intersections before rounding; they do not prescribe feature order.',10)
    text(50,647,'ROOT  START X/Y/Z TO END X/Y/Z',10,True)
    for i,r in enumerate(data['roots'],1):
        a=[v-d*r['lengthMm']/2 for v,d in zip(r['midpoint'],r['direction'])];b=[v+d*r['lengthMm']/2 for v,d in zip(r['midpoint'],r['direction'])]
        text(55,624-(i-1)*23,f"R{i:02d} FROM {a[0]:.8f} {a[1]:.8f} {a[2]:.8f} TO {b[0]:.8f} {b[1]:.8f} {b[2]:.8f}",10)
    text(45,409,'R1-R6: rear wall/base, rear wall/rib and rib/base side junctions. R7: rib front root. R8: front-body step root.',10)
    text(45,376,'SPHERICAL RECESS: CENTER 0.00000000 97.79000000 -15.87500000; R12.70000000; OPEN Y=97.79000000; LOWEST Y=85.09000000.',9.5,True)
    text(45,351,'The hemispherical cup opens on the rear-wall top plane Y=97.79; the lower half lies inside the wall.',10)
    # An axial half section shows the hemispherical cavity below its entrance.
    x0,y0,k=360,240,4;path=c.beginPath();path.moveTo(x0-25*k,y0)
    path.lineTo(x0-12.7*k,y0)
    for j in range(1,81):
        a=math.pi+math.pi*j/80;path.lineTo(x0+12.7*math.cos(a)*k,y0+12.7*math.sin(a)*k)
    path.lineTo(x0+25*k,y0);path.lineTo(x0+25*k,y0-22*k);path.lineTo(x0-25*k,y0-22*k);path.close()
    c.setFillColor(Color(.87,.90,.93));c.drawPath(path,fill=1);c.setFillColor(black)
    text(570,223,'Cup radial section: Y increases upward.',10)
    text(570,199,'Entrance plane Y=97.79; lowest point Y=85.09.',10)
    text(570,175,'The sphere radius is 12.7, centered on the entrance plane.',10)
    text(45,73,'Complete nominal geometry includes both exact side-mark contours and every indicated root transition; no font substitutes for the marks.',9)
    c.showPage();c.save()
    result={'schema':'nist-nominal-drawing/1','caseId':'FTC-06','revision':revision,'coordinateDecimalPlaces':10,'pdf':{'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'pages':pages},
        'measurements':{'path':str(measurements.resolve()),'sha256':hashlib.sha256(measurements.read_bytes()).hexdigest()},
        'views':{'path':str(views.resolve()),'sha256':hashlib.sha256(views.read_bytes()).hexdigest()},'reviewStatus':'pending_final_pdf_readback_and_visual_review'}
    with provenance.open('x') as f:json.dump(result,f,indent=2)
    return result['pdf']
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--measurements',type=Path,required=True);p.add_argument('--views',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();print(json.dumps(generate(a.measurements,a.views,a.out)),flush=True)
