"""Conventional folded-bracket dimensions and sections; no modeling schedule."""
import argparse,hashlib,json,math
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A3,landscape
from reportlab.lib.colors import Color,black

def generate(source,output):
    data=json.loads(source.read_text());output=output.resolve();provenance=output.with_suffix('.provenance.json')
    if output.exists() or provenance.exists():raise FileExistsError(output)
    plans=[]
    for name,p in data['profiles'].items():
        rows=[]
        for e in p['edges']:
            a,b=e['start'],e['end'];rows.append(f"E{e['id']:02d} {e['type']} {a[0]:.10f} {a[1]:.10f} {b[0]:.10f} {b[1]:.10f}")
            if e['type']=='CIRCLE':
                c,m=e['center'],e['mid'];rows.append(f"E{e['id']:02d} R {e['radius']:.10f} C {c[0]:.10f} {c[1]:.10f} M {m[0]:.10f} {m[1]:.10f}")
        for i in range(0,len(rows),34):plans.append((name,p,rows[i:i+34],i//34+1,math.ceil(len(rows)/34)))
    W,H=landscape(A3);pages=4+len(plans);output.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(output),pagesize=(W,H),invariant=1);c.setTitle('CTC-03 derived nominal geometry G1')
    def text(x,y,s,size=9,bold=False):
        font='Helvetica-Bold' if bold else 'Helvetica'
        if c.stringWidth(s,font,size)>W-x-30:raise ValueError('Text exceeds sheet: '+s)
        c.setFont(font,size);c.drawString(x,y,s)
    def frame(page,title):
        c.setFillColor(black);c.setStrokeColor(black);c.setLineWidth(.7);c.rect(18,18,W-36,H-36)
        text(35,H-43,'CTC-03 | '+title,16,True)
        text(35,H-62,'DERIVED NOMINAL GEOMETRY G1 | UNITS MM | Z UP; common datum O=(0,0,0). Views individually scaled.',9)
        text(35,H-79,'Frame coordinates U=H dot P, V=V dot P, D=N dot P. H,V,N are right-handed unit directions. Ten decimals are numerical, not manufacturing precision.',9)
        text(35,32,f'G1 | SHEET {page} OF {pages} | NOMINAL SOLID ONLY - NOT A MANUFACTURING RELEASE',8)
    def plot(lines,rect,hidden=None):
        pts=[p for line in lines for p in line];lo=min(p[0] for p in pts);hi=max(p[0] for p in pts);bottom=min(p[1] for p in pts);top=max(p[1] for p in pts)
        x,y,w,h=rect;scale=min(w/max(hi-lo,1e-9),h/max(top-bottom,1e-9));ox=x+(w-(hi-lo)*scale)/2;oy=y+(h-(top-bottom)*scale)/2
        transform=lambda p:(ox+(p[0]-lo)*scale,oy+(p[1]-bottom)*scale)
        for dashed,paths in [(True,hidden or []),(False,lines)]:
            c.setStrokeColor(Color(.55,.55,.55) if dashed else black);c.setLineWidth(.3 if dashed else .65);c.setDash(3,2) if dashed else c.setDash()
            for line in paths:
                if len(line)<2:continue
                path=c.beginPath();path.moveTo(*transform(line[0]))
                for p in line[1:]:path.lineTo(*transform(p))
                c.drawPath(path)
        c.setDash();c.setStrokeColor(black);return transform
    frame(1,'GENERAL ISOMETRIC VIEW')
    plot(data['views']['ISOMETRIC']['visible'],(60,215,W-120,475))
    text(45,181,'OVERALL X 302.9633335864  Y 485.4448000000  Z 162.6801498312.',11,True)
    text(45,156,'GLOBAL LIMITS: X=-249.5610871434 TO 53.4022464430; Y=0.0000000000 TO 485.4448000000; Z=0.0000000000 TO 162.6801498312.',10)
    text(45,130,'Eight wall surface pairs, seven bend sections, fifteen round holes, two other openings and two back recesses are dimensioned.',10)
    text(45,105,'Wall labels identify physical regions. Surface and axial limits locate geometry; no feature order is prescribed.',10)
    text(45,80,'Source: unchanged NIST CTC-03 Rev C STEP. This derived drawing defines nominal geometry, not manufacturing PMI.',9)
    c.showPage();frame(2,'ORTHOGRAPHIC VIEWS')
    for name,rect,label in [('TOP',(50,170,620,520),'TOP: X RIGHT, Y UP; viewed along -Z.'),
                            ('FRONT',(725,440,405,235),'FRONT: X RIGHT, Z UP; viewed along +Y.'),
                            ('RIGHT',(725,120,405,235),'RIGHT: Y RIGHT, Z UP; viewed along -X.')]:
        view=data['views'][name];plot(view['visible'],rect,view['hidden']);text(rect[0],rect[1]-25,label,9)
    c.showPage();frame(3,'DATUM FRAMES AND SURFACE LOCATIONS')
    text(45,698,f"CONTENTS: FRAMES {len(data['frames'])} PROFILES {len(data['profiles'])} WALLS {len(data['walls'])} BENDS {len(data['bends'])} HOLES {len(data['roundHoles'])} OPENINGS {len(data['openings'])} RECESSES {len(data['recesses'])}.",10,True)
    for i,(name,f) in enumerate(data['frames'].items()):
        fmt=lambda values:' '.join(f'{x:.10f}' for x in values)
        text(45,672-i*22,f"FRAME {name} H {fmt(f['h'])} V {fmt(f['v'])} N {fmt(f['n'])}",8.5)
    text(45,563,'WALL SURFACES: D is signed normal distance from the common origin; each pair has its own actual thickness.',10,True)
    for i,w in enumerate(data['walls']):
        a,b=w['limits'];text(50,541-i*18,f"WALL {w['name']} FRAME {w['frame']} PROFILE {w['profile']} D {a:.10f} TO {b:.10f}",9)
    text(45,372,'BEND CROSS SECTIONS: D gives the physical axial span of each displayed section.',10,True)
    for i,w in enumerate(data['bends']):
        a,b=w['limits'];text(50,350-i*18,f"BEND {w['name']} FRAME {w['frame']} PROFILE {w['profile']} D {a:.10f} TO {b:.10f}",9)
    for i,w in enumerate(data['openings']):
        a,b=w['limits'];text(50,199-i*18,f"OPENING {w['profile']} PANEL {w['panel']} D {a:.10f} TO {b:.10f}",9)
    for i,w in enumerate(data['recesses']):
        text(50,147-i*20,f"RECESS {w['profile']} PANEL {w['panel']} ENTRY {w['entry']:.10f} FLOOR {w['floor']:.10f} DEPTH {w['depth']:.10f}",9)
    text(45,88,'OBLIQUE PANEL INCLINATION: 45 DEG. Opposite surface positions are independently dimensioned; do not assume a uniform wall thickness.',9)
    c.showPage();frame(4,'ROUND HOLE POSITIONS AND DIAMETERS')
    text(45,698,'Hole centers use the named panel frame. D limits are the two material surfaces; all listed round holes are through.',10,True)
    for i,hole in enumerate(data['roundHoles']):
        u,v=hole['center'];a,b=hole['limits']
        text(50,670-i*25,f"H{hole['id']:02d} PANEL {hole['panel']} U {u:.10f} V {v:.10f} DIA {hole['diameter']:.10f} D {a:.10f} TO {b:.10f} THRU",9)
    text(45,223,'The two non-circular web openings have their own dimensioned contours. Back lettering is defined by two exact recess contours.',10)
    text(45,195,'All contour sheets use edge numbers; R is arc radius, C is center and M is a point on the shown arc.',10)
    text(45,166,'Bend inner and outer circular arcs have their actual centers and end points. No concentricity or nominal-thickness substitution is implied.',10)
    c.showPage()
    def arc_points(e):
        if e['type']=='LINE':return [e['start'],e['end']]
        center=e['center'];angles=[math.atan2(p[1]-center[1],p[0]-center[0]) for p in (e['start'],e['mid'],e['end'])]
        start,mid,end=angles;span=(end-start)%math.tau
        if (mid-start)%math.tau>span+1e-8:span-=math.tau
        count=max(8,math.ceil(abs(span)*e['radius']/.25))
        return [[center[0]+e['radius']*math.cos(start+span*i/count),center[1]+e['radius']*math.sin(start+span*i/count)] for i in range(count+1)]
    for page,(name,p,rows,part,total) in enumerate(plans,5):
        frame(page,p['title']);text(40,698,f"PROFILE {name} FRAME {p['frame']} | {p['title']}",10,True)
        text(40,678,f"GEOMETRIC EDGE DIMENSIONS | TABLE PART {part} OF {total} | {len(p['edges'])} EDGES",9)
        transform=plot([arc_points(e) for e in p['edges']],(45,145,475,475))
        for e in p['edges']:
            x,y=transform(e['mid']);c.setFillColor(Color(.05,.25,.65));c.setFont('Helvetica-Bold',7);c.drawString(x+3,y+3,str(e['id']))
        c.setFillColor(black);text(45,111,'Contour coordinates are U,V in the named datum frame.',9)
        text(565,650,'EDGE TYPE  START U V  END U V',9,True)
        text(565,629,'R = radius; C = arc center U,V; M = point on shown arc.',9)
        for i,row in enumerate(rows):
            if c.stringWidth(row,'Courier',8)>W-600:raise ValueError(row)
            c.setFont('Courier',8);c.drawString(565,596-i*14.5,row)
        c.showPage()
    c.save()
    result={'schema':'nist-nominal-drawing/1','caseId':'CTC-03','revision':'G1','coordinateDecimalPlaces':10,
        'pdf':{'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'pages':pages},
        'drawingData':{'path':str(source.resolve()),'sha256':hashlib.sha256(source.read_bytes()).hexdigest()},
        'reviewStatus':'pending_final_pdf_readback_and_visual_review'}
    with provenance.open('x') as f:json.dump(result,f,indent=2)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    print(json.dumps(generate(a.data,a.out)),flush=True)
