"""
Generates the app's images:

  * assets/icon.png         (1024x1024) - the app bundle / Dock icon
  * assets/icon-muted.png   (44x44)     - the menu bar icon shown while muted

Pure numpy + zlib, so it needs no image libraries. Rendered 4x supersampled
and box-filtered down for clean anti-aliased edges.

    python3 -m tools.make_icon
"""

from __future__ import annotations

import os
import struct
import zlib

import numpy as np

SIZE = 1024
SS = 4          # supersample factor
S = SIZE * SS

#: Menu bar image: 44 px is 22 pt at 2x, the usable height of the menu bar.
#: rumps resizes whatever it is given to 20 pt, so a slightly denser source is
#: exactly what a Retina menu bar wants.
MENUBAR_SIZE = 44
MENUBAR_SS = 8

#: Proportions of the keybed, shared by both images so the muted glyph is the
#: app icon's keyboard rather than a second, unrelated drawing: 7 naturals over
#: a band of sharps that reaches 61.5 % of the way down them.
KEYBED_WHITES = 7
KEYBED_BLACK_RATIO = 0.615


def _rounded_rect_mask(h, w, x0, y0, x1, y1, radius):
    """Antialiasing comes from supersampling, so a hard mask is fine here."""
    # 1-D row/column vectors broadcast to the same result as a full mgrid at
    # 1/h (resp. 1/w) of the memory — this helper is called 14 times at 4096².
    yy = np.arange(h, dtype=np.float32)[:, None]
    xx = np.arange(w, dtype=np.float32)[None, :]
    # A radius larger than half a side would invert the clip bounds below,
    # which numpy answers silently with a wrong corner centre.
    radius = min(radius, 0.5 * (x1 - x0), 0.5 * (y1 - y0))
    inner_x = np.clip(xx, x0 + radius, x1 - radius)
    inner_y = np.clip(yy, y0 + radius, y1 - radius)
    dist = np.hypot(xx - inner_x, yy - inner_y)
    inside = (xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)
    return (inside & (dist <= radius)).astype(np.float32)


def _blend(canvas, mask, color):
    m = mask[..., None]
    col = np.array(color, dtype=np.float32).reshape(1, 1, 3)
    canvas *= 1.0 - m
    canvas += m * col


def render() -> np.ndarray:
    """Pastel glass tile with a raised, immediately recognizable piano keybed."""
    yy = np.arange(S, dtype=np.float32)[:, None] / S
    xx = np.arange(S, dtype=np.float32)[None, :] / S
    canvas = np.zeros((S, S, 3), dtype=np.float32)

    def rect(x0, y0, x1, y1, radius):
        return _rounded_rect_mask(S, S, x0*S, y0*S, x1*S, y1*S, radius*S)

    def paint(mask, color, opacity=1.0):
        m = (mask * opacity)[..., None]
        canvas[:] = canvas * (1.0 - m) + np.asarray(color, dtype=np.float32) * m

    tile = rect(.045, .045, .955, .955, .205)
    t = np.clip(.62*xx + .60*yy - .10, 0, 1)[..., None]
    pink = np.array([1.0, .77, .87], dtype=np.float32)
    blue = np.array([.57, .80, 1.0], dtype=np.float32)
    paint(tile, pink*(1-t) + blue*t)
    # Broad reflected light and a restrained lilac edge give the glass depth.
    light = np.exp(-((xx-.26)**2+(yy-.19)**2)/.085)
    paint(tile, [1, .98, 1], light*.68)
    inner = rect(.055, .055, .945, .945, .197)
    paint(np.maximum(tile-inner, 0), [1, 1, 1], .78)
    lower = rect(.060, .067, .940, .945, .193)
    paint(np.maximum(inner-lower, 0), [.65, .68, .90], .22)

    # The piano floats above the colored glass. Soft shadow, no decorative text.
    shadow = np.exp(-((xx-.51)/.33)**8 - ((yy-.66)/.19)**8)
    paint(tile, [.35, .43, .69], shadow*.23)
    bed = rect(.147, .295, .853, .770, .046)
    paint(bed, [.47, .59, .78], .58)
    edge = rect(.147, .283, .853, .742, .043)
    paint(edge, [1, 1, 1], .55)
    left, right, top, bottom = .17, .83, .308, .725
    width = (right-left)/KEYBED_WHITES
    for i in range(KEYBED_WHITES):
        x0, x1 = left+i*width+.003, left+(i+1)*width-.003
        key = rect(x0, top, x1, bottom, .015)
        depth = np.clip((yy-top)/(bottom-top), 0, 1)[..., None]
        color = np.array([1., .985, .995])*(1-depth) + np.array([.81, .90, 1.])*depth
        paint(key, color)
        lip = rect(x0+.003, bottom-.014, x1-.003, bottom-.004, .006)
        paint(lip, [1, 1, 1], .78)
    for i in (1, 2, 4, 5, 6):
        center = left+i*width
        black_w = width*.59
        end = top + (bottom-top)*KEYBED_BLACK_RATIO
        paint(rect(center-black_w/2-.004, top+.006,
                   center+black_w/2+.006, end+.009, .012), [.35, .47, .66], .25)
        key = rect(center-black_w/2, top-.005, center+black_w/2, end, .011)
        depth = np.clip((yy-top)/(end-top), 0, 1)[..., None]
        color = np.array([.24, .32, .48])*(1-depth) + np.array([.36, .47, .64])*depth
        paint(key, color)
        paint(rect(center-black_w/2+.006, top+.002,
                   center+black_w/2-.006, top+.011, .004), [.82, .89, 1], .8)
    # One satin reflection above the keyboard, kept quiet at small icon sizes.
    reflection = rect(.205, .204, .795, .221, .008)
    paint(reflection, [1, 1, 1], .63)
    canvas *= tile[..., None]
    rgba = np.concatenate([canvas, tile[..., None]], axis=2)
    small = rgba.reshape(SIZE, SS, SIZE, SS, 4).mean(axis=(1, 3))
    alpha = small[..., 3:4]
    np.divide(small[..., :3], alpha, out=small[..., :3], where=alpha > 1e-6)
    return np.clip(small*255 + .5, 0, 255).astype(np.uint8)


