# ResidentAgent —— 常驻云端的跨端 LLM Agent 系统

一个 7×24 运行在云服务器上的对话 Agent：云端大脑负责 LLM 调用、长期记忆（向量检索）、
工具调用（生图 / 写日记 / 发朋友圈）与定时自主触发；Windows 桌宠（PyQt6）与 Android App（WebView）
是两个**纯客户端**，共用同一套大脑、同一份记忆。

> 项目名里的 Resident 指的是"常驻"：它不是一个能跑通的脚本，而是一个在云服务器上连续运行数月、
> 记忆跨会话累积、三个客户端随时接入都是同一个人格的进程。
> 内置角色代号「角色」，她的人格由人设文件定义，支持热重载切换 —— **角色名不是项目名**。

| 模块 | 作用 |
|---|---|
| `brain/server.py` | 云端 HTTP 服务（端口 8788，token 鉴权），所有客户端都连它 |
| `brain/brain.py` | 决策核心：上下文组装、人设注入、API 失败兜底 |
| `brain/memory_store_v2.py` | 长期记忆，bge-m3 向量检索，维度变化时索引自动重建 |
| `brain/persona_store.py` | 人设系统：白名单字段 + 全局身份底线，支持热重载 |
| `brain/recap_store.py` | 历史摘要：聊过的内容按 日→周→月 三层滚动压缩，历史不会越滚越长 |
| `brain/agenda.py` | 约定账本：带日期的约定按时间窗强制注入上下文，到点她自己会想起来 |
| `brain/model_hub.py` | 模型中枢：从服务商拉取模型并按对话/看图/生图/语音自动分类 |
| `brain/errlog.py` | 前后端错误日志，客户端可上报与导出 |
| `pet/` | Windows 桌宠（PyQt6），通过 SSH 隧道连云端大脑 |
| `android-app/` | Android 客户端：WebView 单页 + 原生轮询服务 |

> **开发说明（如实标注）**：本项目代码主体在 AI 辅助下完成；作者负责需求拆解、结构设计、部署上线、线上故障定位与数据治理。
>
> **安全说明**：仓库内不含 API key、服务器地址、鉴权 token 与私钥。部署前请按 `brain/config/config.example.json` 自建 `config.json`，并在客户端设置页填写服务地址与 token。

---

## Engineering notes（设计与取舍，写给要看代码的人）

这一节只讲**为什么这么做**和**为此放弃了什么**——不看代码也能判断这个项目是"套壳聊天"还是真在系统层面解决问题。

### 它凭什么算 Agent（而不是套壳聊天）

| Agent 该有的 | 本项目的实现 |
|---|---|
| 工具调用（不只是生成文字） | 生图（参考图锁脸，调用 Z-Image-Turbo，4~31s）、视觉理解（VL 模型把图转成文字描述）、记忆抽取与写入、发朋友圈 |
| 跨会话长期记忆 | `memory_store_v2`：bge-m3 向量检索，条目带来源与时间；`recap_store`：日→周→月三层滚动摘要，历史不会越滚越长 |
| 不在用户请求时也能自己动 | 四条自治循环：主动搭话（默认 40 分钟）、生活推演（30 分钟补算）、夜间写日记、朋友圈（1 小时一次，每天最多 4 条）。**发不发由她自己决定**——模型回"无"就不做 |
| 失败处理与护栏 | 单实例互斥量、token 鉴权、人设 key 白名单（防路径穿越）、静默时段、每日上限、重复内容闸门、上游欠费降级、输出清洗（思考块/时间戳前缀） |
| 真的在长跑 | 云端 7×24 运行，累计 640+ 条真实对话、30+ 条长期记忆；三个客户端（App / 桌宠 / 直接 API）共用同一份记忆与人格 |

### 三个自己踩过的坑（教程里没有的）

1. **语义检索会漏召回**：同一件事换个说法就找不回来——实测"票买了吗"（5 字）压根不进检索、"放假了还上早八"排第 13 名召不回（只取 Top-5）。
   → 拆成三层：事实（语义检索，点状）/ 话题回顾（按时间全量带，线状）/ **约定账本 `agenda.py`（按日期强制注入，定时）**。带日期的约定不靠语义碰运气，快到那天了无论聊什么都会带上。
2. **相对时间会污染记忆**：她许诺"明天来找我"，第二天"明天"自动滑到新的一天，承诺永远兑现不了。
   → `agenda.py` 把"明天/后天/国庆/下周X/月底"按**那句话被说出的那天**解析成绝对日期，并在注入块里区分"还没到的"和"已经过完的日子（别再当约好的事提）"。
3. **自治行为会失控**：主动搭话每小时必发、凌晨连发 8 条、把刚说过的话原样再说一遍（实测 08:43/09:43、14:42/19:42 三组重复）。
   → 静默时段 + 每日上限 + **重复内容闸门**（写入前比对她最近 12 句，相同或一方是另一方的子串就跳过）。

