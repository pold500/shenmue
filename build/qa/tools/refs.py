import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scn import Script, SLOTS
s = Script(sys.argv[1])
want = {struct.unpack('<i', n.encode())[0]: n for n in sys.argv[2:]}
for k, x in enumerate(s.ins):
    if x[2] == 'push' and x[3] in want:
        # find next game/scnf call
        for j in range(k, min(k + 12, len(s.ins))):
            y = s.ins[j]
            if s.ctl(y, 11) or s.ctl(y, 12) or s.ctl(y, 1):
                kind = 'SHENMUE' if s.ctl(y, 11) else 'SCNF' if s.ctl(y, 12) else 'call'
                slot = y[3] if kind != 'SCNF' else y[3] * 2
                name = SLOTS.get((kind, slot), f'{kind}{slot}') if kind != 'call' else f'call {y[3]:#x}'
                print(f"{want[x[3]]} @{x[0]:#x} fn {s.fn_of(x[0]):#x} -> {name}(" + ", ".join(s.show(a) for a in reversed(s.args(j))) + ")")
                break
