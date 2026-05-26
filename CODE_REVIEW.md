# CODE_REVIEW.md

本报告合并两轮审查：第一轮架构层审查，以及本轮对指定源码文件的实现层审查。用户清单中的 `learning/init.py` 与 `computer_server/init.py` 在仓库中对应实际包文件 `computer_server/learning/__init__.py` 与 `computer_server/__init__.py`。

## P0 - 致命问题

- [√] `computer_server/server.py:38`、`computer_server/main.py:54`、`computer_server/main.py:117`、`computer_server/main.py:228`、`computer_server/mcp_server.py:1420`、`computer_server/handlers/windows.py:732` — HTTP/WebSocket 默认对外暴露高危本机能力 | 现状：HTTP server 与直接运行 `main.py` 默认绑定 `127.0.0.1`，CORS 只允许 localhost origin，legacy `/ws` 与 `/commands` API 已删除；HTTP 仅保留 `/status` 与 `/mcp` MCP transport，文件工具已有 root/危险操作限制 | 风险：默认局域网暴露和 legacy WebSocket 命令入口已移除；MCP 工具仍是高权限桌面控制能力，因此非 localhost 绑定仍需 CLI 显式确认 | 修复方案：保留本机 `/mcp`，删除 legacy `/ws`，收紧 CORS，README/CLI 标注 HTTP MCP 本机边界。

- [√] `computer_server/mcp_server.py:824`、`computer_server/mcp_server.py:997`、`computer_server/mcp_server.py:1100` — window-relative resized 坐标反算公式错误 | 现状：pixel click/drag/scroll 在提供 `pid + window_id` 时使用 `int((origin_x + x) * ratio)`，把窗口 screen origin 也乘以 resize ratio | 风险：窗口不在 `(0,0)` 且截图发生缩放时，点击、拖拽、滚动会偏移；高 DPI 或大窗口下可能点到错误应用/错误按钮，replay 也会放大偏差 | 修复方案：改为 `screen_x = origin_x + int(x * ratio)`、`screen_y = origin_y + int(y * ratio)`；drag/scroll 同步修正；为窗口 `(0,0)`、`(200,100)`、负坐标、resize 触发、多显示器分别加坐标单测。

- [√] `computer_server/handlers/windows_uia.py:147`、`computer_server/handlers/windows_uia.py:150`、`computer_server/mcp_server.py:1955`、`computer_server/mcp_server.py:1990` — UIA COM timeout 不能真正取消底层调用，可能泄漏线程并耗尽线程池 | 现状：每个 UIA property/pattern 调用提交到全局 `_com_executor`，`future.result(timeout=3)` 超时后只返回默认值，不取消正在阻塞的 COM 调用；外层 `wait_for(..., timeout=30)` 也不能杀死 `run_in_executor` 中的 `walk_uia_tree` | 风险：目标窗口 UIA provider 挂死时，后台 COM 调用继续占用线程；连续 `get_window_state` 会累积悬挂线程，最终导致 UIA、SOM、recording hook 或其他 executor 任务饥饿/卡死 | 修复方案：为 `(pid, window_id)` 加 in-flight gate；不要为每个属性再嵌套全局线程池，可在单独 STA worker 进程/线程中整体执行并可丢弃；超时后标记窗口 provider unhealthy 并短期熔断；限制 `_com_executor` 队列并记录 active futures。

- [√] `computer_server/overlay/cursor.py:118`、`computer_server/overlay/cursor.py:127`、`computer_server/overlay/cursor.py:311`、`computer_server/overlay/cursor.py:379` — Overlay 窗口跨线程创建/销毁，Win32 消息泵缺失 | 现状：`_create_window()` 在构造线程同步执行，但 `_animate_loop()` 注释称动画线程 owns the window，并在线程结束时 `DestroyWindow`；没有标准 GetMessage/DispatchMessage 消息泵 | 风险：Win32 HWND 与 GDI 对象线程亲和性不清，销毁非创建线程窗口可能不稳定；窗口消息不泵会导致 layered window、WM_DESTROY、系统资源释放边界行为不可预期 | 修复方案：让 overlay 专用线程同时创建窗口、初始化 GDI、运行消息泵和动画；主线程只通过线程安全队列发命令；销毁也在同一线程执行。

