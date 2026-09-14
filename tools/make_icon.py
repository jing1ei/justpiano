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
    canvas = np.zeros((S, S, 3), dtype=np.float32)

    # --- squircle-ish background with a diagonal gradient --------------------
    bg_mask = _rounded_rect_mask(S, S, 0, 0, S - 1, S - 1, 0.2237 * S)
    yy = np.arange(S, dtype=np.float32)[:, None]
    xx = np.arange(S, dtype=np.float32)[None, :]
    t = ((xx + yy) / (2.0 * S))[..., None]
    top = np.array([0.20, 0.23, 0.30], dtype=np.float32)
    bottom = np.array([0.055, 0.065, 0.095], dtype=np.float32)
    gradient = top * (1.0 - t) + bottom * t
    canvas += bg_mask[..., None] * gradient

    # --- warm glow behind the keys ------------------------------------------
    cx, cy = 0.5 * S, 0.40 * S
    glow = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * (0.30 * S) ** 2)))
    canvas += (glow * bg_mask)[..., None] * np.array([0.16, 0.10, 0.03], dtype=np.float32)

    # --- keyboard ------------------------------------------------------------
    kb_x0, kb_x1 = 0.135 * S, 0.865 * S
    kb_y0, kb_y1 = 0.300 * S, 0.760 * S
    n_white = KEYBED_WHITES
    key_w = (kb_x1 - kb_x0) / n_white
    gap = 0.030 * key_w
    radius = 0.055 * key_w

    for i in range(n_white):
        x0 = kb_x0 + i * key_w + gap
        x1 = kb_x0 + (i + 1) * key_w - gap
        mask = _rounded_rect_mask(S, S, x0, kb_y0, x1, kb_y1, radius)
        shade = 0.965 - 0.02 * (i % 2)
        _blend(canvas, mask, (shade, shade * 0.985, shade * 0.955))
        # soft vertical shading towards the bottom of each key
        grad = np.clip((yy - kb_y0) / (kb_y1 - kb_y0), 0, 1)
        canvas -= (mask * grad * 0.13)[..., None]

    # black keys sit between white keys 0-1, 1-2, 3-4, 4-5, 5-6
    black_w = key_w * 0.58
    black_h = (kb_y1 - kb_y0) * KEYBED_BLACK_RATIO
    for i in (1, 2, 4, 5, 6):
        cx_k = kb_x0 + i * key_w
        mask = _rounded_rect_mask(S, S, cx_k - black_w / 2, kb_y0,
                                  cx_k + black_w / 2, kb_y0 + black_h, radius * 1.4)
        _blend(canvas, mask, (0.075, 0.080, 0.105))
        grad = np.clip((yy - kb_y0) / black_h, 0, 1)
        canvas += (mask * grad * 0.10)[..., None]

    # --- amber accent bar above the keybed -----------------------------------
    bar = _rounded_rect_mask(S, S, kb_x0, kb_y0 - 0.055 * S, kb_x1,
                             kb_y0 - 0.018 * S, 0.020 * S)
    _blend(canvas, bar, (0.98, 0.63, 0.16))

    # --- record dot ----------------------------------------------------------
    dot_c = (0.5 * S, 0.845 * S)
    dot_r = 0.043 * S
    dot = (np.hypot(xx - dot_c[0], yy - dot_c[1]) <= dot_r).astype(np.float32)
    _blend(canvas, dot, (0.93, 0.27, 0.29))

    canvas *= bg_mask[..., None]
    np.clip(canvas, 0.0, 1.0, out=canvas)

    # alpha channel = background mask
    rgba = np.concatenate([canvas, bg_mask[..., None]], axis=2)
    small = rgba.reshape(SIZE, SS, SIZE, SS, 4).mean(axis=(1, 3))
    # Colour was premultiplied by the mask above; PNG colour type 6 stores
    # straight alpha, so un-premultiply after the box filter or every
    # antialiased edge pixel composites as alpha²·colour (dark fringe).
    alpha = small[..., 3:4]
    np.divide(small[..., :3], alpha, out=small[..., :3], where=alpha > 1e-6)
    return np.clip(small * 255.0 + 0.5, 0, 255).astype(np.uint8)


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


def main() -> None:
    assets = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets")
    for name, rgba in (("icon.png", render()), ("icon-muted.png", render_muted())):
        out = os.path.join(assets, name)
        write_png(out, rgba)
        print("wrote", out)


if __name__ == "__main__":
    main()
