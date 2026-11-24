import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from jsonschema import ValidationError, validate

from browser import BrowserBackend
from desktop import Desktop
from windows_input import InputError, MAX_DURATION_MS, perform


ROOT = Path(__file__).resolve().parent
PROTOCOL_OUT = sys.stdout
VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
STRING = {'type': 'string'}
ID = {'type': 'integer', 'minimum': 1, 'maximum': 2**53 - 1}


def obj(properties, *required):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


POINT = obj({'x': {'type': 'integer'}, 'y': {'type': 'integer'}}, 'x', 'y')
WINDOW = obj({'id': ID, 'process_id': ID, 'title': STRING}, 'id', 'process_id', 'title')
DURATION = {'type': 'integer', 'minimum': 0, 'maximum': MAX_DURATION_MS}
MODIFIERS = {'type': 'array', 'items': STRING, 'maxItems': 6}
BUTTONS = {'button': {'enum': ['left', 'right', 'middle']}, 'modifiers': MODIFIERS}
TIMING = {'type': 'integer', 'minimum': 1, 'maximum': 120000}
POINTER = {'coordinates': {'enum': ['window', 'screen', 'screenshot']},
           'screenshotId': {'type': 'string', 'minLength': 1}, 'duration_ms': DURATION}
MOVE_COUNT = {'type': 'integer', 'minimum': -(2**31), 'maximum': 2**31 - 1}
ACTIONS = {
    'hold_key': ('Hold a key or chord and release it automatically.',
                 {'key': STRING, 'duration_ms': {**DURATION, 'minimum': 1}}, ('key', 'duration_ms')),
    'click_pointer': ('Click or hold a mouse button, with optional modifiers.',
                      {**POINTER, **BUTTONS, 'point': POINT}, ('point',)),
    'drag_pointer': ('Drag between two points with an optional duration, button and modifiers.',
                     {**POINTER, **BUTTONS, 'start': POINT, 'end': POINT}, ('start', 'end')),
    'drag_path': ('Draw a continuous stroke through multiple points with one mouse press.',
                  {**POINTER, **BUTTONS, 'points': {'type': 'array', 'items': POINT, 'minItems': 2, 'maxItems': 1024},
                   'segment_ms': {'type': 'array', 'items': DURATION, 'minItems': 1, 'maxItems': 1023}},
                  ('points',)),
    'hover_pointer': ('Move the pointer without clicking, optionally waiting for a tooltip.',
                      {**POINTER, 'point': POINT}, ('point',)),
    'move_relative': ('Send signed relative mouse movement to the foreground window. Optionally hold a key chord during movement.',
                      {'dx': MOVE_COUNT, 'dy': MOVE_COUNT, 'duration_ms': DURATION,
                       'steps': {'type': 'integer', 'minimum': 1, 'maximum': 1024}, 'key': STRING, 'modifiers': MODIFIERS},
                      ('dx', 'dy')),
}
SEQUENCE_ACTIONS = ('hold_key', 'move_relative', 'drag_path', 'click_pointer')
STEP_SCHEMAS = [
    obj({'action': {'const': name}, **ACTIONS[name][1]}, 'action', *ACTIONS[name][2])
    for name in SEQUENCE_ACTIONS
] + [obj({'action': {'const': 'wait'}, 'duration_ms': DURATION}, 'action', 'duration_ms')]
INPUT_TOOLS = {
    'inspect_window': ('Inspect a Windows window ID and return the identity and frame.', obj({'window_id': ID, 'observed_title': STRING}, 'window_id')),
    **{name: (description, obj({'window': WINDOW, **properties}, 'window', *required))
       for name, (description, properties, required) in ACTIONS.items()},
    'input_sequence': ('Run a bounded sequence of input steps against one foreground window.',
                       obj({'window': WINDOW,
                            'steps': {'type': 'array', 'items': {'oneOf': STEP_SCHEMAS},
                                      'minItems': 1, 'maxItems': 64}},
                           'window', 'steps')),
}
POINT_FIELDS = {'click_pointer': ('point',), 'hover_pointer': ('point',),
                'drag_pointer': ('start', 'end'), 'drag_path': ('points',)}
TOOLS = {
    'js': ('Run JavaScript in a persistent local CUA session. Globals are cua, sky, windowsInput and nodeRepl. Start with await cua.getState() or cua.rewriteDocumentation().',
           obj({'code': {'type': 'string', 'maxLength': 100000}, 'title': STRING, 'timeout_ms': TIMING}, 'code')),
    'js_reset': ('Reset JavaScript bindings and local browser, window and audio state.', obj({})),
    'turn_ended': ('Notify the runtime that the current host turn ended. Cancels active input.',
                   obj({'hook_event_name': STRING, 'session_id': STRING, 'turn_id': STRING})),
    **INPUT_TOOLS,
}


def tool_result(value):
    return {'content': [{'type': 'text', 'text': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}]}


