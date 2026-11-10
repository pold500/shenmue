#!/usr/bin/env python3
"""Compile Shenmue I scenario C (the scn_decomp.py dialect, see scn.h) back to SCN3 .scn bytecode.

  scn_compile.py build <scene.c> [--scn DIR] [--out DIR] [--rebuild] [--force]
      patch mode (default): start from the original .scn, keep unchanged functions byte for byte, compile changed
      ones in place when they fit, otherwise append them and leave a jump at the old address.
      --rebuild: lay out the whole file from the C source.  --force: recompile every function (patch mode).
  scn_compile.py check <scene.c>...        parse and compile without writing
  scn_compile.py status <src dir>          changed functions per scene (hash compare with the .json sidecar)
  scn_compile.py verify [--src DIR] [--scn DIR] [scene...]
      1) patch build of the unmodified source must equal the original file byte for byte
      2) rebuild from C, decompile the result again, compare every function's C text with the source

Semantics follow SCN_Parse (src/s1/task/scen_vm.cpp); the generated code uses the same statement patterns as the
original compiler (push address, value, store, pop to acc; args right to left; add sp after calls).
"""
import hashlib
import json
import re
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SCN = HERE / "scn"
DEFAULT_SRC = HERE / "work"
DEFAULT_OUT = HERE / "out" / "scn_build"
DECOMP_OUT = HERE / "out" / "scn_c"


class CompileError(Exception):
    def __init__(self, msg, where=None):
        self.msg, self.where = msg, where
        super().__init__(self.text())

    def text(self):
        if self.where:
            return "%s:%d: error: %s" % (self.where[0], self.where[1], self.msg)
        return "error: " + self.msg


# ================================================================= lexer

TOKEN_RE = re.compile(r"""
 (?P<ws>[ \t\r\n]+)
|(?P<lc>//[^\n]*)
|(?P<bc>/\*.*?\*/)
|(?P<pp>^[ \t]*\#[^\n]*)
|(?P<num>0[xX][0-9a-fA-F]+[uUlL]*|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFuUlL]*)
|(?P<id>[A-Za-z_]\w*)
|(?P<chr>'(?:[^'\\\n]|\\.)+')
|(?P<str>"(?:[^"\\\n]|\\.)*")
|(?P<op>\.\.\.|<<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|->|[{}()\[\];,:?=+\-*/%&|^~!<>.])
""", re.S | re.M | re.X)

ESCAPES = {"n": 10, "r": 13, "t": 9, "0": 0, "\\": 92, "'": 39, '"': 34, "?": 63, "a": 7, "b": 8, "f": 12, "v": 11}


class Tok:
    __slots__ = ("k", "v", "line", "pos", "end", "file")

    def __init__(self, k, v, line, pos, end, file):
        self.k, self.v, self.line, self.pos, self.end, self.file = k, v, line, pos, end, file

    def __repr__(self):
        return "%s:%r" % (self.k, self.v)


def unescape(body, where):
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        e = body[i]
        if e == "x":
            m = re.match(r"[0-9a-fA-F]{1,2}", body[i + 1:])
            out.append(chr(int(m.group(0), 16)))
            i += 1 + len(m.group(0))
        elif e in ESCAPES:
            out.append(chr(ESCAPES[e]))
            i += 1
        else:
            raise CompileError("unknown escape \\%s" % e, where)
    return "".join(out)


def tokenize(text, file, include_dirs, seen=None):
    seen = seen if seen is not None else set()
    toks = []
    line = 1
    pos = 0
    for m in TOKEN_RE.finditer(text):
        if m.start() != pos:
            bad = text[pos:m.start()]
            raise CompileError("unexpected character %r" % bad[:1], (file, line))
        kind = m.lastgroup
        val = m.group(0)
        if kind == "pp":
            inc = re.match(r"\s*#\s*include\s+\"([^\"]+)\"", val)
            if inc:
                name = inc.group(1)
                for d in include_dirs:
                    p = Path(d) / name
                    if p.exists():
                        if str(p.resolve()) not in seen:
                            seen.add(str(p.resolve()))
                            toks += tokenize(p.read_text(encoding="utf-8").replace("\r\n", "\n"), str(p), include_dirs, seen)
                        break
                else:
                    raise CompileError("cannot find include file %s" % name, (file, line))
        elif kind not in ("ws", "lc", "bc"):
            toks.append(Tok(kind, val, line, m.start(), m.end(), file))
        line += val.count("\n")
        pos = m.end()
    if pos != len(text):
        raise CompileError("unexpected character %r" % text[pos], (file, line))
    return toks


# ================================================================= types


class Ty:
    """base: int float s8 s16 u8 void fn; ptr: pointer depth; count: array length (None = not an array, 0 = unsized)."""
    __slots__ = ("base", "ptr", "count")

    def __init__(self, base, ptr=0, count=None):
        self.base, self.ptr, self.count = base, ptr, count

    def __eq__(self, o):
        return isinstance(o, Ty) and (self.base, self.ptr, self.count) == (o.base, o.ptr, o.count)

    def __repr__(self):
        return self.base + "*" * self.ptr + ("[%s]" % self.count if self.count is not None else "")

    @property
    def is_array(self):
        return self.count is not None

    @property
    def is_float(self):
        return self.base == "float" and self.ptr == 0 and self.count is None

    @property
    def is_ptr(self):
        return self.ptr > 0 or self.count is not None or self.base == "fn"

    def elem(self):
        if self.count is not None:
            return Ty(self.base, self.ptr)
        if self.ptr > 0:
            return Ty(self.base, self.ptr - 1)
        return INT

    def decay(self):
        return Ty(self.base, self.ptr + 1) if self.count is not None else self

    def size(self):
        if self.count is not None:
            return Ty(self.base, self.ptr).size() * max(self.count, 0)
        if self.ptr:
            return 4
        return {"s8": 1, "u8": 1, "s16": 2, "void": 1}.get(self.base, 4)

    def scalar_size(self):
        return 4 if self.ptr else {"s8": 1, "u8": 1, "s16": 2}.get(self.base, 4)


INT, FLOAT, VOID = Ty("int"), Ty("float"), Ty("void")
BASE_TYPES = {"void", "int", "float", "char", "short", "long", "signed", "unsigned", "s8", "s16", "s32", "u8", "const", "volatile"}
CORE_WORDS = BASE_TYPES - {"s8", "s16", "s32", "u8"}  # words that may follow another type word ("signed char", "short int")


def base_from_words(words):
    w = [x for x in words if x not in ("const", "volatile")]
    if "float" in w or "double" in w:
        return "float"
    if "void" in w:
        return "void"
    if "char" in w:
        return "u8" if "unsigned" in w else "s8"
    if "short" in w:
        return "s16"
    if len(w) == 1 and w[0] in ("s8", "s16", "u8"):
        return w[0]
    return "int"


# ================================================================= AST


class Node:
    __slots__ = ("k", "a", "b", "c", "where")

    def __init__(self, k, a=None, b=None, c=None, where=None):
        self.k, self.a, self.b, self.c, self.where = k, a, b, c, where


class FuncDef:
    def __init__(self, name, ret, params, variadic, prototyped, where):
        self.name, self.ret, self.params, self.variadic, self.prototyped, self.where = name, ret, params, variadic, prototyped, where
        self.body = None
        self.span = None
        self.native = None


class DataDef:
    def __init__(self, name, ty, init, where, static):
        self.name, self.ty, self.init, self.where, self.static = name, ty, init, where, static


# ================================================================= parser


class Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0
        self.typedefs = {"s8": "s8", "s16": "s16", "s32": "int", "u8": "u8"}
        self.funcs = {}
        self.order = []
        self.globals = {}
        self.data = []
        self.enums = {}

    # helpers
    def peek(self, n=0):
        j = self.i + n
        return self.t[j] if j < len(self.t) else Tok("eof", "", self.t[-1].line if self.t else 0, 0, 0, self.t[-1].file if self.t else "?")

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def at(self, v, n=0):
        tok = self.peek(n)
        return tok.k in ("op", "id") and tok.v == v

    def accept(self, v):
        if self.at(v):
            return self.next()
        return None

    def expect(self, v):
        tok = self.next()
        if tok.v != v or tok.k not in ("op", "id"):
            raise CompileError("expected '%s' but found '%s'" % (v, tok.v or "end of file"), (tok.file, tok.line))
        return tok

    def where(self, tok=None):
        tok = tok or self.peek()
        return (tok.file, tok.line)

    def is_type_start(self, n=0):
        tok = self.peek(n)
        return tok.k == "id" and (tok.v in BASE_TYPES or tok.v in self.typedefs)

    # types
    def type_base(self):
        words = []
        while self.peek().k == "id" and (self.peek().v in BASE_TYPES or self.peek().v in self.typedefs):
            if words and self.peek().v not in CORE_WORDS:
                break
            v = self.next().v
            if v in self.typedefs and v not in BASE_TYPES:
                words.append(self.typedefs[v])
            else:
                words.append(v)
            if words[-1] in ("s8", "s16", "u8", "int", "float", "void") and not self.is_type_start():
                break
        if not words:
            raise CompileError("expected a type, found '%s'" % self.peek().v, self.where())
        return base_from_words(words)

    def abstract_type(self):
        base = self.type_base()
        ptr = 0
        while self.accept("*"):
            ptr += 1
            while self.accept("const"):
                pass
        return Ty(base, ptr)

    # top level
    def program(self):
        while self.peek().k != "eof":
            self.top()
        return self

    def top(self):
        w = self.where()
        if self.accept(";"):
            return
        if self.at("typedef"):
            self.next()
            base = self.type_base()
            ptr = 0
            while self.accept("*"):
                ptr += 1
            name = self.next().v
            self.typedefs[name] = base if not ptr else "int"
            self.expect(";")
            return
        if self.at("enum"):
            self.next()
            if self.peek().k == "id" and not self.at("{"):
                self.next()
            self.expect("{")
            val = 0
            while not self.accept("}"):
                name = self.next().v
                if self.accept("="):
                    val = self.const_int(self.expr_cond())
                self.enums[name] = val
                val += 1
                if not self.accept(","):
                    self.expect("}")
                    break
            self.expect(";")
            return
        native = None
        if self.at("SCN_NATIVE"):
            self.next()
            self.expect("(")
            table = self.next().v
            self.expect(",")
            slot = int(self.next().v, 0)
            self.expect(")")
            native = (table, slot)
        storage = set()
        while self.peek().v in ("static", "extern", "inline") and self.peek().k == "id":
            storage.add(self.next().v)
        base = self.type_base()
        while True:
            ptr = 0
            while self.accept("*"):
                ptr += 1
            name_tok = self.next()
            if name_tok.k != "id":
                raise CompileError("expected a name, found '%s'" % name_tok.v, self.where(name_tok))
            name = name_tok.v
            if self.at("("):
                self.function(name, Ty(base, ptr), native, w, name_tok)
                return
            ty = Ty(base, ptr)
            if self.accept("["):
                if self.accept("]"):
                    ty = Ty(base, ptr, 0)
                else:
                    ty = Ty(base, ptr, self.const_int(self.expr()))
                    self.expect("]")
            init = None
            if self.accept("="):
                init = self.initializer()
            if "extern" in storage:
                self.globals[name] = (ty, self.where(name_tok))
            else:
                self.data.append(DataDef(name, ty, init, self.where(name_tok), "static" in storage))
            if self.accept(","):
                continue
            self.expect(";")
            return

    def initializer(self):
        if self.accept("{"):
            items = []
            while not self.accept("}"):
                items.append(self.expr_assign())
                if not self.accept(","):
                    self.expect("}")
                    break
            return Node("list", items)
        return self.expr_assign()

    def function(self, name, ret, native, w, name_tok):
        name_idx = self.i - 1
        self.expect("(")
        params, variadic, prototyped, kr_names = [], False, True, []
        if self.accept(")"):
            prototyped = False
        elif self.at("void") and self.at(")", 1):
            self.next()
            self.next()
        elif self.peek().k == "id" and not self.is_type_start():
            while True:
                kr_names.append(self.next().v)
                if not self.accept(","):
                    break
            self.expect(")")
            prototyped = False
        else:
            while True:
                if self.accept("..."):
                    variadic = True
                    break
                ty = self.abstract_type()
                pname = self.next().v if self.peek().k == "id" else "a%d" % len(params)
                if self.accept("["):
                    self.accept_until("]")
                    ty = Ty(ty.base, ty.ptr + 1)
                params.append((ty, pname))
                if not self.accept(","):
                    break
            self.expect(")")
        f = self.funcs.get(name)
        if f is None:
            f = FuncDef(name, ret, params, variadic, prototyped, w)
            self.funcs[name] = f
            self.order.append(name)
        if native:
            f.native = native
        if kr_names:
            types = {}
            while not self.at("{"):
                ty = self.abstract_type()
                while True:
                    pn = self.next().v
                    types[pn] = ty
                    if not self.accept(","):
                        break
                self.expect(";")
            params = [(types.get(n, INT), n) for n in kr_names]
            f.params, f.prototyped, f.ret = params, False, ret
        elif params or prototyped:
            if f.body is None:
                f.params, f.variadic, f.prototyped, f.ret = params, variadic, prototyped, ret
        if self.accept(";"):
            return
        if not self.at("{"):
            raise CompileError("expected '{' or ';' after the declaration of %s" % name, self.where())
        if f.body is not None:
            raise CompileError("%s is defined twice" % name, w)
        start = self.comment_start(name_idx)
        f.where = w
        f.params = params if params or kr_names else f.params
        f.ret = ret
        f.body = self.block()
        f.span = (start, self.t[self.i - 1].end, name_tok.file)

    def comment_start(self, name_idx):
        """offset of the first token on the line of the function name (Program.func_text adds the comment line above)"""
        name_tok = self.t[name_idx]
        j = name_idx
        while j > 0 and self.t[j - 1].line == name_tok.line and self.t[j - 1].file == name_tok.file:
            j -= 1
        return self.t[j].pos

    def accept_until(self, v):
        while not self.accept(v):
            self.next()

    def const_int(self, e):
        v = fold(e, self.enums)
        if v is None or isinstance(v, float):
            raise CompileError("expected an integer constant", e.where)
        return v

    # statements
    def block(self):
        w = self.where()
        self.expect("{")
        items = []
        while not self.accept("}"):
            if self.peek().k == "eof":
                raise CompileError("missing '}'", w)
            items.append(self.statement())
        return Node("block", items, where=w)

    def declaration(self):
        w = self.where()
        while self.peek().v in ("static", "register", "auto") and self.peek().k == "id":
            self.next()
        base = self.type_base()
        decls = []
        while True:
            ptr = 0
            while self.accept("*"):
                ptr += 1
            name_tok = self.next()
            ty = Ty(base, ptr)
            if self.accept("["):
                ty = Ty(base, ptr, self.const_int(self.expr()))
                self.expect("]")
            init = self.initializer() if self.accept("=") else None
            decls.append((ty, name_tok.v, init, self.where(name_tok)))
            if not self.accept(","):
                break
        self.expect(";")
        return Node("decl", decls, where=w)

    def statement(self):
        tok = self.peek()
        w = self.where()
        if tok.k == "id" and self.at(":", 1) and tok.v not in ("default",):
            self.next()
            self.next()
            if self.at("}"):
                return Node("label", tok.v, Node("empty", where=w), where=w)
            return Node("label", tok.v, self.statement(), where=w)
        if self.at("{"):
            return self.block()
        if self.is_type_start() and not (self.at("(", 1)):
            return self.declaration()
        if self.accept(";"):
            return Node("empty", where=w)
        if self.accept("if"):
            self.expect("(")
            c = self.expr()
            self.expect(")")
            t = self.statement()
            e = self.statement() if self.accept("else") else None
            return Node("if", c, t, e, where=w)
        if self.accept("while"):
            self.expect("(")
            c = self.expr()
            self.expect(")")
            return Node("while", c, self.statement(), where=w)
        if self.accept("do"):
            body = self.statement()
            self.expect("while")
            self.expect("(")
            c = self.expr()
            self.expect(")")
            self.expect(";")
            return Node("do", body, c, where=w)
        if self.accept("for"):
            self.expect("(")
            init = None
            if self.is_type_start():
                init = self.declaration()
            else:
                init = None if self.at(";") else Node("expr", self.expr(), where=w)
                self.expect(";")
            c = None if self.at(";") else self.expr()
            self.expect(";")
            step = None if self.at(")") else self.expr()
            self.expect(")")
            return Node("for", (init, c, step), self.statement(), where=w)
        if self.accept("return"):
            e = None if self.at(";") else self.expr()
            self.expect(";")
            return Node("ret", e, where=w)
        if self.accept("break"):
            self.expect(";")
            return Node("brk", where=w)
        if self.accept("continue"):
            self.expect(";")
            return Node("cont", where=w)
        if self.accept("goto"):
            name = self.next().v
            self.expect(";")
            return Node("goto", name, where=w)
        if self.at("switch"):
            raise CompileError("switch is not supported yet; use if / else if", w)
        e = self.expr()
        self.expect(";")
        return Node("expr", e, where=w)

    # expressions
    def expr(self):
        e = self.expr_assign()
        while self.at(","):
            w = self.where()
            self.next()
            e = Node("comma", e, self.expr_assign(), where=w)
        return e

    ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

    def expr_assign(self):
        lhs = self.expr_cond()
        tok = self.peek()
        if tok.k == "op" and tok.v in self.ASSIGN:
            self.next()
            return Node("asg", tok.v, lhs, self.expr_assign(), where=(tok.file, tok.line))
        return lhs

    def expr_cond(self):
        c = self.binary(0)
        if self.at("?"):
            w = self.where()
            self.next()
            a = self.expr()
            self.expect(":")
            b = self.expr_cond()
            return Node("cond", c, a, b, where=w)
        return c

    LEVELS = [["||"], ["&&"], ["|"], ["^"], ["&"], ["==", "!="], ["<", "<=", ">", ">="], ["<<", ">>"], ["+", "-"], ["*", "/", "%"]]

    def binary(self, level):
        if level == len(self.LEVELS):
            return self.unary()
        e = self.binary(level + 1)
        while self.peek().k == "op" and self.peek().v in self.LEVELS[level]:
            tok = self.next()
            e = Node("bin", tok.v, e, self.binary(level + 1), where=(tok.file, tok.line))
        return e

    def unary(self):
        tok = self.peek()
        w = (tok.file, tok.line)
        if tok.k == "op" and tok.v in ("-", "+", "!", "~", "*", "&"):
            self.next()
            return Node("un", tok.v, self.unary(), where=w)
        if tok.k == "op" and tok.v in ("++", "--"):
            self.next()
            return Node("pre", tok.v, self.unary(), where=w)
        if tok.k == "op" and tok.v == "(" and self.is_type_start(1):
            self.next()
            ty = self.abstract_type()
            self.expect(")")
            return Node("cast", ty, self.unary(), where=w)
        if tok.k == "id" and tok.v == "sizeof":
            self.next()
            self.expect("(")
            ty = self.abstract_type()
            self.expect(")")
            return Node("num", ty.size(), INT, where=w)
        return self.postfix()

    def postfix(self):
        e = self.primary()
        while True:
            tok = self.peek()
            w = (tok.file, tok.line)
            if self.accept("("):
                args = []
                if not self.accept(")"):
                    while True:
                        args.append(self.expr_assign())
                        if not self.accept(","):
                            break
                    self.expect(")")
                e = Node("call", e, args, where=w)
            elif self.accept("["):
                i = self.expr()
                self.expect("]")
                e = Node("idx", e, i, where=w)
            elif tok.k == "op" and tok.v in ("++", "--"):
                self.next()
                e = Node("post", tok.v, e, where=w)
            else:
                return e

    def primary(self):
        tok = self.next()
        w = (tok.file, tok.line)
        if tok.k == "num":
            return number_node(tok.v, w)
        if tok.k == "chr":
            body = unescape(tok.v[1:-1], w)
            v = 0
            for ch in body:
                v = (v << 8) | (ord(ch) & 0xFF)
            if len(body) > 1:
                v &= 0xFFFFFFFF
                v = v - (1 << 32) if v >= 1 << 31 else v
            elif v >= 128:
                v -= 256
            return Node("num", v, INT, where=w)
        if tok.k == "str":
            s = unescape(tok.v[1:-1], w)
            while self.peek().k == "str":
                s += unescape(self.next().v[1:-1], w)
            return Node("str", s, where=w)
        if tok.k == "id":
            return Node("id", tok.v, where=w)
        if tok.v == "(":
            e = self.expr()
            self.expect(")")
            return e
        raise CompileError("unexpected '%s'" % (tok.v or "end of file"), w)


