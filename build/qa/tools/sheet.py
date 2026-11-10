import sys
from pathlib import Path
from PIL import Image, ImageDraw
root=Path(sys.argv[1]); out=Path(sys.argv[2]); only=sys.argv[3:]
T=120; C=11
dirs=[d for d in sorted(root.iterdir()) if d.is_dir() and (not only or d.name in only)]
items=[(d.name,f) for d in dirs for f in sorted(d.glob('*.png'))]
rows=(len(items)+C-1)//C
s=Image.new('RGB',(C*(T+6),rows*(T+18)),(30,30,30)); dr=ImageDraw.Draw(s)
for i,(dn,f) in enumerate(items):
    im=Image.open(f).convert('RGBA'); im.thumbnail((T,T)); bg=Image.new('RGB',im.size,(80,0,80)); bg.paste(im,mask=im.getchannel('A'))
    x=i%C*(T+6); y=i//C*(T+18); s.paste(bg,(x,y)); dr.text((x,y+T+2),f'{dn[:6]}/{f.stem[:10]}',fill=(220,220,220))
s.save(out); print(len(items),s.size)
