import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scn import Script, qte_sites
s = Script(sys.argv[1])
def up(fn, d, seen):
    cs = s.callers(fn)
    if fn in seen or d > 8: print('  '*d + f'{fn:#x} ...'); return
    seen = seen | {fn}
    print('  '*d + f'{fn:#x} {s.roots.get(fn,"")} callers={len(cs)}')
    for c, pc, kind in cs[:12]:
        print('  '*d + f'  <-{kind}@{pc:#x}')
        up(c, d+1, seen)
for h in sorted({s.fn_of(pc) for pc,_ in qte_sites(s)}):
    up(h, 0, frozenset())
