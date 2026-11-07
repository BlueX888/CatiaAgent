"""Compose hash-bound, directed material-comparison edges conservatively."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compose_edges(paths, length_tolerance, volume_tolerance):
    if not 2 <= len(paths) <= 8:
        raise ValueError('A proof chain needs 2–8 comparison edges')
    if any(type(v) not in (float,int) or not math.isfinite(v) or v<0 for v in (length_tolerance,volume_tolerance)):
        raise ValueError('Finite nonnegative tolerances required')
    totals = dict(bbox=0.,missing_material=0.,excess_material=0.)
    chain, first, previous = [], None, None
    for path in map(Path, paths):
        payload = json.loads(path.read_text(encoding='utf-8'))
        if 'ok' in payload and payload['ok'] is not True:
            raise ValueError('Failed comparison cannot be used as proof')
        data = payload.get('data',payload)
        if data.get('verdict') != 'PASS':
            raise ValueError('Every proof edge must pass')
        sources = data['sources']
        for role in ('actual','reference'):
            source = sources[role]
            if digest(source['path']) != source['sha256']:
                raise ValueError('Source hash mismatch')
        if previous is not None and sources['actual']['sha256'] != previous['sha256']:
            raise ValueError('Proof edges do not form a directed chain')
        first = first or sources['actual']
        previous = sources['reference']
        checks = {c['name']:c for c in data['checks']}
        bounds = {}
        for name in totals:
            c = checks[name]
            unit = 'Mm' if name=='bbox' else 'Mm3'
            value, tolerance = c['actual'+unit],c['tolerance'+unit]
            limit = length_tolerance if name=='bbox' else volume_tolerance
            if (c.get('passed') is not True or any(type(v) not in (int,float) or not math.isfinite(v) for v in (value,tolerance))
                    or not 0 <= value <= tolerance <= limit):
                raise ValueError('Invalid or excessive proof-edge bound')
            bounds[name] = value
            totals[name] += value
        chain.append({'path':str(path.resolve()),'sha256':digest(path),'sources':sources,'materialUpperBounds':bounds})
    checks=[]
    for name,value in totals.items():
        unit = 'Mm' if name=='bbox' else 'Mm3'
        tolerance = length_tolerance if name=='bbox' else volume_tolerance
        checks.append({'name':name,'passed':value<=tolerance,'actual'+unit:value,'tolerance'+unit:tolerance})
    passed = all(c['passed'] for c in checks)
    return {'ok':passed,'operation':'compose_material_evidence','data':{
        'schema':'nist-transitive-material-equivalence/1','verdict':'PASS' if passed else 'FAIL',
        'method':'Sum directed missing/excess material and bbox upper bounds; no new boolean or registration',
        'sources':{'actual':first,'reference':previous},'checks':checks,'proofEdges':chain,
        'limitations':['The bounds establish nominal material equivalence only; source drawing and parameter checks remain separate.']}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edge',action='append',type=Path,required=True)
    parser.add_argument('--length-tolerance',type=float,required=True)
    parser.add_argument('--volume-tolerance',type=float,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=compose_edges(args.edge,args.length_tolerance,args.volume_tolerance)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2)
    print(json.dumps({'verdict':result['data']['verdict'],'checks':result['data']['checks']}))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
