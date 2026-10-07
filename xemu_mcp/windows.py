"""Windows-only controller delivery and visible client-region capture."""

import sys
import time


# SDL scancodes from xemu v0.8.136 config_spec.yml. Axis directions are digital.
DEFAULT_KEYS = {
    'a': 4, 'b': 5, 'x': 27, 'y': 28, 'back': 42, 'start': 40,
    'white': 30, 'black': 31, 'lstick_btn': 32, 'rstick_btn': 33, 'guide': 34,
    'dpad_left': 80, 'dpad_up': 82, 'dpad_right': 79, 'dpad_down': 81,
    'lstick_up': 8, 'lstick_left': 22, 'lstick_right': 9, 'lstick_down': 7,
    'ltrigger': 26, 'rstick_up': 12, 'rstick_left': 13, 'rstick_right': 15,
    'rstick_down': 14, 'rtrigger': 18,
}


def controller_keys(buttons, overrides=None):
    if not buttons or len(buttons) > 25:
        raise ValueError('Supply 1 to 25 controller button/direction names')
    mapping = DEFAULT_KEYS | (overrides or {})
    keys = []
    for button in buttons:
        name = button.lower()
        if name not in mapping:
            raise ValueError(f'Unknown controller input {button!r}; allowed: {", ".join(DEFAULT_KEYS)}')
        scan = mapping[name]
        if type(scan) is not int or not 1 <= scan <= 511:
            raise ValueError(f'Invalid/disabled SDL scancode for {name}')
        if scan not in keys:
            keys.append(scan)
    return keys


def _apis():
    if sys.platform != 'win32':
        raise RuntimeError('Keyboard input and region capture require Windows; QMP/GDB are portable')
    import win32api
    import win32gui
    import win32process
    return win32api, win32gui, win32process


def window_for_pid(pid):
    _, gui, process = _apis()
    windows = []
    def collect(hwnd, _):
        if (process.GetWindowThreadProcessId(hwnd)[1] == pid
                and gui.IsWindowVisible(hwnd)
                and gui.GetClassName(hwnd).startswith('SDL')
                and gui.GetWindowText(hwnd).startswith('xemu')):
            windows.append(hwnd)
    gui.EnumWindows(collect, None)
    if len(windows) != 1:
        raise RuntimeError(f'Expected one visible xemu window owned by PID {pid}, got {len(windows)}')
    return windows[0]


def _virtual_key(scan):
    if 4 <= scan <= 29:
        return ord('A') + scan - 4, False
    if 30 <= scan <= 38:
        return ord('1') + scan - 30, False
    if scan == 39:
        return ord('0'), False
    table = {
        40: 0x0d, 41: 0x1b, 42: 0x08, 43: 0x09, 44: 0x20,
        45: 0xbd, 46: 0xbb, 47: 0xdb, 48: 0xdd, 49: 0xdc,
        51: 0xba, 52: 0xde, 53: 0xc0, 54: 0xbc, 55: 0xbe, 56: 0xbf,
        57: 0x14, 73: 0x2d, 74: 0x24, 75: 0x21, 76: 0x2e, 77: 0x23,
        78: 0x22, 79: 0x27, 80: 0x25, 81: 0x28, 82: 0x26,
        224: 0xa2, 225: 0xa0, 226: 0xa4, 227: 0x5b,
        228: 0xa3, 229: 0xa1, 230: 0xa5, 231: 0x5c,
    }
    if 58 <= scan <= 69:
        return 0x70 + scan - 58, False
    if scan not in table:
        raise ValueError(f'SDL scancode {scan} has no supported Windows translation')
    return table[scan], scan in range(73, 83) or scan in (227, 228, 230, 231)


def validate_windows_keys(keys):
    for scan in keys:
        _virtual_key(scan)


def post_key(hwnd, scan, down, expected_pid):
    _, gui, process = _apis()
    if process.GetWindowThreadProcessId(hwnd)[1] != expected_pid:
        raise RuntimeError('xemu window ownership changed; refusing to send input')
    vk, extended = _virtual_key(scan)
    # A zero lParam scancode selects SDL's virtual/on-screen-keyboard path,
    # which accepts targeted messages even when raw keyboard input is active.
    flags = 1 | int(extended) << 24
    if not down:
        flags |= 3 << 30
    # Targeted messages never inject a key into another foreground application.
    gui.SendMessageTimeout(hwnd, 0x100 if down else 0x101, vk, flags, 0x2, 1000)


def capture_region(pid, filename, bring_to_front=False):
    import ctypes
    from PIL import ImageGrab
    _, gui, _ = _apis()
    # Thread-local DPI context also works when called from asyncio.to_thread.
    user32 = ctypes.windll.user32
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    raised = False
    hwnd = None
    try:
        hwnd = window_for_pid(pid)
        if bring_to_front:
            if gui.IsIconic(hwnd):
                gui.ShowWindow(hwnd, 9)
            # Use a correctly pointer-sized HWND_TOPMOST. Briefly raising the
            # owned window works even when Windows forbids stealing keyboard
            # focus, and the original topmost flag is restored below.
            raised = not gui.GetWindowLong(hwnd, -20) & 0x8
            # SDL can finish a queued restore/resize after the first request,
            # clearing topmost. Verify the actual style rather than trusting
            # SetWindowPos's success return alone.
            for _ in range(10):
                if not user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x13):
                    raise RuntimeError('Windows refused to raise xemu; make its window visible manually')
                time.sleep(.05)
                if gui.GetWindowLong(hwnd, -20) & 0x8:
                    break
            else:
                raise RuntimeError('xemu did not remain raised; make its window visible manually')
        if gui.IsIconic(hwnd):
            raise RuntimeError('Region screenshot requires a visible, restored xemu window')
        rect = gui.GetClientRect(hwnd)
        x, y = gui.ClientToScreen(hwnd, (0, 0))
        width, height = rect[2], rect[3]
        if width <= 0 or height <= 0:
            raise RuntimeError('xemu has no visible client area')
        for px, py in ((x + 2, y + 2), (x + width - 3, y + 2),
                       (x + 2, y + height - 3), (x + width - 3, y + height - 3),
                       (x + width // 2, y + height // 2)):
            top = gui.WindowFromPoint((px, py))
            if top != hwnd and not gui.IsChild(hwnd, top):
                raise RuntimeError(f'Region screenshot requires xemu to be unobscured; '
                                   f'point ({px},{py}) is covered by {gui.GetWindowText(top)!r}')
        image = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
        image.save(filename, format='PNG')
        return {'width': width, 'height': height, 'needs_visible_window': True,
                'note': 'Visible client region; may include xemu UI and is not guest resolution'}
    finally:
        if raised and hwnd and gui.IsWindow(hwnd):
            user32.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, 0x13)
        if previous:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
