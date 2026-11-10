import sys, os, json, struct, gzip
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scn import path_hash
sm1 = Path(r"C:\Program Files (x86)\Steam\steamapps\common\SMLaunch\sm1")
db = json.loads(Path(r"C:\opt\shenmue\build\windows-asi\Release\filename_database.json").read_text())
entries = {}
for tad in sorted((sm1 / "archives/dx11/data").glob("*.tad")):
    d = tad.read_bytes()
    order, count = struct.unpack_from("<Q", d, 16)[0], struct.unpack_from("<I", d, 56)[0]
    for i in range(count):
        h1, h2, _, _, off, _, size, _ = struct.unpack_from("<8I", d, 60 + i * 32)
        if (h1, h2) not in entries or entries[(h1, h2)][0] < order:
            entries[(h1, h2)] = (order, tad.with_suffix(".tac"), off, size)
out = Path("subs"); out.mkdir(exist_ok=True)
for item in db:
    p = item["Path"]
    if not (p.startswith("/subs/") and ".sub" in p):
        continue
    hit = entries.get((item["Hash"], item["HashPath"])) or entries.get((path_hash(p.lstrip("/")), 0))
    print(p, "->", (hit[1].name, hit[3]) if hit else None)
    if hit:
        with open(hit[1], "rb") as f:
            f.seek(hit[2]); b = f.read(hit[3])
        if b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
        (out / p.rsplit("/", 1)[1]).write_bytes(b)

want = {}
for c in ["subs/english.sub", "/subs/english.sub", "subs/japanese.sub", "/subs/japanese.sub", "subs/english.sub.0db084a7"]:
    want[path_hash(c)] = c
print({hex(k): v for k, v in want.items()})
for (h1, h2), e in entries.items():
    if h1 in want or h2 in (0x0db084a7, 0x3bafa172):
        print(want.get(h1), hex(h1), hex(h2), e[1].name, e[3])
print(len(entries))

for (h1, h2), e in entries.items():
    names = {0x0db084a7: "english.sub", 0x3bafa172: "japanese.sub"}
    if h2 in names:
        with open(e[1], "rb") as f:
            f.seek(e[2]); b = f.read(e[3])
        print(names[h2], b[:4])
        if b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
        (out / names[h2]).write_bytes(b)

for n in ("english.sub", "japanese.sub"):
    b = (out / n).read_bytes()
    version, count = struct.unpack_from("<ii", b, 0)
    off, stride, field = {1: (8, 12, 8), 2: (16, 16, 12), 3: (16, 28, 24)}[version]
    base = off + count * stride
    print(n, "version", version, "count", count, "size", len(b))
    rows = {}
    for i in range(count):
        r = off + i * stride
        key = b[r:r + field].split(b"\0")[0].decode("latin1")
        o = struct.unpack_from("<I", b, r + field)[0]
        rows[key] = o
    memp = sorted(k for k in rows if k.startswith("MEMP"))
    print(len(memp), memp[:6], memp[-3:])
    for k in memp[:10]:
        o = base + rows[k]
        print(k, b[o:b.index(b"\0", o)][:90])
