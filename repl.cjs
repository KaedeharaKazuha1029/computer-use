"use strict";

const repl = require("node:repl");
const readline = require("node:readline");
const { PassThrough, Writable } = require("node:stream");
const { inspect } = require("node:util");
const { AsyncLocalStorage } = require("node:async_hooks");
const { registerHooks } = require("node:module");

const stdout = process.stdout;
const input = new PassThrough();
const incoming = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Map();
let nextId = 0;
let output = [];
const evaluations = new AsyncLocalStorage();
let activeEvaluation;
const engine = repl.start({
  input,
  output: new Writable({ write(_data, _encoding, done) { done(); } }),
  terminal: false,
  prompt: "",
  useGlobal: false,
  breakEvalOnSigint: false,
  ignoreUndefined: true,
});
delete engine.context.require;
delete engine.context.module;
delete engine.context.exports;
delete engine.context.process;
delete engine.context.Buffer;

function send(value) {
  stdout.write(JSON.stringify(value) + "\n");
}

function rpc(provider, method, args = {}) {
  const evaluation = evaluations.getStore();
  if (evaluation === undefined || evaluation !== activeEvaluation) {
    return Promise.reject(Error("Use await to finish Computer Use calls within the active evaluation."));
  }
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ type: "rpc", id, evaluation, provider, method, args });
  });
}

function write(...values) {
  const text = values.map(value => typeof value === "string" ? value : inspect(value, { depth: 5 })).join(" ");
  output.push({ type: "text", text: text.slice(0, 100_000) });
}

function emitMedia(data, type, defaultMime) {
  if (data?.data_url) data = data.data_url;
  if (data?.bytes) data = data.bytes;
  if (ArrayBuffer.isView(data)) {
    output.push({ type, mimeType: defaultMime, data: Buffer.from(data).toString("base64") });
    return;
  }
  const match = /^data:([^;,]+);base64,([a-z0-9+/=]+)$/i.exec(data);
  if (!match || !match[1].startsWith(type + "/")) throw Error("Expected a base64 media data URL or byte array.");
  output.push({ type, mimeType: match[1], data: match[2] });
}

const emitImage = data => emitMedia(data, "image", "image/png");
const emitAudio = data => emitMedia(data, "audio", "audio/wav");
const bytes = url => new Uint8Array(Buffer.from(url.split(",", 2)[1], "base64"));
const expose = (value, options) => {
  if (options?.emit !== false) write(value);
  return value;
};

const sky = { target: "windows" };
for (const method of [
  "list_windows", "list_apps", "get_window", "launch_app", "get_window_state",
  "click", "drag", "scroll", "press_key", "type_text", "set_value", "select_text",
  "perform_secondary_action", "activate_window", "paste",
]) {
  sky[method] = args => rpc("desktop", method, args);
}
sky.start_audio_recording = args => rpc("audio", "start", args);
sky.stop_audio_recording = async () => {
  const result = await rpc("audio", "stop");
  return { ...result, bytes: bytes(result.data_url) };
};
const skyModule = "local-cua:sky";
globalThis[Symbol.for(skyModule)] = sky;
registerHooks({
  resolve(specifier, context, next) {
    return specifier === "@oai/sky" ? { url: skyModule, shortCircuit: true } : next(specifier, context);
  },
  load(url, context, next) {
    if (url !== skyModule) return next(url, context);
    return {
      format: "module",
      source: "export const sky = globalThis[Symbol.for('local-cua:sky')];",
      shortCircuit: true,
    };
  },
});