def _capsule_mask(h, w, x0, y0, x1, y1, width):
    """Hard mask of a round-ended bar from (x0, y0) to (x1, y1)."""
    yy = np.arange(h, dtype=np.float32)[:, None]
    xx = np.arange(w, dtype=np.float32)[None, :]
    dx, dy = float(x1 - x0), float(y1 - y0)
    span = max(dx * dx + dy * dy, 1e-6)
    # Distance to the *segment*, not to the infinite line: t is clamped, which
    # is what rounds the two ends off.
    t = np.clip(((xx - x0) * dx + (yy - y0) * dy) / span, 0.0, 1.0)
    dist = np.hypot(xx - (x0 + t * dx), yy - (y0 + t * dy))
    return (dist <= width * 0.5).astype(np.float32)


def render_muted(size: int = MENUBAR_SIZE, ss: int = MENUBAR_SS) -> np.ndarray:
    """The menu bar image for the muted state: a slashed keybed.

    Drawn as a *template image* - black pixels over an alpha mask, no colour of
    its own - because that is the only kind of menu bar image macOS recolours
    for the light and dark menu bar (and for a highlighted status item). The
    1024 px `icon.png` stays a full-colour Dock icon; this is the same keybed at
    22 pt, which is all that survives at that size: the 7 naturals of the icon's
    keyboard, the band of sharps above them, and a slash through the lot.

    The slash carries a transparent gutter, so it stays readable whichever way
    round the menu bar's colours are.
    """
    n = size * ss
    a = np.zeros((n, n), dtype=np.float32)

    x0, x1 = 0.085 * n, 0.915 * n
    y0, y1 = 0.255 * n, 0.745 * n
    a += _rounded_rect_mask(n, n, x0, y0, x1, y1, 0.055 * n)

    # The naturals: seams cut up from the front edge, stopping under the band of
    # sharps, exactly the proportion the app icon uses.
    key_w = (x1 - x0) / KEYBED_WHITES
    seam = max(1.0, 0.024 * n)
    band = y0 + (y1 - y0) * KEYBED_BLACK_RATIO
    for i in range(1, KEYBED_WHITES):
        cx = x0 + i * key_w
        a -= _rounded_rect_mask(n, n, cx - seam / 2, band, cx + seam / 2, y1,
                                seam * 0.5)

    # The slash, and the gap that keeps it off the keys.
    sx0, sy0, sx1, sy1 = 0.14 * n, 0.845 * n, 0.86 * n, 0.155 * n
    bar = 0.105 * n
    a -= _capsule_mask(n, n, sx0, sy0, sx1, sy1, bar + 0.09 * n)
    np.clip(a, 0.0, 1.0, out=a)
    a += _capsule_mask(n, n, sx0, sy0, sx1, sy1, bar)
    np.clip(a, 0.0, 1.0, out=a)

    alpha = a.reshape(size, ss, size, ss).mean(axis=(1, 3))
    rgba = np.zeros((size, size, 4), dtype=np.float32)
    rgba[..., 3] = alpha                    # black, so the RGB stays at zero
    return np.clip(rgba * 255.0 + 0.5, 0, 255).astype(np.uint8)


def write_png(path: str, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png)


def write_ico(path: str, rgba: np.ndarray) -> None:
    """PNG-compressed Windows icon with crisp shell/taskbar size variants."""
    images = []
    for size in (16, 32, 64, 128, 256):
        factor = rgba.shape[0] // size
        pixels = rgba.reshape(size, factor, size, factor, 4).mean(axis=(1, 3)).astype(np.uint8)
        raw = b"".join(b"\x00" + row.tobytes() for row in pixels)
        def chunk(tag, data):
            return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))
        images.append((size, png))
    offset = 6 + 16 * len(images)
    header = bytearray(struct.pack('<HHH', 0, 1, len(images)))
    for size, png in images:
        header.extend(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(png), offset))
        offset += len(png)
    with open(path, 'wb') as stream:
        stream.write(header)
        for _, png in images:
            stream.write(png)


def main() -> None:
    assets = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets")
    app_icon = render()
    for name, rgba in (("icon.png", app_icon), ("icon-muted.png", render_muted())):
        out = os.path.join(assets, name)
        write_png(out, rgba)
        print("wrote", out)
    write_ico(os.path.join(assets, "icon.ico"), app_icon)


if __name__ == "__main__":
    main()
