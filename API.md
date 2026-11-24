# Local Computer Use API

MCP 工具 `js({code, title?, timeout_ms?})` 在持久 JavaScript 会话执行代码。可以使用顶层 `await`。等待操作完成后再结束当前调用。调用超时会清除 JS 变量。`js_reset()` 清除窗口观察、录音与自建浏览器。关闭 MCP 连接会清理本地会话。

## Windows

- `cua.getState({emit?})` 返回 `{apps, browsers, errors?}`。
- `cua.listApps({emit?})` / `cua.listWindows({emit?})` 枚举开始菜单应用和已打开窗口。
- `cua.computer.launch_app({app})` 启动应用 ID 或绝对路径指向的 EXE。
- `cua.getApp({windowId, includeAXState?})` 返回 App。默认显示初始控件树。游戏或远控画面可传 `includeAXState:false`，直接进入截图流程。窗口 ID 支持整数或数字字符串。
- `app.getAXState({emit?, disableDiffing?})` 返回带索引的 UI Automation 文本。树与前一次相同时默认返回 `(unchanged)`。用 `disableDiffing:true` 强制取完整树。
- `app.getAXSnapshot({emit?})` 返回完整 `{tree, snapshotId, truncated, ...}`。每次读取控件树会生成新 ID，包括树内容相同的情况。可在按索引点击、滚动、选中文本、设值、执行次级动作或输入文本时传 `{snapshotId}`。服务会拒绝过期 ID。原有的纯数字索引调用保留。`app.getAXStateAndScreenshot()` 也会返回 `snapshotId`，底层 `sky` 使用同名参数。
- `app.getScreenshot({emit?,maxWidth?,maxHeight?,clip?})` 返回截图数组。`maxWidth` 和 `maxHeight` 按比例限制每个区域的图片尺寸。主窗口可用 `clip:{x,y,width,height}` 裁剪，单位是缩放前截图的物理像素。区域的 `width/height` 是返回的图片尺寸，`originX/originY` 是该区域的物理屏幕原点。传入变换选项时，`sourceWidth/sourceHeight` 提供缩放前的区域尺寸。每个区域还包含 `id`、`bytes` 和 `zIndex`。`app.getAXStateAndScreenshot(options)` 接受相同的截图选项。
- `app.click(indexOrPoint, {mouseButton?,clickCount?,screenshotId?})`、`app.drag(from,to,{screenshotId?})`、`app.scroll(indexOrPoint,direction,pagesOrOptions?)`。坐标形如 `[x,y]`，相对实际返回的图片。多个区域、裁剪或缩放时必须指定最新的 `screenshotId`。窗口移动、缩放或重新观察后，需要用新截图。
- `app.inputSequence(steps)` 把多步输入放在一次调用中。要求目标窗口已在前台，步骤同 `windowsInput.input_sequence`。
- `app.typeText(text, elementIndex?)`、`app.pressKey(key)`、`app.paste(text,{format?})`。剪贴板粘贴会更新 Windows 剪贴板。支持 `text`、`md`、`html`，Markdown 以文本形式放入剪贴板。
- `app.selectText(index,text,{prefix?,suffix?,selectionType?})`、`app.setValue(index,value)`、`app.performSecondaryAction(index,action)` 使用控件支持的 UIA pattern。常见动作有 Raise、Invoke、Select、Toggle、Expand、Collapse 和 Scroll Up/Down/Left/Right。
- 原窗口 API 可通过 `sky` 或 `cua.computer` 调用。方法是 `list_windows`、`get_window`、`list_apps`、`launch_app`、`get_window_state`、`click`、`drag`、`scroll`、`press_key`、`type_text`、`set_value`、`select_text`、`perform_secondary_action`、`activate_window`、`paste`。窗口参数使用 `{window:{id,app,title}}`，可从列表取得。`get_window_state` 接受 `screenshot_options:{maxWidth?,maxHeight?,clip?}`，返回 `{window, accessibility, screenshots}`。截图数据 URL 位于各区域的 `url`。

## 浏览器

