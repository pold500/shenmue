"""Shenmue I scenario (.scn) analysis toolkit.

Commands:
  extract <sm1 dir> <filename_database.json> <out dir>     unpack every /scenario/*.scn
  qtes <scn>                                               list QTE begin calls (SCNF pair 2, command 0)
  chain <scn> <qte hex>                                    call/spawn chain from a QTE to its roots, with conditions
  calls <scn> <fn hex>...                                  engine calls in functions (named, with args)
  dis <scn> <from hex> <to hex>                            disassemble a range
  callers <scn> <fn hex>                                   direct callers / spawners of a function
  cond <scn> <fn hex>:<site hex>...                        conditions that must hold for site to run
Decoder follows SCN_Parse (src/s1/task/scen_vm.cpp).
"""
import gzip
import json
import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HDR = struct.Struct("<12i")
SCEN_UTILS, CREATE_TASK, SET_EVENT_TABLE = 81, 2, 1
CMD = {11: "F", 13: "B", 15: "S"}
BIN = {8: "==", 9: "!=", 10: "<", 11: "<=", 12: ">", 13: ">=", 15: "&&", 16: "||", 17: "^", 18: "+", 19: "-", 20: "*", 23: "<<", 24: ">>"}
NAMES = {0: "ret", 1: "call", 2: "callind", 3: "stack", 4: "jmp", 5: "jrel", 6: "jz", 7: "argc", 8: "str", 9: "legacy", 10: "mem", 11: "game", 12: "scnf", 13: "door"}
ALU = {0: "pop->acc", 1: "push acc", 2: "ld8", 3: "ld16", 4: "ld32", 5: "st8", 6: "st16", 7: "st32", 14: "not", **{k: v for k, v in BIN.items()}}


def load_names():
    flags, slots = {}, {}
    for line in (REPO / "src/common/script_catalogue.def").read_text().splitlines():
        m = re.match(r"SHENMUE_S1_FLAG\((\w+), (\d+)\)", line)
        if m:
            flags.setdefault(int(m.group(2)), m.group(1))
    for line in (REPO / "src/s1/task/scenario_dispatch.def").read_text().splitlines():
        m = re.match(r"SCENARIO_(SHENMUE|SCNF)_SLOT\((\d+), (\w+)\)", line)
        if m:
            slots[(m.group(1), int(m.group(2)))] = m.group(3)
    return flags, slots


FLAGS, SLOTS = load_names()

# ---------------------------------------------------------------- extraction

M, MASK = 0x5BD1E995, 0xFFFFFFFF


