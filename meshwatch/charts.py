"""Immagini per Telegram: mappa statica con traccia, ultima posizione, gateway e ripetitori.

Servono per vedere da fuori casa quello che mostra la pagina, senza esporla su Internet.
Le immagini si disegnano con Pillow. Se manca, il bot risponde in testo. Le tessere delle mappe passano dalla cache del Raspberry (tiles.py),
la stessa usata dalla pagina, con le stesse regole di OpenStreetMap.
"""
import asyncio
import importlib.util
import io
import math
import os

TILE = 256
PALETTE = ["#2f80ed", "#e5484d", "#3ccf8e", "#f2994a", "#9b51e0", "#00a3bf", "#d6409f", "#8d6e63"]
INK = "#1f2937"
FONT_PATHS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf")


def available():
    return importlib.util.find_spec("PIL") is not None


_fonts = {}


def font(size):
    """Font con le lettere accentate: DejaVu se c'è, altrimenti quello incluso in Pillow."""
    if size in _fonts:
        return _fonts[size]
    from PIL import ImageFont
    f = None
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                break
            except OSError:
                pass
    if f is None:
        try:
            f = ImageFont.load_default(size=size)   # Pillow >= 10.1
        except TypeError:
            f = ImageFont.load_default()
    _fonts[size] = f
    return f


def _rgb(color, alpha=None):
    c = (color or "#2f80ed").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        rgb = (47, 128, 237)
    return rgb + (alpha,) if alpha is not None else rgb


def _text_w(draw, text, f):
    return draw.textbbox((0, 0), text, font=f)[2]


# ---------------------------------------------------------------- mappe
def _px(lat, lon, z):
    lat = max(-85.0511, min(85.0511, lat))
    n = TILE * 2 ** z
    x = (lon + 180) / 360 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _fit_zoom(points, w, h, max_z=16):
    for z in range(max_z, 1, -1):
        xy = [_px(la, lo, z) for la, lo in points]
        if (max(x for x, _ in xy) - min(x for x, _ in xy) <= w
                and max(y for _, y in xy) - min(y for _, y in xy) <= h):
            return z
    return 2


async def static_map(get_tile, tracks=(), markers=(), width=1000, height=760, max_zoom=16):
    """JPEG della zona che contiene tutto quello da disegnare. Ritorna None se non c'è nulla.
    get_tile(z, x, y) -> bytes (async); se fallisce la tessera resta grigia.
    tracks:  [{color, points: [(lat, lon)], label?}]
    markers: [{lat, lon, color, label?, shape: 'dot'|'diamond'|'square', size?, fit?}]"""
    def valid(pts):
        return [(la, lo) for la, lo in pts if la is not None and lo is not None and (la or lo)]
    # i segnaposto con fit=False (ripetitori, gateway) non allargano la zona: compaiono se ci stanno
    pts = valid([p for t in tracks for p in t["points"]]
                + [(m["lat"], m["lon"]) for m in markers if m.get("fit", True)])
    if not pts:
        pts = valid([(m["lat"], m["lon"]) for m in markers])
    if not pts:
        return None
    pad = 70
    z = _fit_zoom(pts, width - 2 * pad, height - 2 * pad, max_zoom)
    if len(set(pts)) == 1:
        z = min(max_zoom, 15)
    xy = [_px(la, lo, z) for la, lo in pts]
    cx = (max(x for x, _ in xy) + min(x for x, _ in xy)) / 2
    cy = (max(y for _, y in xy) + min(y for _, y in xy)) / 2
    ox, oy = cx - width / 2, cy - height / 2
    n = 2 ** z
    tiles = [(tx, ty) for tx in range(math.floor(ox / TILE), math.floor((ox + width - 1) / TILE) + 1)
             for ty in range(math.floor(oy / TILE), math.floor((oy + height - 1) / TILE) + 1) if 0 <= ty < n]

    async def one(tx, ty):
        try:
            return (tx, ty), await get_tile(z, tx % n, ty)
        except Exception:
            return (tx, ty), None
    got = await asyncio.gather(*(one(tx, ty) for tx, ty in tiles))
    missing = sum(1 for _, data in got if data is None)
    proj = lambda la, lo: tuple(a - b for a, b in zip(_px(la, lo, z), (ox, oy)))   # noqa: E731
    return await asyncio.to_thread(_compose, got, missing, len(tiles), proj, ox, oy, width, height,
                                   tracks, markers)


def _compose(got, missing, total, proj, ox, oy, width, height, tracks, markers):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), "#e8e6e1")
    for (tx, ty), data in got:
        if not data:
            continue
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGB")
            img.paste(tile, (round(tx * TILE - ox), round(ty * TILE - oy)))
        except Exception:
            pass
    img = img.convert("RGBA")
    d = ImageDraw.Draw(img)
    for t in tracks:
        xy = [proj(la, lo) for la, lo in t["points"]]
        col = _rgb(t.get("color"))
        if len(xy) > 1:
            d.line(xy, fill=(255, 255, 255), width=7, joint="curve")
            d.line(xy, fill=col, width=4, joint="curve")
        if xy:
            x, y = xy[0]
            d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(255, 255, 255), outline=col, width=3)
    labels = []
    for m in markers:
        x, y = proj(m["lat"], m["lon"])
        s = m.get("size", 8)
        col = _rgb(m.get("color"))
        shape = m.get("shape", "dot")
        if shape == "diamond":
            d.polygon([(x, y - s - 2), (x + s + 2, y), (x, y + s + 2), (x - s - 2, y)], fill=col, outline=INK)
        elif shape == "square":
            d.rectangle([x - s, y - s, x + s, y + s], fill=col, outline=INK, width=2)
        else:
            d.ellipse([x - s - 3, y - s - 3, x + s + 3, y + s + 3], fill=(255, 255, 255))
            d.ellipse([x - s, y - s, x + s, y + s], fill=col)
        if m.get("label"):
            labels.append((x + s + 6, y - 10, m["label"], m.get("bold")))
    for x, y, text, bold in labels:   # sopra a tutto, con il bordo bianco per leggerle sulla mappa
        d.text((x, y), text, fill=INK, font=font(17 if bold else 15), stroke_width=3, stroke_fill=(255, 255, 255))
    note = "© OpenStreetMap contributors"
    if missing:
        note = (f"{missing} tessere su {total} non disponibili · " if missing < total else "mappa non disponibile · ") + note
    f = font(13)
    w = _text_w(d, note, f)
    d.rectangle([width - w - 14, height - 22, width, height], fill=(255, 255, 255, 210))
    d.text((width - w - 7, height - 20), note, fill=INK, font=f)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue()
