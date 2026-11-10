import json, sys, collections
d=json.load(open('../../../libs/ShenmueDK/resources/filename_database.json'))
print(len(d), d[0])
c=collections.Counter()
for i in d:
    p=i['Path'].lower()
    if p.startswith('/scene/'):
        q=p.split('/')
        if len(q)>3: c[(q[2],q[3])]+=1
print(sorted(k for k in c if k[1].startswith(('yd','jd','jh','ju')) ))
for i in d:
    p=i['Path'].lower()
    if '/yd01/' in p or '/jhd0/' in p: print(p)