def number_node(text, w):
    t = text.rstrip("uUlL")
    if t.lower().startswith("0x"):
        v = int(t, 16) & 0xFFFFFFFF
        return Node("num", v - (1 << 32) if v >= 1 << 31 else v, INT, where=w)
    if t.endswith(("f", "F")) or "." in t or "e" in t.lower():
        f = float(t.rstrip("fF"))
        return Node("num", f, FLOAT, where=w)
    v = int(t, 10) & 0xFFFFFFFF
    return Node("num", v - (1 << 32) if v >= 1 << 31 else v, INT, where=w)


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


def fold(e, enums):
    """constant value of an expression or None"""
    k = e.k
    if k == "num":
        return e.a
    if k == "id" and e.a in enums:
        return enums[e.a]
    if k == "un" and e.a in ("-", "+", "~"):
        v = fold(e.b, enums)
        if v is None:
            return None
        if e.a == "-":
            return -v if isinstance(v, float) else s32(-v)
        if e.a == "~":
            return None if isinstance(v, float) else s32(~v)
        return v
    if k == "call" and e.a.k == "id" and e.a.a == "FOURCC" and len(e.b) == 1:
        v = fold(e.b[0], enums)
        if v is None or isinstance(v, float):
            return None
        return s32(struct.unpack("<i", struct.pack(">i", s32(v)))[0])
    if k == "bin" and e.a in ("+", "-", "*", "|", "&", "^", "<<", ">>"):
        a, b = fold(e.b, enums), fold(e.c, enums)
        if a is None or b is None or isinstance(a, float) or isinstance(b, float):
            return None
        return s32({"+": a + b, "-": a - b, "*": a * b, "|": a | b, "&": a & b, "^": a ^ b, "<<": a << (b & 31), ">>": a >> (b & 31)}[e.a])
    return None


def float_bits(f):
    return struct.unpack("<i", struct.pack("<f", f))[0]


# ================================================================= instructions and assembly

ALU = {"pop": 0, "pushacc": 1, "ld8": 2, "ld16": 3, "ld32": 4, "st8": 5, "st16": 6, "st32": 7, "==": 8, "!=": 9, "<": 10, "<=": 11,
       ">": 12, ">=": 13, "~": 14, "&": 15, "|": 16, "^": 17, "+": 18, "-": 19, "*": 20, "/": 21, "%": 22, "<<": 23, ">>": 24,
       "itof": 29, "ftoi": 30, "f<": 31, "f<=": 32, "f>": 33, "f>=": 34, "f+": 35, "f-": 36, "f*": 37, "f/": 38}
CAT = {"imm": 0, "ws": 4, "ws2": 8, "frame": 12}


def size_code(v):
    if -128 <= v <= 127:
        return 1
    if -32768 <= v <= 32767:
        return 2
    return 3


def operand(code, v):
    if code == 1:
        return struct.pack("<b", v)
    if code == 2:
        return struct.pack("<h", v)
    return struct.pack("<i", s32(v))


def inst_len(ins, jump_sizes, idx):
    k = ins[0]
    if k == "label":
        return 0
    if k == "alu":
        return 1
    if k == "ret":
        return 1
    if k == "jump":
        return 1 + {1: 1, 2: 2, 3: 4}[jump_sizes[idx]]
    if k in ("pushsym", "callsym", "saysym", "jmpabs"):
        return 5
    if k == "push":
        return 1 + {1: 1, 2: 2, 3: 4}[size_code(ins[3])]
    if k == "ctl":
        if ins[1] == 3 and len(ins) > 3 and ins[3] == "s8":
            return 2
        return 1 + {1: 1, 2: 2, 3: 4}[size_code(ins[2])]
    raise AssertionError(k)


def assemble(code, base, resolve):
    """code: list of instruction tuples. Returns bytes placed at file offset `base`."""
    jump_sizes = {i: 1 for i, ins in enumerate(code) if ins[0] == "jump"}
    while True:
        pos, labels, offsets = base, {}, []
        for i, ins in enumerate(code):
            offsets.append(pos)
            if ins[0] == "label":
                labels[ins[1]] = pos
            pos += inst_len(ins, jump_sizes, i)
        changed = False
        for i, ins in enumerate(code):
            if ins[0] != "jump":
                continue
            end = offsets[i] + inst_len(ins, jump_sizes, i)
            need = size_code(labels[ins[2]] - end)
            if need > jump_sizes[i]:
                jump_sizes[i] = need
                changed = True
        if not changed:
            break
    out = bytearray()
    for i, ins in enumerate(code):
        k = ins[0]
        if k == "label":
            continue
        if k == "alu":
            out.append(0x80 | ALU[ins[1]])
        elif k == "ret":
            out.append(0x00)
        elif k == "jump":
            sz = jump_sizes[i]
            end = offsets[i] + inst_len(ins, jump_sizes, i)
            out.append((sz << 4) | ins[1])
            out += operand(sz, labels[ins[2]] - end)
        elif k == "push":
            _, cat, sub, v = ins
            sz = size_code(v)
            out.append(0x40 | (sz << 4) | CAT[cat] | sub)
            out += operand(sz, v)
        elif k == "pushsym":
            out.append(0x40 | (3 << 4) | 0 | 3)
            out += operand(3, resolve(ins[1]) + ins[2])
        elif k == "callsym":
            out.append((3 << 4) | 1)
            out += operand(3, resolve(ins[1]))
        elif k == "saysym":
            out.append((3 << 4) | 8)
            out += operand(3, resolve(ins[1]) + ins[2])
        elif k == "jmpabs":
            out.append((3 << 4) | 4)
            out += operand(3, ins[1])
        elif k == "ctl":
            _, low, v = ins[0], ins[1], ins[2]
            if low == 3 and len(ins) > 3 and ins[3] == "s8":
                out.append(0x13)
                out += struct.pack("<B", v & 0xFF)
                continue
            sz = size_code(v)
            out.append((sz << 4) | low)
            out += operand(sz, v)
    return bytes(out)


# ================================================================= code generation


class Var:
    __slots__ = ("name", "ty", "cat", "off")

    def __init__(self, name, ty, cat, off):
        self.name, self.ty, self.cat, self.off = name, ty, cat, off


LOAD_SUB = {1: 0, 2: 1, 4: 2}
LOAD_ALU = {1: "ld8", 2: "ld16", 4: "ld32"}
STORE_ALU = {1: "st8", 2: "st16", 4: "st32"}
TABLE_LOW = {"SHENMUE": 11, "SCNF": 12, "DOOR": 13}


class Program:
    """Parsed scene plus symbol tables."""

    def __init__(self, path, include_dirs=None):
        self.path = Path(path)
        text = self.path.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.text = text
        dirs = [str(self.path.parent)] + [str(d) for d in (include_dirs or [])] + [str(DECOMP_OUT)]
        toks = tokenize(text, str(self.path), dirs)
        self.p = Parser(toks).program()
        self.funcs = self.p.funcs
        self.enums = self.p.enums
        self.data = {}
        self.data_order = []
        for d in self.p.data:
            if d.name in self.data:
                raise CompileError("%s is defined twice" % d.name, d.where)
            self.data[d.name] = d
            self.data_order.append(d.name)
        self.globals = {}
        for name, (ty, where) in self.p.globals.items():
            m = re.fullmatch(r"(ws2?)_([0-9a-f]+)", name)
            if not m:
                raise CompileError("global %s: workspace variables must be named ws_<hex> or ws2_<hex> (the name is the address)" % name, where)
            self.globals[name] = Var(name, ty, m.group(1), int(m.group(2), 16))
        self.anon = []
        self.scripts = [n for n in self.p.order if self.funcs[n].native is None and n not in ("as_float", "as_int", "Say")]

    def func_text(self, f):
        """the function as the decompiler hashed it: the comment line before the signature through the closing brace"""
        start, end, file = f.span
        line = self.text.rfind("\n", 0, start) + 1
        prev = self.text.rfind("\n", 0, max(line - 1, 0)) + 1
        if line > 0 and self.text[prev:line].lstrip().startswith("/*"):
            line = prev
        return self.text[line:end]

    def add_string(self, s, where):
        name = "__str_%d" % len(self.anon)
        d = DataDef(name, Ty("s8", 0, len(s.encode("cp932")) + 1), Node("str", s, where=where), where, True)
        self.data[name] = d
        self.data_order.append(name)
        self.anon.append(name)
        return name

    def data_bytes(self, d, resolve):
        ty, init, w = d.ty, d.init, d.where
        el = ty.elem() if ty.is_array else ty
        esize = el.size() if ty.is_array else ty.size()
        count = ty.count if ty.is_array else 1
        if init is not None and init.k == "str":
            try:
                raw = init.a.encode("cp932")
            except UnicodeEncodeError as e:
                raise CompileError("%s: text cannot be encoded as Shift-JIS (cp932): %s" % (d.name, e), w)
            n = count if count else len(raw) + 1
            if len(raw) > n:
                raise CompileError("%s: text needs %d bytes, the array has %d" % (d.name, len(raw), n), w)
            return raw + b"\0" * (n - len(raw))
        items = init.a if init is not None and init.k == "list" else ([init] if init is not None else [])
        n = count if count else len(items)
        if len(items) > n:
            raise CompileError("%s: %d initializers for %d elements" % (d.name, len(items), n), w)
        out = bytearray()
        for it in items:
            out += self.const_bytes(it, el, esize, resolve, d.name)
        out += b"\0" * (n * esize - len(out))
        return bytes(out)

    def const_bytes(self, e, el, esize, resolve, owner):
        if e.k == "cast" and e.a.is_ptr:
            e = e.b
        if e.k == "id" and e.a in self.funcs and self.funcs[e.a].native is None:
            return struct.pack("<I", resolve(e.a))
        if e.k == "id" and e.a in self.data:
            return struct.pack("<I", resolve(e.a))
        if e.k == "un" and e.a == "&" and e.b.k == "id" and (e.b.a in self.funcs or e.b.a in self.data):
            return struct.pack("<I", resolve(e.b.a))
        if e.k == "call" and e.a.k == "id" and e.a.a in ("as_int", "as_float") and len(e.b) == 1:
            v = fold(e.b[0], self.enums)
            if v is None:
                raise CompileError("%s: initializer must be a constant" % owner, e.where)
            return struct.pack("<i", float_bits(v) if isinstance(v, float) else s32(v))
        v = fold(e, self.enums)
        if v is None:
            raise CompileError("%s: initializer must be a constant" % owner, e.where)
        if el.is_float:
            return struct.pack("<f", float(v))
        if isinstance(v, float):
            v = int(v)
        if esize == 1:
            return struct.pack("<B", v & 0xFF)
        if esize == 2:
            return struct.pack("<H", v & 0xFFFF)
        return struct.pack("<i", s32(v))


