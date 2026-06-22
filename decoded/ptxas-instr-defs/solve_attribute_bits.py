#!/usr/bin/env python3
"""Consolidate the attribute bit<->name legend.

Strategy: residual greedy. The binary masks drive the solve; an optional name
legend (see NAME_LEGENDS) supplies human-readable attribute names when present.
1. Build matched (mask, flagset). 2. Iteratively: for each unassigned flag,
   candidate = bits set in ALL positives, clear in ALL negatives, minus already
   assigned bits. Assign flags whose candidate is a singleton. Repeat. This peels
   off RESULT(5) etc. first, then FTZ/SAT once their co-flags are removed.
3. Remaining flags with empty residual but a stable >50% positive bit-set that is
   disjoint-ish -> FIELD (multi-bit). Genuine co-occurring indistinguishables ->
   AMBIG. Used bits never positive in any matched record -> UNNAMED.
"""
import re, csv, collections, json

# Optional attribute-name legend file(s). The binary masks drive the solve and
# stand alone; with no legend present every used bit falls through to UNNAMED /
# (used, unlabeled) without affecting the mask solve.
NAME_LEGENDS=[]
def parse_legend():
    by_full={}; by_name=collections.defaultdict(list)
    for p in NAME_LEGENDS:
        try: txt=open(p).read()
        except OSError: continue
        for m in re.finditer(r'DEFINE\(([^)]*)\)', txt):
            parts=[x.strip() for x in m.group(1).split(',')]
            if len(parts)<4: continue
            op,namef,dt,ff=parts[0].replace(' ',''),parts[1],parts[2],parts[3]
            fs=frozenset(t.strip() for t in ff.split('|') if re.match(r'^[A-Z][A-Z0-9_]+$',t.strip()))
            for n in (namef.split('|') if namef else ['']):
                n=n.strip().replace(' ','')
                by_full.setdefault((n,op,dt),fs); by_name[n].append((op,dt,fs))
    return by_full,by_name

