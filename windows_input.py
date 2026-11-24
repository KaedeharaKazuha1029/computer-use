import ctypes as ct
from ctypes import wintypes as wt
from contextlib import contextmanager
from math import dist
import re
import time


class InputError(Exception):
    pass


MAX_DURATION_MS = 60_000
MAX_RELATIVE_STEPS = 1024
MOVE_INTERVAL_MS = 12
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
ENUM_CURRENT_SETTINGS = -1
PER_MONITOR_AWARE_V2 = -4
SEQUENCE_ACTIONS = ('hold_key', 'move_relative', 'wait', 'drag_path', 'click_pointer')
MODIFIERS = {
    'ctrl': 0xA2, 'lctrl': 0xA2, 'rctrl': 0xA3,
    'shift': 0xA0, 'lshift': 0xA0, 'rshift': 0xA1,
    'alt': 0xA4, 'lalt': 0xA4, 'ralt': 0xA5,
}
KEYS = {
    **MODIFIERS, 'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D,
    'escape': 0x1B, 'space': 0x20, 'pageup': 0x21, 'pagedown': 0x22,
    'end': 0x23, 'home': 0x24, 'left': 0x25, 'up': 0x26, 'right': 0x27,
    'down': 0x28, 'insert': 0x2D, 'delete': 0x2E,
    **{chr(code).lower(): code for code in range(0x30, 0x5B) if chr(code).isalnum()},
    **{f'f{number}': 0x6F + number for number in range(1, 25)},
    **{f'numpad{number}': 0x60 + number for number in range(10)},
    ';': 0xBA, '-': 0xBD, '=': 0xBB, '[': 0xDB, ']': 0xDD, '\\': 0xDC,
    "'": 0xDE, ',': 0xBC, '.': 0xBE, '/': 0xBF, '`': 0xC0,
}
ALIASES = {
    'control': 'ctrl', 'control_l': 'lctrl', 'control_r': 'rctrl',
    'shift_l': 'lshift', 'shift_r': 'rshift', 'alt_l': 'lalt', 'alt_r': 'ralt',
    'return': 'enter', 'esc': 'escape', 'prior': 'pageup', 'next': 'pagedown',
    'page_up': 'pageup', 'page_down': 'pagedown', 'spacebar': 'space',
    'period': '.', 'comma': ',', 'minus': '-', 'plus': '=', 'equal': '=',
}
BUTTONS = {'left': (0x01, 0x02, 0x04), 'right': (0x02, 0x08, 0x10), 'middle': (0x04, 0x20, 0x40)}


def key_code(name, mapping):
    canonical = name.strip().lower()
    if re.fullmatch(r'(kp_|numpad_)[0-9]', canonical):
        canonical = 'numpad' + canonical[-1]
    result = mapping.get(ALIASES.get(canonical, canonical))
    if result is None:
        raise InputError(f'Unsupported key: {name!r}')
    return result


def chord_codes(chord):
    return unique_keys([key_code(name, KEYS) for name in chord.split('+')])


def unique_keys(codes):
    if len(codes) != len(set(codes)):
        raise InputError('A key appears twice in the same action.')
    return codes


class MOUSEINPUT(ct.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD),
                ('dwFlags', wt.DWORD), ('time', wt.DWORD), ('dwExtraInfo', ct.c_size_t)]


class KEYBDINPUT(ct.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ct.c_size_t)]


class InputData(ct.Union):
    _fields_ = [('mouse', MOUSEINPUT), ('keyboard', KEYBDINPUT)]


class INPUT(ct.Structure):
    _fields_ = [('type', wt.DWORD), ('data', InputData)]


def mouse_event(flags, x=0, y=0, data=0):
    return INPUT(0, InputData(mouse=MOUSEINPUT(x, y, data & 0xffffffff, flags, 0, 0)))