class FuncGen:
    def __init__(self, prog, f):
        self.prog, self.f = prog, f
        self.code = []
        self.nlabel = 0
        self.labels = {}
        self.loops = []
        self.vars = {}
        self.frame = 0
        self.used_neg = set()
        self.pending = {}
        self.exit_label = None
        for i, (ty, name) in enumerate(f.params):
            self.vars[name] = Var(name, ty, "frame", 8 + 4 * i)
        self.collect(f.body)
        self.alloc_named()

    def err(self, msg, where):
        raise CompileError(msg, where or self.f.where)

    # ----------------------------------------------------------- frame
    def collect(self, node):
        if node is None or not isinstance(node, Node):
            return
        if node.k == "decl":
            for ty, name, init, where in node.a:
                if name in self.vars and self.vars[name].off >= 8:
                    self.err("%s shadows a parameter" % name, where)
                prev = self.pending.get(name)
                if prev is not None and prev[0] != ty:
                    self.err("%s is declared twice with different types" % name, where)
                self.pending[name] = (ty, where)
            return
        if node.k == "block":
            for x in node.a:
                self.collect(x)
        elif node.k in ("if",):
            self.collect(node.b)
            self.collect(node.c)
        elif node.k in ("while",):
            self.collect(node.b)
        elif node.k == "do":
            self.collect(node.a)
        elif node.k == "for":
            self.collect(node.a[0])
            self.collect(node.b)
        elif node.k == "label":
            self.collect(node.b)
            self.labels.setdefault(node.a, self.new_label())

    def alloc_named(self):
        pending = self.pending
        low = 0
        for name, (ty, where) in pending.items():
            m = re.fullmatch(r"local_([0-9a-f]+)", name)
            if m:
                off = -int(m.group(1), 16)
                self.vars[name] = Var(name, ty, "frame", off)
                low = min(low, off)
                if off + ty.size() > 0:
                    self.err("%s (%d bytes) does not fit below the frame pointer at -0x%x" % (name, ty.size(), -off), where)
        self.next_free = low
        for name, (ty, where) in pending.items():
            if name not in self.vars:
                self.vars[name] = Var(name, ty, "frame", self.alloc(ty.size()))
        self.frame = -self.next_free

    def alloc(self, size):
        size = (size + 3) & ~3
        self.next_free -= size
        self.frame = max(self.frame, -self.next_free)
        return self.next_free

    def new_label(self):
        self.nlabel += 1
        return self.nlabel

    # ----------------------------------------------------------- emit helpers
    def emit(self, *ins):
        self.code.append(ins)

    def push_imm(self, v):
        self.emit("push", "imm", 0, s32(v))

    def alu(self, name):
        self.emit("alu", name)

    def label(self, lab):
        self.emit("label", lab)

    def jump(self, lab):
        self.emit("jump", 5, lab)

    def jz(self, lab):
        self.emit("jump", 6, lab)

    # ----------------------------------------------------------- symbols
    def lookup(self, name, where):
        if name in self.vars:
            return ("var", self.vars[name])
        if name in self.prog.globals:
            return ("var", self.prog.globals[name])
        if name in self.prog.data:
            return ("data", self.prog.data[name])
        if name in self.prog.enums:
            return ("enum", self.prog.enums[name])
        if name in self.prog.funcs:
            return ("func", self.prog.funcs[name])
        self.err("unknown name %s" % name, where)

    def static_ref(self, e):
        """fixed memory location of an lvalue/address: (kind, var_or_data, byte offset, type) or None"""
        k = e.k
        if k == "id":
            what = self.lookup(e.a, e.where)
            if what[0] == "var":
                return ("var", what[1], 0, what[1].ty)
            if what[0] == "data":
                return ("data", what[1], 0, what[1].ty)
            return None
        if k == "idx":
            base = self.static_ref(e.a)
            i = fold(e.b, self.prog.enums)
            if base is not None and base[3].is_array and isinstance(i, int):
                el = base[3].elem()
                return (base[0], base[1], base[2] + i * el.size(), el)
            return None
        if k == "un" and e.a == "*":
            a = self.static_addr(e.b)
            if a is not None:
                return a
        return None

    def static_addr(self, e):
        """address expression with a fixed target: (kind, sym, byte offset, pointee type) or None"""
        k = e.k
        if k == "un" and e.a == "&":
            r = self.static_ref(e.b)
            return r
        if k == "id":
            what = self.lookup(e.a, e.where)
            if what[0] in ("var", "data") and what[1].ty.is_array:
                return ("var" if what[0] == "var" else "data", what[1], 0, what[1].ty.elem())
            return None
        if k == "cast" and e.a.is_ptr:
            inner = self.static_addr(e.b)
            if inner is not None:
                return (inner[0], inner[1], inner[2], e.a.elem())
            return None
        if k == "bin" and e.a in ("+", "-"):
            base = self.static_addr(e.b)
            off = fold(e.c, self.prog.enums)
            if base is not None and isinstance(off, int):
                scale = base[3].size() if base[3].base != "void" else 1
                return (base[0], base[1], base[2] + (off if e.a == "+" else -off) * scale, base[3])
        return None

    def push_elem_addr(self, ref):
        """address of an element: base of the array plus offset (original style); scalars are addressed directly"""
        kind, sym, off, _ = ref
        if kind == "var" and not sym.ty.is_array:
            self.emit("push", sym.cat, 3, sym.off + off)
            return
        if kind == "var":
            self.emit("push", sym.cat, 3, sym.off)
        else:
            self.emit("pushsym", sym.name, 0)
        if off:
            self.push_imm(off)
            self.alu("+")

    def push_static_addr(self, ref):
        kind, sym, off, _ = ref
        if kind == "var":
            self.emit("push", sym.cat, 3, sym.off + off)
        else:
            self.emit("pushsym", sym.name, off)

    # ----------------------------------------------------------- types
    def type_of_value(self, e):
        """type of an expression without generating code (used for decisions)"""
        k = e.k
        if k == "num":
            return e.b
        if k == "str":
            return Ty("s8", 1)
        if k == "id":
            what = self.lookup(e.a, e.where)
            if what[0] == "var":
                return what[1].ty.decay()
            if what[0] == "data":
                return what[1].ty.decay()
            if what[0] == "enum":
                return INT
            return Ty("fn", 0)
        if k == "cast":
            return e.a
        if k == "call":
            if e.a.k == "id":
                if e.a.a == "as_float":
                    return FLOAT
                if e.a.a in ("as_int", "FOURCC"):
                    return INT
                f = self.prog.funcs.get(e.a.a)
                if f:
                    return f.ret
            return INT
        if k == "idx":
            t = self.type_of_value(e.a)
            return t.elem().decay() if t.is_ptr else INT
        if k == "un":
            if e.a == "*":
                t = self.type_of_value(e.b)
                return t.elem().decay() if t.is_ptr else INT
            if e.a == "&":
                return Ty(self.type_of_lvalue(e.b).base, self.type_of_lvalue(e.b).ptr + 1)
            if e.a in ("!", "~"):
                return INT
            return self.type_of_value(e.b)
        if k == "bin":
            if e.a in ("==", "!=", "<", "<=", ">", ">=", "&&", "||"):
                return INT
            a, b = self.type_of_value(e.b), self.type_of_value(e.c)
            if e.a in ("+", "-") and a.is_ptr:
                return a
            if e.a == "+" and b.is_ptr:
                return b
            if (a.is_float or b.is_float) and e.a in ("+", "-", "*", "/"):
                return FLOAT
            return INT
        if k in ("asg", "pre", "post"):
            return self.type_of_lvalue(e.b if k == "asg" else e.b)
        if k == "cond":
            a, b = self.type_of_value(e.b), self.type_of_value(e.c)
            return FLOAT if a.is_float or b.is_float else a
        if k == "comma":
            return self.type_of_value(e.b)
        return INT

    def type_of_lvalue(self, e):
        k = e.k
        if k == "id":
            what = self.lookup(e.a, e.where)
            if what[0] in ("var", "data"):
                return what[1].ty
            self.err("%s is not a variable" % e.a, e.where)
        if k == "idx":
            t = self.type_of_value(e.a)
            if not t.is_ptr:
                self.err("subscript of a value that is not an array or pointer", e.where)
            return t.elem()
        if k == "un" and e.a == "*":
            t = self.type_of_value(e.b)
            if not t.is_ptr:
                self.err("dereference of a value that is not a pointer", e.where)
            return t.elem()
        self.err("expression is not assignable", e.where)

    # ----------------------------------------------------------- expressions
    def gen(self, e):
        """returns (type, where, vbool): where 'stk' or 'acc'; vbool = VM truth value -1/0 standing for C 1/0"""
        k = e.k
        if k in ("bin", "un") and e.a not in ("&", "*", "!"):
            v = fold(e, self.prog.enums)
            if v is not None:
                self.push_imm(float_bits(v) if isinstance(v, float) else v)
                return (FLOAT if isinstance(v, float) else INT), "stk", False
        if k == "num":
            if e.b.is_float:
                self.push_imm(float_bits(e.a))
                return FLOAT, "stk", False
            self.push_imm(e.a)
            return INT, "stk", False
        if k == "str":
            name = self.prog.add_string(e.a, e.where)
            self.emit("pushsym", name, 0)
            return Ty("s8", 1), "stk", False
        if k == "id":
            what = self.lookup(e.a, e.where)
            if what[0] == "enum":
                self.push_imm(what[1])
                return INT, "stk", False
            if what[0] == "func":
                if what[1].native is not None or what[1].body is None and e.a in ("as_float", "as_int", "Say"):
                    self.err("%s cannot be used as a value" % e.a, e.where)
                self.emit("pushsym", e.a, 0)
                return Ty("fn"), "stk", False
            ref = ("var" if what[0] == "var" else "data", what[1], 0, what[1].ty)
            return self.load_ref(ref, e.where)
        if k == "idx" or (k == "un" and e.a == "*"):
            ref = self.static_ref(e)
            if ref is not None:
                return self.load_ref(ref, e.where)
            ty = self.addr(e)
            return self.load_top(ty, e.where)
        if k == "un":
            return self.unary(e)
        if k == "cast":
            return self.cast(e)
        if k == "call":
            return self.call(e)
        if k == "bin":
            return self.binary(e)
        if k == "asg":
            return self.assign(e)
        if k in ("pre", "post"):
            return self.incdec(e)
        if k == "cond":
            return self.ternary(e)
        if k == "comma":
            self.effect(e.a)
            return self.gen(e.b)
        self.err("unsupported expression", e.where)

    def load_ref(self, ref, where):
        kind, sym, off, ty = ref
        if ty.is_array:
            self.push_static_addr((kind, sym, off, ty.elem()))
            return ty.decay(), "stk", False
        size = ty.scalar_size()
        if kind == "var" and not sym.ty.is_array:
            self.emit("push", sym.cat, LOAD_SUB[size], sym.off + off)
        else:
            self.push_elem_addr(ref)
            self.alu(LOAD_ALU[size])
        if ty.base == "u8" and not ty.ptr:
            self.push_imm(255)
            self.alu("&")
        return ty, "stk", False

    def load_top(self, ty, where):
        if ty.is_array:
            return ty.decay(), "stk", False
        if ty.base == "void" and not ty.ptr:
            self.err("dereference of void *", where)
        self.alu(LOAD_ALU[ty.scalar_size()])
        if ty.base == "u8" and not ty.ptr:
            self.push_imm(255)
            self.alu("&")
        return ty, "stk", False

    def addr(self, e):
        """push the address of an lvalue; returns the lvalue type"""
        ref = self.static_ref(e)
        if ref is not None:
            self.push_static_addr(ref)
            return ref[3]
        k = e.k
        if k == "un" and e.a == "*":
            t = self.value(e.b)
            if not t.is_ptr:
                self.err("dereference of a value that is not a pointer", e.where)
            return t.elem()
        if k == "idx":
            t = self.value(e.a)
            if not t.is_ptr:
                self.err("subscript of a value that is not an array or pointer", e.where)
            el = t.elem()
            self.index_scaled(e.b, el)
            self.alu("+")
            return el
        if k == "id":
            what = self.lookup(e.a, e.where)
            self.err("%s is not addressable" % e.a, e.where)
        self.err("expression is not addressable", e.where)

    def index_scaled(self, idx, el):
        it = self.value(idx)
        if it.is_float:
            self.err("array index is a float", idx.where)
        size = el.size() if el.base != "void" or el.ptr else 1
        if size == 4:
            self.push_imm(2)
            self.alu("<<")
        elif size == 2:
            self.push_imm(1)
            self.alu("<<")
        elif size != 1:
            self.push_imm(size)
            self.alu("*")

    def to_stack(self, where):
        if where == "acc":
            self.alu("pushacc")

    def value(self, e, want=None):
        """value on the stack with C semantics, converted to `want` if given"""
        ty, where, vb = self.gen(e)
        self.to_stack(where)
        if vb:
            self.push_imm(-1)
            self.alu("*")
            ty = INT
        if ty == VOID:
            self.err("void value used", e.where)
        if want is not None:
            ty = self.convert(ty, want, e.where)
        return ty

    def raw(self, e):
        """value on the stack, keeping VM truth values (-1/0) as they are"""
        ty, where, vb = self.gen(e)
        self.to_stack(where)
        return ty, vb

    def convert(self, have, want, where):
        if want.is_float and not have.is_float:
            if have.is_ptr:
                self.err("pointer converted to float", where)
            self.alu("itof")
            return FLOAT
        if have.is_float and not want.is_float:
            if want.is_ptr:
                self.err("float converted to pointer", where)
            self.alu("ftoi")
            return want
        return want

    def unary(self, e):
        op, x = e.a, e.b
        if op == "&":
            ref = self.static_ref(x)
            if ref is not None:
                self.push_static_addr(ref)
                return Ty(ref[3].base, ref[3].ptr + 1), "stk", False
            if x.k == "id" and x.a in self.prog.funcs:
                self.emit("pushsym", x.a, 0)
                return Ty("fn"), "stk", False
            ty = self.addr(x)
            return Ty(ty.base, ty.ptr + 1), "stk", False
        if op == "-":
            v = fold(e, self.prog.enums)
            if v is not None:
                if isinstance(v, float):
                    self.push_imm(float_bits(v))
                    return FLOAT, "stk", False
                self.push_imm(v)
                return INT, "stk", False
            t0 = self.type_of_value(x)
            if self.is_vbool(x):
                ty, vb = self.raw(x)
                return INT, "stk", False
            if t0.is_float:
                self.push_imm(0)
                self.value(x, FLOAT)
                self.alu("f-")
                return FLOAT, "stk", False
            self.push_imm(0)
            self.value(x, INT)
            self.alu("-")
            return INT, "stk", False
        if op == "+":
            return self.value(x), "stk", False
        if op == "~":
            self.value(x, INT)
            self.alu("~")
            return INT, "stk", False
        if op == "!":
            if self.is_vbool(x):
                self.raw(x)
                self.alu("~")
                return INT, "stk", True
            t = self.value(x)
            self.push_imm(0)
            self.alu("==")
            return INT, "stk", True
        self.err("unsupported operator %s" % op, e.where)

    def is_vbool(self, e):
        k = e.k
        if k == "bin":
            if e.a in ("==", "!=", "<", "<=", ">", ">="):
                return True
            if e.a in ("&", "|", "^"):
                return self.is_vbool(e.b) and self.is_vbool(e.c)
            if e.a in ("&&", "||"):
                return True
        if k == "un" and e.a == "!":
            return True
        return False

    def cast(self, e):
        to = e.a
        if to.base == "void" and not to.ptr:
            self.effect(e.b)
            return VOID, "stk", False
        have = self.value(e.b)
        if to.is_ptr or have.is_ptr:
            if have.is_float:
                self.err("float cast to a pointer", e.where)
            return to, "stk", False
        if to.is_float and not have.is_float:
            self.alu("itof")
            return FLOAT, "stk", False
        if have.is_float and not to.is_float:
            self.alu("ftoi")
            have = INT
        if to.base in ("s8", "s16"):
            sh = 24 if to.base == "s8" else 16
            self.push_imm(sh)
            self.alu("<<")
            self.push_imm(sh)
            self.alu(">>")
        elif to.base == "u8":
            self.push_imm(255)
            self.alu("&")
        return to, "stk", False

    def call(self, e):
        fe, args = e.a, e.b
        if fe.k != "id":
            self.err("indirect calls are not supported", e.where)
        name = fe.a
        if name in ("as_float", "as_int"):
            if len(args) != 1:
                self.err("%s takes one argument" % name, e.where)
            have = self.value(args[0])
            if have.is_float == (name == "as_float"):
                pass
            return (FLOAT if name == "as_float" else INT), "stk", False
        if name == "FOURCC":
            v = fold(e, self.prog.enums)
            if v is None:
                self.err("FOURCC needs a character constant like 'JIMY'", e.where)
            self.push_imm(v)
            return INT, "stk", False
        if name == "Say":
            if len(args) != 1:
                self.err("Say takes one argument", e.where)
            a = args[0]
            if a.k == "str":
                sym, off = self.prog.add_string(a.a, a.where), 0
            else:
                ref = self.static_addr(a)
                if ref is None or ref[0] != "data":
                    self.err("Say needs a string data object such as str_45b97 or a string literal", a.where)
                sym, off = ref[1].name, ref[2]
            self.emit("saysym", sym, off)
            return VOID, "stk", False
        f = self.prog.funcs.get(name)
        if f is None:
            if name in self.vars or name in self.prog.globals:
                self.err("indirect calls are not supported", e.where)
            self.err("call of undeclared function %s" % name, e.where)
        n = len(args)
        if f.prototyped and not f.variadic and n != len(f.params):
            self.err("%s takes %d arguments, %d given" % (name, len(f.params), n), e.where)
        if f.prototyped and f.variadic and n < len(f.params):
            self.err("%s takes at least %d arguments, %d given" % (name, len(f.params), n), e.where)
        for i in range(n - 1, -1, -1):
            want = f.params[i][0] if i < len(f.params) else None
            self.value(args[i], want if want is not None and not want.is_array else None)
        if f.native is not None:
            table, slot = f.native
            low = TABLE_LOW.get(table)
            if low is None:
                self.err("unknown native table %s" % table, e.where)
            if low == 12:
                self.emit("ctl", 7, n)
                self.emit("ctl", 12, slot)
                if n:
                    if n * 4 > 255:
                        self.err("too many arguments for a SCNF call", e.where)
                    self.emit("ctl", 3, n * 4, "s8")
            else:
                self.emit("ctl", low, slot)
                if n:
                    self.emit("ctl", 3, n * 4)
        else:
            if f.body is None and name not in self.prog.funcs:
                self.err("call of undefined function %s" % name, e.where)
            self.emit("callsym", name)
            if n:
                self.emit("ctl", 3, n * 4)
        return f.ret, "acc", False

    ARITH = {"+", "-", "*", "/", "%", "<<", ">>", "&", "|", "^"}

    def binary(self, e):
        op, a, b = e.a, e.b, e.c
        if op in ("&&", "||"):
            return self.logical_value(e)
        ta, tb = self.type_of_value(a), self.type_of_value(b)
        if op in ("==", "!=", "<", "<=", ">", ">="):
            fl = ta.is_float or tb.is_float
            if fl and op in ("<", "<=", ">", ">="):
                self.value(a, FLOAT)
                self.value(b, FLOAT)
                self.alu("f" + op)
            else:
                want = FLOAT if fl else None
                self.value(a, want)
                self.value(b, want)
                self.alu(op)
            return INT, "stk", True
        if op in ("&", "|", "^") and self.is_vbool(a) and self.is_vbool(b):
            self.raw(a)
            self.raw(b)
            self.alu(op)
            return INT, "stk", True
        if op in ("+", "-") and ta.is_ptr and not tb.is_ptr:
            self.value(a)
            el = ta.elem()
            self.index_scaled(b, el)
            self.alu(op)
            return ta.decay() if ta.is_array else ta, "stk", False
        if op == "+" and tb.is_ptr and not ta.is_ptr:
            self.index_scaled(a, tb.elem())
            self.value(b)
            self.alu("+")
            return tb.decay() if tb.is_array else tb, "stk", False
        if op == "-" and ta.is_ptr and tb.is_ptr:
            self.value(a)
            self.value(b)
            self.alu("-")
            size = ta.elem().size() or 1
            if size > 1:
                self.push_imm(size)
                self.alu("/")
            return INT, "stk", False
        if (ta.is_float or tb.is_float) and op in ("+", "-", "*", "/"):
            self.value(a, FLOAT)
            self.value(b, FLOAT)
            self.alu("f" + op)
            return FLOAT, "stk", False
        if ta.is_float or tb.is_float:
            self.err("operator %s needs integer operands" % op, e.where)
        self.value(a)
        self.value(b)
        self.alu(op)
        return INT, "stk", False

    def temp_slot(self):
        return self.alloc(4)

    def logical_value(self, e):
        off = self.temp_slot()
        lf, lend = self.new_label(), self.new_label()
        self.cond(e, lf)
        self.emit("push", "frame", 3, off)
        self.push_imm(-1)
        self.alu("st32")
        self.alu("pop")
        self.jump(lend)
        self.label(lf)
        self.emit("push", "frame", 3, off)
        self.push_imm(0)
        self.alu("st32")
        self.alu("pop")
        self.label(lend)
        self.emit("push", "frame", 2, off)
        return INT, "stk", True

    def ternary(self, e):
        ty = self.type_of_value(e)
        off = self.temp_slot()
        lf, lend = self.new_label(), self.new_label()
        self.cond(e.a, lf)
        self.emit("push", "frame", 3, off)
        self.value(e.b, ty)
        self.alu("st32")
        self.alu("pop")
        self.jump(lend)
        self.label(lf)
        self.emit("push", "frame", 3, off)
        self.value(e.c, ty)
        self.alu("st32")
        self.alu("pop")
        self.label(lend)
        self.emit("push", "frame", 2, off)
        return ty, "stk", False

    def store(self, ty, where):
        if ty.is_array:
            self.err("cannot assign to an array", where)
        self.alu(STORE_ALU[ty.scalar_size()])

    def assign(self, e):
        op, lhs, rhs = e.a, e.b, e.c
        ty = self.type_of_lvalue(lhs)
        ref = self.static_ref(lhs)
        if op == "=":
            if ref is not None:
                self.push_elem_addr(ref)
            else:
                self.addr(lhs)
            self.value(rhs, ty if not ty.is_ptr else None)
            self.store(ty, e.where)
            return ty, "stk", False
        if ref is None and not self.pure(lhs):
            self.err("compound assignment needs a simple target (no calls or assignments inside)", e.where)
        binop = op[:-1]
        if ref is not None:
            self.push_elem_addr(ref)
        else:
            self.addr(lhs)
        fake = Node("bin", binop, lhs, rhs, where=e.where)
        rt, where_, vb = self.binary(fake)
        self.to_stack(where_)
        self.convert(rt, ty, e.where)
        self.store(ty, e.where)
        return ty, "stk", False

    def pure(self, e):
        if e is None or not isinstance(e, Node):
            return True
        if e.k in ("call", "asg", "pre", "post"):
            return False
        return all(self.pure(x) for x in (e.a, e.b, e.c) if isinstance(x, Node))

    def incdec(self, e):
        one = Node("num", 1, INT, where=e.where)
        op = "+=" if e.a == "++" else "-="
        ty, where, vb = self.assign(Node("asg", op, e.b, one, where=e.where))
        if e.k == "post":
            lt = self.type_of_lvalue(e.b)
            if lt.is_float:
                self.push_imm(float_bits(1.0))
                self.alu("f-" if e.a == "++" else "f+")
            else:
                self.push_imm(1 if not lt.is_ptr else lt.elem().size())
                self.alu("-" if e.a == "++" else "+")
        return ty, "stk", False

    # ----------------------------------------------------------- conditions
    def cond(self, e, lfalse):
        """jump to lfalse when e is false, fall through when true"""
        if e.k == "bin" and e.a == "&&":
            self.cond(e.b, lfalse)
            self.cond(e.c, lfalse)
            return
        if e.k == "bin" and e.a == "||":
            lnext, ltrue = self.new_label(), self.new_label()
            self.cond(e.b, lnext)
            self.jump(ltrue)
            self.label(lnext)
            self.cond(e.c, lfalse)
            self.label(ltrue)
            return
        if e.k == "un" and e.a == "!" and e.b.k == "bin" and e.b.a in ("&&", "||"):
            ltrue = self.new_label()
            self.cond(e.b, ltrue)
            self.jump(lfalse)
            self.label(ltrue)
            return
        if e.k == "un" and e.a == "!":
            self.jt(e.b, lfalse)
            return
        ty, where, vb = self.gen(e)
        if ty == VOID:
            self.err("void value used as a condition", e.where)
        if where == "stk":
            self.alu("pop")
        self.jz(lfalse)

    FLIP = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}

    def jump_target_of(self, s):
        if s.k == "brk" and self.loops:
            return self.loops[-1][0]
        if s.k == "cont" and self.loops:
            return self.loops[-1][1]
        if s.k == "goto" and s.a in self.labels:
            return self.labels[s.a]
        if s.k == "ret" and s.a is None and self.f.ret == VOID:
            if self.exit_label is None:
                self.exit_label = self.new_label()
            return self.exit_label
        return None

    def jt(self, e, target):
        """jump to target when e is true, fall through when false"""
        if e.k == "un" and e.a == "!":
            self.cond(e.b, target)
            return
        if e.k == "bin" and e.a in self.FLIP and not (self.type_of_value(e.b).is_float or self.type_of_value(e.c).is_float):
            self.value(e.b)
            self.value(e.c)
            self.alu(self.FLIP[e.a])
            self.alu("pop")
            self.jz(target)
            return
        if e.k == "bin" and e.a == "&&":
            lskip = self.new_label()
            self.cond(e.b, lskip)
            self.jt(e.c, target)
            self.label(lskip)
            return
        if e.k == "bin" and e.a == "||":
            self.jt(e.b, target)
            self.jt(e.c, target)
            return
        lskip = self.new_label()
        self.cond(e, lskip)
        self.jump(target)
        self.label(lskip)

    # ----------------------------------------------------------- statements
    def effect(self, e):
        """evaluate for side effects, as the original compiler did"""
        if e.k == "call":
            ty, where, vb = self.gen(e)
            if where == "stk" and ty != VOID:
                self.alu("pop")
            return
        if e.k == "cast" and e.a.base == "void" and not e.a.ptr:
            self.effect(e.b)
            return
        if e.k == "comma":
            self.effect(e.a)
            self.effect(e.b)
            return
        ty, where, vb = self.gen(e)
        if where == "stk" and ty != VOID:
            self.alu("pop")

    def stmt(self, s):
        k = s.k
        if k == "block":
            for x in s.a:
                self.stmt(x)
        elif k == "decl":
            for ty, name, init, where in s.a:
                if init is not None:
                    if init.k == "list":
                        self.err("array initializers inside functions are not supported", where)
                    self.effect(Node("asg", "=", Node("id", name, where=where), init, where=where))
        elif k == "expr":
            self.effect(s.a)
        elif k == "empty":
            pass
        elif k == "if" and s.c is None and self.jump_target_of(s.b) is not None:
            self.jt(s.a, self.jump_target_of(s.b))
        elif k == "if":
            lelse = self.new_label()
            self.cond(s.a, lelse)
            self.stmt(s.b)
            if s.c is not None:
                lend = self.new_label()
                self.jump(lend)
                self.label(lelse)
                self.stmt(s.c)
                self.label(lend)
            else:
                self.label(lelse)
        elif k == "while":
            ltop, lend = self.new_label(), self.new_label()
            self.label(ltop)
            self.cond(s.a, lend)
            self.loops.append((lend, ltop))
            self.stmt(s.b)
            self.loops.pop()
            self.jump(ltop)
            self.label(lend)
        elif k == "do":
            ltop, lcont, lend = self.new_label(), self.new_label(), self.new_label()
            self.label(ltop)
            self.loops.append((lend, lcont))
            self.stmt(s.a)
            self.loops.pop()
            self.label(lcont)
            self.cond(s.b, lend)
            self.jump(ltop)
            self.label(lend)
        elif k == "for":
            init, c, step = s.a
            if init is not None:
                self.stmt(init)
            ltop, lstep, lend = self.new_label(), self.new_label(), self.new_label()
            self.label(ltop)
            if c is not None:
                self.cond(c, lend)
            self.loops.append((lend, lstep))
            self.stmt(s.b)
            self.loops.pop()
            self.label(lstep)
            if step is not None:
                self.effect(step)
            self.jump(ltop)
            self.label(lend)
        elif k == "brk":
            if not self.loops:
                self.err("break outside a loop", s.where)
            self.jump(self.loops[-1][0])
        elif k == "cont":
            if not self.loops:
                self.err("continue outside a loop", s.where)
            self.jump(self.loops[-1][1])
        elif k == "ret":
            if s.a is not None:
                if self.f.ret == VOID:
                    self.err("void function %s returns a value" % self.f.name, s.where)
                ty, where, vb = self.gen(s.a)
                if vb or (ty.is_float != self.f.ret.is_float) or where == "stk" and False:
                    self.to_stack(where)
                    if vb:
                        self.push_imm(-1)
                        self.alu("*")
                        ty = INT
                    self.convert(ty, self.f.ret, s.where)
                    where = "stk"
                if where == "stk":
                    self.alu("pop")
            if self.exit_label is None:
                self.exit_label = self.new_label()
            self.jump(self.exit_label)
        elif k == "goto":
            if s.a not in self.labels:
                self.err("unknown label %s" % s.a, s.where)
            self.jump(self.labels[s.a])
        elif k == "label":
            self.label(self.labels[s.a])
            self.stmt(s.b)
        else:
            self.err("unsupported statement", s.where)

    def generate(self):
        body = self.code
        self.code = []
        self.stmt(self.f.body)
        main = self.code
        if not main or main[-1][0] != "ret":
            main.append(("ret",))
        if self.exit_label is not None:
            while len(main) >= 2 and main[-2] == ("jump", 5, self.exit_label):
                del main[-2]
            main.insert(len(main) - 1, ("label", self.exit_label))
        self.code = [("ctl", 3, -self.frame_size())] + main
        return self.code

    def frame_size(self):
        return (self.frame + 3) & ~3


