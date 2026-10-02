# ResidentAgent —— 常驻云端的跨端 LLM Agent 系统

一个 7×24 运行在云服务器上的对话 Agent：云端大脑负责 LLM 调用、长期记忆（向量检索）、
工具调用（生图 / 写日记 / 发朋友圈）与定时自主触发；Windows 桌宠（PyQt6）与 Android App（WebView）
是两个**纯客户端**，共用同一套大脑、同一份记忆。

> 项目名里的 Resident 指的是"常驻"：它不是一个能跑通的脚本，而是一个在云服务器上连续运行数周、
> 记忆跨会话累积、三个客户端随时接入都是同一个人格的进程。
> 人格完全由 `brain/config/personas/*.json` 定义，**仓库不预置任何角色**（连示例人设都是空的）—— 角色名不是项目名，也不需要是项目名。

**技术栈**：Python 3.12（`httpx` / `fastembed`+bge-m3 / `numpy` / `Pillow`）· PyQt6 · Android（WebView 单页 + 原生轮询服务）· LLM 走 OpenAI 兼容接口（SiliconFlow 等，可在设置里换服务商与模型）· 语音走阿里云百炼（`qwen-audio-3.1-tts-flash`，音色由参考音频零样本复刻而来）

---

## 一、架构与模块

```
   ┌────────────────┐   SSH 隧道 18787 → 8788    ┌──────────────┐
   │ 桌宠 pet（Win）│ ──────────────────────────► │              │
   └────────────────┘                            │  云端大脑     │
   ┌────────────────┐   HTTP :8788（token 鉴权） │ brain/server │
   │  Android App   │ ──────────────────────────► │              │
   └────────────────┘                            └──────┬───────┘
                                                        │
                         记忆 / 摘要 / 约定账本 / 日记 / 朋友圈 / 图片
```

**大脑只有一份，在云端。** 桌宠和 App 都不养自己的大脑——桌宠靠 `config.json` 里的 `brain_remote`
连过去（配了 SSH 隧道就是 `http://127.0.0.1:18787`），App 直连云上的 `:8788`。
（本机兜底大脑已于 2026-09-29 摘掉，`brain_remote` 为空时桌宠会直接报错退出。）

| 模块 | 作用 |
|---|---|
| `brain/server.py` | 云端 HTTP 服务（端口 8788，token 鉴权）：装配大脑、聊天管线与全部路由；原 2200 行单文件已按领域拆成 runtime / reply / randimg / selfie / imggen / proactive / moment_flow / jobs 八个模块，对外接口零变化 |
| `brain/tools.py` | 工具调用（手写 function calling）：模型判断需要就写 `[tool:...]` 标签，服务端执行后把结果喂回再答——查他的实时位置（IP）/ 天气 / 快递 |
| `brain/brain.py` | 决策核心：上下文组装、人设注入、API 失败兜底 |
| `brain/memory_store_v2.py` | 长期记忆：bge-m3 向量检索，维度变化时索引自动重建 |
| `brain/recap_store.py` | 历史摘要：聊过的内容按 日→周→月 三层滚动压缩，历史不会越滚越长 |
| `brain/agenda.py` | 约定账本：带日期的约定按时间窗强制注入，且带状态（未到/办成/过期未办） |
| `brain/life_engine.py` | 她自己的生活推演与夜间日记 |
| `brain/moments.py` | 朋友圈 |
| `brain/vision.py` | 图片视觉描述（云端 VL 模型转成一句话） |
| `brain/voice.py` | 语音合成：她想用声音说时发语音条。当前平台**阿里云百炼 Qwen-Audio-3.1**（音色为参考音频复刻），旧平台硅基流动保留为兜底；音色清单与热切换见 `config` 的 `voice` 段与 `/api/voices` |
| `brain/persona_store.py` | 人设系统：白名单字段 + 全局身份底线，支持热重载 |
| `brain/model_hub.py` | 模型中枢：拉取模型列表并按对话 / 看图 / 生图 / 语音分类 |
| `brain/errlog.py` | 前后端错误日志，客户端可上报与导出 |
| `pet/` | Windows 桌宠（PyQt6），通过 SSH 隧道连云端大脑 |
| `android-app/` | Android 客户端：WebView 单页 + 原生轮询服务 |

