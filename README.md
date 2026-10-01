# ResidentAgent —— 常驻云端的跨端 LLM Agent 系统

一个 7×24 运行在云服务器上的对话 Agent：云端大脑负责 LLM 调用、长期记忆（向量检索）、
工具调用（生图 / 写日记 / 发朋友圈）与定时自主触发；Windows 桌宠（PyQt6）与 Android App（WebView）
是两个**纯客户端**，共用同一套大脑、同一份记忆。

> 项目名里的 Resident 指的是"常驻"：它不是一个能跑通的脚本，而是一个在云服务器上连续运行数周、
> 记忆跨会话累积、三个客户端随时接入都是同一个人格的进程。
> 内置角色代号「角色」，她的人格由人设文件定义，支持热重载切换 —— **角色名不是项目名**。

**技术栈**：Python 3.12（`httpx` / `fastembed`+bge-m3 / `numpy` / `Pillow`）· PyQt6 · Android（WebView 单页 + 原生轮询服务）· LLM 走 OpenAI 兼容接口（SiliconFlow 等，可在设置里换服务商与模型）

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
| `brain/server.py` | 云端 HTTP 服务（端口 8788，token 鉴权）+ 几条自治循环的调度 |
| `brain/brain.py` | 决策核心：上下文组装、人设注入、API 失败兜底 |
| `brain/memory_store_v2.py` | 长期记忆：bge-m3 向量检索，维度变化时索引自动重建 |
| `brain/recap_store.py` | 历史摘要：聊过的内容按 日→周→月 三层滚动压缩，历史不会越滚越长 |
| `brain/agenda.py` | 约定账本：带日期的约定按时间窗强制注入上下文，到点她自己会想起来 |
| `brain/life_engine.py` | 她自己的生活推演与夜间日记 |
| `brain/moments.py` | 朋友圈 |
| `brain/vision.py` | 图片视觉描述（云端 VL 模型转成一句话） |
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
│   ├── server.py        云端 HTTP 服务（手机 App / 桌宠都连它），端口 8788
│   ├── memory_store_v2.py 记忆库（bge-m3 向量检索）
│   ├── recap_store.py   历史摘要：聊过的内容压成 日→周→月 三层
│   ├── agenda.py        约定账本：带日期的约定按时间窗强制带，到点自己想起来
│   ├── moments.py       朋友圈      stickers.py   表情包
│   ├── vision.py        图片视觉描述（云端 VL）
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
│   ├── watchdog.py      看门狗：桌宠 / 云端大脑掉了自动拉起（计划任务每 5 分钟）
│   ├── remote_brain.py  通过 SSH 隧道连云端大脑
│   ├── requirements.txt 依赖清单
│   ├── zhixia_mod\      立绘素材帧
│   └── launchers\       所有启动脚本（见「如何运行」）
│
├── android-app\     ★ 手机 App（安卓工程，微信风格聊天界面）
│   └── app\src\main\assets\chat.html   前端单页（核心 UI，唯一真源）
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

## 许可

MIT，见 [LICENSE](LICENSE)。