class Windows:
    def __init__(self):
        import win32api

        self.display_api = win32api
        self.user = ct.WinDLL('user32', use_last_error=True)
        self.dwm = ct.WinDLL('dwmapi')
        pointer = ct.c_void_p
        hwnd = wt.HWND
        signatures = {
            'IsWindow': (wt.BOOL, hwnd), 'IsWindowVisible': (wt.BOOL, hwnd),
            'IsIconic': (wt.BOOL, hwnd), 'GetForegroundWindow': (hwnd,),
            'GetAncestor': (hwnd, hwnd, wt.UINT), 'WindowFromPoint': (hwnd, wt.POINT),
            'GetWindowThreadProcessId': (wt.DWORD, hwnd, ct.POINTER(wt.DWORD)),
            'GetWindowTextLengthW': (ct.c_int, hwnd),
            'GetWindowTextW': (ct.c_int, hwnd, wt.LPWSTR, ct.c_int),
            'GetWindowRect': (wt.BOOL, hwnd, ct.POINTER(wt.RECT)),
            'GetAsyncKeyState': (ct.c_short, ct.c_int),
            'GetKeyboardLayout': (pointer, wt.DWORD),
            'MapVirtualKeyExW': (wt.UINT, wt.UINT, wt.UINT, pointer),
            'SetThreadDpiAwarenessContext': (pointer, pointer),
            'SendInput': (wt.UINT, wt.UINT, ct.POINTER(INPUT), ct.c_int),
        }
        for name, (result, *arguments) in signatures.items():
            function = getattr(self.user, name)
            function.restype = result
            function.argtypes = arguments
        self.dwm.DwmGetWindowAttribute.argtypes = [hwnd, wt.DWORD, pointer, wt.DWORD]
        self.dwm.DwmGetWindowAttribute.restype = ct.c_long

    def require_physical_coordinates(self):
        if not self.user.SetThreadDpiAwarenessContext(ct.c_void_p(PER_MONITOR_AWARE_V2)):
            raise InputError('Windows could not enable physical coordinates for this input thread.')

    def inspect(self, hwnd):
        self.require_physical_coordinates()
        process_id = wt.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ct.byref(process_id))
        if not thread or not self.user.IsWindow(hwnd):
            raise InputError('The window ID does not resolve to a Windows window. Refresh Computer Use.')
        title = ct.create_unicode_buffer(self.user.GetWindowTextLengthW(hwnd) + 1)
        self.user.GetWindowTextW(hwnd, title, len(title))
        frame = wt.RECT()
        status = self.dwm.DwmGetWindowAttribute(hwnd, 9, ct.byref(frame), ct.sizeof(frame))
        if status and not self.user.GetWindowRect(hwnd, ct.byref(frame)):
            raise InputError('Cannot read the target window bounds.')
        return {
            'window': {'id': hwnd, 'process_id': process_id.value, 'title': title.value},
            'frame': dict(left=frame.left, top=frame.top, right=frame.right, bottom=frame.bottom),
            'foreground': self.user.GetForegroundWindow() == hwnd,
            'visible': bool(self.user.IsWindowVisible(hwnd)) and not self.user.IsIconic(hwnd),
        }

    def verify(self, window, check_title=True):
        current = self.inspect(window['id'])
        if current['window']['process_id'] != window['process_id'] or (
            check_title and current['window']['title'] != window['title']
        ):
            raise InputError('The window identity has changed. Inspect the target again.')
        if not current['foreground'] or not current['visible']:
            raise InputError('The selected window must be visible and in the foreground.')
        return current['frame']

    def verify_point(self, hwnd, point):
        self.require_physical_coordinates()
        target = self.user.WindowFromPoint(wt.POINT(*point))
        if not target or self.user.GetAncestor(target, 2) != hwnd:
            raise InputError('The pointer coordinate is outside the selected window or is covered.')

    def is_down(self, code):
        return bool(self.user.GetAsyncKeyState(code) & 0x8000)

    def stroke(self, hwnd, code):
        thread = self.user.GetWindowThreadProcessId(hwnd, None)
        scan = self.user.MapVirtualKeyExW(code, 4, self.user.GetKeyboardLayout(thread))
        if scan and (scan >> 8) in (0, 0xE0):
            flags = 0x08 | int((scan >> 8) == 0xE0)
            vk = 0
        else:
            flags, vk, scan = 0, code, 0
        return tuple(INPUT(1, InputData(keyboard=KEYBDINPUT(vk, scan & 0xFF, flags | edge, 0, 0)))
                     for edge in (0, 0x02))

    def send(self, event):
        ct.set_last_error(0)
        if self.user.SendInput(1, ct.byref(event), ct.sizeof(event)) != 1:
            raise InputError(f'Windows rejected SendInput (error {ct.get_last_error()}).')

    def physical_desktop_bounds(self):
        # The rectangles from EnumDisplayMonitors can be DPI-virtualized.
        # Current display modes report native pixel sizes and positions.
        api = self.display_api
        try:
            modes = [api.EnumDisplaySettings(api.GetMonitorInfo(monitor)['Device'], ENUM_CURRENT_SETTINGS)
                     for monitor, _, _ in api.EnumDisplayMonitors()]
        except api.error as exc:
            raise InputError('Cannot read the physical Windows display layout.') from exc
        if not modes or any(mode.PelsWidth <= 0 or mode.PelsHeight <= 0 for mode in modes):
            raise InputError('No valid physical Windows display layout is available.')
        left = min(mode.Position_x for mode in modes)
        top = min(mode.Position_y for mode in modes)
        right = max(mode.Position_x + mode.PelsWidth for mode in modes)
        bottom = max(mode.Position_y + mode.PelsHeight for mode in modes)
        return left, top, right - left, bottom - top

    def move(self, point):
        left, top, width, height = self.physical_desktop_bounds()
        x, y = point[0] - left, point[1] - top
        if not (0 <= x < width and 0 <= y < height):
            raise InputError('The pointer coordinate is off the Windows virtual desktop.')
        absolute_x = (x * 65_536 + width - 1) // width
        absolute_y = (y * 65_536 + height - 1) // height
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_ABSOLUTE
        self.send(mouse_event(flags, absolute_x, absolute_y))

    def move_relative(self, dx, dy):
        self.send(mouse_event(MOUSEEVENTF_MOVE, dx, dy))