---

## 二、快速开始

```bash
# 1) 云端大脑（部署到服务器，或先在本机跑起来试）
pip install -r brain/requirements.txt
cp brain/config/config.example.json brain/config/config.json   # 填 api_key / token
python brain/server.py                                          # 监听 :8788

# 2) 桌宠（本机 Windows；先把隧道建起来）
ssh -L 18787:127.0.0.1:8788 <user>@<服务器>       # 或双击 pet/launchers/run_pet_cloud.bat
pip install -r pet/requirements.txt
python pet/pet.py

# 3) 手机 App
#    用 gradle 全量构建：python deploy/rebuild_apk.py
#    装好后「我 → 设置」里填服务地址与 token（只需填一次）
```

> `fastembed` 第一次跑会下载 bge-m3 模型，需要联网。

---

## 三、目录结构

```
仓库根目录
├── brain\            ★ 云端大脑（部署到云服务器的服务端 + 本地共享核心）
│   ├── paths.py         所有路径的唯一出口（自动以 brain\ 为根，别再手写路径）
│   ├── brain.py         她的脑子：上下文组装、人设注入、API 直出 + 失败兜底
│   ├── server.py        云端 HTTP 服务（手机 App / 桌宠都连它），端口 8788；已拆分：runtime 共享配置｜reply 回复后处理｜randimg 随机图｜selfie 自拍｜imggen 生图锁脸｜proactive 主动搭话｜moment_flow 朋友圈生成｜jobs 后台循环
│   ├── tools.py         工具调用：她自己决定调免费 API（查他在哪 / 天气 / 快递）
│   ├── memory_store_v2.py 记忆库（bge-m3 向量检索）
│   ├── recap_store.py   历史摘要：聊过的内容压成 日→周→月 三层
│   ├── agenda.py        约定账本：带日期的约定按时间窗强制带，到点自己想起来
│   ├── moments.py       朋友圈      stickers.py   表情包
│   ├── vision.py        图片视觉描述（云端 VL）
│   ├── voice.py         语音合成：她发语音条（平台/模型/音色写在 config 的 voice 段，App 里可一键换）
│   ├── life_engine.py   她自己的生活 + 日记
│   ├── worldgen.py      世界设定生成
│   ├── persona_store.py 人设
│   ├── requirements.txt 依赖清单
│   ├── config\          config.json / world.json / personas\  ← 你要改的都在这里
│   ├── data\            程序自己写的：memory / her_life / journal / chat_history / upload / stickers / run / summary
│   ├── assets\          头像、立绘
│   ├── archive\         已被替换的历史版本（*.bak_before_*），只作考古用
│   └── logs\            watchdog.log
│
├── pet\             ★ 桌宠（本机 Windows 客户端，立绘 + 气泡 + 聊天窗）
│   ├── pet.py           主程序（PyQt6）
│   ├── watcher.py       活动监视（你在干嘛 → 决定她要不要主动说话）
│   ├── settings_dialog.py / diary_dialog.py   设置窗口 / 日记窗口
│   ├── watchdog.py      看门狗：**只**把桌宠拉起来（计划任务每 5 分钟）。
│   │                    大脑在另一台机器上，本机够不着它 —— 那边靠自己的计划任务/服务守着
│   ├── remote_brain.py  通过 SSH 隧道连云端大脑
│   ├── requirements.txt 依赖清单
│   ├── zhixia_mod\      立绘素材帧
│   └── launchers\       所有启动脚本（见「如何运行」）
│
├── android-app\     ★ 手机 App（安卓工程，微信风格聊天界面）
│   └── app\src\main\assets\chat.html   前端单页（核心 UI，唯一真源）
│
├── tests\           自动化测试（stdlib unittest，不联网、不读真实 config、不花钱）
├── requirements-dev.txt   跑测试的最小依赖（numpy / httpx）
│
├── deploy\          发布物与打包脚本
│   ├── rebuild_apk.py   gradle 全量构建（快速换 UI 打包见 .workbuddy/scripts/tools/）
│   └── sshkeys\         部署用 SSH 私钥（已在 .gitignore 中）
│
├── docs\            文档：设计与取舍、历次改造说明（含私人内容的不提交，见 .gitignore）
│
└── _attic\          阁楼：历史遗留集中放（2026-09-29 整理，只是挪走，可随时回收）
```

