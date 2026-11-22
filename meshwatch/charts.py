"""Immagini per Telegram: grafici nel tempo e mappe statiche con traccia, ripetitori e copertura.

Servono per vedere da fuori casa quello che mostra la pagina, senza esporla su Internet.
Le immagini si disegnano con Pillow. Se manca, le funzioni sollevano ImportError e il bot
risponde in testo. Le tessere delle mappe passano dalla cache del Raspberry (tiles.py),
la stessa usata dalla pagina, con le stesse regole di OpenStreetMap.
"""
import asyncio
import importlib.util
import io
import math
import os
import statistics
from datetime import datetime

TILE = 256
PALETTE = ["#2f80ed", "#e5484d", "#3ccf8e", "#f2994a", "#9b51e0", "#00a3bf", "#d6409f", "#8d6e63"]
INK, MUTED, GRID, BG = "#1f2937", "#6b7280", "#e5e7eb", "#ffffff"
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


# ---------------------------------------------------------------- grafici
def _nice_ticks(lo, hi, n=4):
    """Valori "tondi" per l'asse verticale."""
    span = hi - lo
    raw = span / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 1e-6:
        ticks.append(round(v, 10))
        v += step
    return ticks, step


TIME_STEPS = [600, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400,
              7 * 86400, 14 * 86400, 30 * 86400, 60 * 86400, 90 * 86400]


