"""Archive access: resolve db paths to blobs in the TAD/TAC files."""
import gzip, json, struct, sys
from pathlib import Path
SM1=Path(r'C:\Program Files (x86)\Steam\steamapps\common\SMLaunch\sm1')
DB=Path(__file__).resolve().parents[3]/'libs/ShenmueDK/resources/filename_database.json'
def load():
    e={}
    for tad in sorted((SM1/'archives/dx11/data').glob('*.tad')):
        d=tad.read_bytes(); order=struct.unpack_from('<Q',d,16)[0]; n=struct.unpack_from('<I',d,56)[0]
        for i in range(n):
            h1,h2,_,_,off,_,size,_=struct.unpack_from('<8I',d,60+i*32)
            if (h1,h2) not in e or e[(h1,h2)][0]<order: e[(h1,h2)]=(order,tad.with_suffix('.tac'),off,size)
    return e
def db(): return json.load(open(DB))
def read(hit):
    _,tac,off,size=hit
    with open(tac,'rb') as f:
        f.seek(off); b=f.read(size)
    return gzip.decompress(b) if b[:2]==b'\x1f\x8b' else b