## P1 - 严重问题

- [√] `computer_server/mcp_server.py:86`、`computer_server/mcp_server.py:1933`、`computer_server/mcp_server.py:2176`、`computer_server/handlers/zoom_registry.py:49` — zoom metadata 按 pid 存储，且裸全局 dict 无锁 | 现状：`_zoom_metadata[pid]` 和 `ZoomRegistry.set(pid, ctx)` 只按进程保存最后一次窗口截图/zoom 上下文 | 风险：Chrome/Edge/VS Code 等多窗口同 pid 场景互相覆盖 zoom context，`from_zoom=True` 可能映射到另一个窗口；HTTP 并发调用时还存在竞态 | 修复方案：统一改为 `(pid, window_id)` key；`computer_zoom` 参数加入 `window_id`；把 `_zoom_metadata` 合并到 `ZoomRegistry` 并加同一把锁或 immutable snapshot。

- [√] `computer_server/mcp_server.py:1905`、`computer_server/mcp_server.py:2074`、`computer_server/mcp_server.py:2213` — 多显示器负坐标/裁剪坐标缺少完整 virtual screen 建模 | 现状：窗口负坐标时 crop 被 clamp 到截图图像范围，但保存的 `win_origin_x/y` 仍是真实窗口 origin；SOM 坐标按 cropped image 直接乘 ratio append 到 cache | 风险：窗口部分位于负坐标显示器或屏幕外时，vision element 与 UIA element 的 window-relative 坐标系不一致，点击 vision index 会偏移 | 修复方案：引入 `WindowSnapshotGeometry`，显式保存 `virtual_origin`、`crop_origin_screen`、`crop_origin_window`、`resize_ratio`；SOM 坐标先从 cropped-resized 转 cropped-full，再加 `crop_origin_window`。

- [√] `computer_server/handlers/windows.py:683`、`computer_server/handlers/windows.py:657`、`computer_server/handlers/generic.py:89` — screen size、screenshot 与 monitor geometry 坐标系不一致 | 现状：`get_screen_size` 用 `SM_CXSCREEN/SM_CYSCREEN`，只代表 primary monitor；`ImageGrab.grab()` 与 `screeninfo.get_monitors()` 可能代表 virtual desktop 或 primary，不统一 | 风险：多屏、负坐标、副屏 DPI 下 `_scale_to_actual`、截图裁剪、click 坐标不在同一空间 | 修复方案：启动时设置 per-monitor DPI awareness；新增 `VirtualScreenGeometry`，使用 `SM_XVIRTUALSCREEN/SM_YVIRTUALSCREEN/SM_CXVIRTUALSCREEN/SM_CYVIRTUALSCREEN`；所有 screenshot metadata 带 origin。

- [√] `computer_server/handlers/windows_uia.py:615`、`computer_server/handlers/windows_uia.py:628` — COM 初始化没有对应释放，thread-local UIA 对象无限保留 | 现状：每次 `walk_uia_tree` 调 `CoInitializeEx`，但没有 `CoUninitialize`；`get_uia_instance()` 将 UIA object 存在线程本地 | 风险：默认线程池线程复用时可工作，但线程退出/异常路径资源释放不明确；长期运行 HTTP server 下 COM apartment 与 UIA object 生命周期不可控 | 修复方案：使用固定 STA UIA worker 线程，在线程启动时初始化 COM，退出时 CoUninitialize；或在明确拥有线程生命周期的 executor 中管理 COM。

