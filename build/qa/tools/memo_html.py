import html, re, struct
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path

OUT = Path(r"C:\opt\shenmue\docs\shenmue1_notebook.html")
b = Path("subs/english.sub").read_bytes()
version, count = struct.unpack_from("<ii", b, 0)
assert version == 3
base = 16 + count * 28
rows = {}
for i in range(count):
    r = 16 + i * 28
    key = b[r:r + 24].split(b"\0")[0].decode("latin1")
    if re.fullmatch(r"MEMP\d{3}L\d[A-Z0-9]", key):
        o = base + struct.unpack_from("<I", b, r + 24)[0]
        rows[key] = b[o:b.index(b"\0", o)].decode("utf-8")

def blank(key, text):
    t = text.strip()
    return not t or t == "[EMPTY LINE]" or text[2:12] == key

pages = OrderedDict()
empty_lines = 0
for key in sorted(rows):
    text = rows[key]
    if blank(key, text):
        empty_lines += 1
        continue
    pages.setdefault(int(key[4:7]), []).append((key, text))

total_lines = sum(len(v) for v in pages.values())
all_pages = {int(k[4:7]) for k in rows}

# Which story flag reveals each line (the notebook's own layout table, English variant).
REPO = Path(r"C:\opt\shenmue")
names = {m[0]: int(m[1]) for m in re.findall(r"SHENMUE_S1_FLAG\((\w+), (\d+)\)", (REPO / "src/common/script_catalogue.def").read_text())}
inc = (REPO / "src/s1/task/mmtk_notebook_flags.inc").read_text()
body = inc[inc.index("kLoadingFlags{"):]
body = body[body.index("{") + 1:body.index("}")]
words = [int(t[:-1], 16) if t.startswith("0x") else names[t.replace("SHENMUE_FLAG_", "")]
         for t in re.findall(r"0x[0-9A-Fa-f]+U|SHENMUE_FLAG_\w+", body)]
flag_of, seen = {}, {}
for r in range(0, len(words) - 8, 9):
    page = words[r] & 0xff
    if page == 0x80:
        break
    side = seen.get(page, 0); seen[page] = side + 1
    for line in range(8):
        flag_of[(2 * page + side, line)] = words[r + 1 + line]
print("line flags", len(flag_of))

NO_HYPHEN = {("Motor-", "cycle"), ("ware-", "house"), ("Ware-", "house")}
KEEP_HYPHEN = {("Hisaka-", "san"), ("Ying-", "Yang"), ("black-", "market")}
HARD_BREAK = {"MEMP001L2A", "MEMP001L6A"}  # separate phone numbers, not one sentence

def join(a, b):
    tail, head = a.split(" ")[-1], b.split(" ")[0]
    if (tail, head) in NO_HYPHEN:
        return a[:-1] + b
    if (tail, head) in KEEP_HYPHEN or (re.search(r"\d-$", a) and b[:1].isdigit()):
        return a + b
    return a + " " + b

def norm(t):
    return " ".join(t.split())

entries = OrderedDict()
for p, lines in pages.items():
    cur, out, prev_line = None, [], None
    for k, t in lines:
        n = int(k[8]); t2 = norm(t); fl = flag_of.get((p, n))
        new = (cur is None or n != prev_line + 1 or fl != cur["flag"] or t2 == "*Clue"
               or k in HARD_BREAK or re.fullmatch(r"<.*>", cur["text"]) is not None)
        # A sentence the game reveals in two steps: the next line starts in lower case right after.
        if new and cur is not None and n == prev_line + 1 and fl != cur["flag"] and t2[:1].islower():
            print("sentence over flags:", k, repr(cur["text"][-30:]), repr(t2))
            new = False
        if new:
            cur = {"first": k, "last": k, "text": t2, "flag": fl}; out.append(cur)
        else:
            cur["text"] = join(cur["text"], t2); cur["last"] = k
        prev_line = n
    entries[p] = out

n_entries = sum(len(v) for v in entries.values())
def key_label(e):
    return e["first"] if e["first"] == e["last"] else f'{e["first"]}–L{e["last"][8]}A'
