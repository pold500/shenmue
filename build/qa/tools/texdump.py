"""Dump every PVR texture of a Shenmue I area to PNG.
usage: texdump.py <scene> <area> <out dir>     e.g. texdump.py 01 jhd0 ../../extracted/s1_jhd0
Reads the TAD/TAC archives via arc.py; decoding follows ShenmueDK's PVR codecs."""
import sys, os, re, struct, hashlib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path
import numpy as np
from PIL import Image
import arc

def twiddle_table(n):
    t = np.zeros(n, dtype=np.int64)
    for i in range(n):
        v, r, b = i, 0, 0
        while v:
            r |= (v & 1) << (2 * b); v >>= 1; b += 1
        t[i] = r
    return t

def tw_index(w, h):
    side = max(w, h)
    t = twiddle_table(side)
    ys, xs = np.mgrid[0:h, 0:w]
    return (t[xs] << 1) | t[ys]

def to_rgba(px, fmt):
    px = px.astype(np.uint32)
    if fmt == 0:   # ARGB1555
        a = ((px >> 15) & 1) * 255; r = ((px >> 10) & 31) * 255 // 31; g = ((px >> 5) & 31) * 255 // 31; b = (px & 31) * 255 // 31
    elif fmt == 1: # RGB565
        a = np.full_like(px, 255); r = ((px >> 11) & 31) * 255 // 31; g = ((px >> 5) & 63) * 255 // 63; b = (px & 31) * 255 // 31
    elif fmt == 2: # ARGB4444
        a = ((px >> 12) & 15) * 17; r = ((px >> 8) & 15) * 17; g = ((px >> 4) & 15) * 17; b = (px & 15) * 17
    elif fmt == 5: # RGB555
        a = np.full_like(px, 255); r = ((px >> 10) & 31) * 255 // 31; g = ((px >> 5) & 31) * 255 // 31; b = (px & 31) * 255 // 31
    else:
        return None
    return np.stack([r, g, b, a], -1).astype(np.uint8)

def yuv(px):
    """px: h x w array of 16-bit YUV422 pairs."""
    h, w = px.shape
    out = np.zeros((h, w, 4), dtype=np.uint8); out[..., 3] = 255
    y = (px >> 8).astype(np.float32); c = (px & 255).astype(np.float32)
    u = c[:, ::2].repeat(2, 1)[:, :w]; v = c[:, 1::2].repeat(2, 1)[:, :w]
    out[..., 0] = np.clip(y + 1.375 * (v - 128), 0, 255)
    out[..., 1] = np.clip(y - 0.6875 * (v - 128) - 0.34375 * (u - 128), 0, 255)
    out[..., 2] = np.clip(y + 1.71875 * (u - 128), 0, 255)
    return out

def pixels(px, fmt):
    return yuv(px) if fmt == 3 else to_rgba(px, fmt)

def decode(b, p):
    """Returns (rgba array, reason)."""
    size, fmt, dat, _, w, h = struct.unpack_from('<IBBHHH', b, p + 4)
    end = p + 8 + size
    if end > len(b): return None, 'truncated'
    d = p + 16
    if dat in (1, 2, 0x12):
        tl = end - w * h * 2 if dat != 1 else d
        raw = np.frombuffer(b, '<u2', w * h, tl)
        return pixels(raw[tw_index(w, h)], fmt), None
    if dat == 0x0d:
        s = min(w, h); idx = tw_index(s, s); n = s * s
        rows = []
        cols = w // s; lines = h // s
        out = np.zeros((h, w), dtype=np.uint16)
        for k in range(cols * lines):
            raw = np.frombuffer(b, '<u2', n, d + k * n * 2)[idx]
            x, y = (k * s, 0) if w > h else (0, k * s)
            out[y:y + s, x:x + s] = raw
        return pixels(out, fmt), None
    if dat in (9, 0x0b):
        return pixels(np.frombuffer(b, '<u2', w * h, d).reshape(h, w), fmt), None
    if dat in (3, 4, 0x10, 0x11):
        small = dat >= 0x10
        entries = 256 if not small else (16 if w <= 16 else 32 if w == 32 else 128 if w == 64 else 256)
        book = np.frombuffer(b, '<u2', entries * 4, d)
        cw, ch = w // 2, h // 2
        ip = (end - cw * ch) if dat in (4, 0x11) else d + entries * 8
        ind = np.frombuffer(b, 'u1', cw * ch, ip)[tw_index(cw, ch)]
        blocks = book[ind.astype(np.int32) * 4].astype(np.uint16)
        out = np.zeros((h, w), dtype=np.uint16)
        for i, (dx, dy) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):  # codebook: TL, BL, TR, BR
            out[dy::2, dx::2] = book[ind.astype(np.int32) * 4 + i]
        return pixels(out, fmt), None
    return None, f'data 0x{dat:02x}'

def scan(b):
    for m in re.finditer(b'PVRT', b):
        p = m.start()
        if p + 16 > len(b): continue
        size, fmt, dat, _, w, h = struct.unpack_from('<IBBHHH', b, p + 4)
        if fmt > 6 or not (1 <= dat <= 0x12) or not (8 <= w <= 2048 and 8 <= h <= 2048) or size < 16: continue
        name = None
        q = b.rfind(b'TEXN', max(0, p - 64), p)
        if q >= 0:
            raw = b[q + 8:q + 16]
            name = re.sub(r'[^A-Za-z0-9_]', '', raw.decode('latin1'))
        yield p, name or f'tex_{p:08x}', w, h, fmt, dat, b[p + 16:p + 8 + size]

def main():
    scene, area, out = sys.argv[1], sys.argv[2].lower(), Path(sys.argv[3])
    e = arc.load(); seen = {}; stats = collections.Counter(); index = []
    prefix = f'/scene/{scene}/{area}/'
    for i in arc.db():
        path = i['Path'].lower()
        key = (i['Hash'], i['HashPath'])
        if not path.startswith(prefix) or key not in e or path.count('/') != 4: continue
        if not path.endswith(('.pkf', '.pks', '.mt5', '.spr', '.pvr')): continue
        b = arc.read(e[key])
        src = Path(path).name.replace('.', '_')
        for p, name, w, h, fmt, dat, payload in scan(b):
            digest = hashlib.sha1(payload).hexdigest()
            if digest in seen:
                stats['duplicates'] += 1; index.append((src, name, w, h, fmt, dat, seen[digest])); continue
            try:
                rgba, why = decode(b, p)
            except Exception as ex:
                rgba, why = None, repr(ex)
            if rgba is None:
                stats[f'skipped {why}'] += 1; continue
            d = out / src; d.mkdir(parents=True, exist_ok=True)
            f = d / f'{name}.png'; n = 1
            while f.exists():
                f = d / f'{name}_{n}.png'; n += 1
            Image.fromarray(rgba, 'RGBA').save(f, optimize=False)
            seen[digest] = str(f.relative_to(out)); stats['written'] += 1
            index.append((src, name, w, h, fmt, dat, seen[digest]))
    (out / 'index.tsv').write_text('source\tname\tw\th\tpixel\tdata\tfile\n' + ''.join('\t'.join(map(str, r)) + '\n' for r in index), encoding='utf-8')
    print(dict(stats))

main()