---

## 四、如何运行

> 所有脚本都在 `pet\launchers\`。

| 场景 | 双击 / 运行 |
|------|------------|
| 只开桌宠（隧道已建好） | `pet\launchers\run_pet.bat` |
| 开桌宠 + 建隧道 | `pet\launchers\run_pet_cloud.bat` |
| 桌宠 + 云端大脑（本机联调） | `pet\launchers\autostart_zhixia.bat` |
| 手动跑云端大脑 | `python brain\server.py` |
| 看门狗（计划任务每 5 分钟） | `pet\launchers\run_watchdog.bat` |
| 开机自启（无窗口） | 把 `pet\launchers\autostart_hidden.vbs` 放进「启动」文件夹 |

- **重启桌宠**：退出现有桌宠（托盘右键退出），再双击 `run_pet.bat`。
- **改了云端代码要生效**：先杀掉旧的 `server.py` 进程再拉起 —— 启动脚本有单实例互斥量，旧进程还活着时新的是空转。

### 跑测试

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t tests -v
```

这套用例**不联网、不读真实 config、不花钱**：embedding 换成确定性假向量，
文件类用例全走临时目录。它罩的是纯逻辑与接口契约那部分 ——
约定账本的日期解析与状态机、历史摘要的水位线与分批、记忆库的索引一致性与并发写、
主动搭话的静默时段/自适应频率/重复闸门、工具标签的执行与兜底清扫、
对话文本的分段与清洗、鉴权判定，以及"两份 chat.html 必须一致"这类工程约定。

CI（`.github/workflows/tests.yml`）在每个 push 上跑这套用例 + 一次全仓 `compileall`
（后者顺带把 PyQt6 桌宠与客户端的语法兜住，不需要装它们的依赖）。

---

## 五、你要改的配置

