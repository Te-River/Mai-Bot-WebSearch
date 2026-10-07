# Mai-Bot-WebSearch 调研与实施计划

> 调研基于：MaiBot 官方文档（更新 2026/10/06）、MaiBot 主程序源码（sparse checkout）、`maibot-plugin-sdk` 2.10.0、MaiBot 1.3.5、两个参考实现，以及本轮**实测**的萌娘百科接口结论。

## 0. 结论摘要

**产品定义：一个跨模态检索矩阵，四条通路。**

| # | 通路 | 输入 → 输出 | 实现 |
| --- | --- | --- | --- |
| 1 | **文搜文** | 文本 → 文本结果 | `web_search`：多引擎并发 + 加权 RRF 融合 |
| 2 | **文搜图** | 文本 → 图片 | `image_search`：图片引擎 → 缩略图 → 发到聊天流 |
| 3 | **图搜图** | 图片 → 相似图 | `image_lookup`：反查引擎 → 相似图候选 → 发到聊天流 |
| 4 | **图搜文** | 图片 → 文本 | `image_lookup`（同一反查管线的文本产出）+ 宿主多模态通道 |

**关键设计：图搜图与图搜文共用同一条反查管线**，只是产出段不同（相似图 vs 作品/角色/来源等文本元数据）。
一条管线两种产出，避免两套代码。

**四条不可动摇的约束**（按优先级）：

0. **延迟与轻量**——麦麦是**高频触发的聊天内场景**，用户发完消息就坐在屏幕前等回复。
   插件与其它插件**共享同一个 Runner 子进程**，慢不只是体验问题，是会拖垮插件宿主。
   所有取舍（默认不抓正文、不二次调 LLM、缩略图优先、早返回 + 主动取消、重依赖可选化）都由这条决定。见 §3.9。
1. **联网可靠性**（代理、超时、限流、熔断、引擎健康度）——"麦麦连不上网"的真实痛点。
2. **结果质量**（融合、去重、CJK 查询保护、正文抽取）。
3. **媒体落地**（图片要下载、校验、去重，并按"发给用户"还是"给模型看"走两条通道）。

**两条本轮实测得到的关键结论**（决定了实现方式，不是猜测）：

* **萌娘百科**：`article HTML` 与 `action=raw` 都是 **403**，`list=search` / `action=parse` / `rest.php` 全部被拒。
  但 **`action=opensearch`（标题匹配）与 `prop=extracts&explaintext=1`（纯文本正文）可用且返回极干净**——
  于是萌娘百科的接入方式是**两次 JSON 调用、零 HTML 解析、零 Cookie**，既满足需求又完全符合轻量约束。详见 §3.6。
* **MaiBot 已经能把用户图片的 base64 直接交给插件**：`message_utils.py` 把 `ImageComponent` 序列化为
  `{"type":"image","data":<平台URL>,"hash":<sha256>,"binary_data_base64":<b64>}`，
  且 `ctx.message.*` 系列能力支持 `include_binary_data=True` 显式取二进制。
  **图搜图/图搜文不缺输入通道**。详见 §3.7。

---

## 1. 调研结果

### 1.1 插件运行时（官方约束）

| 项目 | 事实 |
| --- | --- |
| 架构 | Host / Runner 双进程；第三方插件在 `plugins/` 下由 Third-party Supervisor 拉起，msgpack RPC（UDS / NamedPipe / TCP） |
| 必需文件 | `_manifest.json`、`plugin.py`（含 `create_plugin()`）；可选 `webui.json`、`i18n/`、`assets/` |
| 必需方法 | `on_load()`、`on_unload()`、`on_config_update()`，缺一即拒绝加载 |
| 组件 | `@Tool`、`@Command`、`@HookHandler`、`@EventHandler`、`@API`、`@MessageGateway`、`@HomeCard`、`@LLMProvider`；`@Action` 仅兼容旧代码，**本项目不使用** |
| 能力授权 | manifest `capabilities` 必须在 Host 注册表内（共 **73 项**），**多写一项即拒绝激活** |
| Python 依赖 | 写在 manifest `dependencies`（`type: python_package`），Host 按主程序 `pyproject.toml` 的镜像源自动安装；**与主程序约束无交集则阻止加载** |
| 配置 | `PluginConfigBase` + `Field` 声明于代码，Runner 生成并在插件目录维护 `config.toml`（`.gitignore` 需含 `/config.toml`） |
| 持久化目录 | SDK ≥ 2.6.0：`ctx.paths.data_dir`（跨重启）、`ctx.paths.runtime_dir`（临时/缓存） |
| 版本 | SDK 最新 **2.10.0**（requires-python ≥ 3.10）；MaiBot 最新 **1.3.5** |

会用到的能力：`send.text` / `send.image` / `send.hybrid`、`llm.generate`（按任务路由到宿主模型，
`vlm` = 视觉）、`config.get`、`message.get_by_id`、`tool.get_definitions`、`render.html2png`、`component.enable/disable`。

关键 API：

* Tool 返回媒体必须走 `content_items`（`data`/`uri`、`mime_type`、`name`、`description`），**不要**把 base64 塞进文本。
  宿主会把它拆成"文本 Tool Result + 一条普通 user 图片消息"，**由宿主按模型 `visual` 能力决定是否真的带图**——
  这是官方支持的多模态通道，不要自己拼多模态 prompt。
* Tool 返回值可带 `stop_after_execution: bool`。
* Tool 处理函数 `kwargs` 里有 `stream_id` 与原始 `message` dict。

### 1.2 三个必须知道的工程约束（踩坑清单）

1. **组件 RPC 默认 60s 超时**。`timeout_ms` 必须写在**组件声明 metadata 的顶层**（覆写 `get_components()` 注入）；
   写在装饰器 kwargs 里会落进嵌套 metadata 不被读取。超时会被宿主截断成一次失败的工具调用。
2. **运行时开关不能删组件**。要支持配置热开关，必须保持组件已注册、只用 `metadata["enabled"]=False` 让它对 planner 不可见；
   从注册表移除后热启用会报"未找到组件"，只能重载插件。
3. **工具名冲突**。`google_search_plugin` 也注册了 `web_search`；同时安装会产生歧义。
   本项目启动时用 `tool.get_definitions()` 自检并打印告警。

### 1.3 现成方案盘点

| 方案 | 覆盖 | 缺口 |
| --- | --- | --- |
| `google_search_plugin` v4.0.3 | web_search（google/bing/sogou/ddg/tavily/you/deepseek）、正文抓取、LLM 总结、image_search Action、缩写翻译、`/google_search_status`、代理配置 | 单引擎链式降级（无融合）、失败返回空内容（静默失败）、无联网诊断、**无图搜图/图搜文**、无自建实例支持、无萌娘百科、12 个依赖包 |
| `kitUIN/PicImageSearch`（Python） | 整合 Google/Yandex/Bing/ascii2d/IQDB/SauceNAO/TinEye 以图搜源 | 是**库不是插件**；依赖树重，与我们"轻量"约束直接冲突 → **借鉴其引擎清单与解析思路，不引入其依赖** |
| `erzaozi/imgS-plugin`（Yunzai 框架） | 以图搜源插件（整合图片识别 API） | 面向其它机器人框架；可借鉴产品交互（命令触发、多引擎聚合、结果排版） |
| MCP 集成（`config/mcp-config.toml`） | 直接接外部 MCP 搜索服务 | 需另起进程/服务；图片落到聊天里仍不方便；普通用户门槛高 |
| 官方无内置联网搜索 | — | 这就是本插件存在的理由 |

### 1.4 参考仓库 `Te-River/Opencode-TeamMode` 的搜索组件（`src/tm/search.ts`）

可直接移植的设计（注释里带 2026-09-14 实网 benchmark 结论）：

* **双通道**：`tm_search`（搜什么）与 `tm_webfetch`（读已知 URL）分离，共用同一套抓取管线与域名策略。
* **引擎表驱动**：每个引擎声明 `kind`（`html` / 各 `*-json`）、`buildUrl`、`prepareQuery`、失败提示。
* **CJK 词组保护**：多词中文查询把含 CJK 字符最多的那个词加双引号，避免中文 SERP 跨页碎片匹配冲垮结果。
* **查询分类 + 并行扇出**：`error-code` / `dev-ecosystem` / `cjk` / `general` 分类选引擎集合并发跑。
* **加权 RRF 融合**：归一化为 `SearchHit` → 按 host+path 去重 → 加权倒数排名融合 → top-10，标注来源引擎。
* **抓取管线**：域名白名单、手动跟随重定向、20s 超时、2MB 上限、内容类型判定。
* **结构化引擎**：npm registry、GitHub repo search、StackExchange、HN Algolia、MediaWiki 用 JSON API（免密钥、稳定）。
* **配额与降级**：StackExchange 记录 `quota_remaining`，耗尽后退回 Bing。
* **实网结论**：HTML SERP 里**只有 Bing 还能返回真实结果**；sogou / so.com / baidu 返回反爬壳；bing 国际版（`ensearch=1`）已死。

