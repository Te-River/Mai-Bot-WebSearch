# Mai-Bot-WebSearch

给 [MaiBot（麦麦）](https://github.com/Mai-with-u/MaiBot) 提供**联网检索**与**图片检索**能力的第三方插件。

一句话说明它解决什么问题：麦麦本身不能上网，本插件让她能查资料、读网页、找图、认图。

## 四条通路

| # | 通路 | 说法 | 工具 |
| --- | --- | --- | --- |
| ① | **文搜文** | "帮我查一下 xxx" | `web_search` |
| ② | **文搜图** | "来张猫图" | `image_search` |
| ③ | **图搜图** | 发一张图 + "还有类似的吗" | `image_lookup` |
| ④ | **图搜文** | 发一张图 + "这是谁 / 出自哪部作品" | `image_lookup` |

外加 `read_url`（给我某个已知网址，读正文）与 `/websearch` 诊断命令。

> 四条通路均已实现；**图搜图 / 图搜文在多数网络下不依赖外部反查引擎**，详见下方"图搜图与图搜文怎么工作"。
>
> **关于「图搜文」的两种含义**，两条路径都会支持，但触发方式不同：
> * **反查得到文字**（"这张图出自哪部作品"）——把图交给以图搜源引擎，返回作品 / 角色 / 来源；
> * **模型看图生成文字**（"这张图里有什么"）——把图交回麦麦，由麦麦按模型能力自动处理。

## 安装

1. 把本仓库放进麦麦的插件目录，目录名建议 `mai-websearch`：

   ```text
   MaiBot/plugins/mai-websearch/
   ```

2. 启动麦麦。Python 依赖由 `_manifest.json` 的 `dependencies` 声明，麦麦会自动安装。

3. 在 WebUI 的「插件管理」里确认插件已加载，并在「插件配置」中按需调整。

> 没有 API Key 也能用：默认走 Bing + 免密钥 JSON 引擎（含萌娘百科）。

## 配置要点

配置文件由麦麦根据 `config.py` 的配置模型生成在插件目录下的 `config.toml`，**不建议手改**（WebUI 里改更方便）。

最常需要动的几项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `[network] proxy_mode` | `auto` | 读 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`。**联网失败先看这里** |
| `[network] proxy` | 空 | `proxy_mode = "manual"` 时使用，例如 `http://127.0.0.1:7890` |
| `[engines.bing] enabled` | `true` | 免密钥，实测最可靠的 HTML SERP |
| `[engines.moegirl] enabled` | `true` | 萌娘百科（ACG 语境自动提权，走 JSON 接口） |
| `[engines.searxng] enabled` | `false` | 有自建 SearXNG 实例时开启，结果最干净、无反爬 |
| `[engines.duckduckgo] enabled` | `false` | 实测在部分网络下完全不可达，见下方"实测结果" |
| `[search] fetch_top_n` | `0` | 是否抓取结果正文。默认不抓（快）；要长文摘要就调大 |
| `[llm] summarize` | `false` | 是否在插件内二次调用 LLM 总结。默认关闭（见下方"为什么"） |
| `[images] enabled` | `true` | 文搜图总开关 |
| `[images] max_images_per_call` | `1` | 每次最多发几张 |
| `[images] prefer_thumbnail` | `true` | 优先下载缩略图（见下方"图片为什么要下载"） |
| `[images] repeat_window_minutes` | `30` | 同一关键词短期内不重复发同一张图 |
| `[images] max_bytes` | `2 MB` | 单张图片上限；base64 会再放大约 1.33 倍，也是 RPC 载荷上界 |
| `[images] safe_search` | `true` | 图片安全搜索 |
| `[reverse] enabled` | `true` | 图搜图 / 图搜文入口（`image_lookup`） |
| `[reverse] preview_to_model` | `false` | 是否把用户图片经 `content_items` 回传模型观察 |
| `[reverse] saucenao_enabled` | `false` | 是否启用 SauceNAO 反查（需要 API Key） |
| `[reverse] saucenao_api_key` | 空 | SauceNAO API Key |

### 图搜图与图搜文怎么工作

先说一个实测结论：**多数网络下没有任何可用的以图搜源引擎**。项目实测（2026，直连）：

| 引擎 | 结果 |
| --- | --- |
| ascii2d / IQDB / SauceNAO | 域名不可达 |
| Google Lens / TinEye | 连接超时 |
| Yandex | 只返回壳页面，无结果 |
| Bing 视觉搜索 | 需要浏览器会话参数 |
| 百度识图 | 返回 `Reject`（反爬） |

所以插件**不把主路径押在反查上**：

* **图搜文**（"这张图里有什么"）走麦麦自己的**多模态通道**——插件把图片经官方
  `content_items` 交回宿主，由宿主按模型是否支持视觉来决定怎么处理。
  如果你的模型支持看图，它就能直接回答，不需要任何外部服务。
* **图搜图**（"还有类似的吗"）走**模型桥接**：模型看到图片后提取关键词，
  再调用已经实现的 `image_search` 去找相似图片。
* **来源识别**（"出自哪部作品"）是可选的增益：目前只接入了 **SauceNAO**
  （它有公开文档的 JSON API）。它在本项目实测网络下不可达，因此联网行为**未经验证**；
  你的网络能访问 `saucenao.com` 且填了 API Key 时才会生效。

`python tests/live_probe.py` 会实测你所在网络的引擎连通性，换网络后重跑即可确认。

> 为什么不顺便爬 ascii2d / IQDB / Bing？因为那些只有 HTML 页面且带反爬，
> 写出来的解析代码在这个环境里既跑不通也无法验证——那种代码不该进仓库。

### 为什么要下载图片、以及为什么优先缩略图

麦麦的 `send.image` **只接受 base64**（宿主把参数原样交给平台的图片发送接口），
所以插件必须先把图片下载下来——URL 传不过去。

由此产生一个关键取舍：图片搜索结果里同时有**原图**（常几 MB）和**缩略图**（常 20–60KB），
插件默认走缩略图，只有缩略图不可达时才回退原图。

> 实测提醒：Bing 的缩略图 CDN（`ts*.mm.bing.net`）在部分网络下不可达。
> 这种情况插件会自动回退到原图，并在连续失败后**按主机熔断**跳过该 CDN，
> 所以后续调用不会再为它浪费时间。想完全避免大图，可以把 `[images] max_bytes` 调小。

### 实测结果（决定了上面的默认值）

默认值不是猜的，是 `tests/live_probe.py` 在真实网络上跑出来的：

| 来源 | 结果 | 结论 |
| --- | --- | --- |
| **Bing** | ✅ 3/3 命中，406–1822 ms，各 5 条 | 默认启用 |
| **Bing 图片** | ✅ 候选解析正常，图片可下载（原图 338–479 KB） | 默认启用；缩略图 CDN 不可达时自动回退原图 |
| **DuckDuckGo** | ❌ 3/3 `ConnectTimeout`（各 15 秒） | **默认关闭**，避免必然超时的源拖慢搜索 |
| **萌娘百科** | ✅ 标题查询命中候选；`extracts` 356 ms 取回正文 | 默认启用，走专用 JSON 通路 |
| **萌娘百科（自然语言）** | ⚠️ `opensearch` 对"xxx是什么"零候选 | 所以必须剥疑问词 + `site:` 兜底（已实现） |

对方改版后重跑 `python tests/live_probe.py` 即可知道该关掉哪个源。

### 为什么默认不抓正文、也不二次总结

麦麦是**高频触发的聊天场景**，用户发完消息就在等回复。所以：

* 工具返回的搜索结果会回到**麦麦自己的对话模型**手里，它本来就会读——插件再调一次 LLM 总结等于同一段文字过两遍模型，白烧 2–10 秒和 token；
* SERP 的标题 + 摘要通常已经够模型作答；需要读全文时，让麦麦调用 `read_url` 走深路径即可。

这是"快路径 / 深路径"的分离，不是能力缺失。

## 命令

```text
/websearch [status]     # 查看配置与通路状态（别名 /ws）
```

## 权限与能力声明

插件在 `_manifest.json` 里只申请**实际用到**的能力（宿主会校验：声明了未注册的能力名会被拒绝加载）。
一次性列清楚，便于你判断这个插件要了哪些权限：

| 能力 | 用途 | 何时触发 |
| --- | --- | --- |
| `send.text` | 回复 `/websearch status` | 你主动发命令时 |
| `send.image` | 把搜到的图片发到聊天里 | 麦麦调用 `image_search` 时 |
| `llm.generate` | **可选**的二次总结（默认关闭） | 只有你把 `[llm] summarize` 打开后 |
| `message.get_by_id` | 获取用户发来的图片（`image_lookup`） | 用户发图并问"这是什么"时 |
| `tool.get_definitions` | 启动时自检工具名冲突并告警 | 插件加载时 |

几点值得注意：

* **不申请任何数据库能力**——插件不读写麦麦的数据库，也不落盘用户数据；
* **不调用 `vlm` 任务**——图像理解一律通过官方 `content_items` 通道交回宿主，
  由宿主按模型 `visual` 能力自动判定（`vlm` 留空时宿主不会回退，硬编码它会在一部分部署上直接失败）；
* `llm.generate` 的任务名在配置类型里就被限定为文本任务（`utils` / `replyer` / `tool_use` / `planner`），
  **`vlm` 在类型层面写不进去**；
* 只有 `send.*`、`message.get_by_id` 是"必需"的，其余都可用配置关掉。

出网方面：插件会访问你配置的搜索引擎、网页与（可选的）反查服务，
具体见下方"数据流向与隐私"。

## 故障排查

**搜不到东西 / 报网络错误**

1. 先 `curl -I https://www.baidu.com` 确认机器本身能出网；
2. 需要代理就把 `[network] proxy_mode` 设为 `manual` 并填 `proxy`，或设置 `HTTPS_PROXY` 环境变量后重启麦麦；
3. 用 `/websearch status` 确认引擎开关状态；
4. 某些引擎被反爬时会自动熔断 5 分钟，稍后重试或换引擎。

**插件在插件管理里根本不出现**

最常见的原因是 `_manifest.json` 校验失败（字段多余、版本号格式、声明了未注册的能力名），
或声明的 Python 依赖与麦麦主程序依赖约束冲突。看麦麦日志里 `ManifestValidator` / 依赖解析的报错。

**萌娘百科查不到**

萌娘百科的公开接口只开放了**标题匹配**（`opensearch`）与**正文抽取**（`extracts`），
文章 HTML 与全文检索接口都已关闭（HTML 直接返回 403，插件因此从不抓它的网页）。

这意味着按标题查询（如"初音"）能直接命中，而自然语言问句需要先把主题词剥出来。
插件会自动做这件事，按**代价从低到高**三级尝试：

1. 用原查询做标题匹配；
2. 剥掉疑问成分（`初音未来是什么` → `初音未来`）后再匹配一次；
3. 仍无结果才用通用引擎查 `site:zh.moegirl.org.cn` 定位条目。

第 2 步是实测加上的：`site:zh.moegirl.org.cn 初音未来是什么` 在 Bing 上找不到萌娘百科页面，
而剥成 `初音未来` 后 `opensearch` 能直接命中。若三级都没命中，插件会明确报告
"没有找到对应条目"，其余引擎的结果照常返回。

## 数据流向与隐私

* 搜索关键词会发送给所配置的搜索引擎（Bing / SearXNG / 萌娘百科等）。
* **图搜图 / 图搜文会把用户图片或其链接发送给第三方以图搜源服务**。不需要此功能时请关闭 `[reverse] enabled`。
* 插件不把用户数据写入数据库，缓存仅在内存与运行时目录中短期存在。

## 开发

```bash
pip install -r requirements-dev.txt

ruff check .
pytest
python tests/bench.py --strict     # 延迟门禁（CI 用不带 --strict 的版本）
python tests/smoke_live.py         # 端到端冒烟：真实网络跑四条通路
python tests/live_probe.py         # 实网探针，决定默认引擎集合
```

* 测试**完全离线**（用录制的 fixtures），不依赖网络；
* `tests/conftest.py` 刻意复刻宿主的加载方式（`spec_from_file_location` + `submodule_search_locations`），
  这样"缺少 `__init__.py`""相对导入写错"这类问题会在 CI 直接暴露，而不是等装进麦麦才发现；
* `tests/bench.py` 区分**机器无关门禁**（重依赖不得进入加载路径、提前返回必须真生效）与
  **绝对时间预算**（仅 `--strict` 强制）——共享 CI runner 上的绝对计时不可靠；
* `tests/smoke_live.py` 走的是**插件自己的工具处理函数**（配置 → 处理器 → 管线 → 渲染），
  是"组装好的插件能不能用"的验收脚本。**把它放进 `plugins/` 后运行它，就是一次真实环境验收。**

真机验收的完整步骤（含常见失败对照表）见 [`docs/VERIFY.md`](docs/VERIFY.md)。

设计文档见 [`docs/PLAN.md`](docs/PLAN.md)。

## 许可证

MIT
