#!/usr/bin/env python3
"""Decompile Shenmue I scenario bytecode (SCN3 .scn) to plain C.

  scn_decomp.py all [scn dir] [out dir]       every scene + scn.h + scn_api.json + report.txt (+ clang syntax check)
  scn_decomp.py scene <file.scn> [out dir]    one scene; uses <out dir>/scn_api.json from a previous 'all' run if present

Semantics follow SCN_Parse in src/s1/task/scen_vm.cpp. The C dialect (names that encode addresses,
evaluation order, FOURCC, as_float/as_int, Say) is documented at the top of the generated scn.h.
"""
import bisect
import collections
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import scn

HERE = Path(__file__).resolve().parent
DISPATCH = scn.REPO / "src/s1/task/scenario_dispatch.def"

# ---------------------------------------------------------------- engine tables


def _door_names():
    out = {}
    for line in DISPATCH.read_text().splitlines():
        m = re.match(r"SCENARIO_DOOR_SLOT\((\d+), (\w+)\)", line)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def _native_names():
    raw = {}
    for (table, slot), name in scn.SLOTS.items():
        if table == "SHENMUE":
            raw[("S", slot)] = name
        elif slot % 2 == 0:
            raw[("F", slot // 2)] = name
    for slot, name in _door_names().items():
        raw[("D", slot)] = name
    count = collections.Counter(raw.values())
    return {k: (v if count[v] == 1 else "%s_%d" % (v, k[1])) for k, v in raw.items()}


NATIVE = _native_names()
TABLE = {"S": "SHENMUE", "F": "SCNF", "D": "DOOR"}
FLAG_NAMES = {k: "F_" + v for k, v in scn.FLAGS.items() if re.fullmatch(r"\w+", v)}
SET_GET_FLAG = (11, 12)


def native_name(kind, slot):
    return NATIVE.get((kind, slot)) or {"S": "Script_%d", "F": "SCNF_%d", "D": "Door_%d"}[kind] % slot


# ---------------------------------------------------------------- ALU

CMP = {8: "==", 9: "!=", 10: "<", 11: "<=", 12: ">", 13: ">="}
FCMP = {31: "<", 32: "<=", 33: ">", 34: ">="}
ARITH = {18: "+", 19: "-", 20: "*", 21: "/", 22: "%", 23: "<<", 24: ">>"}
BIT = {15: "&", 16: "|", 17: "^"}
FARITH = {35: "+", 36: "-", 37: "*", 38: "/"}
BINARY = {**CMP, **FCMP, **ARITH, **BIT, **FARITH}
PREC = {"*": 13, "/": 13, "%": 13, "+": 12, "-": 12, "<<": 11, ">>": 11, "<": 10, "<=": 10, ">": 10, ">=": 10,
        "==": 9, "!=": 9, "&": 8, "^": 7, "|": 6}
FLIP = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
WIDTH_TYPE = {1: "s8", 2: "s16", 4: "s32"}


def trunc4(v):
    return int(v / 4)


def float_literal(v):
    raw = struct.pack("<i", v)
    f = struct.unpack("<f", raw)[0]
    if f != f or f in (float("inf"), float("-inf")):
        return None
    for p in range(1, 10):
        s = "%.*g" % (p, f)
        if struct.pack("<f", float(s)) == raw:
            break
    if "e" not in s and "." not in s:
        s += ".0"
    return s + "f"


def plausible_float(v):
    if v == 0 or -0x10000 < v < 0x10000 or fourcc(v):
        return False
    f = struct.unpack("<f", struct.pack("<i", v))[0]
    return f == f and 1e-5 < abs(f) < 1e7


def fourcc(v):
    raw = struct.pack("<i", v)
    return (v & 0xFFFFFFFF) > 0xFFFFFF and all(c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in raw) and any(65 <= c <= 90 for c in raw)


def int_literal(v):
    if fourcc(v):
        return "FOURCC('%s')" % struct.pack("<i", v).decode()
    if -0x10000 < v < 0x10000:
        return str(v)
    if v == -0x80000000:
        return "(-0x7fffffff - 1)"
    return "-0x%x" % -v if v < 0 else "0x%x" % v


# ---------------------------------------------------------------- expression nodes

class N:
    """Expression node. op: c const, fn function, dat data object, ld load of ref, ad address of ref, dr deref,
    as assign, b binary (v = ALU code), not, itof, ftoi, call (v = callee), lnot, undef."""
    __slots__ = ("op", "k", "v", "w", "seq")
    counter = 0

    def __init__(self, op, k=(), v=None, w=4):
        self.op, self.k, self.v, self.w = op, list(k), v, w
        N.counter += 1
        self.seq = N.counter

    def become(self, other):
        self.op, self.k, self.v, self.w = other.op, other.k, other.v, other.w


CONSTLIKE = {"c", "fn", "dat", "ad", "undef"}


def pure(e):
    return e.op not in ("as", "call") and all(pure(x) for x in e.k)


def side_parts(e):
    if e.op in ("as", "call"):
        return [e]
    out = []
    for x in e.k:
        out += side_parts(x)
    return out


def walk(e):
    yield e
    for x in e.k:
        yield from walk(x)


def is_cbool(e):
    if e.op == "b":
        if e.v in CMP or e.v in FCMP:
            return True
        if e.v in BIT:
            return is_cbool(e.k[0]) and is_cbool(e.k[1])
        return False
    if e.op == "not":
        return is_cbool(e.k[0])
    return e.op == "lnot"


# ---------------------------------------------------------------- type inference


class Types:
    """Union-find over type keys with evidence counts: strong/weak float, int, pointer."""
    IDX = {("f", 1): 0, ("i", 1): 1, ("p", 1): 2, ("f", 0): 3, ("i", 0): 4, ("p", 0): 5}

    def __init__(self):
        self.parent, self.ev = {}, {}

    def find(self, k):
        p = self.parent.setdefault(k, k)
        if p == k:
            return k
        r = self.find(p)
        self.parent[k] = r
        return r

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.parent[rb] = ra
        ea, eb = self.ev.setdefault(ra, [0] * 6), self.ev.pop(rb, [0] * 6)
        for i in range(6):
            ea[i] += eb[i]

    def add(self, k, cls, strong, weight=1):
        r = self.find(k)
        self.ev.setdefault(r, [0] * 6)[self.IDX[(cls, 1 if strong else 0)]] += weight

    def evidence(self, k):
        return self.ev.get(self.find(k), [0] * 6)

    def cls(self, k):
        if k is None:
            return None
        sf, si, sp, wf, wi, wp = self.evidence(k)
        if sp and sp >= sf and sp >= si:
            return "p"
        if sf and sf >= si:
            return "f"
        if si:
            return "i"
        if wp > wf and wp > wi:
            return "p"
        if wf > wi:
            return "f"
        return "i"


# ---------------------------------------------------------------- variables


class Var:
    __slots__ = ("name", "off", "size", "ew", "array", "key")

    def __init__(self, name, off, size, ew, array, key):
        self.name, self.off, self.size, self.ew, self.array, self.key = name, off, size, ew, array, key


def layout(acc, end, name, key, absorb_limit=16):
    """acc: off -> {'w': set, 'ad': bool, 'idx': bool}. Returns sorted list of Var."""
    starts = sorted(acc)
    out, i = [], 0
    while i < len(starts):
        s = starts[i]
        info = acc[s]
        ew = max(info["w"]) if info["w"] else 4
        j = i + 1
        if info["ad"] or info["idx"]:
            while j < len(starts) and starts[j] - s < absorb_limit and not acc[starts[j]]["ad"] and acc[starts[j]]["w"] <= {4}:
                j += 1
        nxt = starts[j] if j < len(starts) else end
        size = (nxt - s) if nxt is not None else None
        array = (info["ad"] or info["idx"] or j > i + 1) and (size is None or (size > ew and size % ew == 0))
        if size is not None and size < ew:
            ew = 2 if size >= 2 else 1
        out.append(Var(name(s), s, size, ew, bool(array), key(s)))
        i = j
    return out


# ---------------------------------------------------------------- blocks


class Block:
    def __init__(self, a, b, addr):
        self.a, self.b, self.addr = a, b, addr
        self.term, self.t, self.fall = "fall", None, None
        self.succ, self.preds = [], []
        self.retonly = False
        self.live_in = False
        self.stack_in = 0
        self.stmts, self.cond, self.ret_val = [], None, None
        self.idx = 0


# ---------------------------------------------------------------- scene


class Scene:
    def __init__(self, path, engine_cls=None):
        self.path = Path(path)
        self.name = self.path.stem
        self.s = s = scn.Script(path)
        self.b = s.b
        self.hdr = scn.HDR.unpack_from(s.b, 0)
        self.code_end = min(s.data_off, s.whole)
        self.ins = s.ins
        self.at = s.at
        self.types = Types()
        self.engine_cls = engine_cls or {}
        self.anom = collections.Counter()
        self.anom_ex = collections.defaultdict(list)
        self.native_counts = collections.defaultdict(collections.Counter)
        self.globals = {"G4": {}, "G8": {}}
        self.data_refs = set()
        self.string_refs = set()
        self.event_tables = {}
        self.engine_ev = collections.defaultdict(lambda: [0] * 6)
        self.flows = []
        starts = [x for x in s.starts if x < self.code_end]
        self.fn_ranges = {}
        for i, st in enumerate(starts):
            a = s.at[st]
            b = s.at[starts[i + 1]] if i + 1 < len(starts) else len(self.ins)
            self.fn_ranges[st] = (a, b)
        self.prepass()
        self.funcs = {}
        for st in starts:
            f = Func(self, st)
            f.analyze()
            self.funcs[st] = f
        self.layout_globals()
        self.layout_data()
        for f in self.funcs.values():
            f.unify_arrays()

    def note(self, kind, where):
        self.anom[kind] += 1
        if len(self.anom_ex[kind]) < 5:
            self.anom_ex[kind].append(where)

    def end_pc(self, k):
        return self.ins[k + 1][0] if k + 1 < len(self.ins) else self.code_end

    # call graph facts needed before simulating any function
    def prepass(self):
        ins, ctl = self.ins, scn.Script.ctl
        self.call_counts = collections.defaultdict(list)
        self.callers = collections.defaultdict(set)
        self.spawned = collections.defaultdict(set)
        used, explicit = set(), set()
        ret_calls = collections.defaultdict(set)
        fn_of = {}
        for st, (a, b) in self.fn_ranges.items():
            for k in range(a, b):
                fn_of[k] = st
        self.fn_of_index = fn_of
        for k, x in enumerate(ins):
            if k not in fn_of:
                continue
            nxt = ins[k + 1] if k + 1 < len(ins) else None
            if ctl(x, 1):
                count = trunc4(nxt[3]) if nxt is not None and ctl(nxt, 3) and nxt[3] > 0 else 0
                self.call_counts[x[3]].append(count)
                self.callers[x[3]].add(fn_of[k])
                j = k + 2 if count else k + 1
                if j < len(ins) and (ins[j][2] == "alu" and ins[j][3] == 1 or ctl(ins[j], 6)):
                    used.add(x[3])
            if ctl(x, 11) and x[3] == scn.CREATE_TASK and k and self.s.push_addr(ins[k - 1]):
                self.spawned[ins[k - 1][3]].add(fn_of[k])
            if ctl(x, 11) and x[3] == scn.SET_EVENT_TABLE:
                a = self.s.args(k)
                if len(a) >= 2 and self.s.push_addr(a[0]) and self.s.imm(a[1]) is not None:
                    self.event_tables[a[0][3]] = self.s.imm(a[1])
            is_ret = ctl(x, 0) or (x[2] == "ctl" and (x[1] & 15) in (4, 5) and self.jump_target(k) in self.at and ctl(ins[self.at[self.jump_target(k)]], 0))
            if is_ret and k >= 1:
                p = ins[k - 1]
                if p[2] == "alu" and p[3] == 0 and not (k >= 2 and ins[k - 2][2] == "alu" and ins[k - 2][3] in (5, 6, 7)):
                    explicit.add(fn_of[k])
                q = ins[k - 2] if k >= 2 and ctl(p, 3) else p
                if ctl(q, 1):
                    ret_calls[fn_of[k]].add(q[3])
        value = set(used) | explicit
        changed = True
        while changed:
            changed = False
            for f in list(value):
                for g in ret_calls.get(f, ()):
                    if g not in value:
                        value.add(g)
                        changed = True
        self.value_fns = value

    def jump_target(self, k):
        x = self.ins[k]
        if (x[1] & 15) == 4:
            return x[3]
        return x[0] + 1 + (0, 1, 2, 4)[(x[1] >> 4) & 3] + x[3]

    # ------------------------------------------------------------ globals and data

    def global_access(self, cat, off, w, mode):
        d = self.globals[cat].setdefault(off, {"w": set(), "ad": False, "idx": False})
        if mode == "ad":
            d["ad"] = True
        elif mode == "idx":
            d["idx"] = True
        else:
            d["w"].add(w)

    def layout_globals(self):
        self.gvars = {}
        for cat, prefix in (("G4", "ws"), ("G8", "ws2")):
            vs = layout(self.globals[cat], None, lambda o, p=prefix: "%s_%x" % (p, o), lambda o, c=cat: (self.name, c, o))
            self.gvars[cat] = (vs, [v.off for v in vs])
            for v in vs:
                for o in self.globals[cat]:
                    if v.off <= o and (v.size is None or o < v.off + v.size) and o != v.off and self.globals[cat][o]["w"] <= {4}:
                        self.types.union(v.key, (self.name, cat, o))

    def layout_data(self):
        s = self.s
        bounds = set(r for r in self.data_refs if s.data_off <= r < s.whole) | {s.data_off}
        for i in (5, 6, 7, 9):
            if s.data_off <= self.hdr[i] < s.whole:
                bounds.add(self.hdr[i])
        for t in self.event_tables:
            bounds.add(t)
        bounds = sorted(bounds)
        self.data = []
        for i, a in enumerate(bounds):
            e = bounds[i + 1] if i + 1 < len(bounds) else s.whole
            self.data.append(self.classify_data(a, e))
        self.data_at = {d["addr"]: d for d in self.data}
        self.data_starts = [d["addr"] for d in self.data]

    def classify_data(self, a, e):
        raw = self.b[a:e]
        size = len(raw)
        d = {"addr": a, "size": size}
        if a in self.event_tables and size % 4 == 0:
            words = struct.unpack_from("<%di" % (size // 4), raw)
            d.update(kind="events", name="events_%x" % a, ew=4, words=words)
            return d
        nul = raw.find(b"\0")
        if nul >= 1 and all(c == 0 for c in raw[nul:]) or (a in self.string_refs and nul >= 0 and all(c == 0 for c in raw[nul:])):
            try:
                text = raw[:nul].decode("cp932")
                ok = text.encode("cp932") == raw[:nul] and all(c.isprintable() or c in "\n\r\t" for c in text)
            except UnicodeDecodeError:
                ok = False
            if ok and (a in self.string_refs or any(c.isalnum() for c in text)):
                d.update(kind="str", name="str_%x" % a, ew=1, text=text)
                return d
        if size % 4 == 0 and size:
            words = struct.unpack_from("<%di" % (size // 4), raw)
            floats = all(w == 0 or plausible_float(w) for w in words) and any(w for w in words)
            d.update(kind="float" if floats else "int", name=("vec_%x" if floats else "tab_%x") % a, ew=4, words=words)
            return d
        d.update(kind="bytes", name="bin_%x" % a, ew=1)
        return d

    def data_obj(self, addr):
        i = bisect.bisect_right(self.data_starts, addr) - 1
        return self.data[i] if i >= 0 else None

    def gvar(self, cat, off):
        vs, offs = self.gvars[cat]
        i = bisect.bisect_right(offs, off) - 1
        if i >= 0 and (vs[i].size is None or off < vs[i].off + vs[i].size):
            return vs[i]
        return None

    # ------------------------------------------------------------ rendering

    def render(self):
        self.r = Render(self)
        return self.r.file_text()


# ---------------------------------------------------------------- function analysis


class Func:
    def __init__(self, scene, addr):
        self.sc, self.addr = scene, addr
        self.a, self.b = scene.fn_ranges[addr]
        self.ins = scene.ins
        self.name = "fn_%x" % addr
        self.value = addr in scene.value_fns
        self.frame_acc = {}
        self.args_used = 0
        self.temps = []
        self.uses_acc_var = False
        self.tree = None
        self.flat = False
        x = self.ins[self.a]
        self.prologue = x[2] == "ctl" and (x[1] & 15) == 3 and x[3] <= 0
        self.frame = -x[3] if self.prologue else 0

    def key(self, ref):
        if ref[0] == "L":
            return (self.sc.name, self.addr, "L", ref[1])
        if ref[0] == "T":
            return (self.sc.name, self.addr, "T", ref[1])
        return (self.sc.name, ref[0], ref[1])

    def note(self, kind, pc):
        self.sc.note(kind, "%s:%x" % (self.sc.name, pc))

    # ------------------------------------------------------------ CFG

    def build_blocks(self):
        ins, sc = self.ins, self.sc
        leaders = {self.a}
        for k in range(self.a, self.b):
            x = ins[k]
            if x[2] != "ctl":
                continue
            low = x[1] & 15
            if low in (5, 6, 4):
                t = sc.jump_target(k)
                if t not in sc.at or not (self.a <= sc.at[t] < self.b):
                    self.note("jump_out_of_function", x[0])
                else:
                    leaders.add(sc.at[t])
                leaders.add(k + 1)
            elif low == 0:
                leaders.add(k + 1)
        leaders = sorted(i for i in leaders if i < self.b)
        self.blocks = []
        self.by_addr = {}
        for i, a in enumerate(leaders):
            b = leaders[i + 1] if i + 1 < len(leaders) else self.b
            blk = Block(a, b, ins[a][0])
            self.blocks.append(blk)
            self.by_addr[blk.addr] = blk
        for blk in self.blocks:
            last = ins[blk.b - 1]
            low = last[1] & 15 if last[2] == "ctl" else None
            fall = sc.end_pc(blk.b - 1)
            if low == 0:
                blk.term = "ret"
            elif low in (4, 5):
                blk.term, blk.t = "jmp", sc.jump_target(blk.b - 1)
            elif low == 6:
                blk.term, blk.t, blk.fall = "jz", sc.jump_target(blk.b - 1), fall
            else:
                blk.term, blk.fall = "fall", fall
            for t in (blk.t, blk.fall):
                if t is not None:
                    if t in self.by_addr and self.by_addr[t] not in blk.succ:
                        blk.succ.append(self.by_addr[t])
        seen, todo = set(), [self.blocks[0]]
        while todo:
            blk = todo.pop()
            if blk.addr in seen:
                continue
            seen.add(blk.addr)
            todo.extend(blk.succ)
        self.reach = [b for b in self.blocks if b.addr in seen]
        for i, blk in enumerate(self.reach):
            blk.idx = i
            for s in blk.succ:
                s.preds.append(blk)
        self.unreachable = sum(b.b - b.a for b in self.blocks if b.addr not in seen)
        for blk in self.reach:
            real = [k for k in range(blk.a, blk.b) if ins[k][2] != "nop"]
            blk.retonly = blk is not self.reach[0] and len(real) == 1 and blk.term == "ret"

    def heights(self):
        ins = self.ins
        h_in = {self.reach[0].addr: 0}
        order = list(self.reach)
        changed = True
        while changed:
            changed = False
            for blk in order:
                if blk.addr not in h_in:
                    continue
                h = h_in[blk.addr]
                for k in range(blk.a, blk.b):
                    if k == self.a and self.prologue:
                        continue
                    x = ins[k]
                    pc, op, kind, val = x
                    if kind == "push":
                        h += 1
                    elif kind == "alu":
                        if val == 0 or val in (5, 6, 7) or val in BINARY:
                            h -= 1
                        elif val == 1:
                            h += 1
                    elif kind == "ctl" and (op & 15) == 3:
                        h -= trunc4(val)
                if blk.term == "ret":
                    continue
                for s in blk.succ:
                    if s.addr not in h_in:
                        h_in[s.addr] = h
                        changed = True
                    elif h_in[s.addr] != h:
                        self.note("stack_height_conflict", s.addr)
        for blk in self.reach:
            blk.stack_in = max(0, h_in.get(blk.addr, 0))

    def liveness(self):
        ins = self.ins
        first = {}
        for blk in self.reach:
            ev = None
            for k in range(blk.a, blk.b):
                x = ins[k]
                pc, op, kind, val = x
                if kind == "alu" and val == 0:
                    ev = "def"
                elif kind == "alu" and val == 1:
                    ev = "use"
                elif kind == "ctl" and (op & 15) == 6:
                    ev = "use"
                elif kind == "ctl" and (op & 15) in (1, 2, 9, 10, 11, 12, 13):
                    ev = "def"
                elif kind == "ctl" and (op & 15) == 0:
                    ev = "use" if self.value else "def"
                if ev:
                    break
            first[blk.addr] = ev
        live = {b.addr: first[b.addr] == "use" for b in self.reach}
        changed = True
        while changed:
            changed = False
            for blk in self.reach:
                if live[blk.addr] or first[blk.addr] == "def":
                    continue
                if any(live[s.addr] for s in blk.succ):
                    live[blk.addr] = True
                    changed = True
        for blk in self.reach:
            blk.live_in = live[blk.addr]

    # ------------------------------------------------------------ simulation

    def analyze(self):
        self.build_blocks()
        self.heights()
        self.liveness()
        if self.reach[0].live_in:
            self.note("acc_read_before_set", self.addr)
        for blk in self.reach:
            if not blk.retonly:
                self.sim(blk)
        self.tree = Struct(self).run()
        if self.tree is None:
            self.flat = True
            self.sc.note("flat_fallback", "%s:%x" % (self.sc.name, self.addr))
            self.tree = Struct(self).flat()
        self.drop_unused_temps()

    def temp(self):
        name = "t%d" % len(self.temps)
        self.temps.append(name)
        return name

    def tload(self, name):
        if name == "acc":
            self.uses_acc_var = True
        return N("ld", v=("T", name))

    def frame_access(self, off, w, mode):
        if off >= 8:
            self.args_used = max(self.args_used, (off - 8) // 4 + 1)
            if (off - 8) % 4 or w != 4 and mode != "ad":
                pass
        d = self.frame_acc.setdefault(off, {"w": set(), "ad": False, "idx": False})
        if mode == "ad":
            d["ad"] = True
        elif mode == "idx":
            d["idx"] = True
        else:
            d["w"].add(w)
        if 0 <= off < 8:
            self.note("frame_header_access", self.addr)

    def access(self, ref, w, mode):
        if ref[0] == "L":
            self.frame_access(ref[1], w, mode)
        elif ref[0] in ("G4", "G8"):
            self.sc.global_access(ref[0], ref[1], w, mode)

    # type helpers -------------------------------------------------
    def key_of(self, e):
        if e.op == "ld":
            return self.key(e.v) if e.w == 4 else None
        if e.op == "as":
            return self.key_of(e.k[0])
        if e.op == "call":
            return self.call_key(e.v, "R")
        if e.op == "dr" and e.w == 4:
            base = self.base_ref(e.k[0])
            return self.key(base) if base else None
        return None

    def base_ref(self, p):
        if p.op == "ad":
            return p.v
        if p.op == "b" and p.v in (18, 19) and p.k[0].op == "ad":
            return p.k[0].v
        return None

    def call_key(self, callee, i):
        kind, slot = callee
        if kind == "U":
            if i == "R":
                return (self.sc.name, slot, "R")
            return (self.sc.name, slot, "L", 8 + 4 * i)
        if kind == "SAY":
            return None
        return ("N", kind, slot, i)

    def fixed(self, e):
        if e.op in ("fn", "dat", "ad"):
            return "p", True
        if e.op in ("ld", "dr") and e.w < 4:
            return "i", True
        if e.op == "b":
            if e.v in FARITH:
                return "f", True
            if e.v in (18, 19) and (self.fixed(e.k[0])[0] == "p"):
                return "p", False
            return "i", False
        if e.op == "itof":
            return "f", True
        if e.op in ("ftoi", "not", "lnot"):
            return "i", False
        return None, False

    def engine_fixed(self, key, e):
        ev = self.sc.engine_ev[key]
        if e.op == "c":
            if plausible_float(e.v):
                ev[Types.IDX[("f", 0)]] += 1
            elif e.v != 0:
                ev[Types.IDX[("i", 1)]] += 1
            return
        cls, strong = self.fixed(e)
        if cls:
            ev[Types.IDX[(cls, 1 if strong else 0)]] += 1

    def link(self, key, e):
        if key is None:
            return
        ke = self.key_of(e)
        if key[0] == "N":
            known = self.sc.engine_cls.get(key)
            if known:
                self.demand(e, known, True)
            if ke is not None and ke[0] != "N":
                self.sc.flows.append((key, ke))
            elif ke is None:
                self.engine_fixed(key, e)
            return
        if ke is not None and ke[0] == "N":
            known = self.sc.engine_cls.get(ke)
            if known:
                self.sc.types.add(key, known, True)
            self.sc.flows.append((ke, key))
            return
        if ke is not None:
            self.sc.types.union(key, ke)
            return
        if e.op == "c":
            if plausible_float(e.v):
                self.sc.types.add(key, "f", False)
            elif e.v != 0:
                self.sc.types.add(key, "i", False)
            return
        cls, strong = self.fixed(e)
        if cls:
            self.sc.types.add(key, cls, strong)

    def demand(self, e, cls, strong):
        k = self.key_of(e)
        if k is None:
            return
        if k[0] == "N":
            self.sc.engine_ev[k][Types.IDX[(cls, 1 if strong else 0)]] += 1
            return
        self.sc.types.add(k, cls, strong)

    def demand_ptr(self, e):
        if e.op == "b" and e.v in (18, 19):
            l, r = e.k
            self.demand_ptr(l if l.op != "c" else r)
            return
        self.demand(e, "p", True)

    # simulation ---------------------------------------------------
    def sim(self, blk):
        self.st = [self.tload("s%x_%d" % (blk.addr, i)) for i in range(blk.stack_in)]
        for i in range(blk.stack_in):
            n = "s%x_%d" % (blk.addr, i)
            if n not in self.temps:
                self.temps.append(n)
        self.out = blk.stmts
        self.acc = self.tload("acc") if blk.live_in else None
        self.acc_def = False
        self.pending = False
        self.consumed = False
        ins, sc = self.ins, self.sc
        k = blk.a
        while k < blk.b:
            x = ins[k]
            pc, op, kind, val = x
            if k == self.a and self.prologue:
                k += 1
                continue
            if kind == "nop":
                k += 1
                continue
            if kind == "push":
                self.st.append(self.push_node(op, val))
                k += 1
                continue
            if kind == "alu":
                self.alu(val, pc)
                k += 1
                continue
            low = op & 15
            if low == 3:
                n = trunc4(val)
                if n > 0:
                    if n > len(self.st):
                        self.note("stack_underflow", pc)
                    popped = [self.pop(pc) for _ in range(min(n, len(self.st)))]
                    for e in reversed(popped):
                        if not pure(e):
                            self.emit(e)
                elif n < 0:
                    self.note("stack_alloc_mid_function", pc)
                    self.st.extend(N("undef") for _ in range(-n))
                k += 1
                continue
            if low == 7:
                k += 1
                continue
            if low in (1, 9, 10, 11, 12, 13):
                self.flush_acc()
                nxt = ins[k + 1] if k + 1 < blk.b else None
                has_sp = nxt is not None and nxt[2] == "ctl" and (nxt[1] & 15) == 3 and nxt[3] > 0
                if low == 12:
                    prv = ins[k - 1]
                    count = prv[3] if prv[2] == "ctl" and (prv[1] & 15) == 7 else 0
                    if has_sp and trunc4(nxt[3]) != count:
                        self.note("scnf_argc_mismatch", pc)
                else:
                    count = trunc4(nxt[3]) if has_sp else 0
                args = [self.pop(pc) for _ in range(count)]
                if low == 1:
                    callee = ("U", val)
                elif low in (9, 10):
                    callee = ("L" if low == 9 else "M", val)
                    self.note("legacy_or_mem_call", pc)
                else:
                    callee = ({11: "S", 12: "F", 13: "D"}[low], val)
                    sc.native_counts[callee].update([count])
                for i, a in enumerate(args):
                    self.link(self.call_key(callee, i), a)
                node = N("call", args, v=callee)
                self.set_acc(node)
                k += 2 if has_sp else 1
                continue
            if low == 8:
                sc.string_refs.add(val)
                sc.data_refs.add(val)
                self.emit(N("call", [N("dat", v=val)], v=("SAY", 0)))
                k += 1
                continue
            if low == 2:
                self.note("indirect_call", pc)
                k += 1
                continue
            if low == 6:
                if self.acc is None:
                    self.note("condition_undefined", pc)
                    self.acc = N("undef")
                blk.cond = self.acc
                self.pending = False
                self.consumed = True
                k += 1
                continue
            if low in (0, 4, 5):
                k += 1
                continue
            k += 1
        self.finish(blk)

    def push_node(self, op, val):
        cat, sub = op & 12, op & 3
        if cat == 0:
            if sub == 0:
                return N("c", v=val)
            if val < self.sc.code_end:
                return N("fn", v=val)
            self.sc.data_refs.add(val)
            return N("dat", v=val)
        ref = ("L", val) if cat == 12 else ("G4", val) if cat == 4 else ("G8", val)
        if sub == 3:
            self.access(ref, 4, "ad")
            return N("ad", v=ref)
        w = {0: 1, 1: 2, 2: 4}[sub]
        self.access(ref, w, "ld")
        return N("ld", v=ref, w=w)

    def pop(self, pc):
        if not self.st:
            self.note("stack_underflow", pc)
            return N("undef")
        return self.st.pop()

    def deref(self, p, w, mode):
        if p.op == "ad":
            self.access(p.v, w, mode)
            return N("ld", v=p.v, w=w)
        base = self.base_ref(p)
        if base is not None:
            off = p.k[1]
            if off.op == "c":
                delta = off.v if p.v == 18 else -off.v
                ref = (base[0], base[1] + delta)
                self.access(ref, w, mode)
                return N("ld", v=ref, w=w)
            self.access(base, w, "idx")
        if p.op not in ("ad", "dat"):
            self.demand_ptr(p)
        return N("dr", [p], w=w)

    def alu(self, v, pc):
        st = self.st
        if v == 0:
            self.flush_acc()
            e = self.pop(pc)
            self.set_acc(e)
            return
        if v == 1:
            self.push_acc(pc)
            return
        if v in (2, 3, 4):
            st.append(self.deref(self.pop(pc), {2: 1, 3: 2, 4: 4}[v], "ld"))
            return
        if v in (5, 6, 7):
            w = {5: 1, 6: 2, 7: 4}[v]
            val = self.pop(pc)
            addr = self.pop(pc)
            lhs = self.deref(addr, w, "st")
            self.link(self.key_of(lhs), val)
            st.append(N("as", [lhs, val], w=w))
            return
        if v == 14:
            e = self.pop(pc)
            if not is_cbool(e):
                self.demand(e, "i", True)
            st.append(N("not", [e]))
            return
        if v in (29, 30):
            e = self.pop(pc)
            self.demand(e, "i" if v == 29 else "f", True)
            st.append(N("itof" if v == 29 else "ftoi", [e]))
            return
        if v in BINARY:
            r = self.pop(pc)
            l = self.pop(pc)
            if v in FARITH or v in FCMP:
                self.demand(l, "f", True)
                self.demand(r, "f", True)
            elif v in (18, 19):
                if self.fixed(l)[0] != "p":
                    self.demand(l, "i", False)
                self.demand(r, "i", False)
            elif v in ARITH or v in BIT or v in (10, 11, 12, 13):
                for x in (l, r):
                    if not (v in BIT and is_cbool(x)):
                        self.demand(x, "i", True)
            st.append(N("b", [l, r], v=v))
            return
        self.note("alu_noop_%d" % v, pc)

    def set_acc(self, e):
        self.acc = e
        self.acc_def = True
        self.pending = not pure(e)
        self.consumed = False

    def flush_acc(self):
        if self.pending:
            self.pending = False
            self.emit(self.acc, own=True)

    def push_acc(self, pc):
        if self.acc is None:
            self.note("acc_undefined", pc)
            self.st.append(N("undef"))
            return
        if pure(self.acc):
            self.st.append(self.acc)
            return
        if self.pending:
            self.pending = False
            self.consumed = True
            self.st.append(self.acc)
            return
        t = self.materialize(self.acc)
        self.acc = t
        self.st.append(self.tload(t.v[1]))

    def materialize(self, node, name=None):
        name = name or self.temp()
        copy = N(node.op, node.k, node.v, node.w)
        copy.seq = node.seq
        tl = self.tload(name)
        node.become(N("as", [self.tload(name), copy]))
        self.link(self.key(("T", name)), copy)
        return tl

    def emit(self, e, own=False):
        s = e.seq
        if not own and self.pending and self.acc is not e and self.acc.seq < s:
            self.pending = False
            name = self.temp()
            a = self.acc
            self.link(self.key(("T", name)), a)
            self.spill_before(a.seq)
            self.out.append(("s", N("as", [self.tload(name), a])))
            self.acc = self.tload(name)
        self.spill_before(s)
        for part in side_parts(e):
            self.out.append(("s", part))

    def spill_before(self, s):
        cands = [(e.seq, i) for i, e in enumerate(self.st) if e.op not in CONSTLIKE and e.seq < s]
        for i, e in enumerate(self.st):
            if e.seq > s and any(x.op in ("ld", "dr", "call", "as") and x.seq < s for x in walk(e)):
                self.note("evaluation_order_risk", self.addr)
        for _, i in sorted(cands):
            e = self.st[i]
            name = self.temp()
            self.link(self.key(("T", name)), e)
            self.out.append(("s", N("as", [self.tload(name), e])))
            self.st[i] = self.tload(name)

    def finish(self, blk):
        succs = [s for s in blk.succ]
        need_var = blk.term != "ret" and any(s.live_in and not s.retonly for s in succs)
        need_ret = self.value and (blk.term == "ret" or any(s.retonly for s in succs))
        if need_var and self.acc_def:
            self.assign_acc_var()
        if need_ret:
            if self.acc is None:
                self.note("return_value_undefined", blk.addr)
                blk.ret_val = N("undef")
            elif pure(self.acc):
                blk.ret_val = self.acc
            elif self.pending:
                self.pending = False
                self.spill_before(self.acc.seq)
                blk.ret_val = self.acc
            else:
                blk.ret_val = self.materialize(self.acc)
                self.acc = blk.ret_val
            self.link((self.sc.name, self.addr, "R"), blk.ret_val)
        self.flush_acc()
        if blk.term != "ret" and self.st:
            for s in succs:
                if s.retonly:
                    continue
                for i, e in enumerate(self.st[:s.stack_in]):
                    name = "s%x_%d" % (s.addr, i)
                    self.link(self.key(("T", name)), e)
                    self.out.append(("s", N("as", [self.tload(name), e])))
        if blk.term == "jz" and blk.cond is not None and self.acc is blk.cond and self.pending:
            self.pending = False

    def assign_acc_var(self):
        a = self.acc
        if a.op == "ld" and a.v == ("T", "acc"):
            return
        if self.pending:
            self.pending = False
            self.link(self.key(("T", "acc")), a)
            self.spill_before(a.seq)
            self.out.append(("s", N("as", [self.tload("acc"), a])))
        elif self.consumed and not pure(a):
            self.materialize(a, "acc")
        else:
            self.link(self.key(("T", "acc")), a)
            self.out.append(("s", N("as", [self.tload("acc"), a])))
        self.acc = self.tload("acc")
        self.consumed = False

    def drop_unused_temps(self):
        reads = collections.Counter()

        def scan_expr(e, lhs=False):
            if e.op == "ld" and e.v[0] == "T" and not lhs:
                reads[e.v[1]] += 1
            if e.op == "as":
                scan_expr(e.k[0], True)
                scan_expr(e.k[1])
                return
            for x in e.k:
                scan_expr(x)

        def scan(nodes):
            for n in nodes:
                kind = n[0]
                if kind == "s" or kind == "ret" and n[1] is not None:
                    scan_expr(n[1])
                elif kind == "if":
                    scan_expr(n[1])
                    scan(n[2])
                    scan(n[3] or [])
                elif kind == "loop":
                    if n[2] is not None:
                        scan_expr(n[2])
                    scan(n[3])
                elif kind == "ifnot":
                    scan_expr(n[1])
                    scan(n[2])

        scan(self.tree)

        def fix(nodes):
            out = []
            for n in nodes:
                if n[0] == "s" and n[1].op == "as" and n[1].k[0].op == "ld" and n[1].k[0].v[0] == "T" and not reads[n[1].k[0].v[1]]:
                    if not pure(n[1].k[1]):
                        out.extend(("s", p) for p in side_parts(n[1].k[1]))
                    continue
                if n[0] == "if":
                    n = ("if", n[1], fix(n[2]), fix(n[3]) if n[3] is not None else None)
                elif n[0] == "loop":
                    n = ("loop", n[1], n[2], fix(n[3]))
                elif n[0] == "ifnot":
                    n = ("ifnot", n[1], fix(n[2]))
                out.append(n)
            return out

        self.tree = fix(self.tree)
        for e in self.all_exprs():
            for x in walk(e):
                if x.op == "as" and x.k[0].op == "ld" and x.k[0].v[0] == "T" and not reads[x.k[0].v[1]] and x is not None:
                    x.become(x.k[1])
        self.temps = [t for t in self.temps if reads[t]]
        self.uses_acc_var = reads["acc"] > 0 or self.uses_acc_var and reads["acc"] > 0

    def all_exprs(self):
        def gen(nodes):
            for n in nodes:
                if n[0] in ("s", "ret") and n[1] is not None:
                    yield n[1]
                elif n[0] == "if":
                    yield n[1]
                    yield from gen(n[2])
                    yield from gen(n[3] or [])
                elif n[0] == "loop":
                    if n[2] is not None:
                        yield n[2]
                    yield from gen(n[3])
                elif n[0] == "ifnot":
                    yield n[1]
                    yield from gen(n[2])
        return list(gen(self.tree))

    def unify_arrays(self):
        acc = {o: d for o, d in self.frame_acc.items() if o < 0}
        self.locals = layout(acc, 0, lambda o: "local_%x" % -o, lambda o: self.key(("L", o)))
        self.local_offs = [v.off for v in self.locals]
        for v in self.locals:
            for o, d in acc.items():
                if v.off < o < v.off + (v.size or 0) and d["w"] <= {4}:
                    self.sc.types.union(v.key, self.key(("L", o)))

    def local_var(self, off):
        i = bisect.bisect_right(self.local_offs, off) - 1
        if i >= 0 and off < self.locals[i].off + self.locals[i].size:
            return self.locals[i]
        return None


# ---------------------------------------------------------------- structuring


class Struct:
    """Turn a function's CFG (blocks in address order) into if/else, loops, break/continue, return and goto."""

    def __init__(self, f):
        self.f = f
        self.R = f.reach
        self.by_addr = f.by_addr
        self.reach_set = {b.addr for b in self.R}
        self.emitted = []

    def run(self):
        try:
            tree = self.region(0, len(self.R), [], None, None)
        except RecursionError:
            return None
        want = [b.addr for b in self.R if not b.retonly]
        if sorted(self.emitted) != sorted(want):
            return None
        return tree

    def idx(self, addr):
        return self.by_addr[addr].idx if addr in self.reach_set else None

    def ret_from(self, blk):
        return ("ret", blk.ret_val if self.f.value else None)

    def jump(self, t, loops, follow, src):
        blk = self.by_addr.get(t)
        if blk is None:
            self.f.note("falls_into_next_function", src.addr)
            return [("ret", N("undef") if self.f.value else None)]
        if blk.retonly:
            return [self.ret_from(src)]
        if t == follow:
            return []
        if loops:
            head, exit_ = loops[-1]
            if t == head:
                return [("cont",)]
            if t == exit_:
                return [("brk",)]
        return [("goto", t)]

    def next_emitted(self, i, hi):
        j = i + 1
        while j < hi and self.R[j].retonly:
            j += 1
        return j

    def region(self, lo, hi, loops, follow, skip_head):
        R, out = self.R, []
        i = lo
        while i < hi:
            b = R[i]
            if b.retonly:
                i += 1
                continue
            if b.addr != skip_head:
                latch = max((p.idx for p in b.preds if i <= p.idx < hi and p.addr >= b.addr), default=None)
                if latch is not None:
                    exit_ = R[latch + 1].addr if latch + 1 < hi else follow
                    body = self.region(i, latch + 1, loops + [(b.addr, exit_)], b.addr, b.addr)
                    out.extend(self.mkloop(body))
                    i = latch + 1
                    if self.next_emitted(latch, hi) >= hi and exit_ is not None and exit_ != follow and not self.by_addr[exit_].retonly:
                        out += self.jump(exit_, loops, follow, R[latch])
                    continue
            self.emitted.append(b.addr)
            out.append(("label", b.addr))
            out.extend(b.stmts)
            j = self.next_emitted(i, hi)
            at_end = j >= hi
            nxt = None if at_end else R[j].addr
            here_follow = follow if at_end else None
            if b.term == "ret":
                out.append(self.ret_from(b))
            elif b.term in ("fall", "jmp"):
                t = b.fall if b.term == "fall" else b.t
                if at_end or t != nxt:
                    out += self.jump(t, loops, here_follow, b)
            elif b.term == "jz":
                t, cond = b.t, b.cond
                tb = self.by_addr.get(t)
                ti = self.idx(t)
                fb = self.by_addr.get(b.fall)
                fall_seq = [] if (not at_end and b.fall == nxt) else self.jump(b.fall, loops, here_follow, b)
                inside = ti is not None and i < ti <= hi
                structured = tb is not None and not tb.retonly and t > b.addr and (inside or t == follow)
                if t == b.fall:
                    if not pure(cond):
                        out.append(("s", cond))
                    out += fall_seq
                elif fb is not None and fb.retonly and tb is not None and not tb.retonly:
                    out.append(("if", cond, [self.ret_from(b)], None))
                    if at_end or t != nxt:
                        out += self.jump(t, loops, here_follow, b)
                elif not structured:
                    body = self.jump(t, loops, here_follow, b)
                    if body:
                        out.append(("ifnot", cond, body))
                    elif not pure(cond):
                        out.append(("s", cond))
                    out += fall_seq
                else:
                    then_hi = ti if inside else hi
                    last = next((R[k] for k in range(then_hi - 1, i, -1) if not R[k].retonly), None)
                    els = None
                    if last is not None and last.term == "jmp" and last.t > t:
                        e = last.t
                        eb = self.by_addr.get(e)
                        if eb is not None and not eb.retonly:
                            ei = self.idx(e)
                            if ei is not None and then_hi < ei <= hi:
                                els = (ei, e)
                            elif e == follow and then_hi < hi:
                                els = (hi, e)
                    if els:
                        ei, e = els
                        then = self.region(i + 1, then_hi, loops, e, None)
                        other = self.region(then_hi, ei, loops, e, None)
                        out.append(("if", cond, then, other))
                        i = ei
                        if i >= hi and e != follow:
                            out += self.jump(e, loops, follow, last)
                        continue
                    then = self.region(i + 1, then_hi, loops, t, None)
                    out.append(("if", cond, then, None))
                    i = then_hi
                    if i >= hi and t != follow:
                        out += self.jump(t, loops, follow, b)
                    continue
            i += 1
        return out

    @staticmethod
    def break_test(n):
        if n[0] == "ifnot" and n[2] == [("brk",)]:
            return n[1]
        if n[0] == "if" and n[2] == [("brk",)] and n[3] is None:
            return N("lnot", [n[1]])
        return None

    def has_cont(self, nodes):
        for n in nodes:
            if n == ("cont",):
                return True
            if n[0] == "if" and (self.has_cont(n[2]) or self.has_cont(n[3] or [])):
                return True
            if n[0] == "ifnot" and self.has_cont(n[2]):
                return True
        return False

    def mkloop(self, body):
        while body and body[-1] == ("cont",):
            body.pop()
        n = 0
        while n < len(body) and body[n][0] == "label":
            n += 1
        lead, rest = body[:n], body[n:]
        if rest:
            stop = self.break_test(rest[0])
            if stop is not None:
                return lead + [("loop", "while", stop, rest[1:])]
        if len(rest) == 2 and rest[0][0] == "if" and rest[0][3] is None and rest[1] == ("brk",) and rest[0][2]:
            then = list(rest[0][2])
            if then[-1][0] in ("cont", "brk", "ret", "goto"):
                if then[-1] == ("cont",):
                    then.pop()
                return lead + [("loop", "while", rest[0][1], then)]
        if len(rest) >= 2 and rest[-2][0] == "ifnot" and rest[-2][2] == [("cont",)] and rest[-1] == ("brk",) and not self.has_cont(rest[:-2]):
            return [("loop", "do", N("lnot", [rest[-2][1]]), lead + rest[:-2])]
        if rest:
            stop = self.break_test(rest[-1])
            if stop is not None and not self.has_cont(rest[:-1]):
                return [("loop", "do", N("lnot", [stop]), lead + rest[:-1])]
        return [("loop", "for", None, body)]

    def flat(self):
        out = []
        for i, b in enumerate(self.R):
            if b.retonly:
                continue
            out.append(("label", b.addr))
            out.extend(b.stmts)
            j = self.next_emitted(i, len(self.R))
            nxt = self.R[j].addr if j < len(self.R) else None

            def go(t, b=b, nxt=nxt):
                if t == nxt:
                    return []
                return self.jump(t, [], None, b)

            if b.term == "ret":
                out.append(self.ret_from(b))
            elif b.term == "jmp":
                out += go(b.t)
            elif b.term == "fall":
                out += go(b.fall)
            else:
                out.append(("ifnot", b.cond, self.jump(b.t, [], None, b)))
                out += go(b.fall)
        return out


# ---------------------------------------------------------------- C rendering


class Render:
    def __init__(self, sc):
        self.sc = sc
        self.t = sc.types

    # classes -------------------------------------------------------
    def cls_key(self, k):
        if k is not None and k[0] == "N":
            return self.sc.engine_cls.get(k) or "i"
        return self.t.cls(k)

    def ecls(self, e, f):
        op = e.op
        if op in ("c", "undef"):
            return None
        if op in ("fn", "dat", "ad"):
            return "p"
        if op == "ld":
            if e.w < 4:
                return "i"
            return self.cls_key(f.key(e.v)) if e.v[0] != "L" or e.v[1] >= 8 or f.local_var(e.v[1]) is None else self.cls_key(f.local_var(e.v[1]).key)
        if op == "dr":
            if e.w < 4:
                return "i"
            c = self.element_cls(e, f)
            if c:
                return c
            k = f.key_of(e)
            return self.cls_key(k) if k else "i"
        if op == "as":
            return self.ecls(e.k[0], f)
        if op == "b":
            if e.v in FARITH:
                return "f"
            if e.v in (18, 19) and (self.ecls(e.k[0], f) == "p" or e.v == 18 and self.ecls(e.k[1], f) == "p"):
                return "p"
            return "i"
        if op == "itof":
            return "f"
        if op == "call":
            k = f.call_key(e.v, "R")
            if e.v[0] == "U" and e.v[1] not in self.sc.value_fns:
                return "i"
            return self.cls_key(k) if k else "i"
        return "i"

    def var_cls(self, var):
        return self.cls_key(var.key)

    # names -----------------------------------------------------------
    def ref_var(self, ref, f):
        kind, off = ref
        if kind == "T":
            return None
        if kind == "L":
            if off >= 8:
                return None
            return f.local_var(off)
        return self.sc.gvar(kind, off)

    def ref_text(self, ref, w, f, cls=None):
        """lvalue text; a cast path uses the value class for 4-byte accesses so C semantics keep the bits"""
        kind, off = ref
        ty = "float" if cls == "f" and w == 4 else "void *" if cls == "p" and w == 4 else WIDTH_TYPE.get(w, "u8")
        if kind == "T":
            return off
        if kind == "L" and off >= 8:
            i, r = divmod(off - 8, 4)
            name = "arg%d" % i
            if r == 0 and w == 4:
                return name
            return "*(%s *)((u8 *)&%s + %d)" % (ty, name, r) if r else "*(%s *)&%s" % (ty, name)
        if kind == "L" and off >= 0:
            return "*(%s *)__frame(%d)" % (ty, off)
        var = self.ref_var(ref, f)
        if var is None:
            prefix = {"L": "local", "G4": "ws", "G8": "ws2"}[kind]
            return "%s_%x" % (prefix, -off if kind == "L" else off)
        rel = off - var.off
        if not var.array and rel == 0 and w == var.ew:
            return var.name
        if var.array and w == var.ew and rel % w == 0:
            return "%s[%d]" % (var.name, rel // w)
        base = var.name if var.array else "&" + var.name
        return "*(%s *)((u8 *)%s + %d)" % (ty, base, rel) if rel else "*(%s *)%s" % (ty, base)

    def decl_type(self, var):
        if var.ew == 1:
            return "s8"
        if var.ew == 2:
            return "s16"
        return {"f": "float", "p": "void *", "i": "int"}[self.var_cls(var) or "i"]

    # expressions -----------------------------------------------------
    def conv(self, text, prec, have, want):
        if want is None or have is None or have == want:
            return text, prec
        inner = text
        if have == "f" and want == "i":
            return "as_int(%s)" % inner, 15
        if have == "i" and want == "f":
            return "as_float(%s)" % inner, 15
        if have == "i" and want == "p":
            return "(void *)%s" % self.par(text, prec, 14), 14
        if have == "p" and want == "i":
            return "(int)%s" % self.par(text, prec, 14), 14
        if have == "f" and want == "p":
            return "(void *)as_int(%s)" % inner, 14
        if have == "p" and want == "f":
            return "as_float((int)%s)" % self.par(text, prec, 14), 15
        return text, prec

    @staticmethod
    def par(text, prec, need):
        return text if prec >= need else "(" + text + ")"

    def const(self, v, want):
        if want == "f":
            s = float_literal(v)
            return (s, 16) if s and not s.startswith("-") else ((s, 14) if s else ("as_float(%s)" % int_literal(v), 15))
        if want == "p":
            return ("0", 16) if v == 0 else ("(void *)%s" % int_literal(v), 14)
        s = int_literal(v)
        return s, (14 if s.startswith("-") or s.startswith("(") else 16)

    def expr(self, e, f, want=None, cond=False):
        text, prec, have = self.ex(e, f, want, cond)
        if is_cbool(e) and not cond and e.op != "lnot" or e.op == "lnot" and not cond:
            text, prec, have = "-" + self.par(text, prec, 15), 14, "i"
        return self.conv(text, prec, have, want)

    def ex(self, e, f, want, cond):
        op = e.op
        if op == "undef":
            return "0 /* undefined */", 16, None
        if op == "c":
            t, p = self.const(e.v, want)
            return t, p, want
        if op == "fn":
            return "fn_%x" % e.v, 16, "p"
        if op == "dat":
            d = self.sc.data_obj(e.v)
            if d is None:
                return "(void *)0x%x /* file offset */" % e.v, 14, "p"
            rel = e.v - d["addr"]
            return (d["name"], 16, "p") if rel == 0 else ("((u8 *)%s + %d)" % (d["name"], rel), 16, "p")
        if op == "ld":
            if e.v[0] == "T":
                k = f.key(e.v)
                return e.v[1], 16, self.cls_key(k)
            text = self.ref_text(e.v, e.w, f, self.ecls(e, f))
            return text, (16 if not text.startswith("*") else 14), self.ecls(e, f)
        if op == "ad":
            ref = e.v
            if ref[0] == "T":
                return "&" + ref[1], 14, "p"
            var = self.ref_var(ref, f)
            if var is not None and var.array and ref[1] == var.off:
                return var.name, 16, "p"
            if var is not None and var.array:
                return "((u8 *)%s + %d)" % (var.name, ref[1] - var.off), 16, "p"
            text = self.ref_text(ref, 4 if var is None else var.ew, f)
            if text.startswith("*"):
                inner = text[text.index(")") + 1:].strip()
                return inner, 16, "p"
            return "&" + text, 14, "p"
        if op == "dr":
            return self.deref_text(e, f, want)
        if op == "as":
            lhs = e.k[0]
            rhs_cls = self.ecls(e.k[1], f)
            lcls = self.ecls(lhs, f) if lhs.op != "dr" else ("f" if rhs_cls == "f" else self.ecls(lhs, f))
            if lhs.op == "dr":
                ltext = self.deref_text(lhs, f, lcls)[0]
            else:
                ltext = self.ref_text(lhs.v, lhs.w, f, lcls)
            rt, rp = self.expr(e.k[1], f, lcls if e.w == 4 else "i")
            return "%s = %s" % (ltext, self.par(rt, rp, 2)), 2, lcls if e.w == 4 else "i"
        if op == "b":
            return self.binary(e, f, cond)
        if op == "not":
            if is_cbool(e.k[0]):
                t, p = self.expr(e.k[0], f, None, True)
                return "!" + self.par(t, p, 14), 14, "i"
            t, p = self.expr(e.k[0], f, "i")
            return "~" + self.par(t, p, 14), 14, "i"
        if op == "lnot":
            inner = e.k[0]
            if inner.op == "lnot":
                return self.ex(inner.k[0], f, want, True)
            if inner.op == "b" and inner.v in CMP:
                flipped = N("b", inner.k, v={v: k for k, v in CMP.items()}[FLIP[CMP[inner.v]]])
                return self.binary(flipped, f, True)
            if inner.op == "not" and is_cbool(inner.k[0]):
                return self.ex(inner.k[0], f, want, True)
            t, p = self.expr(inner, f, None, True)
            return "!" + self.par(t, p, 14), 14, "i"
        if op in ("itof", "ftoi"):
            t, p = self.expr(e.k[0], f, "i" if op == "itof" else "f")
            return ("(float)" if op == "itof" else "(int)") + self.par(t, p, 14), 14, ("f" if op == "itof" else "i")
        if op == "call":
            return self.call_text(e, f), 15, self.ecls(e, f)
        return "/* ? */0", 16, None

    def binary(self, e, f, cond):
        v = e.v
        sym = BINARY[v]
        prec = PREC[sym]
        l, r = e.k
        if v in FARITH or v in FCMP:
            lw = rw = "f"
        elif v in (8, 9):
            lc, rc = self.ecls(l, f), self.ecls(r, f)
            lw = rw = "f" if "f" in (lc, rc) else "p" if "p" in (lc, rc) else "i"
        elif v in BIT and is_cbool(l) and is_cbool(r):
            lt, lp = self.expr(l, f, None, True)
            rt, rp = self.expr(r, f, None, True)
            return "%s %s %s" % (self.par(lt, lp, 15), sym, self.par(rt, rp, 15)), prec, "i"
        elif v in (18, 19) and self.ecls(l, f) == "p":
            lt, lp = self.expr(l, f, "p")
            rt, rp = self.expr(r, f, "i")
            base = lt if lt.startswith("(u8 *)") else "(u8 *)" + self.par(lt, lp, 14)
            return "%s %s %s" % (base, sym, self.par(rt, rp, prec + 1)), prec, "p"
        elif v == 18 and self.ecls(r, f) == "p":
            lt, lp = self.expr(l, f, "i")
            rt, rp = self.expr(r, f, "p")
            return "(u8 *)%s + %s" % (self.par(rt, rp, 14), self.par(lt, lp, prec + 1)), prec, "p"
        else:
            lw = rw = "i"
        lt, lp = self.expr(l, f, lw)
        rt, rp = self.expr(r, f, rw)
        if v in BIT:
            need_l = need_r = 14
        elif v in (23, 24):
            need_l, need_r = 13, 13
        else:
            need_l, need_r = prec, prec + 1
        if v in CMP or v in FCMP:
            need_l = max(need_l, 11)
            need_r = max(need_r, 11)
        return "%s %s %s" % (self.par(lt, lp, need_l), sym, self.par(rt, rp, need_r)), prec, ("f" if v in FARITH else "i")

    def decompose(self, p):
        """address -> (base node, const offset, index node, scale) or None"""
        if p.op in ("ad", "dat"):
            return p, 0, None, 0
        if p.op == "b" and p.v in (18, 19):
            l, r = p.k
            d = self.decompose(l)
            if d is None:
                return None
            base, off, idx, scale = d
            if r.op == "c":
                return base, off + (r.v if p.v == 18 else -r.v), idx, scale
            if p.v == 18 and idx is None:
                if r.op == "b" and r.v == 23 and r.k[1].op == "c":
                    return base, off, r.k[0], 1 << r.k[1].v
                if r.op == "b" and r.v == 20 and r.k[1].op == "c":
                    return base, off, r.k[0], r.k[1].v
                return base, off, r, 1
        return None

    DATA_CLS = {"float": "f", "int": "i", "events": "p", "str": "i", "bytes": "i"}

    def element_cls(self, e, f):
        """class of a w=4 deref when it resolves to a data object or variable element, else None"""
        d = self.decompose(e.k[0])
        if d is None:
            return None
        base = d[0]
        if base.op == "dat":
            obj = self.sc.data_obj(base.v)
            return self.DATA_CLS[obj["kind"]] if obj is not None and obj["ew"] == 4 else None
        if base.op == "ad" and base.v[0] in ("L", "G4", "G8"):
            var = self.ref_var(base.v, f)
            if var is not None and var.ew == 4:
                return self.var_cls(var)
        return None

    def deref_text(self, e, f, want):
        p = e.k[0]
        w = e.w
        ty = "float" if want == "f" and w == 4 else ("void *" if want == "p" and w == 4 else WIDTH_TYPE[w])
        d = self.decompose(p)
        if d is not None:
            base, off, idx, scale = d
            name, ew, array, boff = None, None, True, 0
            if base.op == "dat":
                obj = self.sc.data_obj(base.v)
                if obj is not None:
                    name, ew, boff = obj["name"], obj["ew"], base.v - obj["addr"]
            elif base.op == "ad" and base.v[0] in ("L", "G4", "G8"):
                var = self.ref_var(base.v, f)
                if var is not None:
                    name, ew, array, boff = var.name, var.ew, var.array, base.v[1] - var.off
            if name is not None:
                rel = boff + off
                if array and ew == w and rel % w == 0 and (idx is None or scale == w):
                    if idx is None:
                        return "%s[%d]" % (name, rel // w), 15, self.ecls(e, f)
                    it, ip = self.expr(idx, f, "i")
                    k = rel // w
                    return ("%s[%s]" % (name, it) if k == 0 else "%s[%s + %d]" % (name, self.par(it, ip, 12), k)), 15, self.ecls(e, f)
        pt, pp = self.expr(p, f, "p")
        return "*(%s *)%s" % (ty, self.par(pt, pp, 15) if not pt.startswith("(u8 *)") else "(" + pt + ")"), 14, ("f" if ty == "float" else "p" if ty == "void *" else "i" if w < 4 else self.ecls(e, f))

    def param_cls(self, callee, i, f):
        if callee[0] == "SAY":
            return "p"
        k = f.call_key(callee, i)
        return self.cls_key(k)

    def callee_name(self, callee):
        kind, v = callee
        if kind == "U":
            return "fn_%x" % v
        if kind == "SAY":
            return "Say"
        if kind in ("L", "M"):
            return "%s_%d" % ("Legacy" if kind == "L" else "Mem", v)
        return native_name(kind, v)

    def call_text(self, e, f):
        callee = e.v
        args = []
        flag = callee == ("S", scn.SCEN_UTILS) and len(e.k) >= 2 and e.k[0].op == "c" and e.k[0].v in SET_GET_FLAG and e.k[1].op == "c" and e.k[1].v in FLAG_NAMES
        for i, a in enumerate(e.k):
            if flag and i == 1:
                args.append(FLAG_NAMES[a.v])
                continue
            t, p = self.expr(a, f, self.param_cls(callee, i, f))
            args.append(self.par(t, p, 3))
        return "%s(%s)" % (self.callee_name(callee), ", ".join(args))

    # statements ------------------------------------------------------
    def stmts(self, nodes, f, ind, out, labels):
        pad = "    " * ind
        for n in nodes:
            kind = n[0]
            if kind == "label":
                if n[1] in labels:
                    out.append("    " * max(ind - 1, 0) + "L_%x:" % n[1])
                continue
            if kind == "s":
                out.append(pad + self.expr(n[1], f)[0] + ";")
            elif kind == "ret":
                if n[1] is None or not f.value:
                    out.append(pad + "return;")
                else:
                    t, _ = self.expr(n[1], f, self.cls_key((self.sc.name, f.addr, "R")))
                    out.append(pad + "return %s;" % t)
            elif kind == "brk":
                out.append(pad + "break;")
            elif kind == "cont":
                out.append(pad + "continue;")
            elif kind == "goto":
                out.append(pad + "goto L_%x;" % n[1])
            elif kind == "ifnot":
                c = self.expr(N("lnot", [n[1]]), f, None, True)[0]
                body = n[2]
                if len(body) == 1 and body[0][0] in ("brk", "cont", "goto", "ret"):
                    tmp = []
                    self.stmts(body, f, 0, tmp, labels)
                    out.append(pad + "if (%s) %s" % (c, tmp[0].strip()))
                else:
                    out.append(pad + "if (%s) {" % c)
                    self.stmts(body, f, ind + 1, out, labels)
                    out.append(pad + "}")
            elif kind == "if":
                self.if_stmt(n, f, ind, out, labels, "if")
            elif kind == "loop":
                _, lk, c, body = n
                if lk == "for":
                    out.append(pad + "for (;;) {")
                elif lk == "while":
                    out.append(pad + "while (%s) {" % self.expr(c, f, None, True)[0])
                else:
                    out.append(pad + "do {")
                self.stmts(body, f, ind + 1, out, labels)
                self.close_label(out, pad)
                out.append(pad + ("} while (%s);" % self.expr(c, f, None, True)[0] if lk == "do" else "}"))

    @staticmethod
    def close_label(out, pad):
        if out and out[-1].rstrip().endswith(":") and out[-1].lstrip().startswith("L_"):
            out.append(pad + "    ;")

    def if_stmt(self, n, f, ind, out, labels, kw):
        pad = "    " * ind
        _, c, then, other = n
        if not [x for x in then if x[0] != "label"] and other:
            c, then, other = N("lnot", [c]), other, then
            if not [x for x in other if x[0] != "label"] and not [x for x in other if x[0] == "label" and x[1] in labels]:
                other = None
        jumps = ("brk", "cont", "goto", "ret")
        if kw == "if" and other:
            real_o = [x for x in other if x[0] != "label"]
            real_t = [x for x in then if x[0] != "label"]
            no_lab = not any(x[0] == "label" and x[1] in labels for x in other)
            if no_lab and len(real_o) == 1 and real_o[0][0] in jumps and not (len(real_t) == 1 and real_t[0][0] in jumps):
                self.stmts([("ifnot", c, real_o)], f, ind, out, labels)
                self.stmts(then, f, ind, out, labels)
                return
            no_lab_t = not any(x[0] == "label" and x[1] in labels for x in then)
            if no_lab_t and len(real_t) == 1 and real_t[0][0] in jumps:
                self.stmts([("ifnot", N("lnot", [c]), real_t)], f, ind, out, labels)
                self.stmts(other, f, ind, out, labels)
                return
        ct = self.expr(c, f, None, True)[0]
        out.append(pad + ("} else if (%s) {" if kw == "elif" else "if (%s) {") % ct)
        self.stmts(then, f, ind + 1, out, labels)
        self.close_label(out, pad)
        if other:
            real = [x for x in other if x[0] != "label" or x[1] in labels]
            if len(real) == 1 and real[0][0] == "if":
                self.if_stmt(real[0], f, ind, out, labels, "elif")
                return
            out.append(pad + "} else {")
            self.stmts(other, f, ind + 1, out, labels)
            self.close_label(out, pad)
        out.append(pad + "}")

    # functions -------------------------------------------------------
    def param_count(self, f):
        counts = self.sc.call_counts.get(f.addr, [])
        n = max([f.args_used] + counts)
        kr = bool(counts) and min(counts) < n
        return n, kr

    def ret_type(self, f):
        if not f.value:
            return "void"
        return {"f": "float", "p": "void *", "i": "int"}[self.cls_key((self.sc.name, f.addr, "R")) or "i"]

    def arg_type(self, f, i):
        return {"f": "float", "p": "void *", "i": "int"}[self.cls_key(f.key(("L", 8 + 4 * i))) or "i"]

    def prototype(self, f):
        n, kr = self.param_count(f)
        if kr:
            return "%s %s();" % (self.ret_type(f), f.name)
        params = ", ".join("%s arg%d" % (self.arg_type(f, i), i) for i in range(n)) or "void"
        return "%s %s(%s);" % (self.ret_type(f), f.name, params)

    def func_text(self, f):
        n, kr = self.param_count(f)
        lines = []
        roots = []
        if f.addr == self.sc.s.entry:
            roots.append("entry point")
        if f.addr == self.sc.s.func_off:
            roots.append("BASE")
        if f.addr in self.sc.s.roots and self.sc.s.roots[f.addr].startswith("MAPEV"):
            roots.append(self.sc.s.roots[f.addr])
        callers = sorted(self.sc.callers.get(f.addr, ()))
        spawned = sorted(self.sc.spawned.get(f.addr, ()))
        info = ["0x%x" % f.addr]
        info += roots
        if callers:
            info.append("called by " + ", ".join("fn_%x" % c for c in callers[:8]) + (" ..." if len(callers) > 8 else ""))
        if spawned:
            info.append("thread started by " + ", ".join("fn_%x" % c for c in spawned[:8]))
        if f.flat:
            info.append("unstructured control flow")
        lines.append("/* %s */" % "; ".join(info))
        if kr:
            lines.append("%s %s(%s)" % (self.ret_type(f), f.name, ", ".join("arg%d" % i for i in range(n))))
            for i in range(n):
                lines.append("    %s arg%d;" % (self.arg_type(f, i), i))
        else:
            lines.append("%s %s(%s)" % (self.ret_type(f), f.name, ", ".join("%s arg%d" % (self.arg_type(f, i), i) for i in range(n)) or "void"))
        lines.append("{")
        for v in f.locals:
            ty = self.decl_type(v)
            if v.array:
                count = v.size // v.ew
                lines.append("    %s %s[%d];" % (ty, v.name, count) if not ty.endswith("*") else "    %s%s[%d];" % (ty, v.name, count))
            else:
                lines.append("    %s %s;" % (ty, v.name) if not ty.endswith("*") else "    %s%s;" % (ty, v.name))
        temps = list(f.temps)
        if f.uses_acc_var and "acc" not in temps:
            temps.append("acc")
        for t in temps:
            ty = {"f": "float", "p": "void *", "i": "int"}[self.cls_key(f.key(("T", t))) or "i"]
            lines.append("    %s %s;" % (ty, t) if not ty.endswith("*") else "    %s%s;" % (ty, t))
        labels = set()
        self.collect_gotos(f.tree, labels)
        body = []
        self.stmts(f.tree, f, 1, body, labels)
        while body and body[-1].strip() == "return;":
            body.pop()
        self.close_label(body, "")
        lines += body
        lines.append("}")
        return "\n".join(lines)

    def collect_gotos(self, nodes, out):
        for n in nodes:
            if n[0] == "goto":
                out.add(n[1])
            elif n[0] == "if":
                self.collect_gotos(n[2], out)
                self.collect_gotos(n[3] or [], out)
            elif n[0] == "loop":
                self.collect_gotos(n[3], out)
            elif n[0] == "ifnot":
                self.collect_gotos(n[2], out)

    # data ------------------------------------------------------------
    def c_string(self, text):
        out = []
        for ch in text:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "?":
                out.append("\\?")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'

    def data_text(self, d):
        size = d["size"]
        if d["kind"] == "str":
            return "static char %s[%d] = %s;" % (d["name"], size, self.c_string(d["text"]))
        if d["kind"] == "events":
            items = []
            for w in d["words"]:
                if w and w in self.sc.funcs:
                    items.append("fn_%x" % w)
                else:
                    items.append("(void *)%s" % int_literal(w) if w else "0")
            return "static void *%s[%d] = {%s};" % (d["name"], len(d["words"]), ", ".join(items))
        if d["kind"] == "float":
            return "static float %s[%d] = {%s};" % (d["name"], len(d["words"]), ", ".join(float_literal(w) or "0" for w in d["words"]))
        if d["kind"] == "int":
            items = []
            for w in d["words"]:
                s = int_literal(w)
                if plausible_float(w):
                    s += " /* %s */" % float_literal(w)
                items.append(s)
            return "static int %s[%d] = {%s};" % (d["name"], len(d["words"]), ", ".join(items))
        raw = self.sc.b[d["addr"]:d["addr"] + size]
        return "static u8 %s[%d] = {%s};" % (d["name"], size, ", ".join("0x%02x" % c for c in raw))

    def global_decl(self, cat, var):
        ty = self.decl_type(var)
        sep = "" if ty.endswith("*") else " "
        if var.array:
            n = "" if var.size is None or var.size // var.ew > 256 else str(var.size // var.ew)
            return "extern %s%s%s[%s];" % (ty, sep, var.name, n)
        return "extern %s%s%s;" % (ty, sep, var.name)

    def file_text(self):
        sc = self.sc
        out = ["/* %s.scn decompiled by scn_decomp.py. Plain C in the scenario dialect described in scn.h. */" % sc.name,
               '#include "scn.h"', ""]
        funcs = [sc.funcs[a] for a in sorted(sc.funcs)]
        out += [self.prototype(f) for f in funcs]
        out.append("")
        out.append("/* globals: ws_* and ws2_* name byte offsets in the scenario workspace (ws2 = ws + 0x100) */")
        for cat in ("G4", "G8"):
            for v in sc.gvars[cat][0]:
                out.append(self.global_decl(cat, v))
        out.append("")
        out.append("/* data segment, in file order (0x%x..0x%x) */" % (sc.s.data_off, sc.s.whole))
        for d in sc.data:
            out.append(self.data_text(d))
        out.append("")
        self.func_texts = {}
        for f in funcs:
            text = self.func_text(f)
            self.func_texts[f.addr] = text
            out.append(text)
            out.append("")
        return "\n".join(out)


# ---------------------------------------------------------------- outputs


HEADER_DOC = """/*
 * scn.h - declarations for decompiled Shenmue I scenario scripts (generated by scn_decomp.py).
 *
 * The scripts are plain C with these conventions, which the scenario compiler relies on:
 *  - int, float and pointers are 32 bits. Integer arithmetic wraps. Pointers are offsets in task memory.
 *  - Binary operands are evaluated left to right; call arguments right to left (last argument first).
 *  - Names encode addresses: local_<hex> is the frame slot at fp - hex, argN is fp + 8 + 4N,
 *    ws_<hex> / ws2_<hex> are byte offsets in the scenario workspace (ws2 starts at ws + 0x100).
 *    Data objects (str_/vec_/tab_/events_/bin_<hex>) are at that offset in the .scn file and keep file order.
 *  - FOURCC('JIMY') is the 32-bit id whose bytes in memory are 'J','I','M','Y'.
 *  - as_float/as_int reinterpret bits (no conversion); casts (float)/(int) convert.
 *  - Say(text) compiles to the dedicated string opcode (0x08), which runs SCNT_ShowSCNFSubtitleText.
 *  - SCNF functions are latent: the calling thread yields until they finish.
 */
#ifndef SCN_H
#define SCN_H

typedef signed char s8;
typedef short s16;
typedef int s32;
typedef unsigned char u8;

#define SCN_NATIVE(table, slot) /* table slot index in scenario_dispatch.def */
#define FOURCC(s) ((int)__builtin_bswap32(s))

float as_float(int bits);
int as_int(float value);
void Say(char *text);
"""


def header_text(api):
    lines = [HEADER_DOC, "enum scn_flag {"]
    for k in sorted(FLAG_NAMES):
        lines.append("    %s = %d," % (FLAG_NAMES[k], k))
    lines.append("};")
    lines.append("")
    ty = {"f": "float", "p": "void *", "i": "int"}
    for key in sorted(api, key=lambda k: ("SFD".index(k[0]), k[1])):
        kind, slot = key
        info = api[key]
        counts = sorted(info["counts"])
        ret = ty[info.get("ret", "i")]
        sep = "" if ret.endswith("*") else " "
        name = native_name(kind, slot)
        lo = counts[0] if counts else 0
        params = ["%s a%d" % (ty[info["params"].get(str(i), "i")], i) for i in range(lo)]
        if len(counts) > 1:
            decl = "%s%s%s(%s);" % (ret, sep, name, ", ".join(params + ["..."]) if params else "")
        else:
            decl = "%s%s%s(%s);" % (ret, sep, name, ", ".join(params) or "void")
        lines.append("SCN_NATIVE(%s, %d) %s  /* argument counts: %s */" % (TABLE[kind], slot, decl, ", ".join(map(str, counts))))
    lines += ["", "#endif", ""]
    return "\n".join(lines)


def collect_api(scenes_info):
    """scenes_info: list of (native_counts, types, engine_ev, flows) from pass 1."""
    counts = collections.defaultdict(set)
    ev = collections.defaultdict(lambda: [0] * 6)
    for native_counts, types, engine_ev, flows in scenes_info:
        for callee, c in native_counts.items():
            counts[callee] |= set(c)
        for k, e in engine_ev.items():
            for i in range(6):
                ev[k][i] += e[i]
        for nkey, vkey in flows:
            e = types.evidence(vkey)
            if not any(e):
                continue
            cls = types.cls(vkey)
            strong = e[Types.IDX[(cls, 1)]] > 0
            ev[nkey][Types.IDX[(cls, 1 if strong else 0)]] += 1
    api = {}
    for callee, c in counts.items():
        if callee[0] not in "SFD":
            continue
        tmp = Types()
        info = {"counts": sorted(c), "params": {}}
        for i in range(max(c) if c else 0):
            k = ("N", callee[0], callee[1], i)
            tmp.ev[k] = ev.get(k, [0] * 6)
            info["params"][str(i)] = tmp.cls(k)
        kr = ("N", callee[0], callee[1], "R")
        tmp.ev[kr] = ev.get(kr, [0] * 6)
        info["ret"] = tmp.cls(kr)
        api[callee] = info
    return api


def engine_classes(api):
    out = {}
    for (kind, slot), info in api.items():
        for i, c in info["params"].items():
            out[("N", kind, slot, int(i))] = c
        out[("N", kind, slot, "R")] = info["ret"]
    return out


def save_api(api, path):
    path.write_text(json.dumps({"%s:%d" % k: v for k, v in sorted(api.items())}, indent=1))


def load_api(path):
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return {(k.split(":")[0], int(k.split(":")[1])): v for k, v in raw.items()}


def sidecar(sc, r):
    funcs = {}
    for a, f in sorted(sc.funcs.items()):
        lo = sc.ins[f.a][0]
        hi = sc.end_pc(f.b - 1)
        funcs["%x" % a] = {"name": f.name, "start": lo, "end": hi, "bytes_sha1": hashlib.sha1(sc.b[lo:hi]).hexdigest(),
                           "c_sha1": hashlib.sha1(r.func_texts[a].encode()).hexdigest(), "flat": f.flat, "unreachable_instructions": f.unreachable}
    return {"scene": sc.name, "file_sha1": hashlib.sha1(sc.b).hexdigest(), "header": list(sc.hdr),
            "data": [{"name": d["name"], "addr": d["addr"], "size": d["size"], "kind": d["kind"]} for d in sc.data],
            "functions": funcs, "anomalies": dict(sc.anom)}


def find_clang():
    for c in ("clang", "clang.exe"):
        p = shutil.which(c)
        if p:
            return p
    for p in Path("C:/Program Files/Microsoft Visual Studio").glob("*/*/VC/Tools/Llvm/x64/bin/clang.exe"):
        return str(p)
    return None


def clang_check(out_dir, files):
    clang = find_clang()
    if not clang:
        return "clang not found, syntax check skipped"
    flags = ["-fsyntax-only", "-std=c17", "-ferror-limit=50", "-Wall", "-Wno-unused-variable", "-Wno-unused-label", "-Wno-unused-value",
             "-Wno-int-to-void-pointer-cast", "-Wno-void-pointer-to-int-cast", "-Wno-pointer-to-int-cast", "-Wno-bitwise-instead-of-logical",
             "-Wno-uninitialized", "-Wno-sometimes-uninitialized", "-Wno-deprecated-non-prototype", "-Wno-excess-initializers",
             "-Wno-unused-function", "-Wno-parentheses", "-Wno-unused-but-set-variable", "-Wno-pointer-sign", "-Wno-multichar",
             "-Wno-four-char-constants", "-Werror=implicit-function-declaration", "-Werror=int-conversion", "-Werror=incompatible-pointer-types",
             "-I", str(out_dir)]
    errors, warnings, bad, kinds = 0, 0, [], collections.Counter()
    for path in files:
        p = subprocess.run([clang] + flags + [str(path)], capture_output=True, text=True)
        text = p.stderr
        e = len(re.findall(r": error:", text))
        w = len(re.findall(r": warning:", text))
        for m in re.finditer(r": (error|warning): .*?\[(-W[\w-]+)\]", text):
            kinds[m.group(1) + " " + m.group(2)] += 1
        for m in re.finditer(r": error: (.*)", text):
            kinds["error: " + re.sub(r"'[^']*'", "'_'", m.group(1))[:80]] += 1
        errors += e
        warnings += w
        if p.returncode:
            bad.append((path.name, text.splitlines()[:4]))
    lines = ["clang %s: %d files, %d with errors, %d errors, %d warnings" % (clang, len(files), len(bad), errors, warnings),
             "  not counted by design: int<->pointer casts (32-bit offsets), & | on comparisons (VM evaluates both sides),",
             "  uninitialized locals (as in the original scripts), K&R definitions (callers disagree on argument count),",
             "  Shift-JIS strings longer in UTF-8 source than their cp932 size"]
    for k, v in kinds.most_common(25):
        lines.append("  %6d  %s" % (v, k))
    for name, head in bad[:10]:
        lines.append("  %s:" % name)
        lines += ["      " + h[:200] for h in head]
    return "\n".join(lines)


def run_all(scn_dir, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(scn_dir.glob("*.scn"))
    t0 = time.time()
    infos = []
    print("pass 1: engine signatures from %d scenes" % len(files), flush=True)
    for i, p in enumerate(files):
        sc = Scene(p)
        infos.append((dict(sc.native_counts), sc.types, dict(sc.engine_ev), sc.flows))
        if i % 20 == 19:
            print("  %d/%d  %.0fs" % (i + 1, len(files), time.time() - t0), flush=True)
    api = collect_api(infos)
    infos = None
    save_api(api, out_dir / "scn_api.json")
    (out_dir / "scn.h").write_text(header_text(api), encoding="utf-8")
    cls = engine_classes(api)
    print("pass 2: decompile", flush=True)
    total = collections.Counter()
    examples = collections.defaultdict(list)
    stats = collections.Counter()
    written = []
    for i, p in enumerate(files):
        sc = Scene(p, cls)
        text = sc.render()
        path = out_dir / (sc.name + ".c")
        path.write_text(text, encoding="utf-8", newline="\n")
        (out_dir / (sc.name + ".json")).write_text(json.dumps(sidecar(sc, sc.r), indent=1))
        written.append(path)
        total.update(sc.anom)
        for k, v in sc.anom_ex.items():
            examples[k] += v[:2]
        stats["functions"] += len(sc.funcs)
        stats["flat"] += sum(f.flat for f in sc.funcs.values())
        stats["gotos"] += sum(len(re.findall(r"\bgoto L_", sc.r.func_texts[a])) for a in sc.funcs)
        stats["functions_with_goto"] += sum(1 for a in sc.funcs if "goto L_" in sc.r.func_texts[a])
        stats["as_float/as_int"] += len(re.findall(r"\bas_(?:float|int)\(", text))
        stats["undefined"] += text.count("/* undefined */")
        stats["acc_var"] += sum(1 for f in sc.funcs.values() if f.uses_acc_var)
        stats["unreachable_instructions"] += sum(f.unreachable for f in sc.funcs.values())
        stats["lines"] += text.count("\n")
        if i % 20 == 19:
            print("  %d/%d  %.0fs" % (i + 1, len(files), time.time() - t0), flush=True)
    report = ["scn_decomp report  %s  %.0fs" % (time.strftime("%Y-%m-%d %H:%M"), time.time() - t0), ""]
    report += ["%-28s %d" % (k, v) for k, v in sorted(stats.items())]
    report += ["", "anomalies:"]
    report += ["  %-32s %6d  %s" % (k, v, ", ".join(examples[k][:4])) for k, v in total.most_common()]
    report += ["", clang_check(out_dir, written)]
    (out_dir / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))


def run_scene(path, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    api = load_api(out_dir / "scn_api.json")
    if api is None:
        sc = Scene(path)
        api = collect_api([(dict(sc.native_counts), sc.types, dict(sc.engine_ev), sc.flows)])
        (out_dir / "scn.h").write_text(header_text(api), encoding="utf-8")
    sc = Scene(path, engine_classes(api))
    text = sc.render()
    out = out_dir / (sc.name + ".c")
    out.write_text(text, encoding="utf-8", newline="\n")
    (out_dir / (sc.name + ".json")).write_text(json.dumps(sidecar(sc, sc.r), indent=1))
    print("%s: %d functions, %d flat, anomalies %s" % (out, len(sc.funcs), sum(f.flat for f in sc.funcs.values()), dict(sc.anom)))


def main():
    sys.setrecursionlimit(20000)
    args = sys.argv[1:]
    if not args or args[0] not in ("all", "scene"):
        print(__doc__)
        return 2
    if args[0] == "all":
        scn_dir = Path(args[1]) if len(args) > 1 else HERE / "scn"
        out_dir = Path(args[2]) if len(args) > 2 else HERE / "out" / "scn_c"
        run_all(scn_dir, out_dir)
    else:
        out_dir = Path(args[2]) if len(args) > 2 else HERE / "out" / "scn_c"
        run_scene(Path(args[1]), out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
