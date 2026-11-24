# Local Computer Use MCP

一个在本机运行的 stdio MCP server。它保留新版 Computer Use 的 JavaScript 操作方式，并集成了 `windows_input_extra`。桌面、浏览器和 JS worker 都由本机运行。插件没有 OpenAI SDK、账号、端点或 Codex 可执行文件依赖。Agent 自己选择模型，截图等工具结果会返回给接入的 Agent。

项目维护者在 WorkBuddy AI 中通过 UU 远程试玩《原神》，确认截图观察和键鼠主流程基本可用。不同远端画面、游戏灵敏度和鼠标模式仍需单独校准相对位移。

## 安装与接入

需要 Windows 10/11 的交互桌面、Python 3.10 或更高版本以及 Node.js 24 或更高版本。

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

将 [opencode.example.json](opencode.example.json) 的 `mcp.servers.local-cua` 合并到 OpenCode V2 的配置，并按实际环境修改 Python、Node.js 和 `server.py` 路径。OpenCode V1 将该服务器条目放在 `mcp.local-cua`，移除 `codemode`。其他 MCP 客户端将相同的 Python 命令配置为本地 stdio server。

Codex 使用 `[mcp_servers.local-cua]`，WorkBuddy AI 使用 `mcpServers.local-cua`。两者均填写 Python 作为 `command`，其余参数写入 `args`。名称可自行选择。

安装时会下载 Python 包和 Chromium。安装好后，桌面工具可以离线运行，浏览器可以访问本地页面。Windows x64 可用 `python -m pip install -r requirements-capture.txt` 启用 Windows Graphics Capture。WASAPI 回环录音需要额外执行 `python -m pip install -r requirements-audio.txt`。

授权弹窗按需求省略。只把该 stdio 服务接到你信任的本地 Agent，并在正常的 Windows 用户会话中运行。它能读取屏幕、改变窗口和注入键鼠。Node.js 权限模式限制了部分 JS 能力，服务仍需由可信客户端调用。Windows 拒绝前台激活、键鼠注入或截图时，工具返回错误。

## 快速验证

```powershell
python -B smoke_test.py
python -B smoke_test.py --desktop --browser
python -B smoke_test.py --desktop-input --browser
```

第三条会创建专用窗口，对它发送中文文本、按键和多点拖动。浏览器测试只访问本机回环测试页。测试进程退出时会关闭测试窗口和自建 Chromium。

通过 MCP 的 `js` 工具，可以用以下代码列出窗口并检查其中一个。将示例中的数字替换为第一条返回的窗口 ID。

```js
await cua.listWindows();
```

```js
let app = await cua.getApp({ windowId: 123456 });
await app.getAXStateAndScreenshot({ disableDiffing: true });
```

`app` 会在下一次 `js` 调用中保留。完整工具和参数见 [API.md](API.md)。

## 兼容范围

| 原版能力 | 本地实现 |
| --- | --- |
| `cua_repl.js`、`js_reset`、`turn_ended` | 本地 stdio，支持跨调用变量、顶层 `await`、文本和图像输出以及超时重置。客户端可发送 MCP 取消通知或显式 `turn_ended`。 |
| Windows 窗口与控件 | Win32 枚举和激活，UI Automation 控件树与控件操作，按窗口捕获截图，包含可独立枚举的附属对话框。新版控件观察可将索引绑定到 `snapshotId`。UIA 或键鼠是否成功取决于目标控件和 Windows 权限。 |
| `cua` 和 `sky` | `cua.getApp`、应用和窗口列表及常用窗口操作。Windows 窗口方法可以用全局 `sky`，也可以用 `import("@oai/sky")` 加载本地兼容模块。 |
| 浏览器 | 自建 Playwright Chromium、可见或无头运行、标签页和窗口分组、导航、截图、可访问性快照、常见 locator 和坐标操作。浏览器窗口 ID 是服务管理的 context ID。 |
| 原版宿主特性 | Codex IAB、MCP Apps、Dot、既有 Chrome 扩展会话和 ChatGPT 云浏览器没有提供。浏览器档案、标记交付、移交、文件上传下载等原版扩展能力未覆盖。 |
| Windows 后台与捕获 | UIA 控件操作可直接调用。坐标输入及键盘注入使用前台窗口。安装选配的 [Windows Capture](https://github.com/NiiightmareXD/windows-capture/tree/main/windows-capture-python) 后优先用 Windows Graphics Capture，失败或无法使用时退回 PrintWindow。受保护表面、后台输入及窗口事件未做到原版等价。 |
| 音频 | 可选 WASAPI 默认回环设备，输出 24 kHz 双声道 WAV，需要对应驱动和额外依赖。 |
| Windows 输入扩展 | 支持截图像素映射、相对鼠标位移、方向键组合长按、定时多点拖动和单次调用内的输入序列。裁剪或缩放后使用对应的截图 ID。 |

远控画面和游戏可用 `cua.getApp({windowId, includeAXState:false})` 直接进入截图流程。菜单用截图坐标，鼠标锁定后的视角可用 `move_relative` 从小幅位移开始校准。Windows 接受输入只表示已派发，UU 和游戏的实际效果以新截图为准。 [Windows 鼠标输入文档](https://learn.microsoft.com/windows/win32/api/winuser/ns-winuser-mouseinput)

## 实现依据

兼容接口参考了新版 Codex Computer Use 的公开与本地观察。运行时使用本地实现，仓库不包含 Codex 原版组件。Windows 输入后端延续了本项目早期的 Windows Input Extra 实现。控件快照绑定参考了 [DeepSeek Harness CUA](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/experimental/cua) 的观察设计。

## 许可证

[MIT](LICENSE)