→ 架构含义：**不把中文 SERP 抓取当主路径**，主力放免密钥 JSON API + Bing + 可选自建 SearXNG + 萌娘百科专用通路。

---

## 2. 目标与非目标

**目标**

1. 四条通路全部可用：文搜文 / 文搜图 / 图搜图 / 图搜文。
2. 装完即可用（零 API Key 也能搜），有 Key 时质量更好。
3. 网络异常时**明确告知原因与已尝试的引擎**，绝不静默返回空。
4. 一条命令诊断"为什么连不上"：逐个引擎的实时连通性与延迟。
5. **接入萌娘百科**，走实测可用的 `opensearch + extracts` 通路。
6. 图片：搜得到、发得出、不重复刷屏；需要模型看图时走官方多模态通道。
7. 全部核心逻辑离线可测（fixtures，不依赖网络）。

**非目标**

* 不做爬虫框架、不做站点级适配器大军（zhihu 等特例按需再加）。
* 不做浏览器渲染（宿主自带浏览器工具/MCP 更适合 JS 页面）；v1 不引入 Playwright。
* **不做默认重路径**：默认不抓正文、不做二次 LLM 总结——需要时由模型显式调用深路径工具。
* 不替代宿主的记忆/知识库（`ctx.knowledge` 是另一件事）。
* 不修改 MaiBot 主程序。

---

## 3. 架构设计

### 3.1 目录结构

```text
Mai-Bot-WebSearch/                 # 仓库根 = 插件目录（可直接放进 plugins/）
├── _manifest.json
├── plugin.py                      # 装配 + 组件声明（保持薄）
├── config.py                      # PluginConfigBase 配置模型
├── core/
│   ├── http.py                    # httpx 客户端、代理解析、限流、熔断、重试、连接池
│   ├── ssrf.py                    # URL 规范化 + 内网/元数据地址拦截
│   ├── cache.py                   # TTL + LRU（查询、正文、图片 hash）
│   ├── budget.py                  # 全局截止 / 早返回 / 并发信号量 / 背压
│   └── errors.py                  # 统一错误 → 用户可读文案
├── search/
│   ├── types.py                   # SearchRequest / SearchHit / Provider 协议
│   ├── router.py                  # 查询分类 + 引擎选择 + 并行扇出
│   ├── fusion.py                  # 归一化 + host+path 去重 + 加权 RRF
│   ├── query.py                   # CJK 词组保护、限定符折叠、site: 支持、空白归一
│   └── providers/                 # bing.py searxng.py duckduckgo.py tavily.py brave.py
│                                  # github.py stackexchange.py hn.py wikipedia.py npm.py
│                                  # moegirl.py  ← 专用通路（§3.6）
├── reading/
│   ├── fetcher.py                 # 重定向/超时/体积/类型 管控
│   └── extract.py                 # trafilatura → readability → 内置 三级降级
├── images/
│   ├── inbound.py                 # 入站图片获取（三级回退，§3.7）
│   ├── providers/                 # bing_images.py ddg_images.py searxng_images.py you_images.py
│   ├── reverse/                   # saucenao.py ascii2d.py iqdb.py bing_visual.py yandex.py
│   ├── picker.py                  # 尺寸/类型/可达性校验 + 内容哈希去重
│   └── delivery.py                # send.image（发给用户） / content_items（给模型看）
├── tools/
│   ├── web_search.py              # ① 文搜文
│   ├── image_search.py            # ② 文搜图
│   ├── image_lookup.py            # ③ 图搜图 + ④ 图搜文（一条反查管线，两种产出）
│   └── read_url.py                # 深路径：读指定 URL
├── commands/status.py             # @Command /websearch（status|ping|engines|moegirl）
├── tests/
│   ├── fixtures/                  # 录制的 SERP / 正文 / 图片索引 / 萌娘百科响应
│   ├── conftest.py                # fake ctx 测试夹具（§8）
│   ├── test_*.py                  # pytest，离线
│   ├── live_probe.py              # 手动实网引擎质量探针
│   └── bench.py                   # 延迟/内存门禁（§3.10）
├── pyproject.toml                 # 仅开发工具（ruff/pytest），不声明运行时依赖
├── requirements-dev.txt
├── .github/workflows/ci.yml
├── README.md
├── LICENSE
└── .gitignore                     # 含 /config.toml
```

**打包与加载约束（已从 `plugin_loader.py` 源码确认）**

宿主用 `importlib.util.spec_from_file_location(..., submodule_search_locations=[<plugin_dir>])`
把插件目录注册成**一个合成包**，并临时把 `src_root` 与 `plugin_parent_dir` 加入 `sys.path`。因此：

* **子目录必须都有 `__init__.py`**（`core/`、`search/`、`search/providers/`、`reading/`、`images/`、`images/reverse/`、`tools/`、`commands/`、`tests/`）。
* 统一使用**相对导入**（`from .config import ...`、`from .core.http import ...`）——
  这也是 `google_search_plugin`（带 `pipelines/`、`search_engines/` 子包）的实际做法。
* 入口固定 `plugin.py`，工厂函数固定 `create_plugin()`。

### 3.2 抽象层（可替换、可测试）

```python
@dataclass(slots=True)
class SearchHit:
    title: str; url: str; snippet: str
    engine: str; rank: int
    published_at: str | None = None
    extra: dict[str, str] = field(default_factory=dict)   # stars / score / tags

class SearchProvider(Protocol):
    name: str
    kind: Literal["html", "json"]
    requires_key: bool
    def prepare(self, q: str) -> str: ...                 # CJK 保护 / 限定符折叠
    async def search(self, req: SearchRequest, http: HttpClient) -> list[SearchHit]: ...
```

* provider 只负责"构造请求 + 解析成本地 hit 列表"，不关心融合排序。
* 解析函数接受 `str`（HTML/JSON 文本）而非 response 对象 → **fixtures 直接喂字符串即可单测**。

### 3.3 文搜文管线

```
query ─ normalize ─ classify ─ select providers ─ asyncio.gather(限流/熔断/超时)
      ─ normalize hits ─ dedupe(host+path) ─ weighted RRF ─ top-K
      ─ [可选] 抓正文(Top-N) ─ [可选] LLM 总结 ─ 文本结果(含来源 URL)
```

* 分类：`error-code`、`dev-ecosystem`、`cjk`、`general`、`news`、`academic`、**`acg`（ACG 语境 → 提升萌娘百科权重）**。
* 融合权重可配置；起步 `general+CJK`：`bing 0.4 / moegirl 0.3 / searxng 0.2 / ddg 0.2`，用实网探针校准。
* 限流：每引擎令牌桶；熔断：连续 N 次失败 → 打开 5 分钟半开重试，状态在 `/websearch status` 可见。
* v1 缓存：进程内 TTL（查询 10 分钟、正文 15 分钟）。

### 3.4 阅读管线（`read_url`，深路径）

* URL 规范化（去 fragment、去跟踪参数），仅允许 `http/https`。
* **SSRF 防护**：解析目标 IP，拒绝 loopback / 私有段 / link-local / 云元数据 `169.254.169.254`，
  **每一跳重定向都重新校验**（最多 5 跳，手动跟随）。
* 20s 超时、1MB 上限（流式读取，超限即断）、`Content-Type` 白名单（html/plain/json/xml）、编码归一。
* 正文抽取三级降级：`trafilatura` → `readability-lxml` → 内置 `selectolax` 兜底。
* **特殊短路**：URL 落在 `*.moegirl.org.cn` 时**不走 HTML 抓取**（实测 403），改走 §3.6 的
  `opensearch` 解析标题 + `extracts` 取正文。

### 3.5 文搜图管线（`image_search`）

```
query ─ 图片引擎并发 ─ 候选(url, w, h, source) ─ 过滤(类型/最小尺寸/去重)
      ─ 下载(Referer + UA + 体积上限 + 重试下一个候选) ─ 模式分发
```