function bindApp(window) {
  function coordinates(target) {
    return Array.isArray(target) ? { x: target[0], y: target[1] } : { element_index: target };
  }
  function screenshots(regions, options) {
    return regions.map(({ url, ...region }) => {
      if (options?.emit !== false) {
        if (regions.length > 1 || region.sourceWidth !== undefined) write(JSON.stringify(region));
        emitImage(url);
      }
      return { ...region, bytes: bytes(url) };
    });
  }
  async function observe(options, include_text, include_screenshot) {
    const { maxWidth, maxHeight, clip } = options ?? {};
    const screenshot_options = include_screenshot && (maxWidth !== undefined || maxHeight !== undefined || clip !== undefined)
      ? { maxWidth, maxHeight, clip } : undefined;
    const state = await sky.get_window_state({
      window, include_text, include_screenshot, screenshot_options, diff: options?.disableDiffing !== true,
    });
    return {
      state: include_text ? expose(state.accessibility.tree, options) : "",
      ...(include_text ? { snapshotId: state.accessibility.snapshotId } : {}),
      screenshots: screenshots(state.screenshots, options),
    };
  }
  return {
    async getAXState(options) { return (await observe(options, true, false)).state; },
    async getAXSnapshot(options) {
      const reply = await sky.get_window_state({ window, include_text: true, include_screenshot: false });
      return expose(reply.accessibility, options);
    },
    async getScreenshot(options) { return (await observe(options, false, true)).screenshots; },
    getAXStateAndScreenshot(options) { return observe(options, true, true); },
    click(target, options = {}) {
      if ("key" in options || "durationMs" in options) throw Error("Use windowsInput.click_pointer for held Windows clicks.");
      return sky.click({ window, ...coordinates(target), mouse_button: options.mouseButton, click_count: options.clickCount, screenshotId: options.screenshotId, snapshotId: options.snapshotId });
    },
    drag(from, to, options = {}) {
      return sky.drag({ window, from_x: from[0], from_y: from[1], to_x: to[0], to_y: to[1], screenshotId: options.screenshotId });
    },
    async inputSequence(steps) {
      const target = await windowsInput.inspect_window({ window_id: window.id });
      return windowsInput.input_sequence({ window: target.window, steps });
    },
    scroll(target, direction, distance = 1) {
      const sign = { up: -1, u: -1, down: 1, d: 1, left: -1, l: -1, right: 1, r: 1 }[direction];
      if (sign === undefined) throw Error("Expected a scroll direction.");
      const delta = sign * (typeof distance === "number" ? 120 * distance : distance.pixels);
      const horizontal = ["left", "l", "right", "r"].includes(direction);
      return sky.scroll({ window, ...coordinates(target), scrollX: horizontal ? delta : 0, scrollY: horizontal ? 0 : delta, screenshotId: distance.screenshotId, snapshotId: distance.snapshotId });
    },
    selectText(elementIndex, text, options) { return sky.select_text({ window, element_index: elementIndex, text, options, snapshotId: options?.snapshotId }); },
    setValue(elementIndex, value, options) { return sky.set_value({ window, element_index: elementIndex, value, snapshotId: options?.snapshotId }); },
    performSecondaryAction(elementIndex, action, options) { return sky.perform_secondary_action({ window, element_index: elementIndex, action, snapshotId: options?.snapshotId }); },
    paste(text, options) { return sky.paste({ window, text, options }); },
    pressKey(key, options = {}) {
      if ("durationMs" in options) throw Error("Use windowsInput.hold_key for held Windows keys.");
      return sky.press_key({ window, key });
    },
    typeText(text, elementIndex, options) { return sky.type_text({ window, text, element_index: elementIndex, snapshotId: options?.snapshotId }); },
  };
}

const browserRpc = (method, args) => rpc("browser", method, args);
const locatorNames = {
  locator: "locator", getByRole: "get_by_role", getByText: "get_by_text", getByLabel: "get_by_label",
  getByPlaceholder: "get_by_placeholder", getByTestId: "get_by_test_id", getByTitle: "get_by_title",
  getByAltText: "get_by_alt_text", nth: "nth", frameLocator: "frame_locator",
};
const locatorActions = {
  click: "click", dblclick: "dblclick", fill: "fill", press: "press", pressSequentially: "press_sequentially",
  hover: "hover", check: "check", uncheck: "uncheck", selectOption: "select_option", clear: "clear",
  count: "count", textContent: "text_content", innerText: "inner_text", inputValue: "input_value",
  getAttribute: "get_attribute", isVisible: "is_visible", isEnabled: "is_enabled", isChecked: "is_checked",
  waitFor: "wait_for", allTextContents: "all_text_contents",
};
const namedActions = new Set(["fill", "press", "pressSequentially", "selectOption", "getAttribute"]);
function selectorValue(value) {
  if (Object.prototype.toString.call(value) === "[object RegExp]") {
    return { __regexp: value.source, flags: value.flags };
  }
  return value;
}
function optionsForPython(options) {
  return Object.fromEntries(Object.entries(options ?? {}).map(([key, value]) => [
    key.replace(/[A-Z]/g, char => "_" + char.toLowerCase()), selectorValue(value),
  ]));
}
function locators(tab, chain = []) {
  const result = {};
  for (const [name, method] of Object.entries(locatorNames)) {
    result[name] = (value, options) => locators(tab, [...chain, [method, [selectorValue(value)], optionsForPython(options)]]);
  }
  if (chain.length) {
    result.first = () => result.nth(0);
    result.last = () => result.nth(-1);
    for (const [name, method] of Object.entries(locatorActions)) {
      result[name] = (...args) => {
        const values = namedActions.has(name) ? args.splice(0, 1) : [];
        return tab("locator", chain, method, values, optionsForPython(args[0]));
      };
    }
  }
  return result;
}

