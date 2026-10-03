# Vigil

一个常驻云端的对话 Agent。云端跑着它的大脑，桌面和手机是纯客户端，
共用同一份记忆、同一套人格。

不是一个能跑通的脚本，而是一个在服务器上连续跑了几个月、记忆跨会话累积、
三个客户端随时接入都是同一个人的进程。

人格完全由 `brain/config/personas/*.json` 定义，仓库不预置任何角色。
角色名不是项目名，也不需要是项目名。

技术栈：Python 3.12 · PyQt6（桌面）· Android WebView（手机）·
LLM 走 OpenAI 兼容接口 · 语音走阿里云百炼（音色由参考音频零样本复刻）

---

## 它能做什么

| | 做什么 | 在哪实现 |
|---|---|---|
| 长期记忆 | 三层：向量检索的事实、按时间的话题回顾、带状态机的约定账本 | `memory_store_v2.py` / `recap_store.py` / `agenda.py` |
| 自主行为 | 主动搭话、生活推演、夜间日记、朋友圈。做不做由模型自己决定 | `proactive.py` / `life_engine.py` / `moments.py` |
| 工具调用 | 手写 function calling：实时位置、天气、快递 | `tools.py` |
| 识图 | 云端 VL 模型把图转成一句话描述再进上下文 | `vision.py` |
| 语音 | 她想用声音说时发语音条，音色可热切换 | `voice.py` |
| 生图 | 参考图锁脸，同一个人不换脸 | `imggen.py` / `selfie.py` |
| 表情包 | 聊天里甩表情包与趣图 | `stickers.py` / `randimg.py` |
| 人格 | 全部字段可在 App 里改，含关系定位（自由文本，不枚举），热更新 | `persona_store.py` |
| 可观测 | 每轮落一行结构化 trace：命中哪些记忆、各块多少字、耗时与 token | `/api/trace` |

多客户端共用一份大脑：桌面走 SSH 隧道，手机直连云端，桌宠和 App 都是纯客户端，
自己那份 localStorage 只存界面状态。

---

## 快速开始

```bash
# 云端大脑
pip install -r brain/requirements.txt
cp brain/config/config.example.json brain/config/config.json   # 填 api_key / token
python brain/server.py                                          # 监听 :8788

# 桌面端
ssh -L 18787:127.0.0.1:8788 <user>@<server>       # 或双击 pet/launchers/run_pet_cloud.bat
pip install -r pet/requirements.txt
python pet/pet.py

# 手机
python deploy/rebuild_apk.py                     # gradle 全量构建
# 装好后「我 → 设置」里填服务地址与 token（只需填一次）
```

`fastembed` 第一次跑会下载 bge-m3 模型，需要联网。

---

## 架构

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

大脑只有一份，在云端。`brain/server.py` 原来是个 2200 行的单文件，
按领域拆成 runtime / reply / randimg / selfie / imggen / proactive /
moment_flow / jobs 八个模块，对外接口零变化。

### 模块

| 模块 | 作用 |
|---|---|
| `brain/server.py` | HTTP 服务与全部路由：装配大脑、聊天管线 |
| `brain/brain.py` | 决策核心：上下文组装、人设注入、失败兜底 |
| `brain/persona_store.py` | 人设：白名单字段、关系档、身份底线 |
| `brain/memory_store_v2.py` | 长期记忆，bge-m3 向量检索 |
| `brain/recap_store.py` | 历史摘要，日→周→月三层滚动压缩 |
| `brain/agenda.py` | 约定账本：到期提醒，过期标 missed |
| `brain/tools.py` | 工具调用（手写标签，不用原生 function calling） |
| `brain/life_engine.py` | 她的生活推演与日记 |
| `brain/moments.py` | 朋友圈 |
| `brain/voice.py` / `imggen.py` / `selfie.py` | 语音 / 生图 / 自拍 |
| `brain/features.py` | 功能开关，App 远程控制 |
| `brain/errlog.py` | 前后端错误日志 |
| `pet/` | Windows 桌宠（PyQt6） |
| `android-app/` | Android 客户端 |

---

## 设计与取舍

完整的取舍、踩过的坑、实测数据见
[`docs/设计与取舍.md`](docs/设计与取舍.md)。挑几条最有代表性的：

**工具调用用手写标签，不用原生 function calling。** 当时只有 3 个工具、
一次一个、最多两轮，标签够用。代价是拿不到并行调用和参数 schema 校验。
要扩到十个以上工具，这个选择就该反过来。

**长期记忆分三层，不靠每轮全发。** 语义检索会漏召回（"票买了吗"压根不进
检索），所以加了按时间的话题回顾和按日期强制注入的约定账本。
带日期的约定不靠语义碰运气，快到那天了无论聊什么都会带上。

**日期解析取"白名单 + 宁可漏不可猜"。** 用大模型解析日期更灵活，但错误率高
且不可复现。漏一条约定无害，编错一个日期会把已经过去的日子说成还没到。

**主动消息她自己决定发不发。** 模型回"无"就不发。真人不会每小时主动找人，
宁可少发也不要刷屏。

**原生 function calling 是为准确率，不是为省 token。** JSON Schema 比自然语言
更啰嗦，两者换来的是解析可靠性。