| 模式 | 触发场景 | 实现 |
| --- | --- | --- |
| **交付** | "来张猫图"、"搜张风景图" | `ctx.send.image(b64, stream_id)`（或 `send.hybrid` 带来源文字） |
| **观察** | "这张图里是什么"、"这几张哪张更好" | Tool 返回 `content_items`，交宿主按模型 `visual` 能力决定是否带图 |

* **缩略图优先**：图片 SERP（Bing `turl` / DDG `thumbnail`）自带小图直链，聊天发图**默认走缩略图**
  （通常 20–60KB），原图（数 MB）只在显式请求时才下。高频场景下性价比最高的一次延迟优化。
* 交付模式**不阻塞在"下载全部候选"上**：候选按"体积小 + 尺寸达标"排序后顺序尝试，
  第一个下载成功即发送并返回，剩余候选直接放弃。
* 去重：`sha256` 内容哈希 + 每 query 30 分钟窗口，池子上限防内存泄漏。
* 反爬：下载时补 `Referer`/`UA`；`curl_cffi`（若已安装）用于 TLS 指纹敏感的源；失败候选自动顺延。
* 合规：只发不存档（或仅 `runtime_dir` 短期缓存并定期清理）。

### 3.6 接入萌娘百科（本轮实测，专用通路）

**实测结论（2026，chrome UA）**

| 入口 | 结果 |
| --- | --- |
| `GET /初音未来`（文章 HTML） | ❌ **403 禁止** |
| `GET /index.php?search=初音未来`（搜索页 HTML） | ❌ **403 禁止** |
| `api.php?action=raw` | ❌ 403 |
| `api.php?action=query&list=search` | ❌ `action-notallowed / Unauthorized API call` |
| `api.php?action=query&list=prefixsearch` | ❌ `action-notallowed` |
| `api.php?action=parse` | ❌ `action-notallowed` |
| `rest.php/v1/search/page` | ❌ 返回"萌娘认证系统"页（认证墙） |
| `api.php?action=query&meta=siteinfo` | ✅ 200（`sitename=萌娘百科`） |
| **`api.php?action=opensearch&search=<q>`** | ✅ **200，返回标题候选**（`初音` → `初音未来`、`初音未来(世界计划)`…） |
| **`api.php?action=query&prop=extracts&explaintext=1&titles=<t>`** | ✅ **200，纯文本正文**（`初音未来` 全文约 91KB，`exintro=1` 取简介） |

**因此萌娘百科通路 = 两次 JSON 调用：**

```
query ─ opensearch(取候选标题) ─┬─ 命中 ─→ extracts(explaintext) ─→ 正文 → 结果
                                └─ 零命中 ─→ 用通用引擎 site:zh.moegirl.org.cn 定位标题 ─┘
```

* **零 HTML 解析、零 Cookie、零 JS**：正好同时满足"接入萌娘百科"与"轻量快"两个约束。
* **必须处理的真实限制**：`opensearch` 是**标题前缀匹配**，对自然语言零召回——
  实测 `search=初音未来是什么` 返回 `[[],[],[]]`。所以**必须有 site: 兜底**：
  自然语言查询先用通用引擎查 `site:zh.moegirl.org.cn`，拿到标题再走 `extracts`。
* `extracts` 支持多标题（`titles=A|B`）并对不存在的标题返回 `"missing":""`，可优雅降级。
* **禁止尝试 HTML 抓取**：403 会白烧预算并污染熔断器状态；`reading/fetcher.py` 对该域名直接短路。
* 建议用**礼貌 UA**（带插件名与仓库地址），降低被进一步限制的风险。

### 3.7 图搜图 / 图搜文（一条反查管线，两种产出）

**① 入站图片获取（三级回退，已从 MaiBot 源码确认结构）**

源码 `plugin_runtime/host/message_utils.py` 把 `ImageComponent` 序列化为：

```python
{"type": "image", "data": component.content,        # 平台图片 URL（QQ 场景通常公网可达）
 "hash": component.binary_hash,                      # sha256
 "binary_data_base64": "<b64>"}                      # include_binary_data=True 时附带
```

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | Tool `kwargs["message"]["raw_message"]` 里的 image 段 | 直接拿到 **URL + base64 + hash**；有公网 URL 时优先用 URL（免上传，最快） |
| 2 | `ctx.message.get_by_id(message_id, stream_id=..., include_binary_data=True)` | 能力已确认支持该参数（`capabilities/data.py` 读取 `include_binary_data`） |
| 3 | `Images` 表：`image_hash`(sha256) → `full_path`(项目内相对路径)，经 `ctx.db.get("Images", ...)` | 兜底；仅在 1/2 都拿不到字节时使用 |

* 优先用**平台 URL** 做 `uploadbyurl` 类反查（免上传，省一次 multipart），拿不到再用 base64 上传。
* 入站 base64 会让 RPC 载荷变大 → 取到后立刻算 hash 存缓存，避免重复传递。

**② 反查引擎（按"免密钥优先、稳定性优先"排序）**

| 引擎 | 鉴权 | 适用 | 备注 |
| --- | --- | --- | --- |
| **ascii2d** | 免密钥 | ACG 插画 | `search/url/<url>` 与 `search/file`（multipart） |
| **IQDB** | 免密钥 | ACG 插画 | 表单上传 + HTML 结果 |
| **Bing Visual Search** | 免密钥（上传端点） | 通用 | `images/searchbyimage/upload` multipart → 结果页 |
| **Yandex Images** | 免密钥 | 通用（最强） | `images/search?rpt=imageview&url=<url>` |
| **SauceNAO** | 可选 Key | ACG 精确来源 | JSON API（`output_type=2`），免费额度有限 → 记配额并降级 |

* 与 QQ/ACG 场景高度契合：ascii2d / IQDB / SauceNAO 正是"这张图出自哪部作品/哪个角色"的主力。
* **借鉴 `kitUIN/PicImageSearch` 的引擎清单与解析思路，但不引入其依赖**（依赖树重，违反轻量约束）。
* ⚠️ **诚实标注**：上表引擎的**实际可用性尚未实测**（本轮只实测了萌娘百科）。
  P4 的第一件事就是 `tests/live_probe.py` 实测这四个免密钥端点，谁可用留谁，不可用的从默认集移除。

**③ 两种产出**

| 产出 | 对应需求 | 形态 |
| --- | --- | --- |
| **相似图列表** | 图搜图 | 候选图 → 下载缩略图 → `ctx.send.image` / `content_items` |
| **文本元数据** | 图搜文 | 作品名、角色名、作者、来源页 URL、相似度 → 交给 LLM 作答 |
| （可选）**图像理解** | 图搜文的另一半 | 把原图经 `content_items` 回给宿主，由宿主按模型 `visual` 能力走多模态通道 |

* 「图搜文」的两种语义我们都覆盖：**反查得到文字**（出自哪部作品）与**让模型看图后生成文字**（这张图里有什么）。
  两者实现不同（前者反查引擎，后者宿主多模态通道），在 README 里要说清触发差异，避免用户预期错位。
* 单次调用默认**同时**返回文本命中与 `content_items` 图片候选，让模型自己决定怎么用。

#### 宿主模型配置无关性：为什么绝不调用 `vlm` 任务

**场景**：宿主没配 `[model_task_config.vlm]`，而是把多模态模型直接配在 `planner` / `replyer` 上。
这是**必须支持的常见配置**，结论是：**我们的插件根本不需要 vlm 任务，因为正确做法是压根不调它。**

1. **宿主的视觉判定是自动的**。官方文档明确：*"是否直接发送图片由模型能力自动决定：当任务（如 `planner`）
   配置的模型全部为 `visual = true` 时才启用多模态输入，否则退化为纯文本 + 识图结果"*，
   旧版手动「视觉模式」开关已在 1.3.2 移除。
   → "配了多模态模型但没配 `vlm` 任务"**正是宿主一等公民支持的配置**：宿主会把图片作为真正的多模态输入
   送进去，全程不经过 `vlm` 任务。
2. **所以图像理解走 `content_items`，由宿主自动判定**：有视觉能力就带图给模型，没有就降级为纯文本。
   官方文档描述的正是这套机制（工具返回的媒体被拆成"文本 Tool Result + 一条普通 user 图片消息"）。
   → 副作用是**插件对宿主模型配置完全解耦**：不需要探测、不需要自己降级、不会因为 `vlm` 留空而报错。
