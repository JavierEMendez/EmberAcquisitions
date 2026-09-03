"""Measure how much of each page a generated report actually uses.

Counts text blocks, images AND vector drawings -- an earlier version looked at
text alone and reported image-heavy pages as 97% full when they were half
empty, which sent two rounds of tuning in the wrong direction. The running
footer sits at ~95% and is excluded, or every page reads as full.
"""
import sys
import fitz

doc = fitz.open(sys.argv[1])
print(f"{doc.page_count} pages")
worst = []
for i, pg in enumerate(doc):
    H = pg.rect.height
    ys = [b[3] for b in pg.get_text("blocks") if b[3] < H * 0.93]
    ys += [im["bbox"][3] for im in pg.get_image_info()]
    ys += [d["rect"].y1 for d in pg.get_drawings() if d["rect"].y1 < H * 0.93]
    bot = max(ys, default=0)
    frac = bot / H
    head = [t.strip() for t in pg.get_text().split("\n") if t.strip()][2:4]
    flag = "   <-- WASTED" if frac < 0.72 else ""
    if frac < 0.72:
        worst.append((i + 1, frac))
    print(f"  p{i+1:>2}: ends {frac*100:>5.1f}%  {' / '.join(head)[:50]:<52}{flag}")
if worst:
    print("\ntrim the section feeding each of these so it fits one sheet:")
    for n, f in worst:
        print(f"  page {n}: {(1-f)*100:.0f}% of the sheet unused")
else:
    print("\nno page under 72% -- nothing obviously wasted")
