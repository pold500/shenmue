import sys,os; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import arc,struct
e=arc.load(); byp={i['Path'].lower():i for i in arc.db()}
for n in ('hihy','jhd0','kakg','jhw0','tgma'):
    i=byp[f'/scene/01/jhd0/{n}.pks']; b=arc.read(e[(i['Hash'],i['HashPath'])])
    p=b.find(b'IPAC')
    print(n,len(b),b[:4],'IPAC at',p,'TEXL',b.count(b'TEXL'),'TEXD',b.count(b'TEXD'),'PVRT',b.count(b'PVRT'))
    if p<0: continue
    sig,dic,cnt,cs=struct.unpack_from('<4I',b,p)
    names=[]
    for k in range(cnt):
        nm=b[p+dic+k*20:p+dic+k*20+12]
        names.append(nm[:8].decode('latin1').strip('\0 ')+'.'+nm[8:12].decode('latin1').strip('\0 _'))
    print('  ',names[:70])