3. **反过来，硬编码 `vlm` 会在该场景下自我伤害**：文档明确 `vlm` 留空**不自动回退**，
   "调用方会跳过或报错"。于是"没配 vlm 但配了多模态"时我们的识图调用会失败——而宿主明明有能力看图。

**`llm.generate` 的 `model` 参数语义（SDK 2.10.0 源码，真坑）**

| 传法 | 语义 |
| --- | --- |
| 只传 `model` | Host **先当任务名**解析，未命中再当具体模型名（兼容旧插件） |
| 传 `task_name` | 使用该任务；`model` / `model_name` **都只表示具体模型名** |
| 传 `model_name` | 一律直选具体模型，即使它与某任务同名 |

Host 侧默认任务 = `utils`（`_DEFAULT_PLUGIN_LLM_TASK`）。因此文本生成一律写
`ctx.llm.generate(prompt, task_name="utils")`，语义唯一不歧义；
**绝不写 `model="vlm"`**（那会走"先当任务名解析"的兼容路径，行为取决于宿主是否恰好有同名任务）。

**任务留空的回退规则**：`utils` 是唯一安全默认（`learner` / `fast_model` 留空时回退到 `utils`）；
而 `memory` / `emoji` / `vlm` / `voice` / `embedding` / `image_embedding` 留空**不回退**。

**能力依赖矩阵（四条通路对宿主视觉能力不对称）**

| 通路 | 是否依赖宿主视觉能力 |
| --- | --- |
| ① 文搜文 | 否 |
| ② 文搜图 | 否 |
| ③ 图搜图（反查得到相似图） | 否（纯 HTTP + 解析） |
| ④ 图搜文 —— 反查得到文字 | 否（纯 HTTP + 解析） |
| ④ 图搜文 —— 模型看图生成文字 | **是**（走宿主自动多模态判定，而非 `vlm` 任务） |

→ **即使宿主完全没有视觉能力，四条通路里也只有半个功能降级**，其余照常工作。
该矩阵必须写入 README 与 `/websearch status` 输出。

### 3.8 组件与能力清单

组件（v1）：

| 名称 | 类型 | 说明 | 对应通路 |
| --- | --- | --- | --- |
| `web_search` | `@Tool` | 主搜索；参数 `query`（+可选 `freshness`、`max_results`） | ① 文搜文 |
| `image_search` | `@Tool` | 图片搜索（交付/观察双模式） | ② 文搜图 |
| `image_lookup` | `@Tool` | 反查：入站图片 → 相似图 + 文本元数据 | ③ 图搜图 / ④ 图搜文 |
| `read_url` | `@Tool` | 深路径：读指定 URL 正文并总结 | ① 的补充 |
| `websearch` | `@Command` | `/websearch status\|ping\|engines\|moegirl`，`ping` 实探各引擎 | 诊断 |
| `web_search_api` | `@API` | 可选：给其他插件调用的公开 API | 扩展 |

manifest `capabilities`（**只声明实际用到的**）：

```json
["send.text", "send.image", "send.hybrid", "llm.generate",
 "config.get", "tool.get_definitions", "message.get_by_id"]
```

* `message.get_by_id` 是入站图片的二级回退路径，**必须声明**。
* `component.enable/disable` 仅在实现"配置热开关工具"时才加；`render.html2png` 仅在实现结果卡片时才加。
* `database.get` 仅在启用 `Images` 表兜底（三级回退）时才加——**默认不加**，先用前两级。
* 依赖分两层，**manifest 只声明 Tier 0**（版本用宽松下限，降低与主程序约束冲突的概率）：

  | 层 | 包 | 用途 | 说明 |
  | --- | --- | --- | --- |
  | **Tier 0（必装）** | `httpx` | 全部 HTTP | 纯 Python、连接池 keep-alive，无编译依赖 |
  | | `selectolax` | HTML 解析 | 单 wheel（lexbor），比 `lxml`+`bs4` 轻且快数倍 |
  | **Tier 1（可选）** | `curl_cffi` | TLS 指纹反爬 | 仅在 Bing 被反爬时启用 |
  | | `trafilatura` / `readability-lxml` | 正文抽取质量 | 只服务 `read_url` 深路径 |

  Tier 1 **不写进 manifest**（写了就会被强制安装、拖慢加载），改为运行时 `importlib.util.find_spec()`
  探测 + 优雅降级；README 给出 `pip install trafilatura curl_cffi` 的选装指引。
* 所有重依赖**一律惰性导入**（函数内 import），插件加载路径上不得 import 它们。
* → 开工前先比对主程序 `pyproject.toml` 的 `constraint-dependencies`，能复用宿主已有的包就不重复声明。

### 3.9 配置模型（分段，WebUI 友好）

```toml
[plugin]        enabled, config_version
[network]       proxy_mode(auto|manual|off), proxy, no_proxy, timeout_seconds, verify_tls, user_agents
[search]        default_provider_set, max_results=8, per_engine_timeout=3s, concurrency=4,
                global_deadline=6s, early_return_quorum=2,
                freshness, safe_search, rrf_weights, fetch_top_n=0, cache_ttl=600
[engines.bing]     enabled, region, language
[engines.searxng]  enabled, base_url, categories, language
[engines.moegirl]  enabled=true, api_base, extract_mode(intro|full), polite_ua, site_fallback=true
[engines.tavily] / [engines.brave] / [engines.exa] / [engines.you] / [engines.google_cse]
                   enabled, api_key, ...
[images]        enabled, providers, safe_search=true, min_width=300, min_height=200, max_bytes=3MB, prefer_thumbnail=true,
                repeat_window_minutes=30, delivery_mode
[reverse]       enabled, providers(ascii2d|iqdb|bing_visual|yandex|saucenao), saucenao_api_key,
                # ⚠️ 会把用户图片/URL 发给第三方服务；README 必须声明数据流向，用户可整体关闭
                max_candidates=5, per_engine_timeout=6s, global_deadline=10s, upload_when_no_url=true
[reading]       enabled, max_bytes=1MB, content_timeout=8s, extractor_chain, block_private_hosts
[llm]           summarize=false, task_name="utils", temperature, timeout_seconds, max_prompt_chars
                # task_name 只填文本任务（utils），绝不填 "vlm"；图像理解不经过本插件
[limits]        rpc_timeout_seconds=25     # → 写入组件 metadata 顶层（是天花板，不是目标）
[perf]          max_inflight_searches=3, per_stream_cooldown=5s, queue_overflow_policy=reject_with_notice,
                cache: query=600s/200条, page=900s/32MB, image_hash=1800s/32MB
```

* 代理默认 `auto`：读 `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`/`NO_PROXY`——"连不上网"最常见的一步修复。
* 所有开关默认值遵循"零配置也能出结果"。

### 3.10 性能预算与轻量化设计（高频聊天场景的硬约束）

**核心判断：大多数调用发生在"用户发完消息正在等回复"的窗口里。**
所以"少做一点但马上返回"永远优于"做全但慢 5 秒"。

**延迟预算（验收指标，不是愿望）**

| 路径 | p50 | p95 | 硬截止 |
| --- | --- | --- | --- |
| 插件加载（`on_load` 到就绪） | — | — | ≤ 300ms（无网络 I/O、无重依赖 import） |
| `web_search`（免正文，默认） | ≤ 1.2s | ≤ 3s | 6s |
| `web_search`（显式抓正文） | ≤ 3s | ≤ 6s | 12s |
| `read_url` | ≤ 2.5s | ≤ 6s | 12s |
| **萌娘百科通路**（2 次 JSON） | ≤ 0.8s | ≤ 2s | 5s |
| `image_search`（缩略图 + 发送） | ≤ 2.5s | ≤ 5s | 10s |
| `image_lookup`（反查，含可能的 multipart 上传） | ≤ 4s | ≤ 8s | 12s |
| 命中缓存的重复查询 | ≤ 50ms | ≤ 150ms | — |
| 网络不可用时的失败返回 | ≤ 1s | ≤ 2s | — |

**十个具体手段**

1. **默认不抓正文**（`fetch_top_n=0`）：SERP 的 title+snippet 已足够模型作答；需要全文时由模型显式调用
   `read_url`。这是"快路径 / 深路径"分离，不是能力缺失。
2. **默认不二次调用 LLM**（`summarize=false`）：工具结果会回到麦麦自己的对话模型手里，再由插件调一次 LLM
   总结＝**同一段文字过两遍模型**，白烧 2–10s 和 token。
