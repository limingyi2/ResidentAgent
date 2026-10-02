# ResidentAgent —— 常驻云端的跨端 LLM Agent 系统

一个 7×24 运行在云服务器上的对话 Agent：云端大脑负责 LLM 调用、长期记忆（向量检索）、
工具调用（生图 / 写日记 / 发朋友圈）与定时自主触发；Windows 桌宠（PyQt6）与 Android App（WebView）
是两个**纯客户端**，共用同一套大脑、同一份记忆。

> 项目名里的 Resident 指的是"常驻"：它不是一个能跑通的脚本，而是一个在云服务器上连续运行数周、
> 记忆跨会话累积、三个客户端随时接入都是同一个人格的进程。
> 内置角色代号「角色」，她的人格由人设文件定义，支持热重载切换 —— **角色名不是项目名**。

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

`docs\` 下其余文档：

| 文档 | 内容 |
|---|---|
| [`设计与取舍.md`](docs/设计与取舍.md) | 能力构成、踩过的坑、实测数据、已知不足 |
| [`语音平台成本对比.md`](docs/语音平台成本对比.md) | 腾讯 / 阿里 / 火山 / 硅基 / MiniMax 等平台月成本换算过程、两个容易看错的计费口径 |
| [`游戏内录音取样指南.md`](docs/游戏内录音取样指南.md) | 想给她换个游戏角色音色时，怎么录、录什么、哪些角色公开数据集里根本没有 |
| [`记忆与上下文改造说明.md`](docs/记忆与上下文改造说明.md) | 三层记忆（事实 / 话题回顾 / 约定账本）的来由 |
| [`时间感与主动性修复.md`](docs/时间感与主动性修复.md) | 相对时间污染记忆、主动搭话重复这两类问题的修复记录 |

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

## 更新日志

只记重要变更；App 版本号是安装包内的 `APP_CODE`，手机端"检查更新"据此提示。

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
