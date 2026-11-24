from contextlib import contextmanager
import re
from urllib.parse import urlsplit

from desktop import image_url
from windows_input import InputError


def locator_value(value):
    if isinstance(value, dict) and '__regexp' in value:
        mask = sum(flag for char, flag in (('i', re.I), ('m', re.M), ('s', re.S)) if char in value['flags'])
        return re.compile(value['__regexp'], mask)
    return value


class BrowserBackend:
    """Playwright browsers owned by the local MCP process."""

    def __init__(self):
        self.engine = None
        self.browsers = {}
        self.contexts = {}
        self.windows = {}
        self.pages = {}
        self.page_id = 0
        self.window_id = 0

    def close(self):
        for browser in list(self.browsers.values()):
            try:
                browser.close()
            except Exception:
                pass
        if self.engine:
            self.engine.stop()
        self.__init__()

    def inventory(self):
        return [{'id': 'local', 'name': 'Local Chromium', 'family': 'chromium', 'type': 'cdp'}]

    def browser_id(self, value):
        if value not in (None, 'local', 'chrome', 'chromium', 'cdp'):
            raise InputError(f'Browser provider {value!r} is unavailable. Use Local Chromium.')
        return 'local'

    def context(self, visible, new=False):
        if self.engine is None:
            from playwright.sync_api import sync_playwright
            self.engine = sync_playwright().start()
        browser = self.browsers.get(visible)
        if browser is None or not browser.is_connected():
            browser = self.engine.chromium.launch(headless=not visible, timeout=15000)
            self.browsers[visible] = browser
        if new or visible not in self.contexts or self.contexts[visible][1] not in browser.contexts:
            self.window_id += 1
            entry = (str(self.window_id), browser.new_context())
            self.contexts[visible] = entry
            self.windows[entry[0]] = (visible, entry[1], None)
        return self.contexts[visible]

    def tabs(self, browser=None):
        self.browser_id(browser)
        return [self.info(key, page) for key, (_, page) in self.pages.items() if not page.is_closed()]

    def info(self, key, page):
        return {'id': key, 'providerTabId': key, 'browserId': 'local', 'title': page.title(),
                'url': page.url, 'windowId': self.pages[key][0]}

    def page(self, tab_id):
        entry = self.pages.get(str(tab_id))
        if entry is None or entry[1].is_closed():
            raise InputError('The requested tab is closed or unknown.')
        return entry[1]

    def navigate(self, page, url):
        if url != 'about:blank' and urlsplit(url).scheme not in ('http', 'https'):
            raise InputError('Local Chromium accepts HTTP(S) URLs and about:blank.')
        page.goto(url, wait_until='domcontentloaded', timeout=12000)

    def create(self, browser, url, options):
        self.browser_id(browser)
        if options.get('sessionName') is not None and not isinstance(options['sessionName'], str):
            raise InputError('sessionName must be a string.')
        window = options.get('window', {})
        if 'windowId' in window:
            key = str(window['windowId'])
            target = self.windows.get(key)
            if target is None:
                raise InputError('Browser window is unknown.')
            visible, context, _ = target
            if 'visible' in options and options['visible'] != visible:
                raise InputError('An existing browser window cannot change visibility.')
        else:
            key, context = self.context(options.get('visible', True), window.get('newWindow', False))
            if window.get('windowName'):
                visible, _, _ = self.windows[key]
                self.windows[key] = (visible, context, window['windowName'])
        page = context.new_page()
        page.set_default_timeout(6000)
        self.page_id += 1
        tab_id = str(self.page_id)
        self.pages[tab_id] = (key, page)
        page.on('close', lambda: self.pages.pop(tab_id, None))
        try:
            if url:
                self.navigate(page, url)
            return self.info(tab_id, page)
        except Exception:
            page.close()
            raise

    def resolve(self, reference, options):
        self.browser_id(options.get('browser'))
        if isinstance(reference, str):
            return self.info(reference, self.page(reference))
        if 'mention' in reference:
            raise InputError('Codex tab mentions are unavailable in Local Chromium.')
        if 'url' not in reference or not options.get('browser'):
            raise InputError('URL references require an explicit browser and an exact URL.')
        matches = [tab for tab in self.tabs() if tab['url'] == reference['url']]
        if len(matches) != 1:
            raise InputError('No unique tab has the requested URL.')
        return matches[0]

    @contextmanager
    def keys(self, keyboard, keys):
        held = []
        aliases = {'ctrl': 'Control', 'control': 'Control', 'shift': 'Shift', 'alt': 'Alt',
                   'meta': 'Meta', 'cmd': 'Meta', 'esc': 'Escape', 'return': 'Enter'}
        try:
            for key in keys:
                key = aliases.get(key.lower(), key)
                keyboard.down(key)
                held.append(key)
            yield
        finally:
            for key in reversed(held):
                keyboard.up(key)

    def tab_action(self, page, method, args, cancelled):
        if method == 'goto':
            return self.navigate(page, args[0])
        if method in ('back', 'forward', 'reload'):
            name = {'back': 'go_back', 'forward': 'go_forward'}.get(method, method)
            getattr(page, name)(wait_until='domcontentloaded', timeout=12000)
        elif method == 'close':
            page.close()
        elif method == 'title':
            return page.title()
        elif method == 'url':
            return page.url
        elif method == 'snapshot':
            return page.locator('body').aria_snapshot(timeout=6000)
        elif method == 'screenshot':
            return image_url(page.screenshot(**args[0], timeout=6000))
        elif method == 'wait_for_url':
            return page.wait_for_url(locator_value(args[0]), **args[1])
        elif method == 'wait_for_load_state':
            return page.wait_for_load_state(**args[0])
        elif method == 'wait_for_timeout':
            duration = args[0]
            if type(duration) not in (int, float) or not 0 <= duration <= 30_000:
                raise InputError('Browser waits must be between 0 and 30000 ms.')
            if cancelled.wait(duration / 1000):
                raise InputError('Browser wait cancelled.')
        elif method == 'coordinate':
            action, options = args
            keys = options.get('keys', options.get('keypress', []))
            if action == 'keypress' and not keys and 'key' in options:
                keys = options['key'].split('+')
            with self.keys(page.keyboard, keys):
                self.pointer(page, action, options, cancelled)
        elif method == 'locator':
            return self.locator_action(page, *args)
        else:
            raise InputError(f'Browser method {method} is unavailable.')

    def pointer(self, page, action, options, cancelled):
        if action in ('click', 'double_click'):
            button = options.get('button', 'left')
            button = {1: 'left', 2: 'middle', 3: 'right'}.get(button, button)
            if button not in ('left', 'middle', 'right'):
                raise InputError('Local Chromium supports left, middle and right mouse buttons.')
            clicks = 2 if action == 'double_click' else options.get('clickCount', 1)
            page.mouse.click(options['x'], options['y'], button=button, click_count=clicks)
        elif action == 'move':
            page.mouse.move(options['x'], options['y'])
        elif action == 'drag':
            points = options['path']
            if not 2 <= len(points) <= 1024:
                raise InputError('Provide between 2 and 1024 path points.')
            page.mouse.move(points[0]['x'], points[0]['y'])
            page.mouse.down()
            try:
                for point in points[1:]:
                    if cancelled.is_set():
                        raise InputError('Browser drag cancelled.')
                    page.mouse.move(point['x'], point['y'], steps=4)
            finally:
                page.mouse.up()
        elif action == 'scroll':
            if 'x' in options:
                page.mouse.move(options['x'], options['y'])
            page.mouse.wheel(options.get('scrollX', options.get('deltaX', 0)), options.get('scrollY', options.get('deltaY', 0)))
        elif action == 'keypress':
            duration = options.get('duration', 0)
            if type(duration) not in (float, int) or not 0 <= duration <= 30:
                raise InputError('Key hold duration must be between 0 and 30 seconds.')
            if cancelled.wait(duration):
                raise InputError('Browser key hold cancelled.')
        elif action == 'type':
            page.keyboard.insert_text(options['text'])
        else:
            raise InputError(f'Unknown browser coordinate action: {action}')

    def locator_action(self, page, chain, method, args, options):
        selectors = {'locator', 'get_by_role', 'get_by_text', 'get_by_label', 'get_by_placeholder',
                     'get_by_test_id', 'get_by_title', 'get_by_alt_text', 'nth', 'frame_locator'}
        actions = {'click', 'dblclick', 'fill', 'press', 'press_sequentially', 'hover', 'check', 'uncheck',
                   'select_option', 'clear', 'count', 'text_content', 'inner_text', 'input_value',
                   'get_attribute', 'is_visible', 'is_enabled', 'is_checked', 'wait_for', 'all_text_contents'}
        locator = page
        for command, values, keywords in chain:
            if command not in selectors:
                raise InputError(f'Unsupported locator: {command}')
            locator = getattr(locator, command)(*(locator_value(value) for value in values),
                                                **{key: locator_value(value) for key, value in keywords.items()})
        if not chain or method not in actions:
            raise InputError(f'Unsupported locator action: {method}')
        return getattr(locator, method)(*args, **options)

    def call(self, method, args, cancelled):
        if method == 'list':
            return self.inventory()
        if method == 'list_tabs':
            return self.tabs(args.get('browser'))
        if method == 'list_windows':
            tabs = self.tabs()
            return [{'id': key, 'windowId': key, 'name': name, 'tabs': [tab for tab in tabs if tab['windowId'] == key]}
                    for key, (_, context, name) in self.windows.items() if context.browser and context.browser.is_connected()]
        if method == 'get':
            if args.get('extensionInstanceId'):
                raise InputError('Local Chromium has no Codex extension identity.')
            browser_id = self.browser_id(args.get('id'))
            if args.get('url') and not any(tab['url'] == args['url'] for tab in self.tabs(browser_id)):
                raise InputError('No local browser has a tab with that exact URL.')
            return {'browserId': browser_id}
        if method == 'create_tab':
            return self.create(args['browser'], args.get('url'), args.get('options', {}))
        if method == 'get_tab':
            return self.resolve(args['reference'], args.get('options', {}))
        return self.tab_action(self.page(args['id']), method, args.get('args', []), cancelled)