3. **早返回 + 主动取消**：并发扇出，达到 `early_return_quorum` 或全局截止即返回，用 `TaskGroup` 取消
   未完成请求（迟到结果丢弃并计入引擎健康度）。
4. **连接池常驻**：一个长生命周期 `httpx.AsyncClient`（keep-alive + HTTP/2），省掉每次查询的 TCP+TLS 握手
   （100–300ms）；`on_unload` 中 `aclose()`。
5. **熔断优先于重试**：交互路径上**不**做 3 次指数退避重试。连续失败的引擎直接熔断 5 分钟；
   瞬时错误至多原地重试 1 次且不等待。
6. **先裁剪再解析**：SERP/索引 HTML 截到 512KB、正文页 1MB 才交给解析器，
   避免为几 KB 有效内容解析几 MB DOM（CPU 与内存双杀）。
7. **萌娘百科走纯 JSON**：不解析 HTML、不处理 Cookie（实测 HTML 403），两次调用拿到纯文本正文——
   这条通路是"轻量"要求的最佳范例。
8. **重依赖惰性化**：见 §3.8；`on_load` 不 import 任何解析/反爬库。
9. **内存有界**：三类缓存都设条数或字节上限；图片去重表按 query 整条淘汰（最多 200 个 query、过期即丢）；
   下载走流式 + 字节上限，杜绝一次 100MB 图片把 Runner 打爆。
10. **背压而不是排队**：全局并发上限 `max_inflight_searches=3`（与其它插件**共享 Runner 子进程**，不能独占）；
    超出即快速返回"当前搜索任务较多，请稍后再试"，而不是把请求堆在队列里让用户等更久。

**落地方式**：`tests/bench.py` 记录每条路径的 p50/p95、冷启动 import 时间与常驻内存，
作为 P1 / P3 / P4 的**阶段门禁**——不达标不进入下一阶段。

---

## 4. 实施阶段（每阶段可独立验收）

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| **P0 骨架** | manifest、`plugin.py` 生命周期、配置模型、`__init__.py` 包结构、`pyproject.toml`、CI、`/websearch status`、pytest 骨架 + **fake ctx 夹具** | 插件在真实 MaiBot 中加载无 error；WebUI 配置页可编辑；命令可触发；`ruff` + `pytest` + manifest 校验在 CI 全绿 |
| **P1 联网内核 + 文搜文** | `core/http.py`（连接池/代理/限流/熔断/缓存）、`core/ssrf.py`、`core/budget.py`、`bing`+`searxng`+`duckduckgo` provider、融合去重、早返回取消、`web_search` Tool、`timeout_ms` 顶层注入 | fixtures 单测全绿；**`bench.py` 达标（p50 ≤ 1.2s、p95 ≤ 3s，冷启动 ≤ 300ms）**；聊天中模型能调用并拿到带来源的结果 |
| **P2 阅读 + 萌娘百科** | `read_url`、三级抽取、SSRF/重定向/体积管控；`moegirl` provider（`opensearch` + `extracts` + `site:` 兜底）、ACG 查询分类、`/websearch moegirl` 诊断 | 给任意 URL 返回正文摘要；**"初音未来是什么"能通过兜底拿到萌娘百科正文**；`初音` 走 opensearch 直命中；HTML 403 不被反复尝试；萌娘百科 p95 ≤ 2s |
| **P3 文搜图** | 图片 provider、缩略图优先、下载校验去重、双模式分发、30 分钟去重、**spike：`send.image` 是否接受 URL** | "来张猫图"收到图片且 **p95 ≤ 5s**；模型能"看"返回的图；失败给出明确原因而非静默；缓存在字节上限内 |
| **P4 图搜图 + 图搜文** | `images/inbound.py` 三级回退、`images/reverse/` 引擎、`image_lookup` Tool、两种产出（相似图 + 文本元数据） | **先跑 `live_probe.py` 实测反查引擎并据实定默认集**；用户发图后能拿到作品/角色/来源文本；能发出相似图；无公网 URL 时走 multipart 上传；缓存命中 ≤ 50ms |
| **P5 发布** | README（安装/配置/命令/四通路示例/排障）、i18n、ruff + pytest CI、manifest lint、插件中心提交 | 依据官方发布前检查清单逐项通过 |

每阶段按 PDCA 收口：先写验收用例（Check），再实现（Do），跑探针/实机（Check），修正默认值（Act）。

---

## 5. 测试矩阵

**离线（CI 必跑）**

* provider 解析：fixtures（Bing SERP、DDG lite、GitHub/SO/HN JSON、**萌娘百科 opensearch/extracts 响应**）
  → 断言 hit 数量与字段。
* **萌娘百科专项**：`opensearch` 前缀命中、自然语言零命中后的 `site:` 兜底路径、
  `extracts` 多标题与 `missing` 降级、`exintro` vs 全文。
* 反查解析：ascii2d / IQDB / SauceNAO 结果 HTML/JSON fixtures → 断言作品名、角色名、相似度、相似图 URL。
* 融合：构造多引擎结果，断言去重、RRF 顺序、权重生效、稳定性（同输入同输出）。
* 查询处理：CJK 词组保护、限定符折叠、空/超长查询、`site:` 解析、注入型输入。
* SSRF：私网/loopback/元数据/`file:`/`data:`/重定向到内网 全部拒绝；moegirl 域名短路到 API 通路。
* 入站图片：**三级回退**每级单独覆盖（有 base64、只有 URL、都没有→DB 兜底）。
* **模型配置无关性**：静态断言源码中不出现 `vlm` 作为任务名；断言文本生成调用一律带
  `task_name="utils"`；断言没有任何路径把图片 base64 拼进 `llm.generate` 的 prompt。
* 配置：默认值完整、`Field` 描述非空、`config_version` 存在、manifest 合法 JSON 且无多余字段。
* 图片：尺寸过滤、内容哈希去重、窗口去重、候选全失败时的错误路径。

**性能（`tests/bench.py`，阶段门禁）**

* 冷启动：`on_load` 耗时；断言重依赖未被导入（`sys.modules` 中无 `trafilatura`/`curl_cffi`）。
* 每条路径 p50/p95；用必然超时的假 provider 断言硬截止生效（6s 内返回部分结果而非挂死）。
* 缓存命中延迟；熔断打开后的快速失败；并发上限真的背压而非排队。
* 常驻内存与三类缓存上限是否生效。

**实网（手动，`tests/live_probe.py`）**

* 逐引擎固定 query（中文/英文/错误码/技术栈/ACG）→ 命中数、延迟、是否被反爬。
* **反查引擎可用性实测**（P4 前置）：ascii2d / IQDB / Bing Visual / Yandex，各用 2–3 张已知来源的图。
* 结论回写默认 `rrf_weights`、默认引擎集与默认反查集——**默认值由实测决定，不靠猜**。

**实机（MaiBot 集成）**

* 加载/卸载/配置热重载不报错；卸载后无残留任务与句柄。
* 四条通路各跑一次端到端。
* 模型能发现工具（若发现不了，再评估 `core_tool=True`）。
* 慢查询不被 60s 默认值截断（验证 `timeout_ms` 顶层注入生效）。
* 与 `google_search_plugin` 共存时的工具名冲突告警。

---

## 6. 风险与对策