function bindTab(info) {
  const tab = (method, ...args) => browserRpc(method, { id: info.id, args });
  const coordinate = (method, options) => tab("coordinate", method, options);
  const nativeInput = () => { throw Error("Use tab.playwright locators or tab.cua for this DOM-capable browser."); };
  const result = {
    id: info.id,
    windowId: info.windowId,
    async getAXState(options) { return expose(await tab("snapshot"), options); },
    async getScreenshot(options) {
      const url = await tab("screenshot", optionsForPython({ fullPage: options?.fullPage ?? false, clip: options?.clip }));
      if (options?.emit !== false) emitImage(url);
      return bytes(url);
    },
    async getAXStateAndScreenshot(options) {
      return { state: await result.getAXState(options), screenshot: await result.getScreenshot(options) };
    },
    click: nativeInput, drag: nativeInput, scroll: nativeInput, selectText: nativeInput,
    setValue: nativeInput, performSecondaryAction: nativeInput, paste: nativeInput,
    pressKey: nativeInput, typeText: nativeInput,
    goto: url => tab("goto", url),
    back: () => tab("back"),
    forward: () => tab("forward"),
    reload: () => tab("reload"),
    close: () => tab("close"),
    title: () => tab("title"),
    url: () => tab("url"),
    screenshot: options => result.getScreenshot({ ...options, emit: false }),
    cua: Object.fromEntries(["click", "double_click", "drag", "move", "scroll", "keypress", "type"].map(method => [
      method, options => coordinate(method, options),
    ])),
    playwright: {
      ...locators(tab), domSnapshot: () => tab("snapshot"),
      waitForURL: (url, options) => tab("wait_for_url", selectorValue(url), optionsForPython(options)),
      waitForLoadState: options => tab("wait_for_load_state", optionsForPython(options)),
      waitForTimeout: milliseconds => tab("wait_for_timeout", milliseconds),
    },
  };
  return result;
}

function bindBrowser({ browserId }) {
  return {
    browserId,
    documentation: () => rpc("local", "documentation"),
    listWindows: () => browserRpc("list_windows"),
    tabs: {
      list: () => browserRpc("list_tabs", { browser: browserId }),
      get: async id => bindTab(await browserRpc("get_tab", { reference: id, options: { browser: browserId } })),
      new: async () => bindTab(await browserRpc("create_tab", { browser: browserId })),
    },
  };
}

const cua = {
  computer: sky,
  async listWindows(options) { return expose(await sky.list_windows(), options); },
  async listApps(options) { return expose(await sky.list_apps(), options); },
  async listBrowsers(options) { return expose(await browserRpc("list"), options); },
  async listTabs(options = {}) { return expose(await browserRpc("list_tabs", options), options); },
  async getState(options) {
    const errors = [];
    let apps = [];
    let browsers = [];
    try { apps = await sky.list_apps(); } catch (error) { errors.push(error.message); }
    try {
      const listed = await cua.listBrowsers({ emit: false });
      browsers = await Promise.all(listed.map(async browser => ({
        ...browser, tabs: await cua.listTabs({ browser: browser.id, emit: false }),
      })));
    } catch (error) { errors.push(error.message); }
    return expose({ apps, browsers, ...(errors.length ? { errors } : {}) }, options);
  },
  async getApp({ windowId, includeAXState = true }) {
    const window = await sky.get_window({ id: windowId });
    const app = bindApp(window);
    if (includeAXState) await app.getAXState({ disableDiffing: true });
    return app;
  },
  async getBrowser(options = {}) { return bindBrowser(await browserRpc("get", options)); },
  async createBrowserTab(browser, url, options = {}) {
    const tab = bindTab(await browserRpc("create_tab", { browser, url, options }));
    await tab.getAXState();
    return tab;
  },
  async getTab(reference, options = {}) {
    const tab = bindTab(await browserRpc("get_tab", { reference, options }));
    await tab.getAXState();
    return tab;
  },
  async rewriteDocumentation() { return expose(await rpc("local", "documentation")); },
};
cua.initialize = cua.getState;

const windowsInput = Object.fromEntries([
  "inspect_window", "hold_key", "click_pointer", "drag_pointer", "hover_pointer", "drag_path",
  "move_relative", "input_sequence",
].map(method => [method, args => rpc("input", method, args)]));

const agent = { browsers: {
  list: () => cua.listBrowsers({ emit: false }),
  get: id => cua.getBrowser({ id }),
} };
Object.assign(engine.context, {
  cua, sky, agent, windowsInput,
  nodeRepl: Object.freeze({ write, emitImage, emitAudio }),
  console: Object.freeze({ log: write, info: write, warn: write, error: write }),
});

incoming.on("line", line => {
  const message = JSON.parse(line);
  if (message.type === "rpc_result") {
    const handler = pending.get(message.id);
    if (!handler) return;
    pending.delete(message.id);
    if ("error" in message) handler.reject(Error(message.error));
    else handler.resolve(message.result);
    return;
  }
  if (message.type !== "eval") return;
  activeEvaluation = message.evaluation;
  output = [];
  const previousCallbacks = engine._domain.listeners("error");
  let finished = false;
  function finish(error, value) {
    if (finished) return;
    finished = true;
    engine._domain.removeListener("error", finish);
    for (const callback of previousCallbacks) engine._domain.on("error", callback);
    if (error) write(error.message || String(error));
    else if (value !== undefined && !output.length && typeof value !== "function") write(value);
    activeEvaluation = undefined;
    send({ type: "result", evaluation: message.evaluation, content: output, isError: Boolean(error) });
  }
  engine._domain.removeAllListeners("error");
  engine._domain.on("error", finish);
  try {
    evaluations.run(message.evaluation, () => engine.eval(message.code, engine.context, "local-cua", finish));
  } catch (error) {
    finish(error);
  }
});