class Action:
    def __init__(self, backend, window, cancelled):
        self.backend = backend
        self.window = window
        self.hwnd = window['id']
        self.cancelled = cancelled
        self.frame = backend.verify(window)
        self.releases = []
        self.sending_escape = False

    def check(self):
        if self.cancelled.is_set() or self.backend.is_down(0x1B) and not self.sending_escape:
            raise InputError('Input action cancelled.')
        if self.backend.verify(self.window, check_title=False) != self.frame:
            raise InputError('The target window moved or changed size during the action.')

    def wait(self, duration_ms):
        deadline = time.monotonic() + duration_ms / 1000
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.cancelled.wait(min(remaining, 0.025))

    def press(self, code, events):
        self.check()
        if self.backend.is_down(code):
            raise InputError('A requested key or mouse button is already held.')
        down, up = events
        self.backend.send(down)
        self.releases.append(up)

    def press_keys(self, codes):
        self.sending_escape = 0x1B in codes
        for code in codes:
            self.press(code, self.backend.stroke(self.hwnd, code))

    def point(self, point, mode):
        offset_x = self.frame['left'] if mode == 'window' else 0
        offset_y = self.frame['top'] if mode == 'window' else 0
        return point['x'] + offset_x, point['y'] + offset_y

    def move(self, point):
        self.check()
        self.backend.verify_point(self.hwnd, point)
        self.backend.move(point)

    def move_relative(self, dx, dy):
        self.check()
        # Captured cursors may be hidden or recentered by the app. Relative input
        # has no absolute target point to hit-test, so check the foreground window above.
        self.backend.move_relative(dx, dy)

    @contextmanager
    def release_inputs(self):
        try:
            yield
        finally:
            failed = []
            while self.releases:
                event = self.releases.pop()
                try:
                    self.backend.send(event)
                except InputError as error:
                    failed.append(str(error))
            self.sending_escape = False
            if failed:
                raise InputError('Windows could not release every injected input. ' + ' '.join(failed))


def prepare_step(name, args):
    duration = args.get('duration_ms', 320 if name in ('drag_pointer', 'drag_path') else 0)
    if name == 'drag_path' and 'segment_ms' in args:
        if 'duration_ms' in args:
            raise InputError('Use either segment_ms or duration_ms for a drag path.')
        times = args['segment_ms']
        if len(times) != len(args['points']) - 1:
            raise InputError('segment_ms must have one duration per pair of consecutive points.')
        if any(not 0 <= milliseconds <= MAX_DURATION_MS for milliseconds in times):
            raise InputError('Drag segment duration is out of range.')
        duration = sum(times)
    if not 0 <= duration <= MAX_DURATION_MS:
        raise InputError(f'An input action may specify at most {MAX_DURATION_MS} ms of duration.')

    if name == 'hold_key':
        return duration, chord_codes(args['key'])
    keys = [key_code(value, MODIFIERS) for value in args.get('modifiers', [])]
    if name == 'move_relative':
        for delta in (args['dx'], args['dy']):
            if not -2**31 <= delta <= 2**31 - 1 or delta != int(delta):
                raise InputError('Relative deltas must be signed 32-bit integers.')
        steps = args.get('steps', 1)
        if not 1 <= steps <= MAX_RELATIVE_STEPS or steps != int(steps):
            raise InputError(f'Relative steps must be an integer from 1 to {MAX_RELATIVE_STEPS}.')
        if 'key' in args:
            keys.extend(chord_codes(args['key']))
    return duration, unique_keys(keys)


