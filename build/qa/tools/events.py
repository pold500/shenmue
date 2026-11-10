import sys, glob, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scn import Script, qte_sites

def top(s, fn, seen=None):
    seen = seen or set()
    if fn in seen: return []
    seen.add(fn)
    cs = s.callers(fn)
    if not cs: return [(fn, s.roots.get(fn, ''))]
    out = []
    for c, pc, kind in cs:
        out += top(s, c, seen)
    return out

for path in sorted(glob.glob('scn/*.scn')):
    s = Script(path)
    helpers = sorted({s.fn_of(pc) for pc, _ in qte_sites(s)})
    if not helpers: continue
    print('=====', os.path.basename(path))
    for h in helpers:
        # one wrapper level: if helper's callers are themselves only thin wrappers, go up
        level = [h]
        sites = s.callers(h)
        print(f'  helper {h:#x}: {len(sites)} direct call sites')
        byfn = {}
        for c, pc, kind in sites:
            byfn.setdefault(c, []).append(pc)
        for c, pcs in sorted(byfn.items()):
            roots = sorted({f'{f:#x}{"/"+r if r else ""}' for f, r in top(s, c)})
            print(f'     fn {c:#x} x{len(pcs)}  roots={",".join(roots[:4])}{"..." if len(roots)>4 else ""}')