def _time_ticks(t0, t1, max_n=7):
    span = max(1, t1 - t0)
    step = next((s for s in TIME_STEPS if span / s <= max_n), TIME_STEPS[-1])
    # allineati all'ora locale: mezzanotte per i passi di un giorno o più
    midnight = int(datetime.fromtimestamp(t0).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    first = midnight if step >= 86400 else midnight + ((t0 - midnight) // step) * step
    ticks = []
    t = first
    while t <= t1:
        if t >= t0:
            ticks.append(t)
        t += step
    fmt = "%H:%M" if span <= 36 * 3600 else ("%d/%m %Hh" if step < 86400 else "%d/%m")
    return ticks, fmt


def _segments(points, gap=None):
    """Spezza la linea dove mancano dati per molto tempo (radio spenta, fuori copertura)."""
    if len(points) < 2:
        return [points]
    if gap is None:
        dts = [b[0] - a[0] for a, b in zip(points, points[1:]) if b[0] > a[0]]
        med = statistics.median(dts) if dts else 0
        gap = max(3 * 3600, 4 * med)
    segs, cur = [], [points[0]]
    for a, b in zip(points, points[1:]):
        if b[0] - a[0] > gap:
            segs.append(cur)
            cur = []
        cur.append(b)
    segs.append(cur)
    return segs


def line_chart(title, panels, t0, t1, subtitle=""):
    """PNG con un pannello per serie, uno sopra l'altro, stesso asse del tempo.
    panels: [{label, unit, dec, points: [(ts, valore)], color?}]"""
    from PIL import Image, ImageDraw
    panels = [p for p in panels if p.get("points")]
    W, PH, L, R, TOP, GAP, BOT = 1000, 190, 84, 24, 70 if subtitle else 52, 34, 44
    H = TOP + len(panels) * (PH + GAP) - GAP + BOT if panels else TOP + 80
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((L, 14), title, fill=INK, font=font(22))
    if subtitle:
        d.text((L, 42), subtitle, fill=MUTED, font=font(15))
    if not panels:
        d.text((L, TOP + 20), "Nessun dato nel periodo.", fill=MUTED, font=font(18))
        return _png(img)
    t1 = max(t1, t0 + 60)
    xs = lambda t: L + (t - t0) / (t1 - t0) * (W - L - R)   # noqa: E731
    ticks_t, tfmt = _time_ticks(t0, t1)
    for i, p in enumerate(panels):
        top = TOP + i * (PH + GAP)
        bottom = top + PH
        vals = [v for _, v in p["points"]]
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            pad = abs(hi) * 0.05 or 1
            lo, hi = lo - pad, hi + pad
        else:
            pad = (hi - lo) * 0.12
            lo, hi = lo - pad, hi + pad
        hi = lo + (hi - lo) * PH / (PH - 30)   # striscia libera in alto per titolo e valori
        ticks_v, step = _nice_ticks(lo, hi)
        dec = max(p.get("dec", 1), 0 if step >= 1 else min(3, -math.floor(math.log10(step))))
        ys = lambda v: bottom - (v - lo) / (hi - lo) * PH   # noqa: E731
        # griglia e assi
        d.rectangle([L, top, W - R, bottom], outline=GRID)
        for v in ticks_v:
            y = ys(v)
            d.line([L, y, W - R, y], fill=GRID)
            lab = f"{v:.{dec}f}"
            d.text((L - 8 - _text_w(d, lab, font(14)), y - 8), lab, fill=MUTED, font=font(14))
        for t in ticks_t:
            x = xs(t)
            d.line([x, top, x, bottom], fill=GRID)
        color = _rgb(p.get("color") or PALETTE[i % len(PALETTE)])
        unit = f" ({p['unit']})" if p.get("unit") else ""
        last = p["points"][-1][1]
        head = f"{p['label']}{unit}"
        for seg in _segments(p["points"]):
            pts = [(xs(t), ys(v)) for t, v in seg]
            if len(pts) > 1:
                d.line(pts, fill=color, width=3, joint="curve")
            if len(p["points"]) <= 60 or len(pts) == 1:
                for x, y in pts:
                    d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=color)
        # titolo e valori sopra la linea, su fondo chiaro per leggerli anche dove passa
        dp = p.get("dec", 1)
        info = f"ultimo {last:.{dp}f} · min {min(vals):.{dp}f} · max {max(vals):.{dp}f}"
        wi = _text_w(d, info, font(14))
        d.rectangle([L + 1, top + 1, L + 16 + _text_w(d, head, font(16)), top + 27], fill=(255, 255, 255))
        d.rectangle([W - R - 16 - wi, top + 1, W - R - 1, top + 27], fill=(255, 255, 255))
        d.text((L + 8, top + 6), head, fill=INK, font=font(16))
        d.text((W - R - 8 - wi, top + 8), info, fill=MUTED, font=font(14))
    # etichette del tempo sotto l'ultimo pannello
    yb = TOP + len(panels) * (PH + GAP) - GAP + 8
    for t in ticks_t:
        lab = datetime.fromtimestamp(t).strftime(tfmt)
        x = xs(t) - _text_w(d, lab, font(14)) / 2
        if L - 20 <= x <= W - R - 20:
            d.text((x, yb), lab, fill=MUTED, font=font(14))
    return _png(img)


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


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


async def static_map(get_tile, tracks=(), markers=(), cells=(), width=1000, height=760, max_zoom=16):
    """JPEG della zona che contiene tutto quello da disegnare. Ritorna None se non c'è nulla.
    get_tile(z, x, y) -> bytes (async); se fallisce la tessera resta grigia.
    tracks:  [{color, points: [(lat, lon)], label?}]
    markers: [{lat, lon, color, label?, shape: 'dot'|'diamond'|'square', size?}]
    cells:   [{bounds: [[lat1, lon1], [lat2, lon2]], color, alpha?}]"""
    def valid(pts):
        return [(la, lo) for la, lo in pts if la is not None and lo is not None and (la or lo)]
    # i segnaposto con fit=False (ripetitori, gateway) non allargano la zona: compaiono se ci stanno
    pts = valid([p for t in tracks for p in t["points"]] + [tuple(c) for cell in cells for c in cell["bounds"]]
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
                                   tracks, markers, cells)


def _compose(got, missing, total, proj, ox, oy, width, height, tracks, markers, cells):
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
    over = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for c in cells:
        (la1, lo1), (la2, lo2) = c["bounds"]
        x1, y1 = proj(la1, lo1)
        x2, y2 = proj(la2, lo2)
        box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        d.rectangle(box, fill=_rgb(c["color"], c.get("alpha", 120)), outline=_rgb(c["color"], 200))
    img = Image.alpha_composite(img.convert("RGBA"), over)
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


def coverage_color(ok, fail):
    """Stessi colori della mappa della copertura sulla pagina."""
    n = ok + fail
    r = ok / n if n else 0
    return "#3ccf8e" if r >= .8 else "#e8c547" if r >= .5 else "#ff7a45" if r > 0 else "#e5484d"