### 四个设计取舍（我故意没这么做）

- **没用 Agent 框架（LangChain 等），手写编排**。这台云主机只有 2C2G，依赖轻、启动快、出问题时能直接看自己的代码栈；代价是轮子自己造，编排逻辑散在 `server.py` 的几个循环里。
- **图片不把像素喂给主模型**，先用 VL 转成一句话描述再进上下文。省 token、可控（描述里可以明写"没看清"），VL 挂了也能降级成"看不到"，不会让主对话崩。
- **记忆抽取/向量化放后台线程**，不阻塞回复。用户感知的是回复延迟，不是记忆写入延迟。
- **主动消息不做"每轮必发"**，她自己决定（回"无"就不发）。真人不会每小时都主动找人 —— 宁可少发，也不要刷屏。

### 我自己跑出来的数字

| 项目 | 结果 |
|---|---|
| 主模型端到端延迟对比（5 个模型，相同负载） | GLM-5.3 ≈1.6s / DeepSeek-V4-Flash ≈2.0s / Qwen3-30B-A3B ≈3.4s / Qwen3.8-27B 43s（带思考，弃用）→ 选 GLM-5.3 |
| 主动消息重复率 | 加去重闸门前后：08:43 与 09:43、14:42 与 19:42 等重复组 → 0 |
| 上游欠费（HTTP 402） | 降级为固定兜底文案，并**跳过记忆与日记写入**，避免把"脑子转不动"当成她说过的话记进记忆 |

### 已知不足（不粉饰）

- **没有多步规划与工具链编排**：对话本质是"组装上下文 → 一次 LLM 调用"，不是 plan-then-act 的循环。这部分是另一个项目 `SandboxAgent` 的范围。
- **记忆一致性靠规则而非状态机**：约定过期要不要顺延、到点有没有办成，目前是提示词约束，不是硬状态。
- **没有自动化评测集**：效果靠每天真实使用观察（上面三个坑都是这么发现的），没有可复跑的打分脚本。

---

## 一、目录结构（每个子项目各归各位）

```
F:\zhixia
├── brain\            ★ 云端大脑（部署到你的云服务器的服务端 + 本地共享核心）
│   ├── paths.py         所有路径的唯一出口（自动以 brain\ 为根，别再手写路径）
│   ├── brain.py         她的脑子：上下文组装、人设注入、API 直出 + 失败兜底
│   ├── server.py        云端 HTTP 服务（手机 App / 桌宠都连它），端口 8788
│   ├── memory_store_v2.py 记忆库（fastembed 向量检索）
│   ├── recap_store.py   历史摘要：聊过的内容压成 日→周→月 三层
│   ├── agenda.py        约定账本：带日期的约定按时间窗强制带，到点自己想起来
│   ├── moments.py       朋友圈
│   ├── stickers.py      表情包
│   ├── vision.py        图片视觉描述（云端 VL）
│   ├── life_engine.py   她自己的生活 + 日记
│   ├── worldgen.py      世界设定生成
│   ├── persona_store.py 人设
│   ├── config\          config.json / world.json / personas\  ← 你要改的都在这里
│   ├── data\            程序自己写的：memory / her_life / journal / chat_history / upload / stickers / run / summary
│   ├── assets\          头像、立绘
│   ├── archive\         已被替换的历史版本（*.bak_before_*），只作考古用
│   └── logs\            watchdog.log
│
├── pet\             ★ 桌宠（本机 Windows 客户端，立绘 + 气泡 + 聊天窗 + 主动搭话）
│   ├── pet.py           主程序（PyQt6）
│   ├── watcher.py       活动监视（你在干嘛 → 决定她要不要主动说话）
│   ├── settings_dialog.py 设置窗口
│   ├── diary_dialog.py  日记窗口
│   ├── watchdog.py      看门狗：桌宠/云端大脑掉了自动拉起（每 5 分钟计划任务调用）
│   ├── remote_brain.py  通过 SSH 隧道连云端大脑
│   ├── zhixia_mod\      立绘素材帧
│   └── launchers\       所有启动脚本（见下方「如何运行」）
│
├── android-app\     ★ 手机 App（安卓工程，WeChat 风格聊天界面）
│   └── app\src\main\assets\chat.html   前端单页（核心 UI，唯一真源）
│
├── venv\            Python 环境（原 unsloth_env312，已改名）
│
├── deploy\          发布物
│   ├── 角色.apk             最新安装包
│   ├── linzhixia_deploy.zip 云端部署包
│   ├── rebuild_apk.py       APK 重新打包脚本
│   ├── sshkeys\             云端部署 SSH 私钥（linzhixia_deploy）
│   └── generated-images\    App 图标等生成图
│
├── docs\            文档（进度诊断 / 记忆与上下文改造说明 / 记忆库清单 / 人设瘦身对照）
│
└── _attic\          阁楼：历史遗留全集中放（2026-09-29 整理，只是挪走，可随时回收）
    ├── 20260921-removed\          最早清掉的一批（老训练脚本 / 弃用网页 / 历史备份 / 杂项）
    ├── 20260929-backup-localmod\  摘掉本地大脑兜底前的 pet + brain 备份
    ├── 20260929-backup-qqcleanup\ QQ 那套清理前的备份
    ├── mobile-web-20260921\       已被 App 取代的手机网页（无任何调用方）
    ├── dup-copies-20260929\       重复副本（_zhixia.apk、_chat.html 各一份）
    └── junk\                      空文件与无意义的中间产物
```