- 人设 / 世界 / 记忆目录：`brain\config\`。
- 模型 / API key / 主动搭话 / 视觉 / `brain_remote`：`brain\config\config.json`。
- 客户端侧：服务地址与 token 在 App 设置页填一次即可（存本地；仓库里默认值是空的）。

---

## 六、云端部署备注

- 云上服务读它自己的 `C:\linzhixia\app\`（`version.json` / `app.apk` 在此，本地这份不生效）。
- 本地 `deploy\sshkeys\linzhixia_deploy` 是部署私钥；`run_pet_cloud.bat` 用它建隧道 `18787 → 云端 8788`。
- 改完 `brain\` 代码要同步到云端，覆盖后记得重启进程（见第四节末尾那条）。

---

## 七、设计与取舍

工具调用、长期记忆、自主行为与护栏的设计细节，以及实现过程中踩过的坑、
故意没这么做的取舍、实测数据和已知不足，都在 [`docs/设计与取舍.md`](docs/设计与取舍.md)。

`docs\` 下其余文档：

| 文档 | 内容 |
|---|---|
| [`设计与取舍.md`](docs/设计与取舍.md) | 能力构成、踩过的坑、实测数据、已知不足 |
| [`语音平台成本对比.md`](docs/语音平台成本对比.md) | 腾讯 / 阿里 / 火山 / 硅基 / MiniMax 等平台月成本换算过程、两个容易看错的计费口径 |
| [`游戏内录音取样指南.md`](docs/游戏内录音取样指南.md) | 想给她换个游戏角色音色时，怎么录、录什么、哪些角色公开数据集里根本没有 |
| [`记忆与上下文改造说明.md`](docs/记忆与上下文改造说明.md) | 三层记忆（事实 / 话题回顾 / 约定账本）的来由 |
| [`时间感与主动性修复.md`](docs/时间感与主动性修复.md) | 历史消息丢时间戳导致反复重提旧话题、主动搭话不读配置导致凌晨连发、接口不返回时刻 —— 这三处的定位与修复 |

---

## 八、整理约定与归档说明

这几条是为了让目录不再乱长，新增文件前先看一眼：

- **源码目录不留 `.bak`**：被替换的历史版本挪进 `brain\archive\`，文件名带"替换原因"（如 `qq_bot.py.bak_before_qq_retire`）。
- **文档一律进 `docs\`**：根目录只留 `README.md`。含私人内容的（如 `记忆库清单.md`）留在 `docs\` 但加进 `.gitignore`，不公开。
- **不留重复副本**：手机端 UI 的唯一真源是 `android-app\app\src\main\assets\chat.html`。
- **两个例外：根目录的 `_chat.html` 和 `_zhixia.apk` 不许动** —— 它们是**打包流水线的输入**。
  `_repackage.py` 直接读这两个路径；`_chat.html`（前端母本）与工程里那份 `assets\chat.html`
  **内容必须一致**，改前端时两份都要同步，否则"根目录改好了、装到手机上还是旧的"。
- **废弃文件进 `_attic\` 而不是直接删**：确认没用就挪进去（可反悔）。
- **`.workbuddy\scripts\`** 是历次改造的临时脚本与云端回传结果，不进仓库。

---

> **开发说明（如实标注）**：本项目代码主体在 AI 辅助下完成；作者负责需求拆解、结构设计、部署上线、线上故障定位与数据治理。
>
> **安全说明**：仓库内不含 API key、服务器地址、鉴权 token 与私钥。部署前请按 `brain/config/config.example.json` 自建 `config.json`，并在客户端设置页填写服务地址与 token。
>
> **鉴权行为（2026-10-03 起变了，部署时注意）**：`brain_token` **没配**时，远程 API 只接受**本机回环**连接；
> 如果同时配了 `bind_host: "0.0.0.0"`，进程会**拒绝启动这个口**并打印原因（以前是空 token 全放行，
> 而"忘了填 token"恰恰最常见 —— 那等于把接口摆到公网）。要让手机连进来，必须填一个随机长串
> `brain_token`。另外 JSON 接口改用 `Authorization: Bearer <token>` 头发令牌，URL 上的 `?token=`
> 只为 `<img>/<audio>` 这类设不了头的媒体请求保留，服务端两种都认。

## 更新日志

只记重要变更；App 版本号是安装包内的 `APP_CODE`，手机端"检查更新"据此提示。

- **2026-10-03 · 第二轮逐行复核（含上一轮遗留与脱敏副作用）**：
  - **修掉一个静默到几乎不可能被发现的人设回退**：脱敏时把人设文件改成了
    `personas/default.json`，而 `config.json` 的 `persona_file` 还指着旧名字 ——
    `persona_store.load()` 读不到文件时**一句话都不说**，直接返回内置空模板，
    于是她的自定义人设被悄悄换掉，表现只有"她好像不太一样了"。
    现在：读不到会明确打印（并说明改用的是哪一份）；配置那份不在而 `default.json`
    在时先退到它就是。**本地 `config.json` 的 `persona_file` 仍需你手动改成
    `personas/default.json`**（那份文件已 gitignore，我改不了它对你的部署生效）。
  - **测试自身两处问题**：`test_manifest_activity_matches_package` 只匹配 `com.` 开头的
    全限定名，而 Manifest 用的是相对名 `.MainActivity` —— `findall` 返回空、循环一次没跑，
    **这个测试一直"绿"着但什么都没断言**（已改成相对/全限定两种都认，并核对类文件存在）；
    新增 `tests/test_source_hygiene.py`，静态扫"同一作用域重复定义同名函数"—— 这个仓库
    真踩过两次（`chat.html` 的 `copyText`、`remote_brain.py` 的 `_post`），Python 里后者静默覆盖前者。
  - **补掉一个我自己引入的并发缺陷**：`_trace_write` 与 `_trace_trim` 之间没有锁，
    而 trace 是请求线程 + 6 条自治循环一起写的 —— 轮转的"读全文→写回前半"会和
    另一条线程的 append 交错，把刚写进去的那条连带丢掉。现在落盘与轮转共用一把锁。
  - **修掉 `remote_brain.py` 里重复定义的 `_post`**：旧那份（token 拼在 URL 上）和新那份
    （走 `Authorization` 头）同时存在，靠"后定义的赢"才碰巧走对 —— 读代码的人完全看不出。
    顺手让 `RemoteBrain._get` 也走请求头（它访问的全是纯 JSON 接口，能带头）。
  - **trace 记错了轮次**：工具调用会走两轮 `chat`，而快照只抄了**第一轮**（工具判断那次）
    的数字；`[tool:]` 用了哪个工具也只打到 stdout、重启就没。现在每轮覆盖（记最后一轮，
    即用户真正看到的那次），并把工具名写进 trace。
  - **聊天锁的等待上限改成算出来的**：模型 60s + 重试间隔 1.5s + 重试 60s = 121.5s，
    工具轮两次 ≈ 243s，加 UAPI 最坏 12s ≈ **255s** —— 原值 240 会在正常慢请求上误报"她正忙着"，
    已按推导取 300。
  - **脱敏补遗**：`tests/` 里我上一轮用了**真实的云服务器 IP** 当测试样本，已换成
    RFC 5737 的文档保留段（`203.0.113.0/24`）。另外这两处拼音还没脱（都在追踪文件里、
    都是可被搜索到的）：`android-app/settings.gradle` 的 `rootProject.name`、
    `MainActivity.java` 里的 User-Agent `ZhixiaChat/1.0`；`pet/launchers/autostart_*.bat`、
    `deploy/rebuild_apk.py` 与云端 `zhixia.apk` 这类**部署路径/文件名**与本地环境耦合，
    改名要连着改云端和计划任务，没动。

- **2026-10-03 · 一次"说法与代码对齐"的体检（据代码逐行复核）**：这轮改的是**声称与实际不符**和几个一直静默失效的地方，不是新功能。
  - **修掉一个鉴权口子**：`brain_token` 为空时 _check 直接放行（fail-open），配上文档推荐的 `bind_host: "0.0.0.0"` 就是公网无鉴权 —— 现在空 token 只放行本机回环，且对外绑定时**拒绝启动**；比对改用 `hmac.compare_digest`；`do_POST` 加 12MB 请求体上限（以前 `Content-Length` 报多大就读多大进内存）。
  - **并发**：`MemoryStore` 原本**一把锁都没有**，而记忆抽取是后台线程在 `add()`、请求线程同时在 `retrieve()` —— 并发写 `memory.json`/`vectors.npy` 是可达的，且损坏是静默的。现在自带可重入锁，网络调用（embedding）放在锁外。chat 锁改成**有上限地获取**（240s）：以前一个卡住的请求能让后面所有人无限期挂死。trace 的字段改成**持锁时抄一份快照**，不再出锁再读共享对象（并发时那一行会张冠李戴）。
  - **上游失败**：主聊天调用原来**没有重试**，一次瞬时 500 就变成"脑子转不动"。现在对网络异常/超时/429/5xx 重试一次（401/402 不重试，重试没用还费钱）。
  - **trace 覆盖与轮转**：trace 以前只有 `/api/chat` 一条路有，主动搭话/朋友圈/事件提醒**完全没有记录**；现在自治循环各落一行（发的、因重复/自己不想说而拦掉的都记）。`trace.jsonl` 也不再只增不减（4MB 砍一半，和 errlog 同做法），`_trace_tail` 改用 `deque` 边读边丢。
  - **修掉三处静默失效**：桌宠的 `read_history()` / `clear_history()` 在远程大脑上**根本不存在**，`AttributeError` 被 `except` 吞掉 —— 表现是"聊天窗每次启动都是空的"、"清了记录其实没清"（清的还是本机的文件）；`RemoteLife.catch_up/write_diary` 是空操作，于是 `LifeWorker` 每次开机空转、托盘"让她现在写篇日记"永远只报失败。现在补齐了 `/api/history/clear`、`/api/life/catchup`、`/api/diary/write` 三个接口和对应客户端方法。
  - **`errlog` 装错了钩子**：只设了 `sys.excepthook`，而它自己的注释说要覆盖"线程里静默死掉的异常" —— 那需要 `threading.excepthook`，全仓没有。服务跑着 6 条后台 daemon 线程，这条正是最容易死了没人发现的通道。已补。
  - **`threading.excepthook` 之外的两处"注释骗人"**：`watcher.py` 说只看窗口标题"零隐私成本"，实际**标题原文会随 scene 上云**（截屏那条路确实已移除，但标题不是零）；`README` 说看门狗会把"桌宠 / 云端大脑"都拉起来，实际只有桌宠（`BRAIN_PY` 是从未被引用的死代码，已删），大脑在另一台机器上，本机够不着。看门狗自己的单实例守卫还重现了 `pet.py` 注释里专门警告过的 ctypes LastError 坑，已按正确写法修。
  - **修掉一个丢内容的 bug（测试逼出来的）**：`recap_store.pending_rows` 里"某一天自己就超了上限 → 只能硬切"那个分支写的是 `if cut == 0`，而 `cut` 第一轮就至少是 1 —— **该分支从未生效**，一天不管有多少条都会一次性喂给模型，而 `compress_day` 会把正文截到 8000 字，后面的内容等于从没被压过、水位线却已经推过去（永久跳过）。实测 576 条全进一批。
  - **修掉一个标签泄露**：`[tool:]` 的兜底清扫和"执行"共用同一个正则（名字限 ASCII），所以模型编一个中文工具名（`[tool:不存在:xx]`）会**原样发给用户**。清扫改用宽口径，执行仍用严口径。
  - **修掉一个从没生效过的归一化**：`proactive._norm_text` 声称"去时间戳前缀"，但正则要求两个连字符，而真实标签是 `[09-29 06:18]`（只有一个）—— 去重闸门因此对带标签的文本失效。
  - **前端**：`esc()` 用 `textContent→innerHTML`，只转 `<> &` **不转引号**，而它被用在 `data-mid="…"`/`src="…"` 属性里（值里带一个引号就能突破属性）—— 补上引号转义；`copyText` 在同一作用域被定义了**两次**（后者静默覆盖前者，让"已复制"提示成了死代码），合并成一个；`fetch` 包装统一带上鉴权头并把 URL 里的 token 摘掉；发消息失败时不再画一个空的"她"的气泡假装她回了，而是把原话放回输入框（以前消息是真没发出去，下一次拉历史就消失）。
  - **Android**：`setAllowFileAccessFromFileURLs` 关掉（它允许 file:// 页读别的本地文件，页面并不需要；`setAllowUniversalAccessFromFileURLs` 必须保留 —— 页面是 file:// 源、要请求云机接口，关掉会直接 "Failed to fetch"，要彻底去掉得改用 WebViewAssetLoader，那需要真机回归，见上）；`PollService` 的 token 从 URL 挪到 `Authorization` 头；新消息游标改用 `时间+正文`（原来只比分钟级时间戳，**同一分钟内来的第二条会被整个漏掉**）。
  - **新增自动化测试**：`tests/` 下 134 个用例（stdlib unittest，不联网、不读真实 config、不花钱）+ `.github/workflows/tests.yml`（跑用例 + 全仓 `compileall`）。以前**一个自动化测试都没有**，"效果靠日常使用观察"。
  - **示例配置**：`config.example.json` 的 `voice` 段还停在硅基流动的 `CosyVoice2-0.5B`，而主平台早换成阿里百炼 `qwen-audio-3.1-tts-flash` —— 照抄会踩；已改成实际的段位形态（`provider` / `aliyun.api_key` 等）。
  - **没有做的（刻意）**：`print` 换 `logging`（86 处调用点、没有测试网时不该动）、SSE 流式、Dockerfile —— 理由写在 `docs/设计与取舍.md` 的"已知不足"里。

- **2026-10-02 · 服务端**：**她会自己调用工具了**（手写 function calling）——模型判断需要就写 `[tool:...]` 标签，服务端执行后把结果喂回给她再回答，共三个工具：查他的实时位置（IP 定位）、查某地天气、查快递物流；顺带修掉"问我在哪她答自己住址"的主语问题。**修复 UAPI token 从接入起就没生效的坑**（配置白名单漏了 `uapi` 段，此前天气/热榜/快递全部裸奔）+ UAPI 偶发拖死连接加重试。**服务端重构**：2200 行单文件 server.py 按领域拆成 8 个模块（共享配置 / 回复后处理 / 随机图 / 自拍 / 生图 / 主动搭话 / 朋友圈流水线 / 后台循环），对外接口零变化。
- **2026-10-02 · App v4.8（code 21）**：**修表情包模块**——长按聊天图片可"加入我的表情包"，我的表情长按/红点删除，点按发送的原生 confirm 假死改为自绘弹层；表情面板不再混入聊天图与自拍（接口只回图库）；纯图片/表情包消息去掉气泡底色；语音条下方直接显示转写文字，不再单独跟一条复读文字泡；发现页新增**快递管理**（条目点开下拉：复制单号 / 改备注 / 删除，删除即停她的提醒）；她发的图挂了会显示可见提示而不是悄悄塌成空泡。
- **2026-10-02 · 服务端**：`/api/stickers` 只回表情包库（此前把 upload 整个倒出去，面板塞满没添加过的图）；`[rand:]` 标签解析加固（大小写不敏感 + 兜底清扫，任何变体不再原样漏进存档变成 404 裂图）；看图描述 / 快递系统提示只进模型上下文，不再出现在他的聊天气泡里；人设发图规则改写——他斗图/要新图必须走 `[rand:bq]`，清单老图只用于应景；新增 `/api/packages`（列表/删除/备注）三个接口。
- **2026-10-02 · App v4.7（code 20）**：长按聊天图片保存到相册（已保存的长按可删）；设置页新增**功能开关**（热搜 / 天气 / 天气提醒 / 快递提醒 / 聊天趣图 / 朋友圈随机配图，App 里直接远程开关云端功能）；**修复消息整轮显示两遍**的渲染 bug；她聊天时能甩网络表情包与趣图（福瑞类屏蔽，壁纸需点名）；朋友圈配图优先用现成图库，不再每张都现画。
- **2026-10-02 · 服务端**：接入 UAPI —— **快递监控**（发单号即托管，每 30 分钟自动查，派送/签收/异常时她主动提醒）；**天气跟人走**（按对方 IP 定位城市，下雨/降温/高温按规则触发她的主动关心，一天一档不重复）；热搜改抖音源、每 6 小时整点刷新。
- **2026-10-02 · App v4.6（code 19）**：回复按她的换行**拆成多条气泡连发**（像真人打字）；她有了**假期时间感**（今天是假期第几天、几号开学，用户说错她会纠正）；旧聊天记录里残留的时间戳前缀清理。
- **2026-10-01 · App v4.5（code 18）**：语音条不再重复显示文字；**一键换声音**（code 17，清单本地缓存，换音色不用重装）；语音切换阿里百炼 `qwen-audio-3.1-tts-flash`，音色由参考音频零样本复刻，实测频响优于原方案。
- **2026-10-01 · 服务端**：**校历系统**——法定节假日 + 调休逐年填数据，寒暑假由学期起止自动推导；校历没覆盖的年份她会用"放假通知还没出呢"的口气，不把没影的安排说死；修掉时间标签在回复开头漏网的 bug。

## 许可

MIT，见 [LICENSE](LICENSE)。
