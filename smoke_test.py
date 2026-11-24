"""Exercise the stdio plugin against local test fixtures."""

import argparse
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


class Client:
    def __enter__(self):
        self.process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('server.py'))],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, encoding='utf-8')
        self.messages = queue.Queue()
        self.request_id = 0
        threading.Thread(target=self.read, daemon=True).start()
        return self

    def read(self):
        for line in self.process.stdout:
            self.messages.put(line)

    def request(self, method, params):
        self.request_id += 1
        message = {'jsonrpc': '2.0', 'id': self.request_id, 'method': method, 'params': params}
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()
        result = json.loads(self.messages.get(timeout=35))
        assert result['id'] == self.request_id, result
        return result['result']

    def call(self, name, arguments):
        return self.request('tools/call', {'name': name, 'arguments': arguments})

    def js(self, code, expected_error=False, timeout=20000):
        result = self.call('js', {'code': code, 'timeout_ms': timeout})
        assert result['isError'] == expected_error, [item.get('text', item['type']) for item in result['content']]
        return result['content']

    def __exit__(self, *_):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def general(client):
    initialized = client.request('initialize', {'protocolVersion': '2025-11-25'})
    assert initialized['protocolVersion'] == '2025-11-25'
    tools = client.request('tools/list', {})['tools']
    assert {'js', 'js_reset', 'turn_ended', 'inspect_window', 'hold_key', 'click_pointer',
            'drag_pointer', 'drag_path', 'hover_pointer', 'move_relative', 'input_sequence'} == {
        tool['name'] for tool in tools
    }
    client.js('let saved = await Promise.resolve(41);')
    assert client.js('saved + 1')[0]['text'] == '42'
    assert client.js('(await import("@oai/sky")).sky === sky')[0]['text'] == 'true'
    assert client.js('await Promise.reject(Error("expected"))', expected_error=True)[0]['text'] == 'expected'
    assert client.js('for (;;) {}', expected_error=True, timeout=500)[0]['text'].startswith('JavaScript call cancelled')
    assert client.js('typeof saved')[0]['text'] == 'undefined'
    assert client.call('drag_path', {'points': []})['isError']
    assert client.call('input_sequence', {'window': {'id': 1, 'process_id': 1, 'title': ''},
                                          'steps': [{'action': 'move_relative', 'dx': 1, 'dy': 2, 'point': {}}]})['isError']
    print('MCP handshake, JavaScript state, error, timeout reset and validation passed.')


def input_contract():
    from desktop import Desktop, ScreenshotRegion
    from server import project_pointer_inputs
    from windows_input import InputError, perform

    window = {'id': 1, 'process_id': 1, 'title': 'Fixture'}
    frame = {'left': -910, 'top': 120, 'right': -470, 'bottom': 360}
    image = {'id': 'projected', 'width': 160, 'height': 80}
    events = []

    class Backend:
        def inspect(self, hwnd):
            assert hwnd == window['id']
            return {'window': window, 'frame': frame, 'visible': True}

        def verify(self, observed, check_title=True):
            assert observed == window
            return frame

        def is_down(self, key):
            return False

        def stroke(self, hwnd, key):
            return ('down', key), ('up', key)

        def send(self, event):
            events.append(event)

        def move_relative(self, dx, dy):
            events.append(('relative', dx, dy))

    desktop = Desktop.__new__(Desktop)
    desktop.check_target = lambda hwnd: window
    desktop.input = Backend()
    desktop.images = {window['id']: {image['id']: ScreenshotRegion(image, frame, window['id'],
        window['process_id'], (-900, 140, 320, 160), True)}}
    args = {'window': window, 'steps': [
        {'action': 'move_relative', 'dx': -7, 'dy': 4, 'steps': 5, 'key': 'W+A'},
        {'action': 'drag_path', 'coordinates': 'screenshot', 'screenshotId': 'projected',
         'points': [{'x': 30, 'y': 25}, {'x': 160 - 1, 'y': 80 - 1}], 'segment_ms': [150]},
    ]}
    transformed = project_pointer_inputs(desktop, 'input_sequence', args)
    assert transformed['_expected_frame'] == frame
    assert transformed['steps'][1]['points'] == [{'x': -839, 'y': 191}, {'x': -581, 'y': 299}]
    assert transformed['steps'][1]['coordinates'] == 'screen'
    perform(desktop.input, 'input_sequence', {**transformed, 'steps': transformed['steps'][:1]}, threading.Event())
    assert events[:2] == [('down', ord('W')), ('down', ord('A'))]
    assert events[-2:] == [('up', ord('A')), ('up', ord('W'))]
    assert all(event[0] == 'relative' for event in events[2:-2])
    assert tuple(map(sum, zip(*(event[1:] for event in events[2:-2])))) == (-7, 4)
    previous = events.copy()
    args['steps'][1]['screenshotId'] = 'stale'
    try:
        project_pointer_inputs(desktop, 'input_sequence', args)
    except InputError as error:
        assert 'stale' in str(error)
    else:
        raise AssertionError('The invalid screenshot should reject the entire sequence before dispatch.')
    assert events == previous
    print('Negative monitor projection, exact signed W+A motion and stale sequence preflight passed.')


