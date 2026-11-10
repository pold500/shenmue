import json
d=json.load(open('../../../libs/ShenmueDK/resources/filename_database.json'))
for i in d:
    p=i['Path'].lower()
    if ('/yd01/' in p or '/maps/jhd0' in p) and '.xxx_' not in p and not p.startswith('/mods'): print(p)
