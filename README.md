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