def perform(backend, name, args, cancelled):
    if name == 'input_sequence':
        steps = [(step['action'], step) for step in args['steps']]
        if not steps or any(step_name not in SEQUENCE_ACTIONS for step_name, _ in steps):
            raise InputError('An input sequence must contain supported input actions.')
    else:
        steps = [(name, args)]

    prepared = [(step_name, options, *prepare_step(step_name, options)) for step_name, options in steps]
    if sum(duration for _, _, duration, _ in prepared) > MAX_DURATION_MS:
        raise InputError(f'An input sequence may specify at most {MAX_DURATION_MS} ms of total duration.')

    action = Action(backend, args['window'], cancelled)
    if '_expected_frame' in args and action.frame != args['_expected_frame']:
        raise InputError('The target window moved or changed size after observation.')
    for step_name, options, duration, keys in prepared:
        with action.release_inputs():
            perform_step(action, step_name, options, duration, keys)


def perform_step(action, name, args, duration, keys):
    if name == 'wait':
        action.wait(duration)
        return
    if name == 'hold_key':
        action.press_keys(keys)
        action.wait(duration)
        return
    if name == 'move_relative':
        action.press_keys(keys)
        default_steps = min(MAX_RELATIVE_STEPS, max(1, round(duration / MOVE_INTERVAL_MS)))
        move_relative(action, int(args['dx']), int(args['dy']), duration, int(args.get('steps', default_steps)))
        return

    action.check()
    mode = args.get('coordinates', 'window')
    points = args.get('points')
    if name == 'drag_pointer':
        points = [args['start'], args['end']]
    positions = [action.point(point, mode) for point in points] if points else []
    for position in positions:
        action.backend.verify_point(action.hwnd, position)
    start = positions[0] if positions else action.point(args['point'], mode)
    action.move(start)
    if name == 'hover_pointer':
        action.wait(duration)
        return

    action.press_keys(keys)
    code, down, up = BUTTONS[args.get('button', 'left')]
    action.press(code, (mouse_event(down), mouse_event(up)))
    if positions:
        drag_path(action, positions, duration, args.get('segment_ms'))
    else:
        action.wait(duration)


def move_relative(action, dx, dy, duration, steps):
    start = time.monotonic()
    x_sign, y_sign = (-1 if dx < 0 else 1), (-1 if dy < 0 else 1)
    x_count, y_count = abs(dx), abs(dy)
    previous_x = previous_y = 0
    for index in range(1, steps + 1):
        deadline = start + (duration * index) / (1000 * steps)
        action.wait(max(0, (deadline - time.monotonic()) * 1000))
        # Subtract rounded cumulative totals so all emitted deltas sum exactly.
        x = x_sign * ((x_count * index + steps // 2) // steps)
        y = y_sign * ((y_count * index + steps // 2) // steps)
        if (x, y) != (previous_x, previous_y):
            action.move_relative(x - previous_x, y - previous_y)
        previous_x, previous_y = x, y


def drag_path(action, positions, duration, segment_ms=None):
    """Move through every vertex at the specified segment timings or at constant speed."""
    segments = list(zip(positions, positions[1:]))
    if segment_ms is None:
        lengths = [dist(start, end) for start, end in segments]
        total = sum(lengths)
        if not total:
            action.wait(duration)
            return
        segment_ms = [duration * length / total for length in lengths]
    deadline = time.monotonic()
    for (start, end), milliseconds in zip(segments, segment_ms):
        steps = max(1, round(milliseconds / MOVE_INTERVAL_MS))
        for index in range(1, steps + 1):
            deadline += milliseconds / (1000 * steps)
            action.wait(max(0, (deadline - time.monotonic()) * 1000))
            action.move(tuple(round(a + (b - a) * index / steps) for a, b in zip(start, end)))