def project_pointer_inputs(desktop, method, args):
    """Convert reported image coordinates to the physical coordinates sent to Windows."""
    hwnd = args['window']['id']
    is_sequence = method == 'input_sequence'
    steps = args['steps'] if is_sequence else [args]
    mapped = []
    targets = set()
    expected_frame = None
    for step in steps:
        fields = POINT_FIELDS.get(step.get('action', method))
        if not fields:
            mapped.append(step)
            continue
        if step.get('coordinates') != 'screenshot':
            if 'screenshotId' in step:
                raise InputError('screenshotId requires coordinates="screenshot".')
            targets.add(hwnd)
            mapped.append(step)
            continue
        screenshot_id = step.get('screenshotId')
        if not screenshot_id:
            raise InputError('Pass the latest screenshotId for screenshot coordinates.')
        path = fields == ('points',)
        points = step['points'] if path else [step[field] for field in fields]
        target, positions, frame = desktop.map_points(hwnd, [[point['x'], point['y']] for point in points], screenshot_id)
        targets.add(target)
        if expected_frame is not None and frame != expected_frame:
            raise InputError('The sequence references inconsistent screenshot geometry.')
        expected_frame = frame
        updated = {**step, 'coordinates': 'screen'}
        del updated['screenshotId']
        updated.update({'points': positions} if path else zip(fields, positions))
        mapped.append(updated)

    if len(targets) > 1:
        raise InputError('All pointer steps in a sequence must target the same native window.')
    target = next(iter(targets), hwnd)
    window = args['window'] if target == hwnd else desktop.check_target(target)
    result = {**args, 'steps': mapped} if is_sequence else mapped[0]
    result = {**result, 'window': window}
    if expected_frame is not None:
        result['_expected_frame'] = expected_frame
    return result


class Runtime:
    def __init__(self, node):
        self.node = node
        self.process = None
        self.desktop = None
        self.browser = BrowserBackend()
        self.audio = None
        self.evaluation = 0

    def get_desktop(self):
        if sys.platform != 'win32':
            raise InputError('Windows desktop tools require Windows.')
        if self.desktop is None:
            self.desktop = Desktop()
        return self.desktop

    def shutdown(self):
        self.stop_repl()
        self.browser.close()
        if self.audio:
            self.audio.close()
            self.audio = None
        if self.desktop:
            self.desktop.close()

    def stop_repl(self):
        if self.process:
            process, self.process = self.process, None
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

    def start_repl(self):
        worker = ROOT / 'repl.cjs'
        allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        self.process = subprocess.Popen([self.node, '--permission', f'--allow-fs-read={worker}', str(worker)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        env=env, cwd=ROOT, encoding='utf-8', errors='replace', bufsize=1)
        self.messages = queue.Queue()
        process, messages = self.process, self.messages

        def read_worker():
            try:
                for line in process.stdout:
                    messages.put(json.loads(line))
            except (ValueError, OSError):
                pass
            finally:
                messages.put(None)

        threading.Thread(target=read_worker, daemon=True).start()

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=True, allow_nan=False) + '\n')
        self.process.stdin.flush()

    def input(self, method, args, cancelled):
        validate(args, INPUT_TOOLS[method][1])
        desktop = self.get_desktop()
        if method == 'inspect_window':
            result = desktop.input.inspect(args['window_id'])
            if 'observed_title' in args and result['window']['title'] != args['observed_title']:
                raise InputError('Native window title differs from the Computer Use observation.')
            return result
        result = desktop.check_target(args['window']['id'])
        if result != args['window']:
            raise InputError('The window identity changed. Inspect it again.')
        dispatch_args = project_pointer_inputs(desktop, method, args)
        perform(desktop.input, method, dispatch_args, cancelled)
        return {'ok': True, 'action': method}

    def dispatch(self, message, cancelled):
        provider, method, args = (message[key] for key in ('provider', 'method', 'args'))
        if cancelled.is_set():
            raise InputError('Operation cancelled.')
        if provider == 'desktop':
            return self.get_desktop().call(method, args, cancelled)
        if provider == 'browser':
            return self.browser.call(method, args, cancelled)
        if provider == 'audio':
            from audio import Audio
            if sys.platform != 'win32':
                raise InputError('WASAPI loopback requires Windows.')
            if self.audio is None:
                self.audio = Audio()
            if method == 'start':
                return self.audio.start(args.get('max_duration_ms', 60000))
            if method == 'stop':
                return self.audio.stop()
        if provider == 'input' and method in INPUT_TOOLS:
            return self.input(method, args, cancelled)
        if provider == 'local' and method == 'documentation':
            return (ROOT / 'API.md').read_text(encoding='utf-8')
        raise InputError('Unknown local capability.')

    def evaluate(self, args, cancelled):
        if self.process is None:
            self.start_repl()
        try:
            self.evaluation += 1
            deadline = time.monotonic() + args.get('timeout_ms', 30000) / 1000
            timer = threading.Timer(args.get('timeout_ms', 30000) / 1000, cancelled.set)
            timer.start()
            try:
                self.send({'type': 'eval', 'evaluation': self.evaluation, 'code': args['code']})
                while time.monotonic() < deadline and not cancelled.is_set():
                    try:
                        message = self.messages.get(timeout=0.08)
                    except queue.Empty:
                        continue
                    if message is None:
                        raise InputError('JavaScript worker exited.')
                    if message.get('evaluation') != self.evaluation:
                        if message['type'] == 'rpc':
                            self.send({'type': 'rpc_result', 'id': message['id'], 'error': 'The JavaScript evaluation has ended.'})
                        continue
                    if message['type'] == 'result':
                        return {'content': message['content'], 'isError': message['isError']}
                    try:
                        value = self.dispatch(message, cancelled)
                        self.send({'type': 'rpc_result', 'id': message['id'], 'result': value})
                    except Exception as error:
                        self.send({'type': 'rpc_result', 'id': message['id'], 'error': str(error)})
                raise InputError('JavaScript call cancelled or timed out. Its REPL was reset.')
            finally:
                timer.cancel()
        except (InputError, OSError, ValueError):
            self.stop_repl()
            raise

    def call(self, name, args, cancelled):
        if name == 'js':
            return self.evaluate(args, cancelled)
        if name == 'js_reset':
            self.shutdown()
            return tool_result('Local CUA session reset.')
        if name == 'turn_ended':
            if self.audio:
                self.audio.close()
                self.audio = None
            return tool_result('Turn ended.')
        return tool_result(self.input(name, args, cancelled))