- [√] `computer_server/mcp_server.py:1985`、`computer_server/handlers/element_cache.py:24`、`computer_server/mcp_server.py:748`、`computer_server/mcp_server.py:1335` — ElementCache 缓存 COM `platform_ref`，跨调用/跨线程复用 | 现状：UIA element COM object 直接存入 `CachedElement.platform_ref`，后续 click/set_value 直接调用 `GetCurrentPattern` | 风险：COM object 可能过期、跨 apartment 使用不安全、窗口重绘后引用 stale；失败会 fallback 到 pixel click，但 set_value 行为不稳定 | 修复方案：cache 只保存 RuntimeId/bbox/name/control_type；执行 invoke/set_value 前重新按 RuntimeId 或 point resolve 当前 UIA element；cache 加 TTL 和 HWND validity check。

- [√] `computer_server/handlers/element_cache.py:137`、`computer_server/mcp_server.py:363`、`computer_server/mcp_server.py:928` — 缺失 window origin 时静默回退 `(0,0)` | 现状：`get_window_origin()` 找不到时返回 `(0,0)`，`_build_window_ref()` 因此可能把未初始化 cache 当成有效 window_ref | 风险：未先调用 `get_window_state` 或 cache 被清理时，坐标转换静默使用错误 origin，replay 记录错误基础数据 | 修复方案：`get_window_origin` 返回 Optional 或抛出明确错误；调用方区分“无 origin”和“origin 正好为 0”；未命中时强制 `GetWindowRect`。

- [√] `computer_server/mcp_server.py:2499`、`computer_server/mcp_server.py:2516`、`computer_server/mcp_server.py:2519` — replay 只对 activate_window 做 stale HWND resolution | 现状：coordinate tools 只有在原 `window_ref.window_id` 的 `GetWindowRect` 成功时修坐标；stale HWND 搜索只用于 `computer_activate_window` | 风险：窗口重开后 click/drag/scroll 继续使用旧绝对坐标或不修正，可能操作错误窗口 | 修复方案：`window_ref` 存 pid/process_name/title/class/rect；对 click/drag/scroll 也先 resolve 当前窗口；找不到则停止 replay 并返回需要人工确认的错误。

- [√] `computer_server/recording/session.py:109`、`computer_server/recording/session.py:134`、`computer_server/mcp_server.py:336` — recording hook 每个动作同步截图并写盘，可能拖慢/阻塞动作流 | 现状：每个 recordable tool 后截图，再 `record_turn` 通过 default executor 写 JSON/PNG/click image | 风险：连续操作时 screenshot + PNG + executor 写盘成为明显 tail latency；default executor 也和其他 `run_in_executor` 任务竞争 | 修复方案：为 recording 建独立 bounded writer queue/thread；截图可按配置采样或异步；写盘失败要计数并暴露状态。

- [√] `computer_server/learning/recorder.py:229`、`computer_server/learning/recorder.py:263`、`computer_server/learning/recorder.py:383` — learning 中部分 UIA 调用没有统一 timeout gate | 现状：URL 读取走 `_run_uia_with_timeout`，但 `is_password_field_focused()`、`focused_element_context()`、`read_focused_value()` 直接在 hook/timer 线程调用 | 风险：某些 UIA provider 卡住时，pynput callback 或 flush timer 线程被阻塞，录制事件丢失或 stop 卡顿 | 修复方案：所有 UIA 调用统一走 timeout/gate；或使用 learning 专属 STA worker；超时后对该进程短期禁用 UIA enrichment。

- [√] `computer_server/learning/recorder.py:228`、`computer_server/learning/recorder.py:334`、`computer_server/learning/recorder.py:404`、`computer_server/learning/recorder.py:669` — 脱敏顺序不完整，OCR 区域可能保存敏感内容 | 现状：键盘事件会检查 password field，但 mouse click 仍可能对无 UIA label 区域截图 OCR；typeText ASCII fallback 会保存 active window 截图；保存 session 时把 `_ocr_region` 写入 screenshots | 风险：密码框、token 输入框或隐私窗口的局部/全窗口截图可能落盘，绕过文本脱敏 | 修复方案：click/typeText 截图前也检查 focused/point element 是否 password/sensitive；restricted process 和 sensitive window 下禁止 OCR 截图落盘；默认不保存 OCR image，需显式开启 debug。