---

## 二、三个子项目的关系

- **桌宠（pet）** 走 API 直出（`config.json` 里的 siliconflow key），说话/记忆/生活引擎都在本机 `pet\` + `brain\`（共享）。
- **手机 App（android-app）** 连 **云端大脑 `server.py`**（部署在你自己的云服务器，端口 8788，需放行安全组）。
- **云端大脑（brain）** 既部署到云，也是本地共享核心：`pet` 通过 `sys.path` 直接 import `brain` 里的 `Brain` / `memory_store_v2` / `life_engine` 等模块。
  - `paths.py` 是路径唯一出口，**自动以 `brain\` 为根**——所以 `config\` / `data\` / `assets\` 都在 `brain\` 下，别再手写绝对路径。

---

## 三、如何运行（全部已更新到新路径）

> 所有脚本都在 `pet\launchers\`，Python 用 `venv\Scripts\pythonw.exe`（无控制台）或 `python.exe`。

| 场景 | 双击 / 运行 |
|------|------------|
| 只开桌宠 | `pet\launchers\run_pet.bat` |
| 开桌宠 + 云端大脑（本地联调） | `pet\launchers\autostart_zhixia.bat` |
| 走云端（SSH 隧道 + 桌宠） | `pet\launchers\run_pet_cloud.bat` |
| 手动跑云端大脑 | `venv\Scripts\python.exe brain\server.py` |
| 看门狗（计划任务每 5 分钟） | `pet\launchers\run_watchdog.bat` |
| 开机自启（无窗口） | 把 `pet\launchers\autostart_hidden.vbs` 放进「启动」文件夹 |

- **重启桌宠**：关掉现有桌宠（托盘/右键退出），再双击 `run_pet.bat`。
- **计划任务**：`LinZhixiaWatchdog` 已改为调用新路径 `pet\launchers\run_watchdog.bat`（当前为「已禁用」状态，需要自启拉起的话在任务计划程序里启用它）。

---

## 四、你要改的配置

- 人设 / 世界 / 记忆目录：`brain\config\`（或直接用桌宠里的「设置」窗口）。
- 模型 / API key / 主动搭话 / 视觉：`brain\config\config.json`。

---

## 五、云端部署备注

- 云端机器上，服务读的是它自己的 `C:\linzhixia\app\`（`version.json` / `app.apk` 在此，本地这份不生效）。
- 本地 `deploy\sshkeys\linzhixia_deploy` 是部署私钥；`run_pet_cloud.bat` 用它建隧道 `18787 → 云端 8788`。
- 改完 `brain\` 代码要同步到云端：用部署包/SSH 把 `brain\` 传上去覆盖即可。

---

## 六、整理约定与归档说明

这几条是为了让目录不再乱长，新增文件前先看一眼：

- **源码目录不留 `.bak`**：被替换的历史版本挪进 `brain\archive\`，文件名带"替换原因"（如 `qq_bot.py.bak_before_qq_retire`）。
- **文档一律进 `docs\`**：根目录只留 `README.md`。含私人内容的（如 `记忆库清单.md`）留在 `docs\` 但加进 `.gitignore`，不公开。
- **不留重复副本**：手机端 UI 的唯一真源是 `android-app\app\src\main\assets\chat.html`，别再往根目录拷一份。
- **两个例外：根目录的 `_chat.html` 和 `_zhixia.apk` 不许动** —— 它们是**打包流水线的输入**，不是历史遗留。
  `_repackage.py` 直接读这两个路径；`_chat.html`（前端母本）与工程里那份 `assets\chat.html` **内容必须一致**，
  改前端时两份都要同步，否则"根目录改好了、装到手机上还是旧的"。
- **废弃文件进 `_attic\` 而不是直接删**：确认没用就挪进去（可反悔），隔很久确认不要了再整目录清掉。
- **`.workbuddy\scripts\`** 是历次改造的临时脚本与云端回传结果，不进仓库。结构见 `scripts\README.md`：
  `tools\` 是**还在用的工具**（投递器、重打包、自检脚本，路径稳定），其余按 `日期-主题\` 分批次存档。
- 原 `F:\Unsloth` 已清空；残留的空目录若删除被系统拦截，可忽略（里面没东西）。