**上下文预算怎么花。** 每轮注入的块里，输出风格约束占 34%、历史摘要 16%、
人设 15%。风格约束砍不得（砍了她就开始客服腔），摘要砍不得（砍了她会失忆）。
真正该省的是不该常驻的东西 —— 日记正文只在他问到时才带，
时间戳说明只在真打了标签时才解释格式。

---

## 更新日志

完整版见 [`CHANGELOG.md`](CHANGELOG.md)。

**2026-10-03 · 项目改名**
Agent 定名 **Vigil**（值夜 —— 你睡了她还在）。人设名仍是人设自己的事，
与项目名无关。App 显示名、网页标题、相册目录、GitHub 仓库名同步。
包名不变，手机上是覆盖升级。

**2026-10-03 · 人设改成完全自定义**
6 套预设删除，关系定位从"朋友 / 伴侣"两档枚举改成自由文本。
App 里「她是谁」直接进编辑器，9 个字段随便改，改完热更新。

**2026-10-03 · App v4.9**
模型密钥不再回显到客户端，打码串也不会被写回覆盖真 key；
版本号收敛到一处，修掉"一直提示更新"；设置页底部显示当前版本号。

**2026-10-03 · 服务端**
关系定位从提示词里抽成独立维度（此前"不是恋人"写死在权重最高的位置，
人设文件里写什么都压不过它）；历史摘要超预算时优先保最近的；
图片通道放开选择权（原来只教了她一种图，其余选项写上去会被静默删掉）。

**2026-10-02 · 服务端**
工具调用上线（实时位置 / 天气 / 快递）；2200 行单文件按领域拆成 8 个模块；
修掉 UAPI token 从接入起就没生效的坑。

**2026-10-02 · App v4.8**
长按聊天图片可加入表情包，长按表情包删除；发现页新增快递管理；
语音条下方直接显示转写文字。

**2026-10-01 · 服务端**
校历系统（法定节假日 + 调休，寒暑假按学期起止推导）；
语音平台换成阿里百炼，音色由参考音频零样本复刻。

---

## 文档

| 文档 | 解决什么问题 |
|---|---|
| [`设计与取舍.md`](docs/设计与取舍.md) | 能力构成、踩过的坑、实测数据、已知不足 |
| [`记忆与上下文改造说明.md`](docs/记忆与上下文改造说明.md) | 三层记忆为什么这么分、约定账本的状态机 |
| [`时间感与主动性修复.md`](docs/时间感与主动性修复.md) | 她分不清"刚说的"和"六小时前说的"怎么修的 |
| [`语音平台成本对比.md`](docs/语音平台成本对比.md) | 各家月成本换算，两个容易看错的计费口径 |
| [`游戏内录音取样指南.md`](docs/游戏内录音取样指南.md) | 想换游戏角色音色时怎么录、哪些角色没有公开数据集 |
| [`人设瘦身对照.md`](docs/人设瘦身对照.md) | 提示词瘦身前后的逐条对照与验证方法 |

---

## 配置

- 人设 / 世界：`brain/config/personas/` 与 `world.json`
- 模型、API key、主动搭话、`brain_remote`：`brain/config/config.json`
- 版本号：`version.json`（构建时注入到 APK 与 App）
- 客户端地址与 token：App 设置页填一次，存在本地

---

## 云端部署

云上是 Windows-only（单实例守卫走 `CreateMutexW`），扁平结构 `C:\linzhixia\`。
改完 `brain/` 下的代码要同步到云端并重启进程。

`deploy/rebuild_apk.py` 构建 APK；`tools/` 下另有发布与同步脚本（含真实服务器
信息，不进公开仓库，副本在私有仓库）。

---

## 跑测试

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t tests
```

226 个用例，不联网、不读真实配置、不花钱：embedding 换成确定性假向量，
文件类用例走临时目录。CI 在每个 push 上跑这套用例 + 一次全仓 `compileall`。

新增用例当天就靠它抓出过 4 个真 bug（人设静默回退、trace 锁竞争、
`pending_rows` 的分支从未生效、密钥泄露）。

---

## 整理约定

- 源码目录不留 `.bak`，被替换的版本进 `brain/archive/`
- 文档一律进 `docs/`，根目录只留 `README.md`
- 手机端 UI 唯一真源是 `android-app/app/src/main/assets/chat.html`，
  根目录 `_chat.html` 是打包流水线输入，两份内容必须一致
- 废弃文件进 `_attic/`，不直接删

---

## 安全说明

仓库内不含 API key、服务器地址、鉴权 token 与私钥。部署前按
`brain/config/config.example.json` 自建 `config.json`，并在客户端设置页填
服务地址与 token。

`brain_token` 没配时远程 API 只接受本机回环；同时配了 `bind_host: "0.0.0.0"`
的话进程会拒绝启动。JSON 接口用 `Authorization: Bearer <token>`，
URL 上的 `?token=` 只为 `<img>` / `<audio>` 这类设不了头的媒体请求保留。

服务端目前是纯 HTTP，所以 Android 侧开着 `usesCleartextTraffic`。
要收掉得先上 HTTPS。

---

## 许可

MIT，见 [LICENSE](LICENSE)。