- `cua.listBrowsers({emit?})`、`cua.getBrowser({id?,url?})`、`cua.listTabs({browser?,emit?})`。本地浏览器 ID 是 `local`，`chrome`、`chromium` 和 `cdp` 可作为别名。
- `cua.createBrowserTab(browserId,url?,{visible?,sessionName?,window?})` 打开 tab。`window` 支持 `{windowId}` 或 `{newWindow,windowName}`。本地 Chromium 不使用 Codex 的 sessionName。
- `cua.getTab(id,{browser?})` 或 `cua.getTab({url},{browser})` 绑定现有 tab。URL 必须唯一匹配。
- `browser.tabs.list()`、`browser.tabs.get(id)`、`browser.tabs.new()`、`browser.listWindows()`、`browser.documentation()`。
- `tab.goto(url)`、`back()`、`forward()`、`reload()`、`close()`、`title()`、`url()`、`getAXState()`、`getScreenshot()`、`getAXStateAndScreenshot()`。
- `tab.playwright.domSnapshot()`，以及 `locator`、`frameLocator`、`getByRole`、`getByText`、`getByLabel`、`getByPlaceholder`、`getByTitle`、`getByTestId`、`getByAltText`。`Locator` 支持 `nth/first/last`、`click/dblclick`、`fill/clear`、`press/pressSequentially`、`hover`、`check/uncheck`、`selectOption`、`count`、`textContent/innerText/allTextContents`、`inputValue`、`getAttribute`、`isVisible/isEnabled/isChecked`、`waitFor`。
- `tab.playwright.waitForURL(url,{timeout?,waitUntil?})`、`waitForLoadState({state?,timeout?})`、`waitForTimeout(milliseconds)`。
- `tab.cua.click({x,y,button?,keypress?})`、`double_click({x,y,keypress?})`、`move({x,y,keys?})`、`drag({path:[{x,y},…],keys?})`、`scroll({x?,y?,scrollX?,scrollY?,keypress?})`、`keypress({keys,duration?})`、`type({text})`。时长单位是秒。浏览器 tab 的 Windows 原生控件操作会返回错误。

## windows_input_extra

这些工具也能通过全局 `windowsInput` 调用。先用 `inspect_window({window_id,observed_title?})` 获取目标 `window`。指针动作支持 `coordinates:"window"`（默认）或 `"screen"`，单位是物理像素。`coordinates:"screenshot"` 使用截图像素，并要求传入最新的 `screenshotId`。窗口焦点和几何变化会中断输入。按键和鼠标按下、抬起在同一次操作内部完成。

- `hold_key({window,key,duration_ms})`
- `click_pointer({window,point,coordinates?,screenshotId?,button?,modifiers?,duration_ms?})`
- `drag_pointer({window,start,end,coordinates?,screenshotId?,button?,modifiers?,duration_ms?})`
- `hover_pointer({window,point,coordinates?,screenshotId?,duration_ms?})`
- `drag_path({window,points,coordinates?,screenshotId?,button?,modifiers?,duration_ms?,segment_ms?})`。各点为 `{x,y}`，一次按下右、左或中键后经过全部点，默认左键和 320 ms。指定 `segment_ms` 时，每对相邻点对应一个毫秒数，可以用重复点和正时长表示停顿。选 `duration_ms` 或 `segment_ms` 其中一个。
- `move_relative({window,dx,dy,duration_ms?,steps?,key?,modifiers?})` 发出相对鼠标位移。正 `dx` 向右，正 `dy` 向下。数值单位是 Windows 相对输入计数。可选 `key:"W+A"` 在整个移动期间同时按住组合键。省略时长默认为 0，立即发出输入。系统指针速度、UU 转发和游戏灵敏度都可能改变最终视角。
- `input_sequence({window,steps})` 接受 1–64 步，计划时长合计最多 60 秒。步骤用 `action` 选择 `hold_key`、`move_relative`、`click_pointer`、`drag_path` 或 `wait`，其余参数沿用相应工具，省略每步的 `window`。`wait` 传 `duration_ms`。含截图坐标的步骤会在派发前一起验证。

```js
let {window} = await windowsInput.inspect_window({window_id: 123456});
await windowsInput.drag_path({
  window,
  points: [{x: 80, y: 100}, {x: 125, y: 135}, {x: 160, y: 100}],
  duration_ms: 650,
});
```

远控或游戏进入鼠标锁定模式后，可以先在静止画面中发一个小的水平相对位移，再观察视频更新。确认方向和幅度之后再组合移动键和视角。菜单、地图按钮和桌面仍用截图坐标。

```js
let app = await cua.getApp({windowId: 123456, includeAXState: false});
await app.inputSequence([
  {action: "move_relative", dx: 3, dy: 0},
  {action: "wait", duration_ms: 500},
]);
await app.getScreenshot({maxWidth: 1280});
```

## 音频和输出

- `cua.computer.start_audio_recording({max_duration_ms?})` 开始默认设备的 WASAPI 回环录音。
- `cua.computer.stop_audio_recording()` 返回 `{bytes,data_url,filepath}`。临时 WAV 包含 24 kHz 双声道 PCM，并在会话重置或退出时清理。
- `nodeRepl.write(...values)` 写文本工具结果。`nodeRepl.emitImage(image)` 和 `nodeRepl.emitAudio(audio)` 接受字节数组或对应的 data URL。