# ================================================================= scene build


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_sidecar(c_path):
    p = Path(c_path).with_suffix(".json")
    if not p.exists():
        raise CompileError("missing %s (the sidecar written by scn_decomp.py next to the .c)" % p.name)
    return json.loads(p.read_text())


def func_addr(name):
    m = re.fullmatch(r"fn_([0-9a-f]+)", name)
    return int(m.group(1), 16) if m else None


def data_addr(name):
    m = re.fullmatch(r"(?:str|vec|tab|events|bin)_([0-9a-f]+)", name)
    return int(m.group(1), 16) if m else None


class Build:
    def __init__(self, c_path, scn_dir=DEFAULT_SCN, log=print):
        self.c_path = Path(c_path)
        self.log = log
        self.prog = Program(self.c_path)
        self.side = load_sidecar(self.c_path)
        self.scene = self.side["scene"]
        scn_path = Path(scn_dir) / (self.scene + ".scn")
        if not scn_path.exists():
            raise CompileError("original %s not found in %s" % (scn_path.name, scn_dir))
        self.orig = scn_path.read_bytes()
        if hashlib.sha1(self.orig).hexdigest() != self.side["file_sha1"]:
            raise CompileError("%s does not match the file this source was decompiled from" % scn_path)
        self.side_funcs = {int(a, 16): v for a, v in self.side["functions"].items()}
        self.side_data = {d["name"]: d for d in self.side["data"]}

    def changed_functions(self, force=False):
        out = []
        for name in self.prog.scripts:
            f = self.prog.funcs[name]
            if f.body is None:
                continue
            a = func_addr(name)
            info = self.side_funcs.get(a) if a is not None else None
            if force or info is None or info["c_sha1"] != sha1(self.prog.func_text(f)):
                out.append(name)
        return out

    def compile_funcs(self, names):
        code = {}
        for name in names:
            code[name] = FuncGen(self.prog, self.prog.funcs[name]).generate()
        return code

    @staticmethod
    def code_size(code):
        return len(assemble(code, 0, lambda s: 0))

    # ----------------------------------------------------------- patch mode
    def patch(self, force=False):
        out = bytearray(self.orig)
        changed = self.changed_functions(force)
        code = self.compile_funcs(changed)
        sizes = {n: self.code_size(c) for n, c in code.items()}
        addr = {}
        for name in self.prog.scripts:
            a = func_addr(name)
            if a is not None and a in self.side_funcs:
                addr[name] = a
        tail = (len(out) + 3) & ~3
        placed, appended, redirects = [], [], []
        for name in changed:
            a = func_addr(name)
            info = self.side_funcs.get(a) if a is not None else None
            if info is not None and sizes[name] <= info["end"] - info["start"]:
                placed.append(name)
            else:
                appended.append(name)
        # data: compile with provisional addresses to learn sizes
        data_names = list(self.prog.data_order)
        new_data, changed_data, data_addr_map = [], [], {}
        for dn in data_names:
            sd = self.side_data.get(dn)
            if sd is not None:
                data_addr_map[dn] = sd["addr"]
        pos = tail
        for name in appended:
            addr[name] = pos
            pos += sizes[name]
        for dn in data_names:
            if dn not in data_addr_map:
                pos = (pos + 3) & ~3
                data_addr_map[dn] = pos
                new_data.append(dn)
                pos += len(self.prog.data_bytes(self.prog.data[dn], lambda s: 0))

        def resolve(sym):
            if sym in addr:
                return addr[sym]
            if sym in data_addr_map:
                return data_addr_map[sym]
            a = func_addr(sym)
            if a is not None and a in self.side_funcs:
                return a
            raise CompileError("no address for %s" % sym)

        # data objects whose bytes or size changed
        grown = []
        for dn in data_names:
            if dn in new_data:
                continue
            sd = self.side_data[dn]
            blob = self.prog.data_bytes(self.prog.data[dn], resolve)
            old = bytes(self.orig[sd["addr"]:sd["addr"] + sd["size"]])
            if blob == old:
                continue
            if len(blob) == len(old):
                out[sd["addr"]:sd["addr"] + len(blob)] = blob
                changed_data.append(dn)
            else:
                grown.append(dn)
        for dn in grown:
            pos = (pos + 3) & ~3
            old_addr = self.side_data[dn]["addr"]
            data_addr_map[dn] = pos
            new_data.append(dn)
            pos += len(self.prog.data_bytes(self.prog.data[dn], resolve))
            self.patch_refs(out, old_addr, pos - len(self.prog.data_bytes(self.prog.data[dn], resolve)), changed, dn)
        # functions in place
        for name in placed:
            info = self.side_funcs[func_addr(name)]
            blob = assemble(code[name], info["start"], resolve)
            span = info["end"] - info["start"]
            # unreachable ret bytes keep "ret, then add sp" in front of the next function, which is how starts are found
            out[info["start"]:info["end"]] = blob + b"\x00" * (span - len(blob))
        # appended functions and data
        if len(out) < tail:
            out += b"\xff" * (tail - len(out))
        for name in appended:
            blob = assemble(code[name], addr[name], resolve)
            assert len(out) == addr[name], (len(out), addr[name])
            out += blob
            a = func_addr(name)
            info = self.side_funcs.get(a) if a is not None else None
            if info is not None:
                span = info["end"] - info["start"]
                if span >= 5:
                    stub = bytes([0x34]) + struct.pack("<i", addr[name])
                    out[info["start"]:info["end"]] = stub + b"\x00" * (span - 5)
                    redirects.append(name)
                else:
                    self.patch_refs(out, info["start"], addr[name], changed, name)
        for dn in new_data:
            a = data_addr_map[dn]
            if len(out) < a:
                out += b"\0" * (a - len(out))
            out += self.prog.data_bytes(self.prog.data[dn], resolve)
        struct.pack_into("<i", out, 4, len(out))
        report = {"mode": "patch", "changed": changed, "in_place": placed, "appended": appended, "redirected": redirects,
                  "data_changed": changed_data, "data_new": [d for d in new_data if not d.startswith("__")],
                  "strings_new": len([d for d in new_data if d.startswith("__")]), "size": len(out), "orig_size": len(self.orig)}
        return bytes(out), report

    def patch_refs(self, out, old, new, changed, what):
        """point every 4-byte reference to `old` in unchanged code at `new`"""
        import scn
        s = scn.Script.__new__(scn.Script)
        s.b = self.orig
        h = scn.HDR.unpack_from(self.orig, 0)
        s.whole, s.func_off, s.data_off, s.entry = h[1], h[3], h[4], h[8]
        bad = []
        for pc, op, kind, val in s._decode(s.entry, min(s.data_off, s.whole)):
            if val != old:
                continue
            is_ref = (kind == "push" and (op & 15) == 3) or (kind == "ctl" and (op & 15) in (1, 4, 8))
            if not is_ref:
                continue
            if (op >> 4) & 3 == 3:
                struct.pack_into("<i", out, pc + 1, new)
            else:
                bad.append(pc)
        if bad:
            raise CompileError("%s moved to 0x%x but %d references to 0x%x use short operands (e.g. at 0x%x); keep it the same size"
                               % (what, new, len(bad), old, bad[0]))

    # ----------------------------------------------------------- rebuild mode
    def rebuild(self):
        names = [n for n in self.prog.scripts if self.prog.funcs[n].body is not None]
        names.sort(key=lambda n: (func_addr(n) is None, func_addr(n) or 0))
        code = self.compile_funcs(names)
        addr, pos = {}, 48
        entry_name = "fn_%x" % self.side["header"][8]
        if names and names[0] != entry_name:
            raise CompileError("the entry function %s must exist and come first" % entry_name)
        for n in names:
            addr[n] = pos
            pos += self.code_size(code[n])
        pos = (pos + 3) & ~3
        data_start = pos
        data_names = list(self.prog.data_order)
        sizes = {dn: len(self.prog.data_bytes(self.prog.data[dn], lambda s: 0)) for dn in data_names}
        daddr = {}
        for dn in data_names:
            daddr[dn] = pos
            pos += sizes[dn]

        def resolve(sym):
            if sym in addr:
                return addr[sym]
            if sym in daddr:
                return daddr[sym]
            raise CompileError("no address for %s" % sym)

        out = bytearray(48)
        for n in names:
            out += assemble(code[n], len(out), resolve)
        out += b"\xff" * (data_start - len(out))
        for dn in data_names:
            out += self.prog.data_bytes(self.prog.data[dn], resolve)
        hdr = list(self.side["header"])
        old_funcs = sorted(self.side_funcs.items())
        old_data = sorted((d["addr"], d) for d in self.side["data"])

        def remap(v):
            for a, info in old_funcs:
                if info["start"] <= v < info["end"] and info["name"] in addr:
                    return addr[info["name"]] + (v - info["start"])
            for a, d in old_data:
                if a <= v < a + d["size"] and d["name"] in daddr:
                    return daddr[d["name"]] + (v - a)
            if v == self.side["header"][1]:
                return len(out)
            return v

        hdr[1] = len(out)
        hdr[3] = remap(hdr[3])
        hdr[4] = data_start
        for i in (5, 6, 7, 9):
            if hdr[i]:
                hdr[i] = remap(hdr[i])
        hdr[8] = 48
        struct.pack_into("<12i", out, 0, *hdr)
        layout = {"functions": addr, "data": daddr}
        return bytes(out), {"mode": "rebuild", "functions": len(names), "size": len(out), "orig_size": len(self.orig)}, layout