| 风险 | 对策 |
| --- | --- |
| SERP 反爬随时变化 | 主路径放免密钥 JSON API + Bing + 可选 SearXNG；熔断器 + `live_probe` 定期体检；默认值可配置 |
| **萌娘百科进一步收紧接口**（当前 `list=search`/`parse`/HTML 已全线关闭） | 只用实测可用的 `opensearch + extracts`；礼貌 UA；`site:` 兜底；接口失效时快速降级到通用引擎而非报错 |
| **反查引擎可用性未经验证**（ascii2d/IQDB/Bing Visual/Yandex 均可能改版或反爬） | P4 前置 `live_probe` 实测；多引擎并存 + 熔断；SauceNAO 作可选 Key 兜底；全部失效时明确告知而非静默 |
| Python 依赖与主程序约束冲突 → 插件被拒绝加载 | 开工前比对主程序 `pyproject.toml`；版本用宽松 `>=`；重抽取库全部放 Tier 1 可选层 |
| 组件 60s RPC 超时截断慢查询 | 覆写 `get_components()`，`timeout_ms` 写 metadata 顶层，= 搜索+抓取+LLM 预算，下限 180s（但实际控制靠内部 6s 截止） |
| **高频调用把 Runner 拖慢/拖爆**（与其它插件共享子进程） | 全局并发上限 + 背压快速失败 + 三类缓存上限 + 流式下载限长 + 早返回取消 |
| 入站 base64 图片撑大 RPC 载荷 | 优先用平台 URL 做反查（免上传）；取到 base64 立刻算 hash 入缓存，不重复传递 |
| **误依赖 `vlm` 任务**（宿主可能只配了多模态 planner/replyer 而没配 vlm，此时 vlm 留空**不回退**） | 图像理解一律走 `content_items` 交宿主自动判定；文本生成固定 `task_name="utils"`；单元测试断言代码里永不出现 `vlm` |
| 模型不主动调用工具 | 工具 `description` 写清"何时使用"；必要时 `core_tool=True`；README 给正反例 |
| 「图搜文」语义歧义导致用户预期错位 | README 明确区分：**反查得到文字**（出自哪部作品）vs **模型看图生成文字**；两者由不同路径实现 |
| 轻量化目标被后续功能侵蚀 | 延迟预算写进阶段门禁（`bench.py`），不达标不进下一阶段；重依赖永久留在 Tier 1 |
| 合规与隐私 | 只处理用户显式请求；不落库、不长期落盘；README 声明数据流向 |

---

## 7. 已确认的决策（锁定）

| # | 决策 | 结果 |
| --- | --- | --- |
| 1 | 插件标识 | `id = "com.teriver.mai-websearch"`；`author = { name: "Te-River", url: "https://github.com/Te-River" }`；`urls.repository = "https://github.com/Te-River/Mai-Bot-WebSearch"` |
| 2 | 产品范围 | **四条通路全做**：文搜文 / 文搜图 / 图搜图 / 图搜文 |
| 3 | 零配置默认路径 | **Bing + 免密钥 JSON 引擎**（GitHub / StackExchange / HN / MediaWiki / npm / **萌娘百科**）；SearXNG 为可选 provider |
| 4 | 与竞品共存 | **共存 + 启动自检告警**（`tool.get_definitions()` 检测 `web_search` 是否已被注册） |
| 5 | `host_application` | `min_version = "1.3.0"`，`max_version = "999.999.999"`（官方建议：只认真约束下界） |
| 6 | `sdk` | `min_version = "2.6.0"`（`ctx.paths` 起始版本），`max_version = "2.99.99"` |
| 7 | 萌娘百科接入方式 | `opensearch`（标题）+ `extracts`（正文）+ `site:` 兜底；**禁止 HTML 抓取** |

确定的 manifest 骨架：

```json
{
  "manifest_version": 2,
  "id": "com.teriver.mai-websearch",
  "version": "0.1.0",
  "name": "麦麦联网搜索",
  "description": "为麦麦提供文搜文、文搜图、图搜图、图搜文与网页阅读能力",
  "author": { "name": "Te-River", "url": "https://github.com/Te-River" },
  "license": "MIT",
  "urls": { "repository": "https://github.com/Te-River/Mai-Bot-WebSearch" },
  "host_application": { "min_version": "1.3.0", "max_version": "999.999.999" },
  "sdk": { "min_version": "2.6.0", "max_version": "2.99.99" },
  "plugin_type": "integration",
  "dependencies": [],
  "capabilities": [
    "send.text", "send.image", "send.hybrid",
    "llm.generate", "config.get", "tool.get_definitions", "message.get_by_id"
  ],
  "i18n": { "default_locale": "zh-CN" }
}
```

> **必须准确**：Runner 校验的是"声明的能力名**是否都在 Host 注册表中**"——声明了**未注册**的名字会被拒绝激活，
> 而声明了合法但暂未使用的名字**不会**被拒。所以"用到才加"是**卫生习惯**（减少授权面），不是硬性校验。
> `database.get`（`Images` 表兜底）、`render.html2png`（结果卡片）、`component.enable/disable`（热开关）
> 仍按"用到才加"处理。

---

## 8. 第二轮完整复查：补漏清单

第一次成稿后逐节复查发现并已修正的缺口（每条都已落到上文对应位置）：

| # | 缺口 | 修正 |
| --- | --- | --- |
| 1 | **子包缺 `__init__.py`**，且未确认加载方式 | 已从 `plugin_loader.py` 确认宿主把插件目录注册为合成包（`submodule_search_locations`）→ 相对导入可用，**每个子目录都要 `__init__.py`**（§3.1） |
| 2 | **`@Command` 返回契约未写明** | 已读 `commands.md`：必须返回三元组 `(success: bool, response: str, weight: int)`；kwargs 含 `stream_id`/`matched_groups`/`raw_message`/`message`；支持 `aliases`（§3.8） |
| 3 | **能力校验规则写错** | 纠正：拒绝的是**未注册**的能力名，不是"多余的"声明（§7） |
| 4 | **缺 CI / 开发工具链** | 新增 `pyproject.toml`（仅 ruff/pytest 配置）、`requirements-dev.txt`、`.github/workflows/ci.yml`（§3.1、P0/P5） |
| 5 | **缺 handler 级离线测试手段** | 新增 `tests/conftest.py` 的 **fake ctx 夹具**：纯函数测试之外，还能直接调 Tool/Command 处理函数（§3.1、§5） |
| 6 | **`send.image` 是否接受 URL 未验证** | 若接受则**省掉整次下载**（最大的一次延迟优化）→ P3 列为 spike，失败则回退 base64（P3） |
| 7 | **缺每会话限流** | 群聊里可能被连续触发 → 新增 `perf.per_stream_cooldown=5s`（§3.9） |
| 8 | **图片搜索缺 NSFW 防护** | 群聊场景真实风险 → 新增 `images.safe_search=true` 默认开启（§3.9） |
| 9 | **反查的隐私影响未声明** | 会把用户图片发给第三方 → 配置项加警示注释 + README 必须声明数据流向，可整体关闭（§3.9） |
| 10 | **早返回可能只返回最差引擎的结果** | 新增 `grace_window_ms=800`：达到 quorum 后再给优质引擎一个宽限窗口（§3.9） |
| 11 | **`web_search` 收到 URL 时行为未定义** | 按参考实现处理：识别为 URL 则转交阅读管线，避免模型困惑（§3.3） |
| 12 | **`host_application.min_version` 与 `include_binary_data` 的支持版本无法确认**（浅克隆无历史） | 二级回退路径用 `try/except` 容错，能力不可用时自动降到三级；不把版本当作保证（§3.7） |
| 13 | **配置热更新会泄漏旧 HTTP 客户端** | `on_config_update` 必须 `aclose()` 旧客户端；`on_unload` 取消在途任务、关客户端、清缓存（§5 实机验收） |
| 14 | **萌娘百科全文 91KB 会撑爆上下文** | `extracts` 结果按 `max_content_length` 截断；多标题合并成**一次**调用（2 次请求拿多篇）（§3.6） |
| 15 | **缺 `web_search` 的 URL 输入与 `read_url` 的职责边界** | 已明确：`web_search` 是"我要搜什么"，`read_url` 是"给我某个已知 URL"，`web_search` 收到 URL 时内部转交（§3.3） |
| 16 | **i18n 目录属过度设计** | v1 去掉 `i18n/` 目录，仅保留 manifest 的 `i18n.default_locale`（用户可见文本直接用简体中文）（§3.1） |
| 17 | **日志可能记录完整查询（隐私）** | INFO 级只记引擎名/耗时/命中数；查询词降级到 DEBUG 并截断（§5） |
| 18 | **`send.image` 的体积上限与平台限制** | 出站图片按 `max_bytes` 限制，超大图跳过换下一候选（§3.5） |

**至此确认无阻塞性缺口。** 剩余未验证项只有两个，且都已安排为对应阶段的前置 spike（第 6 条 `send.image` URL、§3.7 反查引擎可用性）——
两者都有明确的回退方案，不会阻塞开发推进。

---

## 9. 实施记录与实测证据

### P1（联网内核 + 文搜文）——已完成

**门禁结果**

| 项目 | 实测 | 预算 |
| --- | --- | --- |
| `pytest` | **171 项全绿**（连跑 5 次无 flaky） | — |
| `ruff check` | 全绿（已启用 `BLE`：兜底捕获必须显式标注） | — |
| `on_load` | **0.1 ms** | ≤ 300 ms |
| Tier-1 重依赖 | 未进入加载路径 | 必须为空 |
| 管线编排 p50 / p95 | **0.05 ms / 0.07 ms** | ≤ 50 / 150 ms |
| 提前返回（慢引擎睡 30s） | **58–63 ms 返回** | ≤ 2000 ms |