class Server:
    def __init__(self, runtime):
        self.runtime = runtime
        self.worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.lock = threading.RLock()
        self.output = threading.Lock()
        self.active = None
        self.closed = False

    def emit(self, request_id, **fields):
        with self.output:
            print(json.dumps({'jsonrpc': '2.0', 'id': request_id, **fields}, ensure_ascii=True),
                  file=PROTOCOL_OUT, flush=True)

    def error(self, request_id, code, text):
        self.emit(request_id, error={'code': code, 'message': text})

    def run_tool(self, request_id, name, args, cancelled):
        try:
            result = self.runtime.call(name, args, cancelled)
        except Exception as error:
            result = {**tool_result(str(error)), 'isError': True}
        with self.lock:
            if self.active and self.active[0] == request_id:
                self.active = None
            if not self.closed:
                self.emit(request_id, result=result)

    def receive(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or type(message.get('method')) is not str:
            self.error(None, -32600, 'Invalid request.')
            return
        method, request_id, params = message['method'], message.get('id'), message.get('params', {})
        if not isinstance(params, dict):
            self.error(request_id, -32602, 'Expected named parameters.')
            return
        if method == 'notifications/cancelled':
            with self.lock:
                if self.active and self.active[0] == params.get('requestId'):
                    self.active[1].set()
            return
        if 'id' not in message:
            return
        if method == 'initialize':
            version = params.get('protocolVersion')
            self.emit(request_id, result={
                'protocolVersion': version if version in VERSIONS else VERSIONS[0],
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'local-cua-mcp', 'version': '0.2.1'},
            })
        elif method == 'ping':
            self.emit(request_id, result={})
        elif method == 'tools/list':
            self.emit(request_id, result={'tools': [
                {'name': name, 'description': desc, 'inputSchema': schema}
                for name, (desc, schema) in TOOLS.items()
            ]})
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if type(name) is not str or name not in TOOLS:
                self.error(request_id, -32602, 'Unknown tool.')
                return
            try:
                validate(args, TOOLS[name][1])
            except ValidationError as error:
                self.emit(request_id, result={**tool_result(error.message), 'isError': True})
                return
            with self.lock:
                if self.active and name == 'turn_ended':
                    self.active[1].set()
                    self.worker.submit(self.run_tool, request_id, name, args, threading.Event())
                elif self.active:
                    self.error(request_id, -32000, 'A CUA operation is already in progress.')
                else:
                    cancelled = threading.Event()
                    self.active = request_id, cancelled
                    self.worker.submit(self.run_tool, request_id, name, args, cancelled)
        else:
            self.error(request_id, -32601, 'Method not found.')

    def run(self):
        try:
            for line in sys.stdin.buffer:
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    self.error(None, -32700, 'Parse error.')
                    continue
                self.receive(message)
        finally:
            with self.lock:
                self.closed = True
                if self.active:
                    self.active[1].set()
            self.worker.submit(self.runtime.shutdown)
            self.worker.shutdown()


if __name__ == '__main__':
    sys.stdout = sys.stderr
    parser = argparse.ArgumentParser(description='Local Computer Use MCP server')
    parser.add_argument('--node', default=shutil.which('node'), help='Path to Node.js 24 or newer')
    options = parser.parse_args()
    if not options.node:
        parser.error('Node.js was not found. Pass --node with its absolute path.')
    Server(Runtime(options.node)).run()
