#!/usr/bin/python3
"""0Hero: one-tap updater for every CardputerZero store app.

Draws straight to the LCD framebuffer with PIL and reads the internal
keyboard, like the other external APPLaunch apps. The update work itself
lives in zerohero_core.py, which also backs the `zerohero` command.
"""
import argparse
import array
import ctypes
import fcntl
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zerohero_core as core  # noqa: E402

W, H = 320, 170
STATE_DIR = Path.home() / '.config/zerohero'
KEYBOARD = '/dev/input/cardputer-zero-internal'
DEJAVU = '/usr/share/fonts/truetype/dejavu/'
FONT, FONT_BOLD = DEJAVU + 'DejaVuSans.ttf', DEJAVU + 'DejaVuSans-Bold.ttf'
FONT_TITLE = '/usr/share/APPLaunch/share/font/Montserrat-Bold.ttf'
BG, FG, MUTED, FAINT = '#000000', '#ffffff', '#a7b0bb', '#7d8793'
ROW, RULE = '#1c232b', '#2a3139'
GREEN, YELLOW, RED, CYAN = '#33e06a', '#ffcc1a', '#ff5a52', '#38d6ff'
VISIBLE = 5


class Bitfield(ctypes.Structure):
    _fields_ = [('offset', ctypes.c_uint32), ('length', ctypes.c_uint32), ('msb_right', ctypes.c_uint32)]


class VarInfo(ctypes.Structure):
    _fields_ = ([(n, ctypes.c_uint32) for n in ('xres', 'yres', 'xres_virtual', 'yres_virtual', 'xoffset',
                                                 'yoffset', 'bits_per_pixel', 'grayscale')]
                + [(n, Bitfield) for n in ('red', 'green', 'blue', 'transp')]
                + [(n, ctypes.c_uint32) for n in ('nonstd', 'activate', 'height', 'width', 'accel_flags',
                                                   'pixclock', 'left_margin', 'right_margin', 'upper_margin',
                                                   'lower_margin', 'hsync_len', 'vsync_len', 'sync', 'vmode',
                                                   'rotate', 'colorspace')]
                + [('reserved', ctypes.c_uint32 * 4)])


class FixInfo(ctypes.Structure):
    _fields_ = [('id', ctypes.c_char * 16), ('smem_start', ctypes.c_ulong), ('smem_len', ctypes.c_uint32),
                ('type', ctypes.c_uint32), ('type_aux', ctypes.c_uint32), ('visual', ctypes.c_uint32),
                ('xpanstep', ctypes.c_uint16), ('ypanstep', ctypes.c_uint16), ('ywrapstep', ctypes.c_uint16),
                ('line_length', ctypes.c_uint32), ('mmio_start', ctypes.c_ulong), ('mmio_len', ctypes.c_uint32),
                ('accel', ctypes.c_uint32), ('capabilities', ctypes.c_uint16), ('reserved', ctypes.c_uint16 * 2)]


class Framebuffer:
    def __init__(self):
        path = '/dev/fb_lcd' if os.path.exists('/dev/fb_lcd') else '/dev/fb0'
        self.fd = os.open(path, os.O_RDWR)
        self.var, self.fix = VarInfo(), FixInfo()
        fcntl.ioctl(self.fd, 0x4600, self.var)  # FBIOGET_VSCREENINFO
        fcntl.ioctl(self.fd, 0x4602, self.fix)  # FBIOGET_FSCREENINFO
        v = self.var
        if (v.xres, v.yres, v.bits_per_pixel, v.red.offset, v.green.offset, v.blue.offset) != (W, H, 16, 11, 5, 0):
            os.close(self.fd)
            raise RuntimeError('Unsupported LCD framebuffer format')
        self.memory = mmap.mmap(self.fd, self.fix.smem_len, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.previous = None

    def draw(self, image):
        data = image.tobytes()
        if data == self.previous:
            return
        self.previous = data
        raw = array.array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                                for r, g, b in image.getdata())).tobytes()
        for y in range(H):
            start = (y + self.var.yoffset) * self.fix.line_length + self.var.xoffset * 2
            self.memory[start:start + W * 2] = raw[y * W * 2:(y + 1) * W * 2]

    def close(self):
        self.memory.close()
        os.close(self.fd)