def short(t, n=52):
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"
def strip_clue(t):
    return t[6:] if t.startswith("*Clue ") else t

toc = "\n".join(f'<li><a href="#p{p:03d}"><span class="n">{p:03d}</span>{html.escape(short(strip_clue(l[0]["text"])))}</a></li>' for p, l in entries.items())
secs = []
for p, l in entries.items():
    trs = "\n".join(f'<tr id="{e["first"]}"><td class="k"><code>{key_label(e)}</code></td><td>{html.escape(e["text"])}</td></tr>' for e in l)
    secs.append(f'<section id="p{p:03d}"><h2>Страница {p:03d}<a class="top" href="#toc">к оглавлению ↑</a></h2>\n'
                f'<table><thead><tr><th>Ключ</th><th>Строка</th></tr></thead><tbody>\n{trs}\n</tbody></table></section>')
doc = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Блокнот Shenmue I</title>
<style>
:root{{--bg:#fbfaf7;--fg:#22211e;--muted:#6f6b63;--line:#e2ded5;--link:#8a3b12;--head:#f1eee6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#191917;--fg:#e8e5de;--muted:#9c978d;--line:#34332f;--link:#e9a26f;--head:#23221f}}}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0 auto;max-width:920px;padding:24px 16px 64px;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,"Segoe UI",sans-serif}}
h1{{font-size:1.7rem;margin:0 0 4px}}
.sub{{color:var(--muted);margin:0 0 24px}}
h2{{font-size:1.1rem;margin:0 0 8px;display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
a{{color:var(--link);text-decoration:none}}
a:hover{{text-decoration:underline}}
.top{{font-size:.8rem;font-weight:400}}
#toc ol{{list-style:none;margin:0;padding:0;columns:2;column-gap:24px}}
#toc li{{break-inside:avoid;padding:2px 0}}
#toc .n{{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:8px}}
section{{margin-top:32px;scroll-margin-top:12px}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:.8rem;color:var(--muted);background:var(--head);font-weight:600}}
th,td{{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
td{{overflow-wrap:anywhere}}
.k{{width:12.5em;white-space:nowrap}}
code{{font:.85rem ui-monospace,Consolas,monospace}}
@media (max-width:640px){{#toc ol{{columns:1}}.k{{width:auto;white-space:normal}}}}
</style></head><body>
<h1>Блокнот Shenmue I</h1>
<p class="sub">{n_entries} записей на {len(entries)} страницах (склеено из {total_lines} строк). Источник: таблица строк <code>/subs/english.sub</code>, ключи <code>MEMP</code>; запись — соседние строки, которые игра открывает одним флагом.</p>
<nav id="toc"><h2>Оглавление</h2><ol>
{toc}
</ol></nav>
{chr(10).join(secs)}
</body></html>
"""
OUT.write_text(doc, encoding="utf-8")

class C(HTMLParser):
    ids, hrefs = set(), []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a: self.ids.add(a["id"])
        if tag == "a": self.hrefs.append(a["href"][1:])
c = C(); c.feed(doc)
print("entries", n_entries, "from lines", total_lines, "pages", len(entries), "broken links", [h for h in c.hrefs if h not in c.ids], "dup ids", n_entries + 2*0)
# Nothing lost: same characters as the source apart from spaces and the removed wrap hyphens.
sq = lambda x: re.sub(r"\s+", "", x)
src = "".join(sq(t) for l in pages.values() for _, t in l)
dst = "".join(sq(e["text"]) for l in entries.values() for e in l)
print("chars", len(src), len(dst), "diff", len(src) - len(dst))
missing = [k for k in (kk for l in pages.values() for kk, _ in l) if (int(k[4:7]), int(k[8])) not in flag_of]
print("lines without layout record:", missing[:10], len(missing))
for p in (1, 2, 16, 26, 33, 47, 57, 62, 64, 65, 72, 73, 81, 94):
    print(p, [(key_label(e), e["text"]) for e in entries[p]])
