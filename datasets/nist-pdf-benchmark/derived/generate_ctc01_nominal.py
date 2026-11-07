"""Reference-side conventional CTC-01 drawing; no modeling instructions."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas


def generate(measurements, overview, output, revision='G1', precision=4):
    output = output.resolve()
    provenance = output.with_suffix('.provenance.json')
    for p in (output, provenance):
        if p.exists() or p.is_symlink(): raise FileExistsError(p)
    data = json.loads(measurements.read_text(encoding='utf-8'))
    plans = []
    for name, profile in data['profiles'].items():
        rows = []
        for e in profile['edges']:
            a,b=e['start'],e['end']
            rows.append(f"E{e['id']:02d} {e['type']} {a[0]:.{precision}f} {a[1]:.{precision}f} {b[0]:.{precision}f} {b[1]:.{precision}f}")
            if e['type']=='CIRCLE':
                p,m=e['center'],e['mid']
                rows.append(f"E{e['id']:02d} R {e['radius']:.{precision}f} C {p[0]:.{precision}f} {p[1]:.{precision}f} M {m[0]:.{precision}f} {m[1]:.{precision}f}")
        for i in range(0,len(rows),34): plans.append((name,profile,rows[i:i+34],i//34+1,math.ceil(len(rows)/34)))
    W,H=landscape(A3)
    stream=BytesIO(); c=canvas.Canvas(stream,pagesize=(W,H),invariant=1)
    c.setTitle('CTC-01 derived nominal geometry '+revision)
    pages=len(plans)+3
    def text(x,y,s,size=9,bold=False):
        font='Helvetica-Bold' if bold else 'Helvetica'
        if c.stringWidth(s,font,size)>W-x-32: raise ValueError(f'Text does not fit: {s}')
        c.setFont(font,size);c.drawString(x,y,s)
    def frame(page,title):
        c.setFillColor(black);c.setStrokeColor(black);c.setLineWidth(.7)
        c.rect(18,18,W-36,H-36)
        text(35,H-43,'CTC-01 | '+title,16,True)
        text(35,H-61,f'DERIVED NOMINAL GEOMETRY {revision} | mm | X right, Y up in the top view; Z up from the main top face at Z=0.',9)
        text(35,H-77,f'Common datum O=(0,0,0). Coordinate tables: {precision} decimal places for numeric reconstruction; not manufacturing precision. Views individually scaled.',9)
        text(35,31,f'{revision} | SHEET {page} OF {pages} | NOMINAL SOLID ONLY - NOT A MANUFACTURING RELEASE',8)

    frame(1,'GENERAL VIEWS AND DIMENSION BASIS')
    # The existing independently checked pure-solid HLR views are inserted as
    # ordinary 2D vectors after the reportlab content has been saved.
    text(40,147,'OVERALL X 800.0000  Y 450.0000  Z -100.0000 TO 50.0000. Main plate top Z=0, underside Z=-50.',10,True)
    text(40,127,'Four feet extend to Z=-100; see each foot outline and R10 underside-root sections. No assumed symmetry at the northeast corner.',9)
    text(40,108,'Central hexagon: 100 across flats, top Z=50, R5 at its root, top bevel 5 x 45 DEG. Exact section contour appears below.',9)
    text(40,89,'Eight vertical circular through holes, two through apertures, two side blind drilled holes and two side-mark recess contours are dimensioned.',9)
    text(40,70,'Source: unchanged NIST CTC-01 Rev D STEP. Reconstruction limits 0.005 mm / 7321.4 mm3 are numerical checks, not manufacturing tolerances.',8)
    c.showPage()
    for page,(name,profile,rows,part,count) in enumerate(plans,2):
        frame(page,profile['title'])
        text(40,698,f"PROFILE {name} | PLANE {profile['plane']} | {profile['location']}",10,True)
        text(40,680,f"GEOMETRIC EDGE DIMENSIONS | TABLE PART {part} OF {count} | {len(profile['edges'])} EDGES",9)
        edges=profile['edges']; points=[p for e in edges for p in e['points']]
        xs,ys=zip(*points); xmin,xmax,ymin,ymax=min(xs),max(xs),min(ys),max(ys)
        scale=min(475/max(xmax-xmin,1),490/max(ymax-ymin,1))
        ox,oy=45+(475-(xmax-xmin)*scale)/2,140+(490-(ymax-ymin)*scale)/2
        tr=lambda p:(ox+(p[0]-xmin)*scale,oy+(p[1]-ymin)*scale)
        for e in edges:
            path=c.beginPath();path.moveTo(*tr(e['points'][0]))
            for p in e['points'][1:]:path.lineTo(*tr(p))
            c.drawPath(path)
            c.setFillColor(Color(.05,.25,.65));c.setFont('Helvetica-Bold',7)
            x,y=tr(e['mid']);c.drawString(x+3,y+3,str(e['id']));c.setFillColor(black)
        text(45,112,'Edge numbers locate the tabulated geometric dimensions.',9)
        text(565,650,'EDGE TYPE  START (first,second)  END (first,second)',9,True)
        text(565,634,'XY: first=X, second=Y. YZ: first=Y, second=Z.',9)
        text(565,618,'R = radius; C = center; M = point on the shown arc.',9)
        for i,row in enumerate(rows):
            c.setFont('Courier',8.3)
            if c.stringWidth(row,'Courier',8.3)>W-600:raise ValueError(row)
            c.drawString(565,593-i*14.5,row)
        c.showPage()

    page=len(plans)+2
    frame(page,'CIRCULAR HOLES AND BLIND DRILLED-HOLE SECTION')
    text(45,695,'VERTICAL HOLES: axes parallel to Z, all dimensions from common datum O.',11,True)
    text(55,666,'HOLE  X             Y             DIAMETER      Z BOTTOM       Z TOP       TYPE',10,True)
    for i,h in enumerate(data['holes'],1):
        row=f"H{i:02d} X {h['x']:.4f} Y {h['y']:.4f} D {h['diameter']:.4f} Z {h['bottom']:.4f} TO {h['top']:.4f} THRU"
        text(55,641-(i-1)*22,row,10)
    text(45,430,'TWO SIDE BLIND DRILLED HOLES: axes parallel to +Y; entrance on Y=-125.',11,True)
    for i,h in enumerate(data['sideBores'],1):
        text(55,400-(i-1)*25,f"B{i} X {h['x']:.4f} Z {h['z']:.4f} D {h['diameter']:.4f} ENTRY Y {h['entryY']:.4f} CYL END Y {h['straightEndY']:.4f} TIP Y {h['tipY']:.{max(5,precision)}f}",10)
    text(55,335,'B1 AND B2: CYLINDRICAL DEPTH 45.0000; INCLUDED TIP ANGLE 118.0000 DEG.',10,True)
    # Axisymmetric section of each blind hole, radial coordinate vertical.
    def sec(y,r):return(150+(y+125)*8,190+r*6)
    for sign in (-1,1):
        c.line(*sec(-125,sign*10),*sec(-80,sign*10));c.line(*sec(-80,sign*10),*sec(data['sideBores'][0]['tipY'],0))
    c.saveState();c.setDash(3,2);c.line(*sec(-130,0),*sec(-70,0));c.restoreState()
    text(150,100,'B-B: radial section through B1 or B2; +Y to the right. Material surrounds the bore and conical tip.',9)
    c.showPage()

    frame(pages,'UNDERSIDE ROOT AND HEXAGON DETAILS')
    text(45,695,'UNDERSIDE TRANSITION: R10.0000 on all eight straight internal root spans between the main plate and feet.',10,True)
    text(45,673,'ROOT LOCATIONS BEFORE ROUNDING: Z=-50; Y=+/-125 at X=250 TO 400 or -400 TO -250;',10)
    text(45,654,'and X=+/-250 at Y=125 TO 150 or -150 TO -125. Other main-plate underside edges remain sharp.',10)
    text(45,632,'The R10 transitions occupy Z=-60 TO -50. Each foot nominal section is constant below Z=-60.',10)
    x0,y0,k=130,365,7
    tr=lambda y,z:(x0+(y-90)*k,y0+(z+70)*k)
    path=c.beginPath();path.moveTo(*tr(90,-50));path.lineTo(*tr(115,-50))
    for n in range(1,31):
        a=math.pi/2*(1-n/30);path.lineTo(*tr(115+10*math.cos(a),-60+10*math.sin(a)))
    path.lineTo(*tr(125,-80));path.lineTo(*tr(145,-80));path.lineTo(*tr(145,-35));path.lineTo(*tr(90,-35));path.close()
    c.setFillColor(Color(.88,.90,.92));c.drawPath(path,fill=1);c.setFillColor(black)
    text(100,270,'TYPICAL ROOT SECTION: +Y FOOT, normal to X; X-face roots have the corresponding rotated/mirrored section.',9)
    text(100,250,'Horizontal underside Z=-50; vertical foot wall Y=125; circular center (Y,Z)=(115,-60), R10.',9)
    text(690,570,'HEXAGON DETAIL',11,True)
    text(690,546,'STRAIGHT-WALL ACROSS FLATS 100.0000',10)
    text(690,526,'ROOT Z=0; R5.0000 TO STRAIGHT SIDE AT Z=5',10)
    text(690,506,'STRAIGHT SIDE Z=5 TO 45',10)
    text(690,486,'TOP BEVEL 5.0000 X 45.0000 DEG',10)
    text(690,466,'TOP Z=50; TOP ACROSS FLATS 90.0000',10)
    text(690,446,'All six root edges have R5; all six top edges bevel.',9)
    text(45,213,'SIDE MARKS: both dimensioned YZ contours open on the +X face X=400; flat floor X=398, depth 2.0000.',10,True)
    text(45,191,'The coordinate contours define the nominal marks; a generic font must not replace their geometry.',9)
    text(45,152,'Completeness: main perimeter; four foot outlines and their roots; two through apertures; eight Z holes;',9)
    text(45,132,'two side blind bores with conical tips; hexagon with root radii and top bevels; both side recess contours.',9)
    text(45,95,'No manufacturing PMI, material designation or tolerance release is asserted by this geometric reconstruction drawing.',9)
    c.showPage();c.save()
    # The bundled document interpreter supplies reportlab, while the existing
    # CAD-support interpreter supplies PyMuPDF. Keep those environments intact.
    pdf_python=Path(__file__).resolve().parents[3]/'work/text-to-cad-env/Scripts/python.exe'
    merge='''import sys, pymupdf
with pymupdf.open(sys.argv[1]) as doc, pymupdf.open(sys.argv[2]) as source:
    p=doc[0]; w,h=p.rect.width,p.rect.height
    clip=pymupdf.Rect(0,0,source[0].rect.width,source[0].rect.height*.745)
    p.show_pdf_page(pymupdf.Rect(36,95,w-36,h-172),source,0,clip=clip)
    doc.save(sys.argv[3],garbage=4,deflate=True)
'''
    with tempfile.TemporaryDirectory(prefix='ctc01-drawing-') as tmp:
        content,merged=Path(tmp)/'content.pdf',Path(tmp)/'merged.pdf'
        content.write_bytes(stream.getvalue())
        subprocess.run([str(pdf_python),'-c',merge,str(content),str(overview.resolve()),str(merged)],
                       capture_output=True,check=True)
        pdf_bytes=merged.read_bytes()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as f:f.write(pdf_bytes)
    report={'schema':'nist-nominal-drawing/1','caseId':'CTC-01','revision':revision,'coordinateDecimalPlaces':precision,'pdf':{'path':str(output),'sha256':hashlib.sha256(pdf_bytes).hexdigest(),'pages':pages},
        'measurements':{'path':str(measurements),'sha256':hashlib.sha256(measurements.read_bytes()).hexdigest()},
        'overview':{'path':str(overview),'sha256':hashlib.sha256(overview.read_bytes()).hexdigest()},
        'reviewStatus':'pending_final_pdf_readback_and_visual_review','scope':'Nominal solid geometry; generation receives this PDF only.'}
    with provenance.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
    return report['pdf']


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--measurements',type=Path,required=True);p.add_argument('--overview',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--revision',default='G1');p.add_argument('--precision',type=int,choices=(4,6,8),default=4)
    a=p.parse_args();print(json.dumps(generate(a.measurements,a.overview,a.out,a.revision,a.precision)))