def desktop(client, send_input):
    fixture_code = '''
import sys
import win32api as api
import win32con as c
import win32gui as gui

def procedure(window, message, wp, lp):
    if message == c.WM_COMMAND and lp and api.HIWORD(wp) == c.EN_CHANGE:
        print(gui.GetWindowText(lp), flush=True)
    if message == c.WM_LBUTTONDOWN:
        print("clicked", flush=True)
    if message == c.WM_MOUSEMOVE and wp & c.MK_LBUTTON:
        print("dragged", flush=True)
    if message == c.WM_DESTROY:
        gui.PostQuitMessage(0)
    return gui.DefWindowProc(window, message, wp, lp)

kind = gui.WNDCLASS()
kind.hInstance = api.GetModuleHandle(None)
kind.lpszClassName = "LocalCuaFixture"
kind.lpfnWndProc = procedure
kind.hbrBackground = c.COLOR_WINDOW + 1
gui.RegisterClass(kind)
root = gui.CreateWindow(kind.lpszClassName, "Local CUA Smoke Fixture",
    c.WS_OVERLAPPEDWINDOW, 140, 140, 530, 300, 0, 0, kind.hInstance, None)
entry = gui.CreateWindow("Edit", "", c.WS_CHILD | c.WS_VISIBLE | c.WS_BORDER | c.ES_AUTOHSCROLL,
    12, 10, 360, 30, root, 0, kind.hInstance, None)
send_input = sys.argv[1] == "input"
gui.ShowWindow(root, c.SW_SHOW if send_input else c.SW_SHOWNOACTIVATE)
gui.UpdateWindow(root)
if send_input:
    try:
        gui.SetForegroundWindow(root)
    except gui.error:
        pass
    gui.SetFocus(entry)
print("ready", flush=True)
gui.PumpMessages()
'''
    lines = queue.Queue()
    with subprocess.Popen([sys.executable, '-u', '-X', 'utf8', '-c', fixture_code, 'input' if send_input else 'observe'],
                          stdout=subprocess.PIPE, text=True, encoding='utf-8') as fixture:
        def read_fixture():
            for line in fixture.stdout:
                lines.put(line.strip())

        threading.Thread(target=read_fixture, daemon=True).start()
        try:
            assert lines.get(timeout=10) == 'ready'
            client.js('let target = (await cua.listWindows({emit:false})).find(w => w.title === "Local CUA Smoke Fixture");'
                      'let app = await cua.getApp({windowId:target.id});')
            observed = client.js('await app.getAXStateAndScreenshot({disableDiffing:true});')
            assert any('Edit' in item.get('text', '') for item in observed), observed[0]['text']
            assert any(item['type'] == 'image' and item['mimeType'] == 'image/png' for item in observed)
            client.js('let oldAX = await app.getAXSnapshot({emit:false});'
                      'let liveAX = await app.getAXSnapshot({emit:false});'
                      'if (oldAX.snapshotId === liveAX.snapshotId || !liveAX.tree.includes("Edit")) throw Error("Invalid AX snapshots");')
            stale = client.js('await app.setValue(0, "stale", {snapshotId:oldAX.snapshotId});', expected_error=True)
            assert 'snapshot is stale' in stale[0]['text']
            inspected = client.js('nodeRepl.write(JSON.stringify(await windowsInput.inspect_window({window_id:target.id})));')
            window = json.loads(inspected[0]['text'])['window']
            assert window['title'] == 'Local CUA Smoke Fixture'
            client.js('await app.click([50, 60], {screenshotId:"stale"});', expected_error=True)
            projected = client.js('''
let pixelApp = await cua.getApp({windowId:target.id, includeAXState:false});
let [projected] = await pixelApp.getScreenshot({
  maxWidth: 160, maxHeight: 80, clip: {x:16, y:90, width:320, height:160}
});
''')
            assert {item['type'] for item in projected} == {'text', 'image'}
            region = json.loads(next(item['text'] for item in projected if item['type'] == 'text'))
            assert (region['width'], region['height'], region['sourceWidth'], region['sourceHeight']) == (160, 80, 320, 160)
            client.js('await pixelApp.click([30,25]);', expected_error=True)
            if send_input:
                import win32gui

                foreground = win32gui.GetForegroundWindow()
                assert foreground == window['id'], ('Test window lost foreground before input', foreground, window['id'])
                client.js('await app.typeText("hello \\u4f60\\u597d");')
                client.js('await app.pressKey("space");')
                deadline = time.monotonic() + 6
                while lines.get(timeout=max(0, deadline - time.monotonic())) != 'hello \u4f60\u597d':
                    pass
                result = client.call('drag_path', {'window': window,
                                                    'points': [{'x': 80, 'y': 100}, {'x': 120, 'y': 130}, {'x': 150, 'y': 95}],
                                                    'duration_ms': 250})
                assert not result.get('isError'), result
                deadline = time.monotonic() + 6
                while lines.get(timeout=max(0, deadline - time.monotonic())) != 'dragged':
                    pass
                motion(client, window, region, lines)
            screenshot_backend = client.js('nodeRepl.write((await app.getScreenshot({emit:false}))[0].captureBackend);')[0]['text']
            print(f'Windows enumeration, UI Automation, PNG screenshot ({screenshot_backend}) and window inspection passed.'
                  + (' Unicode text, key input and multi-point drag also passed.' if send_input else ''))
        finally:
            fixture.terminate()
            fixture.wait(timeout=5)