- [√] `computer_server/learning/platform.py:781`、`computer_server/learning/platform.py:823` — learning 截图只用 primary monitor 或未验证负坐标 region | 现状：`capture_full()` 取 `sct.monitors[1]`，`capture_region()` 直接用 `{left:x, top:y}` | 风险：副屏或负坐标点击无法 OCR，或截取错误区域；中文 IME fallback 在多屏下不可靠 | 修复方案：使用 `sct.monitors[0]` virtual desktop 或按点选择 monitor；region clamp 到 virtual desktop；记录截图 origin。

- [√] `computer_server/handlers/som_bridge.py:83`、`computer_server/handlers/som_bridge.py:117`、`computer_server/mcp_server.py:2721` — SOM warm-up daemon 线程与 tool 调用没有状态协调 | 现状：warm_up 后台初始化 parser/model，tool handler 可同时调用 `detect_sync`；parser singleton 锁只保护创建，不保护 `parser.parse` 与 model loading 状态 | 风险：首次请求与 warm-up 并发时模型加载竞争，可能重复加载、超时或异常；GPU/CPU 降级失败只记录 warning | 修复方案：为 parser 增加 `ready/loading/failed` 状态；detect 等待 ready 或明确返回 warming_up；记录 device、fallback 原因；GPU OOM 时自动 CPU fallback 并缓存状态。

- [√] `computer_server/handlers/browser_cdp.py:61`、`computer_server/handlers/browser_cdp.py:81`、`computer_server/mcp_server.py:2338` — CDP 每次新建 WebSocket 且端口发现脆弱 | 现状：每条 CDP 命令 discover `/json` 后新连 websocket；端口从 `GetProcessCommandLine` 解析，失败 fallback 9222 | 风险：多浏览器、多 profile、远程调试端口变化时连错实例；高频 page 操作性能差；断线/target closed 没有重试 | 修复方案：缓存 `(port, target_id) -> websocket` 或至少缓存 targets；验证 target pid/window 关系；target closed 时 rediscover + retry 一次；端口发现失败时返回需要用户启动 remote debugging 的明确错误而非静默 9222。

- [√] `computer_server/mcp_server.py:2055`、`computer_server/handlers/windows_uia.py:684`、`computer_server/mcp_server.py:2099` — tree_quality 定义前后不一致 | 现状：UIA quality 用 named ratio + count；SOM 合并后 `_classify_tree_quality(total_elements)` 只按 count 覆盖 quality | 风险：SOM 低质量大量文本会把结果标成 rich；调用方误判 UIA 可交互质量 | 修复方案：拆分 `uia_quality`、`vision_quality`、`merged_node_count`；不要用 vision count 覆盖 UIA quality。

- [√] `computer_server/server.py:60`、`computer_server/server.py:90`、`computer_server/server.py:102` — async server stop 不能真正通知 uvicorn 优雅退出 | 现状：`_should_exit` Event 只在 Server 自己设置，从未传给 `uvicorn.Server`；stop 直接 cancel `server.serve()` task | 风险：HTTP server 嵌入式使用时连接和 lifespan 不一定优雅关闭，资源泄漏或端口释放延迟 | 修复方案：保存 `uvicorn.Server` 实例，stop 时设置 `server.should_exit = True` 并 await task；必要时加超时后 cancel。

## P2 - 建议改进