def main():
    by_full,by_name=parse_legend()
    rows=[]
    for r in list(csv.reader(open('/tmp/instr_table.tsv'),delimiter='\t'))[1:]:
        if len(r)<7: continue
        site,idx,name,op,dt,mask,kind=r[:7]
        if len(mask)!=32: continue
        rows.append((name,op,dt,int.from_bytes(bytes.fromhex(mask),'little')))
    union_all=0
    for *_,m in rows: union_all|=m
    used_all=[b for b in range(128) if (union_all>>b)&1]

    pairs=[]
    for name,op,dt,mask in rows:
        fs=by_full.get((name,op,dt))
        if fs is None:
            c=by_name.get(name,[])
            dm=[x for x in c if x[1]==dt]
            if dm and len(set(x[2] for x in dm))==1: fs=dm[0][2]
            elif c and len(set(x[2] for x in c))==1: fs=c[0][2]
        if fs is not None: pairs.append((mask,fs))

    allflags=set().union(*[fs for _,fs in pairs]) if pairs else set()
    matched_used=set()
    for m,_ in pairs:
        for b in used_all:
            if (m>>b)&1: matched_used.add(b)

    assigned={}   # bit->flag (BOOL)
    flagbit={}
    def candidates(F):
        pos=[m for m,fs in pairs if F in fs]; neg=[m for m,fs in pairs if F not in fs]
        if not pos: return set(),0,0
        c=set(used_all)
        for m in pos: c&={b for b in c if (m>>b)&1}
        for m in neg: c-={b for b in c if (m>>b)&1}
        return c,len(pos),len(neg)
    # iterate
    changed=True
    while changed:
        changed=False
        scored=[]
        for F in allflags:
            if F in flagbit: continue
            c,np,nn=candidates(F)
            c=c-set(assigned)
            scored.append((F,c,np,nn))
        # assign singletons
        for F,c,np,nn in scored:
            if len(c)==1:
                b=next(iter(c))
                if b not in assigned:
                    assigned[b]=F; flagbit[F]=b; changed=True

    # remaining flags
    ambig=collections.defaultdict(set); fields={}
    for F in allflags:
        if F in flagbit: continue
        c,np,nn=candidates(F)
        c=frozenset(c-set(assigned))
        if c:
            ambig[c].add(F)   # share residual candidate -> truly indistinguishable
        else:
            # FIELD: bits set in >=40% pos, <5% neg, not already a BOOL bit
            pos=[m for m,fs in pairs if F in fs]; neg=[m for m,fs in pairs if F not in fs]
            pc=collections.Counter(); nc=collections.Counter()
            for m in pos:
                for b in used_all:
                    if (m>>b)&1: pc[b]+=1
            for m in neg:
                for b in used_all:
                    if (m>>b)&1: nc[b]+=1
            fb=sorted(b for b in pc if b not in assigned and pc[b]>=0.4*len(pos) and nc[b]<0.05*max(1,len(neg)))
            if fb: fields[F]=fb

    out={'bool_bit':{str(b):f for b,f in assigned.items()},
         'flag_bit':flagbit,
         'ambiguous_groups':[{'bits':sorted(c),'flags':sorted(fl)} for c,fl in ambig.items()],
         'fields':fields,
         'used_bits_all':used_all,
         'matched_used':sorted(matched_used),
         'unnamed_used':sorted(set(used_all)-matched_used),
         'n_pairs':len(pairs)}
    json.dump(out,open('/tmp/final_solve.json','w'),indent=1)

    # write attribute_bits.tsv
    bit2=[None]*128; kind=[None]*128; conf=[None]*128
    for b,f in assigned.items(): bit2[b]=f; kind[b]='BOOL'; conf[b]='HIGH'
    fld_only={F:bs for F,bs in fields.items() if len(bs)>1}
    for F,bs in fields.items():
        if len(bs)==1 and bit2[bs[0]] is None:
            bit2[bs[0]]=F; kind[bs[0]]='BOOL'; conf[bs[0]]='HIGH'
    for F,bs in fld_only.items():
        for b in bs:
            if bit2[b] is None: bit2[b]=F; kind[b]='FIELD'; conf[b]='MED'
            elif kind[b]=='FIELD': bit2[b]+='/'+F
    for g in out['ambiguous_groups']:
        nm='|'.join(g['flags'])
        for b in g['bits']:
            if bit2[b] is None: bit2[b]=nm; kind[b]='AMBIG'; conf[b]='LOW'
    for b in out['unnamed_used']:
        if bit2[b] is None: bit2[b]='(unnamed)'; kind[b]='UNNAMED'; conf[b]='-'

    with open('decoded/ptxas-instr-defs/attribute_bits.tsv','w') as o:
        o.write("bit_index\tattribute_name\tkind\tconfidence\tused\n")
        for b in range(128):
            if bit2[b] is None and b not in set(used_all): continue
            o.write(f"{b}\t{bit2[b] or '(used, unlabeled)'}\t{kind[b] or 'UNK'}\t{conf[b] or '-'}\t{'yes' if b in set(used_all) else 'no'}\n")

    nb=sum(1 for b in range(128) if kind[b]=='BOOL')
    nf=sum(1 for b in range(128) if kind[b]=='FIELD')
    na=sum(1 for b in range(128) if kind[b]=='AMBIG')
    nu=sum(1 for b in range(128) if kind[b]=='UNNAMED')
    nun=sum(1 for b in used_all if bit2[b] is None)
    print(f"pairs={len(pairs)} used_bits={len(used_all)} matched_used={len(matched_used)}")
    print(f"BOOL(HIGH)={nb} FIELD={nf} AMBIG={na} UNNAMED={nu} used-unlabeled={nun}")
    print(f"distinct boolean flags named={len(set(flagbit) | {F for F,bs in fields.items() if len(bs)==1})}")

if __name__=='__main__': main()