def motion(client, window, region, lines):
    user32 = ctypes.windll.user32
    set_dpi_context = user32.SetThreadDpiAwarenessContext
    set_dpi_context.argtypes = [ctypes.c_void_p]
    set_dpi_context.restype = ctypes.c_void_p

    def cursor():
        previous = set_dpi_context(ctypes.c_void_p(-4))
        assert previous
        try:
            point = wintypes.POINT()
            assert user32.GetPhysicalCursorPos(ctypes.byref(point))
            return point.x, point.y
        finally:
            set_dpi_context(previous)

    def physical(x, y):
        return region['originX'] + 2 * x + 1, region['originY'] + 2 * y + 1

    client.js('await pixelApp.click([30,25], {screenshotId:projected.id});')
    deadline = time.monotonic() + 6
    while lines.get(timeout=max(0, deadline - time.monotonic())) != 'clicked':
        pass
    position = cursor()
    assert position == physical(30, 25), (position, physical(30, 25), region)
    screenshot = {'coordinates': 'screenshot', 'screenshotId': region['id']}
    result = client.call('input_sequence', {'window': window, 'steps': [
        {'action': 'drag_path', **screenshot,
         'points': [{'x': 30, 'y': 25}, {'x': 35, 'y': 30}, {'x': 45, 'y': 25}], 'segment_ms': [100, 150]},
        {'action': 'wait', 'duration_ms': 20},
    ]})
    assert not result.get('isError'), result
    position = cursor()
    assert position == physical(45, 25), (position, physical(45, 25))
    result = client.call('move_relative', {'window': window, 'dx': 12, 'dy': 0,
                                           'duration_ms': 220, 'steps': 6, 'key': 'W+A'})
    assert not result.get('isError'), result
    moved_x, moved_y = cursor()
    assert moved_x > physical(45, 25)[0] and moved_y == physical(45, 25)[1]
    client.js('await pixelApp.getScreenshot({emit:false});')
    result = client.call('input_sequence', {'window': window, 'steps': [
        {'action': 'move_relative', 'dx': 20, 'dy': 0},
        {'action': 'click_pointer', **screenshot, 'point': {'x': 40, 'y': 40}},
    ]})
    assert result.get('isError') and cursor() == (moved_x, moved_y), result
    result = client.js('await pixelApp.inputSequence([{action:"wait",duration_ms:5}]);')
    assert 'true' in result[0]['text']
    print('Projected screenshot coordinates, timed drag, relative motion and stale batch rejection passed.')


class FixturePage(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"""<title>Local CUA fixture</title><label>Name <input></label>
        <button onclick="this.textContent='Done'">Submit</button>"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def browser(client):
    with ThreadingHTTPServer(('127.0.0.1', 0), FixturePage) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/'
            content = client.js(f'''
let tab = await cua.createBrowserTab("local", {json.dumps(url)}, {{visible:false}});
await tab.playwright.getByLabel("Name").fill("MCP test");
await tab.cua.keypress({{keys:["Control","a"]}});
await tab.cua.type({{text:"Local input"}});
nodeRepl.write(await tab.playwright.getByLabel("Name").inputValue());
await tab.playwright.getByRole("button", {{name:/^submit$/i}}).click();
nodeRepl.write(await tab.playwright.getByRole("button").innerText());
await tab.playwright.waitForURL({json.dumps(url)});
await tab.getScreenshot();
''')
            assert any(item.get('text') == 'Local input' for item in content)
            assert any(item.get('text') == 'Done' for item in content)
            assert any(item['type'] == 'image' for item in content)
            assert 'Local CUA fixture' in client.js('await cua.listTabs()')[0]['text']
            client.call('js_reset', {})
            assert client.js('typeof tab')[0]['text'] == 'undefined'
            print('Local Chromium, DOM snapshot, locator input, screenshot and session reset passed.')
        finally:
            server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--desktop-input', action='store_true')
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    input_contract()
    with Client() as client:
        general(client)
        if args.desktop or args.desktop_input:
            desktop(client, args.desktop_input)
        if args.browser:
            browser(client)