- [ ] `computer_server/mcp_server.py:491`、`computer_server/mcp_server.py:1835`、`computer_server/mcp_server.py:2461` — `mcp_server.py` 过度承担 application service 职责 | 现状：工具注册、坐标转换、window state、SOM、replay、learning 入口都集中在单文件 | 风险：修改任何能力都容易影响其他工具，测试粒度过大 | 修复方案：拆出 `window_state_service.py`、`coordinate_service.py`、`replay_resolver.py`；`mcp_server.py` 只做 schema/参数校验/调用服务。

- [√] `computer_server/handlers/factory.py:23`、`computer_server/mcp_server.py:126`、`computer_server/mcp_server.py:1321` — `_get_handlers()` 返回 5 元组，调用点靠位置解构 | 现状：大量 `_, automation_handler, _, _, _` 和 index-style 解构 | 风险：新增 handler 或调整顺序会产生隐蔽错误 | 修复方案：改为 `@dataclass Handlers(accessibility, automation, file, desktop, window)`。

- [√] `computer_server/handlers/generic.py:149`、`computer_server/handlers/generic.py:801` — GenericWindowHandler 混入 Windows 专有 foreground lock 逻辑 | 现状：generic handler 内部使用 Win32 `SetForegroundWindow`、Alt key bypass | 风险：模块命名误导，未来跨平台维护容易出错 | 修复方案：拆 `WindowsWindowHandler`；generic 只保留平台无关 open/path 逻辑。

- [√] `computer_server/handlers/windows.py:363`、`computer_server/mcp_server.py:1023`、`computer_server/mcp_server.py:1117` — async handler 内使用 `time.sleep` 阻塞事件循环 | 现状：drag/scroll/activate 等路径在 async 函数中直接 sleep | 风险：MCP/HTTP 并发请求时阻塞整个 event loop，尤其 smooth scroll 和 drag 可持续数百毫秒 | 修复方案：替换为 `await asyncio.sleep`；纯同步 mouse stepping 放到专用 worker 或明确串行化。

- [√] `computer_server/mcp_server.py:434`、`computer_server/mcp_server.py:878` — `_send_input_click` 命名与实现不符 | 现状：函数名/结果写 `send_input`，实际使用 legacy `win32api.mouse_event` | 风险：误导调试和文档；在 UIPI、RDP、DPI 场景行为不可控 | 修复方案：要么改名 `_mouse_event_click`，要么用 ctypes 实现真正 `SendInput` 并检查返回值。

- [√] `computer_server/handlers/windows.py:485`、`computer_server/handlers/base.py:370`、`computer_server/mcp_server.py:1196` — clipboard typing 缺少并发保护和敏感内容策略 | 现状：type_text 保存原剪贴板、分块复制、最后恢复；多个调用并发时会互相覆盖；中途失败可能保留待输入文本 | 风险：用户剪贴板被污染或敏感输入短暂暴露给其他应用 | 修复方案：加进程级 clipboard lock；失败路径 finally 恢复；提供 `paste_all_at_once` 默认；敏感文本不写日志。

- [√] `computer_server/learning/coalescer.py:72`、`computer_server/learning/recorder.py:592` — timer flush 与 stop 并发边界需要更明确 | 现状：Timer 回调可能与 stop flush 同时触发，依赖 buncher 内部锁避免重复，但 recorder 状态切换与 `_last_uia_value` 不是同一锁 | 风险：极端时 typeText 顺序/IME diff 可能错乱 | 修复方案：stop 时先 freeze input，再 cancel timers，再在 recorder lock 下统一 flush；`_last_uia_value` 与 buncher lifecycle 绑定。

- [√] `computer_server/learning/session.py:120`、`computer_server/learning/session.py:179` — `SECURE_FIELD` 序列化缺少显式 payload | 现状：`ObservedActionType.SECURE_FIELD` 在 `to_dict` 中没有专门字段，只保留通用 app/window/element | 风险：下游模型难以区分“安全字段被跳过”与普通空动作 | 修复方案：为 secureField 输出 `reason: "sensitive_field"` 和 `redacted: true`。

