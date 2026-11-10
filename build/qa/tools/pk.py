import sys,os; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import arc,struct,collections
e=arc.load(); d=arc.db()
byp={i['Path'].lower():i for i in d}
def get(p):
    i=byp[p]; return arc.read(e[(i['Hash'],i['HashPath'])])
b=get('/scene/01/jhd0/hihy.pkf')
print(len(b),b[:48].hex(),b[:48])
print(struct.unpack_from('<4I',b,0))
off=struct.calcsize('<4I')
fmt=collections.Counter()
n=0
while off+8<=len(b):
    sig=b[off:off+4]; sz=struct.unpack_from('<I',b,off+4)[0]
    if n<6: print(off,sig,sz,b[off+8:off+56].hex())
    if sig==b'TEXN':
        p=off+16
        while b[p:p+4]!=b'PVRT' and b[p:p+4]!=b'DDS ' and p<off+64: p+=4
        fmt[(b[p:p+4], b[p+8:p+10].hex() if b[p:p+4]==b'PVRT' else '')]+=1
    n+=1
    if sz==0 or sig not in(b'DUMY',b'TEXN'): print('stop',off,sig); break
    off+=sz
print(n,fmt)