def fit(draw, text, font, width):
    """Trim text with an ellipsis so it fits in `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + '…', font=font) > width:
        text = text[:-1]
    return text + '…'


class App:
    # screens: checking | list | none | running | done | error
    def __init__(self):
        self.screen = 'checking'
        self.updates = []
        self.message = ''
        self.top = 0
        self.selected = 0
        self.running = True
        self.lock = threading.Lock()

        def load(path, size):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                return ImageFont.truetype(FONT, size)
        self.title_font = load(FONT_TITLE, 17)
        self.font = load(FONT, 13)
        self.bold = load(FONT_BOLD, 13)
        self.small = load(FONT, 11)

    # ---- work (background threads) ----
    def check(self):
        self.screen, self.message, self.top, self.selected = 'checking', '', 0, 0
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        if core.store_busy() and not core.repair_pending():
            self.screen = 'error'
            self.message = 'The Store has an unfinished transaction. Open the Store once, then retry.'
            return
        try:
            ups = core.find_updates()
        except Exception:
            self.screen = 'error'
            self.message = 'Could not reach the app registry. Check Wi-Fi and retry.'
            return
        self.updates = ups
        self.screen = 'list' if ups else 'none'

    def start(self):
        if not core.can_sudo():
            self.screen = 'error'
            self.message = 'Updating needs passwordless sudo (the default CardputerZero user has it).'
            return
        self.screen = 'running'
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        def changed(u):
            if u.status == 'working':
                i = self.updates.index(u)
                self.selected = i
                self.top = max(min(self.top, i), i - VISIBLE + 1)
        core.run_updates(self.updates, changed)
        self.screen = 'done'

    # ---- drawing ----
    def header(self, d, right, right_color=MUTED):
        d.text((10, 6), '0Hero', font=self.title_font, fill=FG)
        d.text((W - 10 - d.textlength(right, font=self.small), 11), right, font=self.small, fill=right_color)
        d.line((0, 30, W, 30), fill=RULE)

    def footer(self, d, text):
        d.line((0, H - 20, W, H - 20), fill=RULE)
        d.text((10, H - 16), text, font=self.small, fill=FAINT)

    def rows(self, d):
        y = 34
        for i in range(self.top, min(len(self.updates), self.top + VISIBLE)):
            u = self.updates[i]
            if self.screen == 'list' and i == self.selected:
                d.rectangle((0, y - 1, W, y + 20), fill=ROW)
            icon, color = {'waiting': ('·', FAINT), 'working': ('›', YELLOW),
                           'done': ('✓', GREEN), 'failed': ('✗', RED)}[u.status]
            if self.screen != 'list':
                d.text((8, y + 2), icon, font=self.bold, fill=color)
            x = 8 if self.screen == 'list' else 24
            ver = u.new if u.status != 'failed' else 'failed'
            vcol = MUTED if u.status in ('waiting', 'working') else (RED if u.status == 'failed' else GREEN)
            if self.screen == 'list':
                ver, vcol = '%s → %s' % (u.have, u.new), MUTED
            vw = min(d.textlength(ver, font=self.small), 150)
            d.text((x, y + 2), fit(d, u.title, self.font, W - x - vw - 18), font=self.font, fill=FG)
            d.text((W - 8 - vw, y + 4), fit(d, ver, self.small, 150), font=self.small, fill=vcol)
            y += 21
        if len(self.updates) > VISIBLE:  # scroll hint
            bar = int((H - 56) * VISIBLE / len(self.updates))
            pos = 32 + int((H - 56 - bar) * self.top / max(1, len(self.updates) - VISIBLE))
            d.rectangle((W - 3, pos, W - 1, pos + bar), fill=RULE)

    def center(self, d, lines):
        total = sum(f.size + 6 for _, f, _ in lines)
        y = 30 + (H - 50 - total) // 2
        for text, font, color in lines:
            d.text(((W - d.textlength(text, font=font)) // 2, y), text, font=font, fill=color)
            y += font.size + 6

    def wrap(self, d, text, font, width):
        out, line = [], ''
        for word in text.split():
            trial = (line + ' ' + word).strip()
            if d.textlength(trial, font=font) <= width:
                line = trial
            else:
                out.append(line)
                line = word
        return out + [line] if line else out

    def render(self):
        img = Image.new('RGB', (W, H), BG)
        d = ImageDraw.Draw(img)
        n = len(self.updates)
        if self.screen == 'checking':
            self.header(d, 'checking…')
            self.center(d, [('Checking for updates…', self.font, MUTED)])
            self.footer(d, 'Esc exit')
        elif self.screen == 'none':
            self.header(d, 'up to date', GREEN)
            self.center(d, [('✓', self.title_font, GREEN), ('Every app is up to date', self.font, FG)])
            self.footer(d, 'R check again   Esc exit')
        elif self.screen == 'list':
            self.header(d, '%d update%s' % (n, '' if n == 1 else 's'), YELLOW)
            self.rows(d)
            self.footer(d, 'Enter update all   R refresh   Esc exit')
        elif self.screen == 'running':
            done = sum(u.status in ('done', 'failed') for u in self.updates)
            self.header(d, 'updating %d/%d' % (min(done + 1, n), n), YELLOW)
            self.rows(d)
            d.line((0, H - 20, W, H - 20), fill=RULE)
            d.rectangle((0, H - 20, int(W * done / max(1, n)), H - 19), fill=CYAN)
            cur = next((u for u in self.updates if u.status == 'working'), None)
            step = (cur.error if cur and cur.error else 'Working… keep the device on')
            d.text((10, H - 16), fit(d, step, self.small, W - 20), font=self.small, fill=FAINT)
        elif self.screen == 'done':
            bad = sum(u.status == 'failed' for u in self.updates)
            self.header(d, '%d updated%s' % (n - bad, (', %d failed' % bad) if bad else ''),
                        RED if bad else GREEN)
            self.rows(d)
            self.footer(d, '↑↓ scroll   R check again   Esc exit')
        else:  # error
            self.header(d, 'needs attention', RED)
            y = 44
            for line in self.wrap(d, self.message, self.font, W - 30):
                d.text((15, y), line, font=self.font, fill=FG)
                y += 19
            self.footer(d, 'R retry   Esc exit')
        return img

    # ---- input ----
    def key(self, code):
        code = {33: 103, 45: 108, 44: 105, 46: 106}.get(code, code)  # F/X/Z/C as arrows
        if self.screen in ('running', 'checking'):
            if code in (1, 41) and self.screen == 'checking':
                self.running = False
            return  # never leave mid-install
        if code in (1, 41):  # Esc or bare backtick
            self.running = False
        elif code == 19:  # R
            self.check()
        elif code in (28, 57) and self.screen == 'list':  # Enter, Space
            self.start()
        elif code in (103, 17, 39) and self.updates:  # Up, W, ;
            self.selected = max(0, self.selected - 1)
        elif code in (108, 31, 52) and self.updates:  # Down, S, .
            self.selected = min(len(self.updates) - 1, self.selected + 1)
        self.top = max(min(self.top, self.selected), self.selected - VISIBLE + 1)


def run():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / 'app.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = App()
    frame = Framebuffer()
    keyboard = os.open(KEYBOARD, os.O_RDONLY | os.O_NONBLOCK)
    # No EVIOCGRAB: APPLaunch keeps its emergency return shortcut.
    event = struct.Struct('llHHi')
    signal.signal(signal.SIGTERM, lambda *_: setattr(app, 'running', False))
    signal.signal(signal.SIGINT, lambda *_: setattr(app, 'running', False))
    app.check()
    try:
        while app.running:
            frame.draw(app.render())
            ready, _, _ = select.select([keyboard], [], [], 0.25)
            if ready:
                data = os.read(keyboard, event.size * 64)
                for offset in range(0, len(data) - event.size + 1, event.size):
                    _, _, kind, code, value = event.unpack_from(data, offset)
                    if kind == 1 and value == 1:
                        app.key(code)
    finally:
        os.close(keyboard)
        frame.close()
        lock.close()


def demo(screen):
    """Build an App in a given state with sample data (for store screenshots)."""
    app = App()
    sample = [('MeshZero', '0.2.1', '0.3.0'), ('ZeroSDR', '0.3.6', '0.5.1'), ('RF Heatmap', '0.1.0-1', '0.3.9-1'),
              ('Zerogotchi', '0.4.25', '0.4.26'), ('ZeroTag', '0.5.0', '1.0.0'), ('Octet Desktop', '4.7', '5.3'),
              ('WebRadio', '1.0.5-1', '1.0.13-1')]
    app.updates = [core.Update(str(i), t, t.lower(), a, b) for i, (t, a, b) in enumerate(sample)]
    if screen in ('running', 'done'):
        for i, u in enumerate(app.updates):
            u.status = 'done' if (screen == 'done' or i < 2) else ('working' if i == 2 else 'waiting')
        if screen == 'done':
            app.updates[4].status, app.updates[4].error = 'failed', 'install failed'
    app.screen = screen
    return app


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--preview', metavar='PNG', help='render one frame to a PNG and exit')
    p.add_argument('--screen', default='list',
                   choices=['checking', 'list', 'none', 'running', 'done', 'error'],
                   help='screen to render with --preview (sample data)')
    args = p.parse_args()
    if args.preview:
        a = demo(args.screen)
        if args.screen == 'error':
            a.message = 'Could not reach the app registry. Check Wi-Fi and retry.'
        a.render().save(args.preview)
    else:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / 'app.log', 'w', buffering=1) as log:
            sys.stdout = sys.stderr = log
            try:
                run()
            except Exception:
                import traceback
                traceback.print_exc()
                raise SystemExit(1)
