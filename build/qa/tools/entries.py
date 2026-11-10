import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scn import Script
# For each script: find "game 38" (GetCurrentEntryPoint) stored to a frame slot, then comparisons of that slot with constants.
for path in sorted(glob.glob('scn/*.scn')):
    s = Script(path)
    found = {}
    for k, x in enumerate(s.ins):
        if s.ctl(x, 11) and x[3] == 38:
            # pattern: push.frame.3 slot ; game 38 ; push acc ; st32
            p = s.ins[k - 1]
            if p[2] == 'push' and (p[1] & 12) == 12:
                slot, fn = p[3], s.fn_of(x[0])
                end = s.fn_end(fn)
                vals = set()
                for j in range(k, len(s.ins)):
                    y = s.ins[j]
                    if y[0] >= end: break
                    if y[2] == 'push' and (y[1] & 12) == 12 and (y[1] & 3) == 2 and y[3] == slot:
                        n = s.ins[j + 1]
                        if s.imm(n) is not None and s.ins[j + 2][2] == 'alu' and s.ins[j + 2][3] in (8,):
                            vals.add(s.imm(n))
                found.setdefault(fn, set()).update(vals)
    for fn, v in found.items():
        if v:
            print(os.path.basename(path), hex(fn), sorted(v))
