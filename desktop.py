import base64
import ctypes as ct
from ctypes import wintypes as wt
from dataclasses import dataclass
import io
from importlib.util import find_spec
import math
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from windows_input import Action, INPUT, InputData, InputError, KEYBDINPUT, Windows, mouse_event, perform


def image_url(data):
    return 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')


def window_id(value):
    if isinstance(value, dict):
        value = value.get('id', value.get('windowId'))
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 < value < 2**53:
        raise InputError('Pass a Windows ID from listWindows().')
    return value


def graphics_capture(hwnd):
    from PIL import Image

    if find_spec('windows_capture') is None:
        return None
    try:
        worker = str(Path(__file__).with_name('capture_wgc.py'))
        with subprocess.Popen([sys.executable, '-B', worker, str(hwnd)], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as process:
            try:
                data, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                data, _ = process.communicate()
        if data:
            with Image.open(io.BytesIO(data)) as image:
                return image.convert('RGB')
    except (OSError, ValueError):
        return None
    return None


def screenshot_options(options):
    if options is None:
        return {}
    if not isinstance(options, dict) or options.keys() - {'maxWidth', 'maxHeight', 'clip'}:
        raise InputError('Expected screenshot_options with optional maxWidth, maxHeight and clip fields.')
    for key in ('maxWidth', 'maxHeight'):
        if key in options and (type(options[key]) is not int or options[key] < 1):
            raise InputError(f'{key} must be a positive integer.')
    if 'clip' in options:
        clip = options['clip']
        if (not isinstance(clip, dict) or clip.keys() != {'x', 'y', 'width', 'height'}
                or any(type(value) is not int for value in clip.values())
                or clip['x'] < 0 or clip['y'] < 0 or clip['width'] < 1 or clip['height'] < 1):
            raise InputError('clip must contain non-negative integer x and y and positive integer width and height.')
    return options


def project_screenshot(image, options, clip):
    from PIL import Image

    x = y = 0
    if clip is not None:
        x, y, width, height = (clip[key] for key in ('x', 'y', 'width', 'height'))
        if x + width > image.width or y + height > image.height:
            raise InputError('clip extends outside the window screenshot.')
        image = image.crop((x, y, x + width, y + height))
    source_width, source_height = image.size
    max_width = min(source_width, options.get('maxWidth', source_width))
    max_height = min(source_height, options.get('maxHeight', source_height))
    scale = min(max_width / source_width, max_height / source_height)
    if scale < 1:
        size = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    return image, (x, y, source_width, source_height)


@dataclass(frozen=True)
class ScreenshotRegion:
    data: dict
    frame: dict
    hwnd: int
    process_id: int
    source: tuple[int, int, int, int]
    transformed: bool


class Desktop:
    def __init__(self):
        import comtypes.client
        import psutil
        import uiautomation as uia
        import win32gui
        import win32con

        comtypes.client.gen_dir = None  # Generate UI Automation bindings in memory.
        uia.Logger.SetLogFile(sys.stderr)
        self.psutil = psutil
        self.uia = uia
        self.gui = win32gui
        self.con = win32con
        self.input = Windows()
        self.trees = {}
        self.images = {}
        self.targets = {}
        self.revision = 0
        self.uia_thread = uia.UIAutomationInitializerInThread()

    def close(self):
        self.trees.clear()
        self.images.clear()
        self.targets.clear()

    def window(self, hwnd):
        native = self.input.inspect(hwnd)['window']
        pid = native['process_id']
        try:
            app = self.psutil.Process(pid).exe()
        except (self.psutil.Error, OSError):
            app = f'pid:{pid}'
        return {'id': hwnd, 'app': app, 'title': native['title']}

    def windows(self):
        result = []

        def visit(hwnd, _):
            if self.gui.IsWindowVisible(hwnd) and self.gui.GetWindowText(hwnd):
                try:
                    result.append(self.window(hwnd))
                except InputError:
                    pass
            return True

        self.gui.EnumWindows(visit, None)
        return result

    def shortcuts(self):
        for key in ('APPDATA', 'ProgramData'):
            if directory := os.environ.get(key):
                yield from (Path(directory) / 'Microsoft/Windows/Start Menu/Programs').rglob('*.lnk')

    def apps(self):
        apps = {}
        for window in self.windows():
            name = Path(window['app']).stem
            entry = apps.setdefault(window['app'], {'id': window['app'], 'displayName': name, 'isRunning': True, 'windows': []})
            entry['windows'].append(window)
        for shortcut in self.shortcuts():
            app_id = str(shortcut)
            apps[app_id] = {'id': app_id, 'displayName': shortcut.stem, 'isRunning': False, 'windows': []}
        return sorted(apps.values(), key=lambda app: (not app['isRunning'], app['displayName'].casefold()))

    def launch(self, app):
        selected = next((item for item in self.apps() if item['id'].casefold() == app.casefold()), None)
        if selected:
            os.startfile(selected['id'])
            return
        executable = Path(app)
        if not executable.is_absolute() or executable.suffix.lower() != '.exe' or not executable.is_file():
            raise InputError('Pass an application ID from listApps() or an absolute .exe path.')
        os.startfile(executable)

    def check_target(self, hwnd):
        native = self.input.inspect(hwnd)['window']
        pid = native['process_id']
        try:
            identity = (pid, self.psutil.Process(pid).create_time())
        except (self.psutil.Error, OSError):
            identity = (pid, None)
        previous = self.targets.setdefault(hwnd, identity)
        if identity != previous:
            raise InputError('The selected window identity changed. Select it again with getApp().')
        return native

    def select(self, hwnd):
        self.targets.pop(hwnd, None)
        self.trees.pop(hwnd, None)
        self.images.pop(hwnd, None)
        self.check_target(hwnd)
        return self.window(hwnd)

    def activate(self, hwnd):
        native = self.check_target(hwnd)
        if self.gui.IsIconic(hwnd):
            self.gui.ShowWindow(hwnd, self.con.SW_RESTORE)
        if self.gui.GetForegroundWindow() != hwnd:
            try:
                self.gui.SetForegroundWindow(hwnd)
            except self.gui.error:
                pass
        self.input.verify(native)
        return native

    def tree(self, hwnd, cancelled, diff=False, keep_images=False):
        self.check_target(hwnd)
        previous = self.trees.pop(hwnd, None)
        if not keep_images:
            self.images.pop(hwnd, None)
        windows = [hwnd, *self.owned_windows(hwnd)]
        nodes = []
        lines = []
        focus = None
        focused_fields = {}
        truncated = False
        stack = [(self.uia.ControlFromHandle(window), 0) for window in reversed(windows)]
        while stack and len(nodes) < 700:
            if cancelled.is_set():
                raise InputError('Accessibility observation cancelled.')
            control, depth = stack.pop()
            try:
                role = control.ControlTypeName.removesuffix('Control')
                name = ' '.join(control.Name.split())
                index = len(nodes)
                focused = control.HasKeyboardFocus
                password = control.IsPassword
                value = None
                if not password and role in ('Edit', 'Slider', 'Spinner', 'ProgressBar'):
                    value_pattern = control.GetPattern(self.uia.PatternId.ValuePattern)
                    range_pattern = None if value_pattern else control.GetPattern(self.uia.PatternId.RangeValuePattern)
                    value = value_pattern.Value if value_pattern else range_pattern.Value if range_pattern else None
                nodes.append(control)
                line = f'{"  " * depth}{index} {role} {name[:250]}'.rstrip()
                if value not in (None, '', name):
                    line += f' (value: {str(value)[:250]!r})'
                if focused:
                    focus = line
                    if not password and role in ('Edit', 'Document'):
                        try:
                            if text_pattern := control.GetPattern(self.uia.PatternId.TextPattern):
                                focused_fields['document_text'] = text_pattern.DocumentRange.GetText(4000)
                                selection = [selection.GetText(1000) for selection in text_pattern.GetSelection()]
                                if any(selection):
                                    focused_fields['selected_text'] = '\n'.join(selection)
                        except (OSError, AttributeError):
                            pass
                lines.append(line)
                if depth < 24:
                    children = control.GetChildren()
                    truncated |= len(children) > 200
                    stack.extend((child, depth + 1) for child in reversed(children[:200]))
                else:
                    truncated |= control.GetFirstChildControl() is not None
            except (OSError, AttributeError):
                continue
        content = '\n'.join(lines)
        if cancelled.is_set():
            raise InputError('Accessibility observation cancelled.')
        snapshot_id = uuid4().hex
        self.trees[hwnd] = (nodes, content, snapshot_id)
        result = {'tree': content if not diff or not previous or previous[1] != content else '(unchanged)',
                  'snapshotId': snapshot_id, 'truncated': bool(stack) or truncated, **focused_fields}
        if focus:
            result['focused_element'] = focus
        return result

    def owned_windows(self, hwnd):
        result = []

        def visit(child, _):
            if len(result) < 8 and child != hwnd and self.gui.IsWindowVisible(child) and not self.gui.IsIconic(child):
                owner = self.gui.GetWindow(child, self.con.GW_OWNER)
                while owner and owner != hwnd:
                    owner = self.gui.GetWindow(owner, self.con.GW_OWNER)
                if owner == hwnd:
                    result.append(child)
            return True

        self.gui.EnumWindows(visit, None)
        return result

    def screenshot(self, hwnd, options=None):
        self.check_target(hwnd)
        options = screenshot_options(options)
        self.images.pop(hwnd, None)
        regions = {}
        for region_hwnd in [hwnd, *self.owned_windows(hwnd)]:
            try:
                clip = options.get('clip') if region_hwnd == hwnd else None
                region = self.screenshot_region(region_hwnd, len(regions), options, clip)
            except (InputError, OSError, self.gui.error):
                if region_hwnd == hwnd:
                    raise
                continue
            regions[region.data['id']] = region
        self.images[hwnd] = regions
        return [region.data for region in regions.values()]

    def screenshot_region(self, hwnd, z_index, options, clip):
        if self.gui.IsIconic(hwnd):
            raise InputError('Restore the minimized window before capturing it.')
        state = self.input.inspect(hwnd)
        rect = self.gui.GetWindowRect(hwnd)
        frame = state['frame']
        if (rect[2] - rect[0]) * (rect[3] - rect[1]) > 40_000_000:
            raise InputError('The target window is too large to capture.')
        left, top, right, bottom = (frame[key] for key in ('left', 'top', 'right', 'bottom'))
        glass_dimensions = (right - left, bottom - top)
        window_dimensions = (rect[2] - rect[0], rect[3] - rect[1])
        image = graphics_capture(hwnd)
        backend = 'windows-graphics-capture'
        if image is None or image.size not in (glass_dimensions, window_dimensions):
            image = self.capture_window(hwnd, *window_dimensions)
            backend = 'print-window'
        if image.size == (rect[2] - rect[0], rect[3] - rect[1]) and rect != (left, top, right, bottom):
            image = image.crop((left - rect[0], top - rect[1], right - rect[0], bottom - rect[1]))
        if image.size != (right - left, bottom - top):
            raise InputError('The screenshot dimensions changed. Capture the window again.')
        current = self.input.inspect(hwnd)
        if current['frame'] != frame or current['window']['process_id'] != state['window']['process_id']:
            raise InputError('The window moved during capture. Capture it again.')
        x, y, source_width, source_height = 0, 0, image.width, image.height
        transformed = False
        if options:
            image, (x, y, source_width, source_height) = project_screenshot(image, options, clip)
            transformed = clip is not None or image.size != (source_width, source_height)
        origin_x, origin_y = left + x, top + y
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        self.revision += 1
        data = {'id': f'screenshot-{self.revision}', 'originX': origin_x, 'originY': origin_y,
                'width': image.width, 'height': image.height, 'zIndex': z_index, 'captureBackend': backend,
                'url': image_url(buffer.getvalue())}
        if options:
            data.update(sourceWidth=source_width, sourceHeight=source_height)
        return ScreenshotRegion(data, frame, hwnd, state['window']['process_id'],
                                (origin_x, origin_y, source_width, source_height), transformed)

    def capture_window(self, hwnd, width, height):
        from PIL import Image
        import win32ui

        capture = self.input.user.PrintWindow
        capture.argtypes = [wt.HWND, wt.HDC, wt.UINT]
        capture.restype = wt.BOOL
        window_dc = self.gui.GetWindowDC(hwnd)
        source = target = bitmap = None
        try:
            source = win32ui.CreateDCFromHandle(window_dc)
            target = source.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, width, height)
            previous = target.SelectObject(bitmap)
            try:
                if not capture(hwnd, target.GetSafeHdc(), 2):
                    raise InputError('Windows could not render this window for capture.')
                return Image.frombytes('RGB', (width, height), bitmap.GetBitmapBits(True), 'raw', 'BGRX')
            finally:
                target.SelectObject(previous)
        finally:
            if target:
                target.DeleteDC()
            if bitmap:
                self.gui.DeleteObject(bitmap.GetHandle())
            self.gui.ReleaseDC(hwnd, window_dc)

    def element(self, hwnd, index):
        snapshot = self.trees.get(hwnd)
        nodes = snapshot[0] if snapshot else []
        if type(index) is not int or not 0 <= index < len(nodes):
            raise InputError('Pass an element index from the latest accessibility observation.')
        return nodes[index]

    def pattern(self, hwnd, index, name, required=True):
        try:
            element = self.element(hwnd, index)
            pattern = element.GetPattern(getattr(self.uia.PatternId, f'{name}Pattern'))
        except (OSError, AttributeError):
            pattern = None
        if required and not pattern:
            raise InputError(f'The element does not support the Windows {name} pattern.')
        return pattern

    def region(self, hwnd, screenshot_id):
        regions = self.images.get(hwnd)
        if not regions:
            raise InputError('Capture a fresh screenshot before coordinate input.')
        if screenshot_id is None:
            if len(regions) != 1:
                raise InputError('Pass screenshotId to select one of the captured windows.')
            region = next(iter(regions.values()))
            if region.transformed:
                raise InputError('Pass screenshotId for a resized or clipped screenshot.')
            return region
        if not isinstance(screenshot_id, str) or screenshot_id not in regions:
            raise InputError('The screenshot ID is stale.')
        return regions[screenshot_id]

    def map_points(self, hwnd, points, screenshot_id):
        self.check_target(hwnd)
        region = self.region(hwnd, screenshot_id)
        if region.hwnd != hwnd and region.hwnd not in self.owned_windows(hwnd):
            raise InputError('The captured dialog is no longer owned by the selected window.')
        current = self.input.inspect(region.hwnd)
        if current['window']['process_id'] != region.process_id or not current['visible']:
            raise InputError('The captured window changed. Refresh the observation.')
        if current['frame'] != region.frame:
            raise InputError('The window moved or resized after observation.')
        origin_x, origin_y, source_width, source_height = region.source
        width, height = region.data['width'], region.data['height']
        mapped = []
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) not in (float, int) or (type(value) is float and not math.isfinite(value))
                           for value in point)):
                raise InputError('Expected [x, y] in screenshot pixels.')
            x, y = map(round, point)
            if not (0 <= x < width and 0 <= y < height):
                raise InputError('Coordinate is outside the screenshot.')
            # Choose the source pixel at the center of the rendered pixel's footprint.
            mapped.append({'x': origin_x + (2 * x + 1) * source_width // (2 * width),
                           'y': origin_y + (2 * y + 1) * source_height // (2 * height)})
        return region.hwnd, mapped, current['frame']

    def point(self, hwnd, target, screenshot_id=None):
        if type(target) is int:
            rect = self.element(hwnd, target).BoundingRectangle
            if rect.right <= rect.left or rect.bottom <= rect.top:
                raise InputError('The element has no visible bounding rectangle.')
            return {'x': (rect.left + rect.right) // 2, 'y': (rect.top + rect.bottom) // 2}
        return self.map_points(hwnd, [target], screenshot_id)[1][0]

    def select_text(self, hwnd, index, text, options):
        document = self.pattern(hwnd, index, 'Text').DocumentRange
        prefix, suffix = options.get('prefix', ''), options.get('suffix', '')
        selection = options.get('selectionType', 'text')
        if selection not in ('text', 'cursor_before', 'cursor_after') or not text:
            raise InputError('Expected non-empty text and a valid selectionType.')
        candidate = document.FindText(prefix + text + suffix, False, False)
        if not candidate:
            raise InputError('The requested text was not found.')
        start, end = self.uia.TextPatternRangeEndpoint.Start, self.uia.TextPatternRangeEndpoint.End
        unit = self.uia.TextUnit.Character
        if suffix:
            candidate.MoveEndpointByUnit(end, unit, -len(suffix))
        if prefix:
            candidate.MoveEndpointByUnit(start, unit, len(prefix))
        if selection == 'cursor_before':
            candidate.MoveEndpointByRange(end, candidate, start)
        elif selection == 'cursor_after':
            candidate.MoveEndpointByRange(start, candidate, end)
        candidate.Select()

    def secondary(self, hwnd, index, name):
        operation = name.strip().casefold()
        if operation == 'raise':
            self.activate(hwnd)
        elif operation in ('expand', 'collapse'):
            getattr(self.pattern(hwnd, index, 'ExpandCollapse'), operation.title())()
        elif operation in ('toggle', 'invoke', 'select'):
            owner = 'SelectionItem' if operation == 'select' else operation.title()
            getattr(self.pattern(hwnd, index, owner), operation.title())()
        elif operation in ('scroll up', 'scroll down', 'scroll left', 'scroll right'):
            none, decrement, increment = self.uia.ScrollAmount.NoAmount, self.uia.ScrollAmount.SmallDecrement, self.uia.ScrollAmount.SmallIncrement
            axes = {'scroll up': (none, decrement), 'scroll down': (none, increment),
                    'scroll left': (decrement, none), 'scroll right': (increment, none)}
            self.pattern(hwnd, index, 'Scroll').Scroll(*axes[operation])
        else:
            raise InputError(f'Unsupported accessibility action: {name}')

    def type_text(self, window, text, cancelled):
        action = Action(self.input, window, cancelled)
        encoded = text.encode('utf-16-le')
        for offset in range(0, len(encoded), 2):
            action.check()
            unit = int.from_bytes(encoded[offset:offset + 2], 'little')
            down, up = (INPUT(1, InputData(keyboard=KEYBDINPUT(0, unit, flag, 0, 0))) for flag in (4, 6))
            self.input.send(down)
            self.input.send(up)

    def paste(self, window, text, options, cancelled):
        import win32clipboard as clipboard

        format_name = options.get('format', 'text')
        if format_name not in ('text', 'md', 'html'):
            raise InputError('Unsupported clipboard format.')
        payload = None
        if format_name == 'html':
            header = 'Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n'
            prefix = b'<html><body><!--StartFragment-->'
            suffix = b'<!--EndFragment--></body></html>'
            fragment = text.encode('utf-8')
            start = len(header.format(0, 0, 0, 0))
            payload = header.format(start, start + len(prefix + fragment + suffix), start + len(prefix), start + len(prefix + fragment)).encode() + prefix + fragment + suffix + b'\x00'
        action = Action(self.input, window, cancelled)
        action.check()
        clipboard.OpenClipboard()
        try:
            clipboard.EmptyClipboard()
            clipboard.SetClipboardText(text, 13)
            if payload is not None:
                clipboard.SetClipboardData(clipboard.RegisterClipboardFormat('HTML Format'), payload)
        finally:
            clipboard.CloseClipboard()
        perform(self.input, 'hold_key', {'window': window, 'key': 'Ctrl+V', 'duration_ms': 25}, cancelled)

    def act(self, hwnd, method, args, cancelled):
        self.check_target(hwnd)
        if cancelled.is_set():
            raise InputError('Input action cancelled.')
        index = args.get('element_index')
        if 'snapshotId' in args:
            snapshot = self.trees.get(hwnd)
            if index is None:
                raise InputError('snapshotId requires an accessibility element index.')
            if snapshot is None or snapshot[2] != args['snapshotId']:
                raise InputError('The accessibility snapshot is stale. Observe the target window again.')
        if method == 'set_value':
            pattern = self.pattern(hwnd, index, 'Value', required=False)
            if pattern:
                pattern.SetValue(args['value'])
            elif slider := self.pattern(hwnd, index, 'RangeValue', required=False):
                slider.SetValue(float(args['value']))
            else:
                self.pattern(hwnd, index, 'LegacyIAccessible').SetValue(args['value'])
            return
        if method == 'select_text':
            return self.select_text(hwnd, index, args['text'], args.get('options', {}))
        if method == 'perform_secondary_action':
            return self.secondary(hwnd, index, args['action'])
        if method == 'click' and index is not None and args.get('mouse_button', 'left') in ('left', 'l') and args.get('click_count', 1) == 1:
            if pattern := self.pattern(hwnd, index, 'Invoke', required=False):
                pattern.Invoke()
                return
        if method not in ('activate_window', 'press_key', 'type_text', 'paste', 'click', 'drag', 'scroll'):
            raise InputError(f'Unsupported desktop method: {method}')
        if method in ('click', 'drag', 'scroll'):
            screenshot_id = args.get('screenshotId')
            points = []
            if index is None:
                points.append([args.get('x', args.get('from_x')), args.get('y', args.get('from_y'))])
            if method == 'drag':
                points.append([args.get('to_x'), args.get('to_y')])
            mapping = self.map_points(hwnd, points, screenshot_id) if points else None
            if index is None:
                target_hwnd, mapped, _ = mapping
                point = mapped[0]
            else:
                point = self.point(hwnd, index)
                target_hwnd = self.gui.GetAncestor(self.gui.WindowFromPoint((point['x'], point['y'])), 2)
                if target_hwnd not in self.owned_windows(hwnd):
                    target_hwnd = hwnd
            end = mapping[1][-1] if method == 'drag' else None
        else:
            foreground = self.gui.GetForegroundWindow()
            target_hwnd = foreground if foreground in self.owned_windows(hwnd) else hwnd
        native = self.activate(target_hwnd)
        if method in ('click', 'drag', 'scroll'):
            if mapping and mapping != self.map_points(hwnd, points, screenshot_id):
                raise InputError('The target moved after activation. Refresh the observation.')
            if index is not None and point != self.point(hwnd, index):
                raise InputError('The target moved after activation. Refresh the observation.')
            pointer = {'window': native, 'coordinates': 'screen', 'point': point}
            if mapping and target_hwnd == mapping[0]:
                pointer['_expected_frame'] = mapping[2]
        if method == 'activate_window':
            return
        if method == 'press_key':
            return perform(self.input, 'hold_key', {'window': native, 'key': args['key'], 'duration_ms': 25}, cancelled)
        if method == 'type_text':
            if index is not None:
                self.element(hwnd, index).SetFocus()
            return self.type_text(native, args['text'], cancelled)
        if method == 'paste':
            return self.paste(native, args['text'], args.get('options', {}), cancelled)
        if method == 'click':
            button = args.get('mouse_button', 'left')
            pointer['button'] = {'l': 'left', 'm': 'middle', 'r': 'right'}.get(button, button)
            count = args.get('click_count', 1)
            if type(count) is not int or not 1 <= count <= 3:
                raise InputError('clickCount must be 1, 2 or 3.')
            for _ in range(count):
                perform(self.input, 'click_pointer', pointer, cancelled)
        elif method == 'drag':
            perform(self.input, 'drag_pointer', {**pointer, 'start': point, 'end': end}, cancelled)
        elif method == 'scroll':
            action = Action(self.input, native, cancelled)
            if '_expected_frame' in pointer and action.frame != pointer['_expected_frame']:
                raise InputError('The window moved or resized after observation.')
            action.move((point['x'], point['y']))
            for delta, flags in ((args.get('scrollX', 0), 0x1000), (-args.get('scrollY', 0), 0x0800)):
                if delta:
                    action.check()
                    self.input.send(mouse_event(flags, data=round(delta)))

    def call(self, method, args, cancelled):
        if method == 'list_windows':
            return self.windows()
        if method == 'list_apps':
            return self.apps()
        if method == 'launch_app':
            return self.launch(args['app'])
        hwnd = window_id(args.get('window', args.get('id')))
        if method == 'get_window':
            return self.select(hwnd)
        if method == 'get_window_state':
            capture = args.get('include_screenshot', True)
            tree = self.tree(hwnd, cancelled, args.get('diff', False), capture) if args.get('include_text') else None
            screenshots = self.screenshot(hwnd, args.get('screenshot_options')) if capture else []
            if not capture:
                self.images.pop(hwnd, None)
            return {'window': self.window(hwnd), 'accessibility': tree, 'screenshots': screenshots}
        return self.act(hwnd, method, args, cancelled)
