"""Builds logo.png (transparent header logo) and icon.ico / icon.png (VP monogram)
from logo_source.webp. Run once: python assets/make_assets.py"""
import os
from collections import deque

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
src = Image.open(os.path.join(HERE, "logo_source.webp")).convert("RGB")
a = np.asarray(src).astype(int)
redness = a[..., 0] - np.maximum(a[..., 1], a[..., 2])
alpha = np.clip((redness - 20) * 255 / 120, 0, 255).astype(np.uint8)  # soft edges
RED = (237, 18, 36)


def rgba_from(alpha_arr):
    out = np.zeros(alpha_arr.shape + (4,), np.uint8)
    out[..., :3] = RED
    out[..., 3] = alpha_arr
    return Image.fromarray(out, "RGBA")


# ---- header logo: whole wordmark, black removed, trimmed ----
logo = rgba_from(alpha)
logo = logo.crop(logo.getbbox())
logo.save(os.path.join(HERE, "logo.png"))


# ---- VP monogram: cut the V and P letters out by connected component ----
def component(seed_x, seed_y):
    m = alpha > 60
    m[515:] = False  # exclude the underline bar
    seen = np.zeros_like(m)
    q = deque([(seed_y, seed_x)])
    seen[seed_y, seed_x] = True
    H, W = m.shape
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and m[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                q.append((ny, nx))
    # grow by a few px so anti-aliased edges are kept
    grown = seen.copy()
    for _ in range(3):
        g = grown.copy()
        g[1:] |= grown[:-1]; g[:-1] |= grown[1:]
        g[:, 1:] |= grown[:, :-1]; g[:, :-1] |= grown[:, 1:]
        grown = g
    letter = np.where(grown, alpha, 0).astype(np.uint8)
    img = rgba_from(letter)
    return img.crop(img.getbbox())


def first_pixel(x0, x1):
    for x in range(x0, x1):
        ys = np.where(alpha[239:505, x] > 200)[0]
        if len(ys):
            return x, 239 + ys[0]


V = component(*first_pixel(196, 361))
P = component(*first_pixel(697, 875))

# overlap the letters slightly, like the italic wordmark
gap = -int(V.width * 0.12)
h = max(V.height, P.height)
mono = Image.new("RGBA", (V.width + P.width + gap, h), (0, 0, 0, 0))
mono.alpha_composite(V, (0, h - V.height))
mono.alpha_composite(P, (V.width + gap, h - P.height))

S = 1024
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(icon)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=180, fill=(10, 10, 12, 255))
scale = (S * 0.78) / mono.width
mono = mono.resize((int(mono.width * scale), int(mono.height * scale)), Image.LANCZOS)
bar_h = int(S * 0.055)
total_h = mono.height + int(S * 0.05) + bar_h
top = (S - total_h) // 2
icon.alpha_composite(mono, ((S - mono.width) // 2, top))
by = top + mono.height + int(S * 0.05)
d.rounded_rectangle(((S - mono.width) // 2, by, (S + mono.width) // 2, by + bar_h),
                    radius=bar_h // 2, fill=RED + (255,))

icon.save(os.path.join(HERE, "icon.png"))
icon.save(os.path.join(HERE, "icon.ico"),
          sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("logo", logo.size, "icon built")


# ---- console tab icons (simple fan-made drawings, not the official logos) ----
def console_icons():
    S = 256  # draw big, scale down for smooth edges

    # SNES: the controller's four face buttons in a diamond (Y green, X blue,
    # A red, B yellow - the Super Famicom / PAL colours)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = 46
    for (cx, cy), col in (((S // 2, 58), (64, 120, 230)),       # X (top)
                          ((58, S // 2), (40, 170, 90)),         # Y (left)
                          ((S - 58, S // 2), (226, 40, 52)),     # A (right)
                          ((S // 2, S - 58), (245, 200, 30))):   # B (bottom)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (255,))
    im.resize((64, 64), Image.LANCZOS).save(os.path.join(HERE, "icon_snes.png"))

    # GBA: a landscape handheld silhouette in indigo with a screen and buttons
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    body = (82, 72, 168, 255)
    d.rounded_rectangle((8, 66, S - 8, S - 66), radius=58, fill=body)
    d.rounded_rectangle((70, 84, S - 70, S - 84), radius=10, fill=(40, 36, 70, 255))
    d.rectangle((80, 94, S - 80, S - 94), fill=(150, 200, 170, 255))      # screen
    cx, cy = 38, S // 2                                                   # d-pad
    d.rectangle((cx - 18, cy - 6, cx + 18, cy + 6), fill=(30, 28, 50, 255))
    d.rectangle((cx - 6, cy - 18, cx + 6, cy + 18), fill=(30, 28, 50, 255))
    d.ellipse((S - 52, cy - 22, S - 30, cy), fill=(30, 28, 50, 255))      # A / B
    d.ellipse((S - 70, cy - 2, S - 48, cy + 20), fill=(30, 28, 50, 255))
    im.resize((64, 64), Image.LANCZOS).save(os.path.join(HERE, "icon_gba.png"))
    print("console icons built")


console_icons()


def folder_icon():
    """A simple folder for the Scan Games button."""
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((16, 52, 120, 100), radius=18, fill=(224, 160, 40, 255))   # tab
    d.rounded_rectangle((16, 76, S - 16, S - 40), radius=22, fill=(240, 182, 50, 255))
    d.rounded_rectangle((16, 108, S - 16, S - 40), radius=22, fill=(252, 204, 74, 255))
    im.resize((64, 64), Image.LANCZOS).save(os.path.join(HERE, "icon_folder.png"))
    print("folder icon built")


folder_icon()


def more_console_icons():
    """Simple fan-made drawings for the other console tabs (not official logos)."""
    S = 256
    out = lambda im, name: im.resize((64, 64), Image.LANCZOS).save(
        os.path.join(HERE, f"icon_{name}.png"))
    dark = (30, 30, 34, 255)

    def dpad(d, cx, cy, r=20, w=8, col=dark):
        d.rectangle((cx - r, cy - w, cx + r, cy + w), fill=col)
        d.rectangle((cx - w, cy - r, cx + w, cy + r), fill=col)

    # NES: grey controller with black face plate, d-pad and two red buttons
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 72, S - 8, S - 72), radius=12, fill=(200, 200, 204, 255))
    d.rounded_rectangle((22, 86, S - 22, S - 86), radius=8, fill=dark)
    dpad(d, 66, S // 2, 22, 8, (90, 90, 96, 255))
    for cx in (S - 92, S - 52):
        d.ellipse((cx - 16, S // 2 - 16, cx + 16, S // 2 + 16), fill=(205, 30, 40, 255))
    out(im, "nes")

    def handheld(body, screen_border, screen, btn):
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
        d.rounded_rectangle((52, 6, S - 52, S - 6), radius=18, fill=body)
        d.rounded_rectangle((66, 22, S - 66, 124), radius=10, fill=screen_border)
        d.rectangle((84, 36, S - 84, 110), fill=screen)
        dpad(d, 96, 170, 18, 7)
        d.ellipse((150, 152, 174, 176), fill=btn)
        d.ellipse((172, 140, 196, 164), fill=btn)
        return im
    # Game Boy: grey brick, green screen, magenta buttons
    out(handheld((196, 194, 188, 255), (90, 92, 106, 255), (150, 172, 60, 255),
                 (150, 30, 90, 255)), "gb")
    # Game Boy Color: teal body, dark border, lit screen
    out(handheld((40, 170, 170, 255), (40, 40, 48, 255), (170, 210, 230, 255),
                 (40, 40, 48, 255)), "gbc")

    # DS: silver clamshell with two screens
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((40, 8, S - 40, 120), radius=14, fill=(190, 194, 200, 255))
    d.rounded_rectangle((40, 132, S - 40, S - 8), radius=14, fill=(170, 174, 182, 255))
    d.rectangle((72, 26, S - 72, 104), fill=(40, 44, 56, 255))
    d.rectangle((84, 150, S - 84, 230), fill=(40, 44, 56, 255))
    d.rectangle((40, 118, S - 40, 134), fill=(120, 124, 132, 255))
    out(im, "nds")

    # Master System: black pad with a red stripe and buttons 1 / 2
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((10, 74, S - 10, S - 74), radius=16, fill=(28, 28, 30, 255))
    d.rectangle((10, 100, S - 10, 112), fill=(210, 30, 36, 255))
    dpad(d, 70, 146, 22, 8, (100, 100, 106, 255))
    for cx in (S - 96, S - 54):
        d.rounded_rectangle((cx - 16, 130, cx + 16, 162), radius=6, fill=(110, 110, 116, 255))
    out(im, "sms")

    # Genesis / Mega Drive: rounded black pad, d-pad and three buttons in an arc
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 64, S - 8, S - 64), radius=60, fill=(24, 24, 28, 255))
    dpad(d, 70, S // 2, 24, 9, (90, 90, 96, 255))
    for i, cx in enumerate((S - 118, S - 84, S - 50)):
        cy = S // 2 + 14 - i * 12
        d.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), fill=(200, 40, 50, 255))
    out(im, "genesis")
    print("more console icons built")


more_console_icons()


def n64_gg_icons():
    """Fan-made N64 and Game Gear tab icons (not official logos)."""
    S = 256
    out = lambda im, name: im.resize((64, 64), Image.LANCZOS).save(
        os.path.join(HERE, f"icon_{name}.png"))
    # N64: grey three-pronged controller with a d-pad, blue/green A-B and yellow C buttons
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    grey = (150, 152, 160, 255)
    d.rounded_rectangle((12, 64, S - 12, 140), radius=36, fill=grey)
    for x0 in (14, S // 2 - 22, S - 58):                      # the three handles
        d.rounded_rectangle((x0, 100, x0 + 44, 236), radius=22, fill=grey)
    d.rectangle((46, 90, 70, 102), fill=(40, 40, 46, 255))      # d-pad
    d.rectangle((52, 84, 64, 108), fill=(40, 40, 46, 255))
    d.ellipse((S // 2 - 12, 92, S // 2 + 12, 116), fill=(40, 40, 46, 255))  # stick
    d.ellipse((S - 86, 104, S - 64, 126), fill=(40, 90, 220, 255))          # A
    d.ellipse((S - 104, 84, S - 84, 104), fill=(40, 170, 80, 255))          # B
    for cx, cy in ((S - 52, 80), (S - 38, 96), (S - 66, 96), (S - 52, 112)):  # C
        d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=(240, 196, 30, 255))
    out(im, "n64")
    # Game Gear: black landscape handheld, screen in the middle, red/blue buttons
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((6, 58, S - 6, S - 58), radius=46, fill=(32, 32, 36, 255))
    d.rounded_rectangle((72, 76, S - 72, S - 76), radius=8, fill=(70, 72, 80, 255))
    d.rectangle((84, 88, S - 84, S - 88), fill=(120, 170, 210, 255))
    d.rectangle((24, 122, 56, 134), fill=(110, 110, 116, 255))              # d-pad
    d.rectangle((34, 112, 46, 144), fill=(110, 110, 116, 255))
    d.ellipse((S - 62, 108, S - 42, 128), fill=(40, 90, 220, 255))
    d.ellipse((S - 40, 122, S - 20, 142), fill=(210, 40, 50, 255))
    out(im, "gg")
    print("n64 / game gear icons built")


n64_gg_icons()


def trophy_icon():
    """A gold trophy for the RetroAchievements button (fan-made, not their logo)."""
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=48, fill=(24, 26, 40, 255))
    gold, dark = (246, 190, 40, 255), (200, 140, 20, 255)
    d.ellipse((40, 52, 92, 124), outline=gold, width=14)          # handles
    d.ellipse((164, 52, 216, 124), outline=gold, width=14)
    d.pieslice((66, 20, 190, 176), 0, 180, fill=gold)             # cup
    d.rectangle((66, 40, 190, 98), fill=gold)
    d.rectangle((116, 150, 140, 196), fill=dark)                  # stem
    d.rounded_rectangle((80, 192, 176, 222), radius=8, fill=gold) # base
    im.resize((64, 64), Image.LANCZOS).save(os.path.join(HERE, "icon_ra.png"))
    print("trophy icon built")


trophy_icon()