- [√] `computer_server/handlers/browser_cdp.py:195` — `query_dom` 允许提取 `innerHTML`，可能返回敏感 DOM 片段 | 现状：attributes 可由调用方指定，`innerHTML` 截断到 1000 字符 | 风险：页面 token、隐藏字段、用户隐私可能被读出 | 修复方案：默认禁用 `innerHTML`，需要显式 `allow_html=True`；对 password/input hidden 做过滤。

- [√] `computer_server/handlers/som_bridge.py:197`、`computer_server/mcp_server.py:2059` — 同时保留 async `detect` 与 sync `detect_sync`，但实际策略混乱 | 现状：注释说 async-friendly executor，实际 mcp_server 因 anyio 限制同步调用 `detect_sync` | 风险：未来调用者误用 async detect 造成之前已知死锁/延迟问题 | 修复方案：明确废弃或隔离 async detect；文档写清只允许启动前预加载 + handler 内 sync detect。

- [√] `computer_server/main.py:117`、`computer_server/mcp_server.py:491` — HTTP handlers 与 MCP tools 两套 API 不一致 | 现状：legacy HTTP `/ws` 与 `/commands` 已删除，HTTP 仅保留 `/status` 与 `/mcp` MCP transport；完整工具能力只通过 MCP 暴露 | 风险：旧的双 API 分裂路径已移除 | 修复方案：保留 MCP tool registry 作为唯一高权限工具入口。

- [√] `computer_server/handlers/generic.py:912`、`computer_server/handlers/generic.py:924`、`computer_server/handlers/generic.py:1009` — 文件工具无 sandbox/root 限制 | 现状：任意绝对路径读写删，路径只做 expanduser/resolve | 风险：配合 HTTP 暴露时可读写用户任意文件；即使本地 MCP，也缺少审计边界 | 修复方案：支持 `--root`/allowlist；危险写删默认禁用；返回路径审计日志。

- [√] `computer_server/handlers/windows_uia.py:208`、`computer_server/learning/platform.py:554` — UIA ControlType 映射维护两套 | 现状：`windows_uia` 映射为 Button/Edit 等，learning 映射为 AXButton/AXTextField 等 | 风险：同一控件在 MCP 与 learning 输出语义不一致 | 修复方案：建立统一 control type normalization 模块，同时输出 `uia_control_type` 与 `semantic_role`。

- [ ] `computer_server/recording/session.py:21`、`computer_server/learning/recorder.py:611` — Recording 与 Learning 不应合并，但底层能力重复 | 现状：两套系统分别做截图、OCR、UIA、窗口上下文 | 风险：多显示器、IME、隐私修复需要改两遍 | 修复方案：保留两个 recorder；共享 `capture_service`、`uia_service`、`ocr_service`、`window_identity_service`。

- [ ] `computer_server/handlers/browser_cdp.py:119`、`computer_server/handlers/browser_cdp.py:160`、`computer_server/mcp_server.py:2338` — CDP 按 `tab_index` 而不是 `target_id` 跟踪 tab | 现状：`evaluate / query_dom / get_page_text / get_page_info` 全部接收 `tab_index`；首次 discover 后用 `targets[tab_index]` 拿 ws_url，命令失败时 `force_refresh` 重新 discover 再按同一 `tab_index` 取 target | 风险：旧 tab 关闭或顺序变化时，`tab_index` 漂移到不同 tab，retry 可能在错误的页面执行 JS 或读 DOM；多窗口/多 profile 浏览器尤其敏感 | 修复方案：在 caller 调用前用 `target_id`（CDP 的 `id` 字段）锁定 tab，CDP client 内部缓存 `target_id → ws_url`；retry 时按 `target_id` 重新解析而不是按 index；保留 `tab_index` 仅作为初次查找的便捷参数。