# ================================================================= commands


def status_of(c_path):
    """quick hash compare without compiling: names of changed functions"""
    p = Path(c_path)
    side = json.loads(p.with_suffix(".json").read_text())
    text = p.read_text(encoding="utf-8").replace("\r\n", "\n")
    funcs = {int(a, 16): v for a, v in side["functions"].items()}
    changed, seen = [], set()
    for m in re.finditer(r"^/\* 0x([0-9a-f]+)[^\n]*\*/\n[^\n]*\bfn_([0-9a-f]+)\([^\n]*\n(?:.*?\n)*?}", text, re.M):
        a = int(m.group(2), 16)
        seen.add(a)
        info = funcs.get(a)
        if info is None or info["c_sha1"] != sha1(m.group(0)):
            changed.append("fn_%x" % a)
    return changed


def cmd_build(c_path, scn_dir, out_dir, rebuild=False, force=False):
    t0 = time.time()
    b = Build(c_path, scn_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if rebuild:
        blob, rep, _ = b.rebuild()
    else:
        blob, rep = b.patch(force)
    target = out_dir / (b.scene + ".scn")
    target.write_bytes(blob)
    rep["output"] = str(target)
    rep["seconds"] = round(time.time() - t0, 2)
    (out_dir / (b.scene + ".build.json")).write_text(json.dumps(rep, indent=1))
    if rep["mode"] == "patch":
        print("%s: %d changed functions (%d in place, %d appended), %d data objects changed, %d new; %d -> %d bytes"
              % (target, len(rep["changed"]), len(rep["in_place"]), len(rep["appended"]), len(rep["data_changed"]),
                 len(rep["data_new"]) + rep["strings_new"], rep["orig_size"], rep["size"]))
        for n in rep["changed"]:
            print("  compiled %s%s" % (n, " (appended, old address jumps to it)" if n in rep["redirected"] else " (appended)" if n in rep["appended"] else ""))
        if not rep["changed"] and not rep["data_changed"] and not rep["data_new"] and not rep["strings_new"]:
            print("  no changes: output is identical to the original")
    else:
        print("%s: rebuilt %d functions; %d -> %d bytes" % (target, rep["functions"], rep["orig_size"], rep["size"]))
    return 0


def cmd_check(paths, scn_dir):
    bad = 0
    for p in paths:
        try:
            prog = Program(p)
            n = 0
            for name in prog.scripts:
                f = prog.funcs[name]
                if f.body is not None:
                    FuncGen(prog, f).generate()
                    n += 1
            for dn in prog.data_order:
                prog.data_bytes(prog.data[dn], lambda s: 0)
            print("%s: ok, %d functions" % (p, n))
        except CompileError as e:
            print(e.text())
            bad += 1
    return 1 if bad else 0


def normalize(text, rename):
    lines = text.split("\n")
    if lines and lines[0].startswith("/*"):
        lines = lines[1:]
    body = "\n".join(lines)
    return re.sub(r"\b((?:fn|str|vec|tab|events|bin)_[0-9a-f]+)\b", lambda m: rename.get(m.group(1), m.group(1)), body)


def cmd_verify(src_dir, scn_dir, names, out_dir):
    import scn_decomp
    api = scn_decomp.load_api(DECOMP_OUT / "scn_api.json")
    if api is None:
        raise CompileError("run scn_decomp.py all first (scn_api.json is missing)")
    cls = scn_decomp.engine_classes(api)
    src_dir, out_dir = Path(src_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [src_dir / (n + ".c") for n in names] if names else sorted(src_dir.glob("*.c"))
    tot = {"scenes": 0, "patch_identical": 0, "functions": 0, "same": 0, "data_identical": 0}
    diffs = []
    t0 = time.time()
    for i, c in enumerate(files):
        try:
            b = Build(c, scn_dir)
            blob, rep = b.patch(False)
            tot["scenes"] += 1
            if blob == b.orig:
                tot["patch_identical"] += 1
            else:
                diffs.append("%s: patch build of the unmodified source differs (changed: %s)" % (b.scene, ", ".join(rep["changed"][:5])))
            rb, rrep, layout = b.rebuild()
            path = out_dir / (b.scene + ".scn")
            path.write_bytes(rb)
            orig_data = b.orig[b.side["header"][4]:]

            def orig_addr(sym, b=b):
                a = func_addr(sym)
                if a is not None and a in b.side_funcs:
                    return a
                return b.side_data[sym]["addr"]

            same_data = b"".join(b.prog.data_bytes(b.prog.data[dn], orig_addr) for dn in b.prog.data_order) == orig_data
            if same_data:
                tot["data_identical"] += 1
            else:
                diffs.append("%s: data segment compiled from C differs from the original" % b.scene)
            sc = scn_decomp.Scene(path, cls)
            sc.render()
            rename = {}
            for name, a in layout["functions"].items():
                rename["fn_%x" % a] = name
            for dn, a in layout["data"].items():
                for d in sc.data:
                    if d["addr"] == a:
                        rename[d["name"]] = dn
            src_funcs = {n: b.prog.func_text(b.prog.funcs[n]) for n in b.prog.scripts if b.prog.funcs[n].body is not None}
            for name, a in layout["functions"].items():
                tot["functions"] += 1
                got = sc.r.func_texts.get(a)
                want = normalize(src_funcs[name], {})
                have = normalize(got, rename) if got is not None else "<missing>"
                if want == have:
                    tot["same"] += 1
                elif len(diffs) < 400:
                    wl, hl = want.split("\n"), have.split("\n")
                    j = next((k for k in range(max(len(wl), len(hl))) if (wl[k] if k < len(wl) else None) != (hl[k] if k < len(hl) else None)), 0)
                    diffs.append("%s %s line %d:\n    source:    %s\n    recompiled: %s" % (b.scene, name, j + 1, wl[j] if j < len(wl) else "<end>", hl[j] if j < len(hl) else "<end>"))
        except CompileError as e:
            diffs.append(e.text())
        print("  %d/%d %s  %.0fs" % (i + 1, len(files), c.stem, time.time() - t0), flush=True)
    lines = ["verify %s  %.0fs" % (time.strftime("%Y-%m-%d %H:%M"), time.time() - t0),
             "scenes: %d" % tot["scenes"],
             "patch build of unmodified source identical to original: %d/%d" % (tot["patch_identical"], tot["scenes"]),
             "data segment compiled from C identical to the original: %d/%d" % (tot["data_identical"], tot["scenes"]),
             "functions whose recompiled code decompiles to the same C: %d/%d (%.1f%%)" % (tot["same"], tot["functions"], 100.0 * tot["same"] / max(1, tot["functions"])),
             "", "first differences:"] + diffs[:400]
    (out_dir / "verify.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:6]))
    print("details: %s" % (out_dir / "verify.txt"))
    return 0 if tot["patch_identical"] == tot["scenes"] else 1


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Compile scenario C back to .scn")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("source")
    b.add_argument("--scn", default=str(DEFAULT_SCN))
    b.add_argument("--out", default=str(DEFAULT_OUT))
    b.add_argument("--rebuild", action="store_true")
    b.add_argument("--force", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("sources", nargs="+")
    c.add_argument("--scn", default=str(DEFAULT_SCN))
    s = sub.add_parser("status")
    s.add_argument("src", nargs="?", default=str(DEFAULT_SRC))
    v = sub.add_parser("verify")
    v.add_argument("names", nargs="*")
    v.add_argument("--src", default=str(DECOMP_OUT))
    v.add_argument("--scn", default=str(DEFAULT_SCN))
    v.add_argument("--out", default=str(HERE / "out" / "verify"))
    a = ap.parse_args(argv)
    sys.setrecursionlimit(20000)
    try:
        if a.cmd == "build":
            return cmd_build(a.source, a.scn, a.out, a.rebuild, a.force)
        if a.cmd == "check":
            return cmd_check(a.sources, a.scn)
        if a.cmd == "status":
            for p in sorted(Path(a.src).glob("*.c")):
                if p.with_suffix(".json").exists():
                    ch = status_of(p)
                    print("%s\t%d\t%s" % (p.stem, len(ch), " ".join(ch)))
            return 0
        if a.cmd == "verify":
            return cmd_verify(a.src, a.scn, a.names, a.out)
    except CompileError as e:
        print(e.text())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