def murmur2(data):
    n = len(data)
    h = ((n + n // 0xFFFFFFFF) ^ 0x066EE5D0) & MASK
    off = 0
    while off + 4 <= n:
        k = struct.unpack_from("<I", data, off)[0]
        k = (k * M) & MASK
        k ^= k >> 24
        k = (k * M) & MASK
        h = ((h * M) & MASK) ^ k
        off += 4
    rest = n - off
    if rest == 3:
        h ^= data[off + 2] << 16
    if rest >= 2:
        h ^= data[off + 1] << 8
    if rest >= 1:
        h = (M * (h ^ data[off])) & MASK
    h = (M * (h ^ (h >> 13))) & MASK
    return h ^ (h >> 15)


def path_hash(path):
    allowed, filtered = 0x2000000000FFD, ""
    for ch in path.lower():
        bit = ord(ch) - 46
        if "a" <= ch <= "z" or (0 <= bit <= 49 and (allowed >> bit) & 1):
            filtered += ch
    base = murmur2(filtered.encode())
    return (0x1003F * base + 0x2001F * ((len(filtered) ** 2) % 0xFFFFFFFF)) & MASK


def extract(sm1, db_path, out):
    out.mkdir(parents=True, exist_ok=True)
    entries = {}
    for tad in sorted((sm1 / "archives/dx11/data").glob("*.tad")):
        data = tad.read_bytes()
        order, count = struct.unpack_from("<Q", data, 16)[0], struct.unpack_from("<I", data, 56)[0]
        for i in range(count):
            h1, h2, _, _, off, _, size, _ = struct.unpack_from("<8I", data, 60 + i * 32)
            if (h1, h2) not in entries or entries[(h1, h2)][0] < order:
                entries[(h1, h2)] = (order, tad.with_suffix(".tac"), off, size)
    seen = set()
    for item in json.loads(db_path.read_text()):
        p = item["Path"]
        name = Path(p).name
        if not p.startswith("/scenario/") or not p.endswith(".scn") or name in seen:
            continue
        hit = entries.get((path_hash("scenario/" + name), 0)) or entries.get((item["Hash"], item["HashPath"]))
        if not hit:
            continue
        seen.add(name)
        _, tac, off, size = hit
        with open(tac, "rb") as f:
            f.seek(off)
            blob = f.read(size)
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
        (out / name).write_bytes(blob)
    print(f"extracted {len(seen)} scn files to {out}")

# ---------------------------------------------------------------- decoding


class Script:
    def __init__(self, path):
        self.b = Path(path).read_bytes()
        h = HDR.unpack_from(self.b, 0)
        self.whole, self.func_off, self.data_off, self.entry = h[1], h[3], h[4], h[8]
        self.ins = list(self._decode(self.entry, min(self.data_off, self.whole)))
        self.at = {x[0]: k for k, x in enumerate(self.ins)}
        self._graph()

    def _decode(self, pc, end):
        prev = 0
        while pc < end:
            at, op = pc, self.b[pc]
            pc += 1
            top = op & 0xC0
            if top == 0x80:
                yield at, op, "alu", op & 0x3F
                continue
            if top == 0xC0:
                yield at, op, "nop", None
                continue
            size = (0, 1, 2, 4)[(op >> 4) & 3]
            if size:
                prev = struct.unpack_from({1: "<b", 2: "<h", 4: "<i"}[size], self.b, pc)[0]
            pc += size
            yield at, op, "push" if top == 0x40 else "ctl", prev

    @staticmethod
    def ctl(x, low):
        return x[2] == "ctl" and (x[1] & 15) == low

    @staticmethod
    def imm(x):
        return x[3] if x[2] == "push" and (x[1] & 12) == 0 and (x[1] & 3) == 0 else None

    @staticmethod
    def push_addr(x):
        return x[2] == "push" and (x[1] & 12) == 0 and (x[1] & 3) == 3

    def args(self, k):
        out, j = [], k - 1
        while j >= 0 and (self.ins[j][2] == "push" or self.ctl(self.ins[j], 7)):
            if self.ins[j][2] == "push":
                out.append(self.ins[j])
            j -= 1
        return out  # top of stack first

    def _graph(self):
        starts, self.edges, self.roots = {self.entry, self.func_off}, {}, {}
        for k, x in enumerate(self.ins):
            if self.ctl(x, 1):
                starts.add(x[3])
                self.edges.setdefault(x[3], set()).add((x[0], "call"))
            if self.ctl(x, 11) and x[3] == CREATE_TASK and k and self.push_addr(self.ins[k - 1]):
                starts.add(self.ins[k - 1][3])
                self.edges.setdefault(self.ins[k - 1][3], set()).add((x[0], "spawn"))
            if self.ctl(x, 11) and x[3] == SET_EVENT_TABLE:
                a = self.args(k)
                if len(a) >= 2 and self.push_addr(a[0]) and self.imm(a[1]) is not None:
                    for i in range(self.imm(a[1])):
                        fn = struct.unpack_from("<I", self.b, a[0][3] + 4 * i)[0]
                        if fn:
                            starts.add(fn)
                            self.roots.setdefault(fn, f"MAPEV[{i}]")
            if self.ctl(x, 0) and k + 1 < len(self.ins) and self.ctl(self.ins[k + 1], 3):
                starts.add(self.ins[k + 1][0])
        self.roots.setdefault(self.func_off, "BASE")
        self.starts = sorted(s for s in starts if s in self.at)

    def fn_of(self, pc):
        best = self.entry
        for s in self.starts:
            if s <= pc:
                best = s
        return best

    def fn_end(self, fn):
        later = [s for s in self.starts if s > fn]
        return later[0] if later else 1 << 31

    def callers(self, fn):
        return sorted({(self.fn_of(pc), pc, kind) for pc, kind in self.edges.get(fn, ())})

    def jz_target(self, x):
        return x[0] + 1 + (0, 1, 2, 4)[(x[1] >> 4) & 3] + x[3]

    def show(self, x):
        v = self.imm(x)
        if self.push_addr(x) and self.data_off <= x[3] < self.whole - 12:
            f = struct.unpack_from("<3f", self.b, x[3])
            if all(abs(c) < 1e5 for c in f):
                return "vec(" + ", ".join(f"{c:.2f}" for c in f) + ")"
            return f"@{x[3]:#x}"
        if v is not None:
            raw = struct.pack("<I", v & MASK)
            if v > 0xFFFFFF and all(0x20 < c < 0x7F for c in raw):
                return "'" + raw.decode() + "'"
            return str(v)
        if x[2] == "push":
            return {4: "ws", 8: "ws2", 12: "fp"}.get(x[1] & 12, "addr") + f"[{x[3]}]"
        return "?"

    def symbolic(self, j):
        k = j - 1
        while k >= 0 and not (self.ins[k][2] == "ctl" and (self.ins[k][1] & 15) in (0, 1, 4, 5, 6)):
            k -= 1
        stack, acc = [], "?"
        for x in self.ins[k + 1:j]:
            kind, op, val = x[2], x[1], x[3]
            if kind == "push":
                cat = op & 12
                stack.append(str(val) if cat == 0 and (op & 3) == 0 else {0: "addr", 4: "ws", 8: "ws2", 12: "fp"}[cat] + f"[{val}]")
            elif kind == "alu":
                if val == 0:
                    acc = stack.pop() if stack else "?"
                elif val == 1:
                    stack.append(acc)
                elif val == 14 and stack:
                    stack[-1] = f"!({stack[-1]})"
                elif val in (2, 3, 4) and stack:
                    stack[-1] = f"*{stack[-1]}"
                elif val in (5, 6, 7) and len(stack) >= 2:
                    r = stack.pop()
                    stack.pop()
                    stack.append(r)
                elif val in BIN and len(stack) >= 2:
                    r, l = stack.pop(), stack.pop()
                    stack.append(f"({l} {BIN[val]} {r})")
                else:
                    return None
            elif self.ctl(x, 11) or self.ctl(x, 10) or self.ctl(x, 12):
                acc = f"game{val}"
                if self.ctl(x, 11) and val == SCEN_UTILS and len(stack) >= 2 and stack[-1] in ("11", "13", "15"):
                    acc = CMD[int(stack[-1])] + stack[-2]
            elif self.ctl(x, 3):
                for _ in range(max(0, val // 4)):
                    if stack:
                        stack.pop()
            elif not self.ctl(x, 7):
                return None
        return acc

    def conditions(self, fn, site):
        out = []
        for j, x in enumerate(self.ins):
            if not (fn <= x[0] < site) or not self.ctl(x, 6):
                continue
            target = self.jz_target(x)
            if x[0] < site < target:
                need = True
            elif x[0] < target <= site:
                tb = self.at.get(target)
                prev = self.ins[tb - 1] if tb else None
                if prev and (self.ctl(prev, 5) or self.ctl(prev, 4)) and self.jz_target(prev) > site:
                    need = False
                else:
                    continue
            else:
                continue
            e = self.symbolic(j)
            if e and re.search(r"\b[FBS]\d", e) or (e and "ws" in e):
                out.append((e, need))
        return out


def name_flags(expr):
    return re.sub(r"\bF(\d+)", lambda m: f"F{m.group(1)}" + (f"<{FLAGS[int(m.group(1))]}>" if int(m.group(1)) in FLAGS else ""), expr)


def qte_sites(s):
    for k, x in enumerate(s.ins):
        if s.ctl(x, 12) and x[3] == 2:
            a = s.args(k)
            if len(a) >= 6 and s.imm(a[5]) == 0:
                yield x[0], a


def cmd_chain(s, q):
    fn, site, seen = s.fn_of(q), q, set()
    while fn not in seen:
        seen.add(fn)
        print(f"fn {fn:#x} {s.roots.get(fn, '')} callers={len(s.callers(fn))}")
        for e, n in s.conditions(fn, site):
            print(f"     {'' if n else 'NOT '}{name_flags(e)}")
        up = s.callers(fn)
        if not up:
            break
        fn, site, _ = up[0]


def cmd_calls(s, fn):
    print(f"== fn {fn:#x} {s.roots.get(fn, '')}")
    for k, x in enumerate(s.ins):
        if fn <= x[0] < s.fn_end(fn) and (s.ctl(x, 11) or s.ctl(x, 12) or s.ctl(x, 1)):
            if s.ctl(x, 1):
                print(f"   {x[0]:#x} call {x[3]:#x}")
                continue
            kind = "SHENMUE" if s.ctl(x, 11) else "SCNF"
            slot = x[3] if kind == "SHENMUE" else x[3] * 2
            print(f"   {x[0]:#x} {SLOTS.get((kind, slot), f'{kind}{slot}')}(" + ", ".join(s.show(a) for a in reversed(s.args(k))) + ")")


def cmd_dis(s, lo, hi):
    for pc, op, kind, val in s.ins:
        if not lo <= pc < hi:
            continue
        if kind == "ctl":
            t = f"{NAMES[op & 15]} {val}"
            if op & 15 in (5, 6):
                t += f"  -> {pc + 1 + (0, 1, 2, 4)[(op >> 4) & 3] + val:#x}"
        elif kind == "push":
            t = f"push.{ {0: 'imm' if (op & 3) == 0 else 'addr', 4: 'ws', 8: 'ws2', 12: 'frame'}[op & 12]}.{op & 3} {val}"
        else:
            t = ALU.get(val, f"alu{val}")
        print(f"{pc:#07x}: {op:02x}  {t}")


def main():
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "extract":
        extract(Path(rest[0]), Path(rest[1]), Path(rest[2]))
        return
    s = Script(rest[0])
    hexes = [int(v, 16) for v in rest[1:] if ":" not in v]
    if cmd == "qtes":
        for pc, a in qte_sites(s):
            print(f"qte@{pc:#x} fn@{s.fn_of(pc):#x} args(mask,timeout,mode,bonus)=" + ", ".join(s.show(x) for x in a[:4]))
    elif cmd == "chain":
        cmd_chain(s, hexes[0])
    elif cmd == "calls":
        for fn in hexes:
            cmd_calls(s, fn)
    elif cmd == "dis":
        cmd_dis(s, hexes[0], hexes[1])
    elif cmd == "callers":
        for caller, pc, kind in s.callers(hexes[0]):
            print(f"<- {kind} {pc:#x} in fn {caller:#x} {s.roots.get(caller, '')}")
    elif cmd == "cond":
        for pair in rest[1:]:
            fn, site = (int(v, 16) for v in pair.split(":"))
            print(f"fn {fn:#x} site {site:#x}:")
            for e, n in s.conditions(fn, site):
                print(f"    {'' if n else 'NOT '}{name_flags(e)}")


if __name__ == "__main__":
    main()
