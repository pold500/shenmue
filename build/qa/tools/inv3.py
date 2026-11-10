import sys,os; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import arc,re,collections
e=arc.load(); d=arc.db()
print('entries',len(e))
hits=[i for i in d if (i['Hash'],i['HashPath']) in e]
print('named',len(hits))
exts=collections.Counter(i['Path'].rsplit('.',1)[-1].lower() for i in hits if len(i['Path'].rsplit('.',1)[-1])<6)
print(exts.most_common(25))
for i in hits:
    p=i['Path'].lower()
    if re.search(r'doj|douj|dozy',p): print('DOJO',p,e[(i['Hash'],i['HashPath'])][3])
for i in hits:
    p=i['Path'].lower()
    if p.startswith('/scene/01/jhd0/') and p.endswith(('.pkf','.pks','.spr')): print(p,e[(i['Hash'],i['HashPath'])][3])
print([i['Path'] for i in hits if '/tex/' in i['Path'].lower()][:12])