**导入成本拆解（各自独立解释器，实测）**

| 依赖 | 耗时 | 结论 |
| --- | --- | --- |
| `maibot_sdk` | 461.6 ms | 宿主本身已加载，不计入我们的边际成本 |
| `httpx` | 286.2 ms | 唯一显著的边际成本；刻意放在模块层，换取"首次搜索不现加载" |
| `pydantic` | 141.1 ms | 宿主已加载 |
| **`selectolax`** | **3.0 ms** | **Tier-0 选型得到实测支持**（`lxml`+`bs4` 通常是百毫秒级） |

→ 插件激活时的**边际**导入成本 ≈ 290 ms，全部来自 httpx；`on_load` 本身是 0.1 ms。

**实网探针（`tests/live_probe.py`，直连环境）**

| 来源 | 结果 | 决策 |
| --- | --- | --- |
| **Bing** | ✅ 3/3 命中，406 / 871 / 1822 ms，各 5 条 | 保持默认启用 |
| **DuckDuckGo** | ❌ **3/3 `ConnectTimeout`（各 ~15 s）** | **新增开关，默认关闭** |
| **萌娘百科 `opensearch`** | ✅ 标题查询命中候选（1902 ms 冷 / 396 ms 热） | 按 §3.6 设计实现 |
| **萌娘百科 自然语言** | ⚠️ `初音未来是什么` → **零候选** | **证明 `site:` 兜底是必需的**（已验证） |
| **萌娘百科 `extracts`** | ✅ 356 ms 取回 446 字符简介 | 通路成立 |

**由实测产生的修正**

1. **DuckDuckGo 从"无条件启用"改为默认关闭的开关**——原实现会在每次搜索里挂一个必然超时 15 秒的源，
   虽然早返回会取消它，但仍会污染 `engine_status` 与用户可见的诊断信息。
2. **萌娘百科的延迟预算补充"冷/热"区分**：首次 1.9 s（含 DNS+TLS），此后 ~0.4 s；
   §3.10 的"≤2s"预算是针对热路径的。
3. **Tier-1 重依赖的检查写成了机器无关门禁**（`bench.py`），任何后续改动把 trafilatura/curl_cffi
   拉进加载路径都会直接失败。

**实现期发现并修掉的真实缺陷（均由测试或实跑暴露）**

| 缺陷 | 影响 | 修法 |
| --- | --- | --- |
| 熔断器半开状态未限制探测并发 | 冷却结束后流量会瞬间全打回坏源，熔断形同虚设 | `allow()` 在 `probing` 时直接返回 `False`，一次只放一个探测 |
| `gather_early` 用"重新读时钟"判断超时归因 | 定时器精度导致 `deadline_hit` 偶发判错（**flaky 测试**） | 改为按"本次等待被截止时间还是宽限窗口截断"显式归因 |
| `host_path_key("")` 返回 `"/"` | 空 URL / 相对路径不会被丢弃，污染结果 | 无主机名时返回空串，由 `fuse` 丢弃 |
| `tree.css("div.result, div.web-result")` | 同时带两个类的节点被匹配两次，**结果数直接翻倍** | 改为单选择器 + 失败回退 |
| `CircuitBreaker` 字段名 `clock` 与内部 `_clock` 不一致 | 一调用就 `AttributeError` | 统一使用公开字段 |
| `selectolax.parser` 在 1.0 已被移除 | 直接 `ImportError` | 改用 `selectolax.lexbor.LexborHTMLParser` |
| `lexbor` 默认 `text()` 会粘连内联标签 | `Hello <b>World</b>` → `HelloWorld`，标题丢空格 | 统一用 `text(separator=" ", strip=True)` |
| `ruff --fix` 曾把 `BLE001` 的 noqa 当多余指令删掉 | 打开 BLE 规则后报错 | 补回并显式启用 `BLE` 规则 |
| 冷启动门禁把 `maibot_sdk` 的导入算进我们的成本 | 预算失真（973ms 报警，但其中 500ms+ 是宿主已付的） | 改为测**边际导入**：先导入 SDK 作基线，只对我们引入的部分设门禁 |

### P2（阅读管线 + 萌娘百科）——已完成

**门禁结果**：`pytest` **270 项全绿**；`ruff` 全绿；`bench --strict` 全过。

| 项目 | 实测 | 预算 |
| --- | --- | --- |
| `on_load` | 0.2 ms | ≤ 300 ms |
| **边际导入** | **289.6 ms**（几乎全是 httpx；`selectolax` 仅 2.7 ms） | ≤ 450 ms |
| Tier-1 重依赖 | 未进入加载路径 | 必须为空 |
| 编排 p50 / p95 | 0.05 / 0.09 ms | ≤ 50 / 150 ms |
| 提前返回 | 59.4 ms | ≤ 2000 ms |

**实网端到端验证（`tests/live_probe.py` 跑真实管线，非单点探测）**

| 场景 | 结果 |
| --- | --- |
| 融合与分类 | ACG 查询下萌娘百科占据第 1–3 位（`acg` 类别权重 0.5 生效） |
| 缓存 | 重复查询 **0.0 ms**，`from_cache=True` |
| `read_url` 普通站点 | `example.com` → `builtin` 抽取，1000 ms，156 字符 |
| **`read_url` 萌娘百科** | **`moegirl-extracts`，843 ms，446 字符正文——成功绕开 HTML 的 403** |
| `初音未来` | `bing ok:10 / moegirl ok:3` |
| `初音未来是什么` | 修复前 `moegirl: 失败`；**修复后 `moegirl ok:3` 且排前三** |

**读源码时确定的读取语义**（官方文档未写明，从源码得出）

宿主的 `SessionMessage` → 插件字典的序列化在 ``plugin_runtime/host/message_utils.py``：
图片段为 ``{"type":"image","data":<平台URL>,"hash":<sha256>,"binary_data_base64":<b64>}``；
``ctx.message.*`` 系列能力支持 ``include_binary_data=True`` 显式索取二进制。
这直接确定了 P4 图搜图/图搜文的入参形态（三级回退）。

**实现期发现并修掉的真实缺陷**

| 缺陷 | 影响 | 修法 |
| --- | --- | --- |
| **`site:` 兜底用完整问句** | 实测 `site:zh.moegirl.org.cn 初音未来是什么` 在 Bing 上找不到萌娘百科页面，**自然语言查询完全拿不到百科正文** | 新增 `search_subject()` 剥离疑问词，并把标题解析改成**三级递进**：原查询 → 主题词 → `site:` 兜底 |
| 站点专用通路的结果未受长度约束 | 萌娘百科全文可达数万字，会吃满模型上下文 | 专用通路结果同样按 `max_content_length` 截断并打标记 |
| 专用通路绕过 SSRF 校验 | 理论上可被利用 | 只在硬编码域名白名单（`moegirl.org.cn`）上匹配，不接受用户输入决定 |
| `parse_extracts` 的 `order` 语义含糊 | 若当作过滤器，会在 MediaWiki 归一化标题（下划线/空格/重定向）时**误删有效结果** | 明确为"只排序不过滤"并写入 docstring 与测试 |

### P3（文搜图）——已完成

**门禁结果**：`pytest` **331 项全绿**；`ruff` 全绿；`bench --strict` 全过。

**前置 spike 的确定答案：`send.image` 只接受 base64。**

读宿主实现（``plugin_runtime/capabilities/core.py::_cap_send_image``）得出结论：
参数被原样传给 ``send_api.image_to_stream_with_message(image_base64=...)``，
**传 URL 会直接失败**。所以"下载整张图"不可避免——
这让**缩略图优先**从优化项变成了主要的延迟手段（缩略图 20–60KB vs 原图数 MB）。

**实网端到端验证**

| 场景 | 结果 |
| --- | --- |
| Bing 图片解析 | 4 条候选（原图 + 缩略图 + 标题 + 来源页） |
| `布偶猫` | `ok`，2485 ms，479 KB JPEG |
| `赛博朋克城市` | `ok`，1155 ms，338 KB JPEG |

**实网暴露的真实问题与修正**

实测发现下载到的都是**原图**（479 KB / 338 KB），缩略图优先没有生效。诊断后确认根因：