- [√] `computer_server/mcp_server.py:610`、`computer_server/handlers/windows.py:661` — screenshot 大小控制分散 | 现状：automation handler 产 base64，MCP 再 decode/resize/re-encode；window_state 再存文件 | 风险：CPU/内存额外消耗，截图路径难测 | 修复方案：集中 screenshot service，支持直接返回 PIL/image bytes/file path，并携带 geometry metadata。

- [√] `computer_server/mcp_server.py:2445`、`computer_server/mcp_server.py:2578` — recording/learning 单例生命周期挂在函数属性或模块全局 | 现状：recording session 是 module global，learning recorder 存在 `computer_learn_start._recorder` | 风险：多 FastMCP 实例、HTTP+stdio 并存或测试并发时状态污染 | 修复方案：引入 `ServerContext`，所有 session/registry/cache 挂在 context 上。

- [√] `computer_server/handlers/som_bridge.py:31` — `VisionResult.metadata` 使用 mutable-ish 默认 None + type ignore | 现状：dataclass 字段 `metadata: Dict[str, Any] = None` 再 `__post_init__` 修正 | 风险：类型检查噪音，小问题但可清理 | 修复方案：改为 `field(default_factory=dict)`。

- [√] `computer_server/overlay/motion.py:37`、`computer_server/overlay/motion.py:171` — overlay motion 使用 raw str phase 和参数过多 | 现状：`phase` 为字符串，`plan_path` 参数 10+ | 风险：状态机 typo 和调用可读性差 | 修复方案：引入 `Phase` enum 和 `Pose` dataclass。

- [√] `computer_server/cli.py:33`、`computer_server/server.py:38` — CLI 默认暴露范围不适合桌面自动化工具 | 现状：host 默认 `0.0.0.0`，日志提示 HTTP API 可用 | 风险：用户无意中把桌面控制服务暴露给局域网 | 修复方案：默认 `127.0.0.1`；启动时如果 host 非 localhost，打印强警告并要求 `--i-know-this-exposes-my-computer`。

## 整体评价

- 代码质量：7/10

项目的核心方向正确：Windows + 中文 UI 优先、UIA 优先/SOM 回退、MCP recording 与 human learning 分离、对 FastMCP outputSchema bug 和 C-extension import deadlock 有明确工程经验沉淀。实现已经能覆盖大量真实 Windows 自动化场景；默认 HTTP 暴露和坐标几何风险已处理，当前主要短板是 `mcp_server.py` 仍承担过多 application service 职责，以及 Recording/Learning 底层能力还有重复。

### Top 3 致命风险

1. 非 localhost 绑定 `/mcp` 仍是高权限桌面控制入口，必须保持显式确认和清晰文档。
2. `mcp_server.py` 聚合工具注册、window state、坐标转换、replay、learning，后续修改仍容易产生跨能力回归。
3. Recording/Learning 共用的截图、OCR、UIA、窗口识别能力未完全服务化，多显示器和隐私修复仍可能需要重复维护。

### Top 3 性能瓶颈

1. `get_window_state` 串行执行截图、UIA tree walk、SOM detect、JPEG 写盘，tail latency 高。
2. Recording 每个动作后截图 + PNG/click image 写盘，会显著拖慢连续操作。
3. CDP 每次 discover + 新建 WebSocket，且 screenshot 反复 base64 encode/decode/re-encode，频繁调用时开销大。

### 建议优先测的 3 个模块

1. 坐标系统：`computer_get_window_state` + `computer_click/drag/scroll` + `computer_zoom/from_zoom`，覆盖 resize、多显示器负坐标、高 DPI、同 pid 多窗口。
2. UIA + ElementCache：hung UIA provider、stale HWND、窗口重开、disabled element、COM stale platform_ref。
3. Recording/Learning：replay stale window resolution、IME 中文输入、敏感字段/OCR 脱敏、连续高速输入/滚动下的事件顺序。
