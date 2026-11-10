import re
src=open('memo_html.py',encoding='utf-8').read().split('odd = sorted')[0].replace('OUT = Path','OUT_ = Path')
exec(src)
for p,l in pages.items():
    n=[(int(k[8]),t.rstrip()) for k,t in l]
    print(p,' | '.join(f'{i}:{t}' for i,t in n).encode('ascii','replace').decode())