> Bing 的缩略图 CDN ``ts*.mm.bing.net`` 在本网络下**完全不可达**（``ConnectError``，加 ``Referer`` 也一样）。

回退逻辑本身工作正常（缩略图失败 → 顺延原图），但每次都要先浪费一次失败连接。修正：**熔断键按主机细化**（``image:<host>``），
挂掉的 CDN 连续失败后直接熔断跳过。**实测验证**（同进程连跑 4 次同一查询）：

| 次数 | 耗时 | 说明 |
| --- | --- | --- |
| 第 1 次 | 1852 ms | 冷启动 + 失败的缩略图尝试 + 原图下载 |
| 第 2–3 次 | 639 / 660 ms | 缩略图仍在重试 |
| **第 4 次** | **514 ms** | ``image:ts4.mm.bing.net`` 已熔断（冷却 299 s），**不再浪费连接** |

同时把 ``images.max_bytes`` 默认值从 3 MB 收到 2 MB，并在配置说明里写明
"base64 会再放大约 1.33 倍，这个值同时也是 RPC 载荷的上界"。

**实现期发现并修掉的缺陷**

| 缺陷 | 影响 | 修法 |
| --- | --- | --- |
| 图片下载走了文本路径 | `fetch` 会对字节做字符集解码，**图片会被破坏** | 新增 `HttpClient.fetch_bytes`；两条路径共用同一套代理/限流/熔断/体积上限 |
| 熔断键全通路共用一个 `"images"` | 一个 CDN 主机挂掉会连累原图下载 | 改为 `image:<host>` |
| 只信 ``Content-Type`` 判断图片 | 不少 CDN 用 ``application/octet-stream`` 发图，会被误判为非图片 | 增加魔数嗅探兜底（PNG/JPEG/GIF/WEBP/AVIF） |
| DDG 图片搜索未实现却可能被期待 | 用户开了 DDG 开关会以为图片也走 DDG | 明确不实现（该域名实测不可达），README 与状态输出只列实际支持的源 |

### P4（图搜图 + 图搜文）——已完成，但**结论与预期不同**

**门禁结果**：`pytest` **386 项全绿**；`ruff` 全绿；`bench --strict` 全过。

#### 前置实测的结论：本环境下没有任何可用的反查引擎

计划要求"P4 第一件事就是实测反查引擎，谁可用留谁"。实测（2026，直连）：

| 引擎 | 结果 |
| --- | --- |
| ascii2d / IQDB / **SauceNAO** | **域名不可达**（`ConnectError`） |
| Google Lens / TinEye | 连接超时（12 s） |
| Yandex | 200，但只返回 **1778 字符**的壳页面，无结果 |
| Bing 视觉搜索（URL 式） | 降级成**文本**图片搜索，无 `insightsToken` |
| Bing 视觉搜索（上传式） | `302` 回自身 / multipart `400`（需要浏览器会话参数） |
| 百度识图 | `{"status":1,"msg":"Reject"}`（反爬） |

→ **对着这些端点写 HTML 爬虫会产出无法验证、且在本环境注定失败的代码。**

#### 因此 P4 的实际设计：主路径不依赖反查引擎

| 通路 | 实现方式 | 依赖 |
| --- | --- | --- |
| **图搜文** | 宿主**官方多模态通道**：把用户图片经 ``content_items`` 交回宿主，由宿主按模型 ``visual`` 能力决定是否喂给模型 | 无外部服务 |
| **图搜图** | **模型桥接**：模型看到图片 → 提取关键词 → 调用已实现的 ``image_search`` | 无外部服务（复用 P3） |
| 来源识别（增益） | **只实现 SauceNAO**：它有公开文档的 JSON API，可按规范实现 + 固定响应离线测试 | 可选 API Key |

* **只实现有公开文档的 JSON API**，不做 ascii2d / IQDB / Bing / Yandex / 百度的 HTML 爬虫——
  不可验证的解析代码不进仓库。
* SauceNAO 在配置说明、模块 docstring、README 三处**明确标注"本环境未验证"**（域名不可达）。
* 工具输出在**没有引擎可用时也把链路走通**，并明确指引"要找相似图请用 image_search"，
  避免模型反复重试或误以为插件坏了。

#### 实网端到端

`tests/live_probe.py` 新增"以图搜源引擎连通性"一节，把上表变成**可复现**的检查——
用户换网络后重跑即可知道自己能不能用反查。

#### 实现期发现并修掉的缺陷

| 缺陷 | 影响 | 修法 |
| --- | --- | --- |
| 入站图片段没有 mime 字段，取到字节后未推断类型 | PNG 会被标成默认的 `image/jpeg` 交给宿主与模型 | 拿到字节后立刻魔数嗅探补上 `mime_type`（由测试暴露） |
| 计划里的"三级回退"含 ``Images`` 表 | ``full_path`` 是"项目内相对路径"，插件拿不到宿主项目根目录，按它拼路径脆弱且可能读错文件 | **砍掉这一级**，改为两级回退 + "只有 URL 时自行下载"，拿不到就明确失败 |

### P5（发布）——已完成

**门禁结果**：`pytest` **409 项全绿**；`ruff` 全绿；`bench --strict` 全过。

#### 发布前自检抓到的两类真问题

对"代码实际用到的 ctx 能力 vs manifest 声明"做静态比对后，发现**声明了 4 项能力但功能根本没接线**：

| 声明的能力 | 实际情况 | 处理 |
| --- | --- | --- |
| `tool.get_definitions` | §1.2 约束 3 要求的"工具名冲突自检"**从未实现** | **补齐实现**（见下） |
| `llm.generate` | `[llm] summarize` 配置项存在、README 解释了它，但**从没接线** | **补齐实现**（见下） |
| `send.hybrid` / `config.get` | 完全没用到 | **从 manifest 移除**（不扩大授权面） |

> 声明未使用的能力只是卫生问题；但**配置项存在却不生效是欺骗用户**，必须补实现。

**补齐 1：工具名冲突自检**（`_warn_on_tool_name_conflicts`）

宿主侧工具是全局注册的，`google_search_plugin` 也注册了 `web_search`。自检做法：
用 `tool.get_definitions()` 数名字出现次数，**同一个名字出现两次就说明有两个插件注册了它**
（我们自己只注册一次）。只告警、不擅自禁用自己——取舍留给用户。

**补齐 2：可选的二次总结**（`_maybe_summarize`）

默认关闭（工具结果本来就会回到麦麦自己的对话模型手里，再总结一次是重复消耗）。
开启后作用在 `web_search` 结果与 `read_url` 正文两条路径上；
**失败必须退回原文**而不是让工具失败；任务名取自配置的文本任务（类型上排除 `vlm`）。

#### 新增的发布门禁（`tests/test_release_readiness.py`）

| 门禁 | 拦住的问题 |
| --- | --- |
| 能力一致性双向检查 | ①用到未声明（运行时报错）②声明未使用（扩大授权面）——**本次两类都真实发生过** |
| 组件注册检查 | 四条通路的工具/命令是否都注册成功（少一个就是少一条通路） |
| `timeout_ms` 顶层注入 | 宿主只读顶层，写错位置会让慢查询被 60 秒默认值截断 |
| 启动自检行为 | 冲突告警、无冲突不告警、自检失败不影响加载 |
| 总结接线 | 默认不调 LLM、任务名来自配置、失败退回原文、无结果不总结 |
| 仓库卫生 | 无 `TODO/FIXME/XXX` 注释、`/config.toml` 已忽略、LICENSE 与 manifest 一致、版本为 1.0.0 |

> 静态能力扫描器自带一条"扫描器有效性"断言（先确认它能扫到已知用法），
> 避免"什么都没扫到所以全部通过"的假绿。

#### 发布内容

* 版本 `0.1.0` → **`1.0.0`**（`version.py` 与 manifest 一致，由测试断言）；
* manifest 增加 `display.icon`（lucide `globe`），改善 WebUI 展示；
* README 补齐官方发布检查清单要求的**权限与能力说明**章节（逐项说明申请了什么、何时触发、可否关闭）。

#### 唯一未完成项

**未在真实麦麦实例中加载验证过。** 已通过"复刻宿主加载方式"的测试夹具覆盖包结构、
相对导入、组件注册与 `ManifestValidator` 的严格规则，但"宿主真的把插件拉起来并触发命令"
这一步需要完整麦麦环境（配置、适配器、模型），无法在纯代码环境里替代。




