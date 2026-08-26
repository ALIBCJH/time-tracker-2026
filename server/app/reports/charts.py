"""Charts as PNG.

Raster, not SVG, and not JavaScript: Gmail strips <svg> and no mail client runs
a script. A chart that does not survive the mail client is not a chart.

Everything is drawn at 3× and downsampled, which is the cheapest way to get
clean curves and text out of Pillow's non-antialiased primitives.

Every function returns None rather than raising when it cannot draw. A report
that arrives without its picture is a small loss; a report that does not arrive
because a font was missing is a large one.
"""
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger('reports.charts')

SUPERSAMPLE = 3
WHITE = (255, 255, 255)
INK = (11, 11, 11)
INK_2 = (82, 81, 78)
MUTED = (137, 135, 129)
HAIRLINE = (225, 224, 217)
TRACK = (242, 241, 238)
BLUE = (42, 120, 214)

# Fixed categorical order, validated for colour-vision-deficiency-safe
# adjacency on white. Never cycle hues — a series must keep its colour from one
# report to the next or the comparison people actually make (this month against
# last) silently breaks.
PALETTE = [(42, 120, 214), (235, 104, 52), (27, 175, 122), (237, 161, 0),
           (232, 123, 164), (0, 131, 0)]
OTHER = (137, 135, 129)

_FONT_DIRS = ('/usr/share/fonts/truetype/dejavu', '/usr/share/fonts/dejavu',
              '/usr/share/fonts/TTF', '/Library/Fonts')


def _font(bold, size):
    name = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    for directory in _FONT_DIRS:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _hm(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f'{hours}h {minutes:02d}m' if hours else f'{minutes}m'


def _finish(image, width, height):
    image = image.resize((width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, 'PNG', optimize=True)
    return buffer.getvalue()


def bars(items, width=520, height=260, highlight=None):
    """Vertical bars. items: [{'label', 'seconds'}].

    Scaled to the tallest bar, not to a fixed axis — the question a reader has
    is "which day was heaviest", and a fixed axis flattens a quiet week into
    nothing.
    """
    items = [i for i in items or []]
    if not items:
        return None
    try:
        S = SUPERSAMPLE
        W, H = width * S, height * S
        image = Image.new('RGB', (W, H), WHITE)
        d = ImageDraw.Draw(image)
        day_font, value_font = _font(False, 11 * S), _font(True, 11 * S)

        pad = 14 * S
        top, baseline = 26 * S, H - 30 * S
        plot = baseline - top
        peak = max(i['seconds'] for i in items) or 1
        slot = (W - 2 * pad) / len(items)
        bar_width = min(slot * 0.54, 46 * S)

        for fraction in (0.25, 0.5, 0.75, 1.0):
            y = baseline - plot * fraction
            d.line([pad, y, W - pad, y], fill=HAIRLINE, width=max(1, S // 2))

        for index, item in enumerate(items):
            cx = pad + slot * (index + 0.5)
            x0, x1 = cx - bar_width / 2, cx + bar_width / 2
            h = plot * (item['seconds'] / peak) if item['seconds'] else 0
            colour = INK if (highlight is not None and index == highlight) else BLUE

            if h > 0:
                d.rounded_rectangle([x0, baseline - h, x1, baseline],
                                    radius=4 * S, fill=colour)
                d.rectangle([x0, baseline - 4 * S, x1, baseline], fill=colour)
                text = _hm(item['seconds'])
                w = d.textlength(text, font=value_font)
                d.text((cx - w / 2, baseline - h - 17 * S), text, font=value_font, fill=INK)
            else:
                d.rounded_rectangle([x0, baseline - 3 * S, x1, baseline],
                                    radius=1 * S, fill=TRACK)

            w = d.textlength(item['label'], font=day_font)
            d.text((cx - w / 2, baseline + 9 * S), item['label'], font=day_font, fill=MUTED)

        d.line([pad, baseline, W - pad, baseline], fill=HAIRLINE, width=max(1, S // 2))
        return _finish(image, width, height)
    except Exception as e:
        logger.error(f'Bar chart failed, falling back to CSS bars: {e}')
        return None


def donut(slices, width=520, height=300):
    """Donut plus legend. slices: [{'label', 'seconds'}], colours assigned here.

    A donut rather than a full pie: the hole gives the total a home, and the eye
    compares arc length far more accurately than wedge area.
    """
    slices = [s for s in (slices or []) if s['seconds'] > 0]
    if not slices:
        return None
    try:
        S = SUPERSAMPLE
        W, H = width * S, height * S
        image = Image.new('RGB', (W, H), WHITE)
        d = ImageDraw.Draw(image)
        total = sum(s['seconds'] for s in slices)

        coloured = [(s, PALETTE[i] if i < len(PALETTE) else OTHER)
                    for i, s in enumerate(slices)]

        size = int(H * 0.80)
        cx, cy = int(W * 0.23), H // 2
        box = [cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2]

        start = -90.0                                # 12 o'clock, clockwise
        for entry, colour in coloured:
            sweep = 360.0 * entry['seconds'] / total
            d.pieslice(box, start, start + sweep, fill=colour)
            start += sweep

        hole = int(size * 0.56)
        d.ellipse([cx - hole // 2, cy - hole // 2, cx + hole // 2, cy + hole // 2],
                  fill=WHITE)
        total_font, caption_font = _font(True, 21 * S), _font(False, 10 * S)
        text = _hm(total)
        w = d.textlength(text, font=total_font)
        d.text((cx - w / 2, cy - 15 * S), text, font=total_font, fill=INK)
        w = d.textlength('TRACKED', font=caption_font)
        d.text((cx - w / 2, cy + 10 * S), 'TRACKED', font=caption_font, fill=MUTED)

        name_font, value_font = _font(False, 12 * S), _font(True, 12 * S)
        lx, row = int(W * 0.47), 26 * S
        ly = cy - (len(coloured) * row) // 2
        for entry, colour in coloured:
            chip = 9 * S
            d.rounded_rectangle([lx, ly + 4 * S, lx + chip, ly + 4 * S + chip],
                                radius=2 * S, fill=colour)
            d.text((lx + chip + 9 * S, ly), entry['label'][:34], font=name_font, fill=INK)
            text = f"{_hm(entry['seconds'])}   {round(100 * entry['seconds'] / total)}%"
            w = d.textlength(text, font=value_font)
            d.text((W - 16 * S - w, ly), text, font=value_font, fill=INK_2)
            ly += row

        return _finish(image, width, height)
    except Exception as e:
        logger.error(f'Donut failed, falling back to CSS bars: {e}')
        return None


def palette_hex(index):
    colour = PALETTE[index] if index < len(PALETTE) else OTHER
    return '#%02x%02x%02x' % colour
