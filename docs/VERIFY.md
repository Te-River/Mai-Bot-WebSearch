# 真机验收清单

插件中心提交的官方检查清单里有一条是「本地已用真实 MaiBot 验证过插件能正常加载运行」。
本文就是那一步的可执行版本：**照着做完，就能确定这个插件在你的麦麦环境里是好的。**

> 为什么不能省：项目的离线测试复刻了宿主的加载方式（`spec_from_file_location` +
> `submodule_search_locations`），也逐字段核对过宿主的 `PluginManifest` 定义，
> 但「宿主真的把插件拉起来、真的把工具交给 planner」这一步只能在你的环境里验证。

## 0. 准备

```text
MaiBot/
└── plugins/
    └── mai-websearch/        ← 把本仓库整个放进来（目录名建议用这个）
        ├── _manifest.json
        ├── plugin.py
        └── ...
```

不需要手动装依赖：`_manifest.json` 里的 `dependencies` 会让麦麦自动安装。

## 1. 冒烟脚本（最快的一步）

在插件目录里执行：

```bash
python tests/smoke_live.py
```

它按宿主的方式加载插件、检查组件注册，然后用**真实网络**跑一遍四条通路。
期望看到（数字会因网络而异）：

```text
== 加载与注册 ==
  [OK]   on_load 未抛异常  — 0.x ms
  [OK]   组件注册齐全  — 5 个

== ① 文搜文 ==
  [OK]   web_search 返回内容  — 1865 字符｜关于「初音未来」的搜索结果（8 条）：
...
冒烟结果：通过 [OK]
```

只看加载与注册（不联网）：

```bash
python tests/smoke_live.py --offline
```

## 2. WebUI 确认

1. 打开麦麦 WebUI → **插件管理**：应能看到「麦麦联网搜索」，状态为已加载/已启用。
2. 进入**插件配置**：应能看到 9 个配置段（插件 / 网络 / 搜索引擎 / 搜索 / 网页阅读 /
   模型 / 限制 / 性能 / 图片搜索 / 以图搜源），字段都有中文说明。
3. 顺手确认：**没有** `config.toml` 被提交进仓库（它会由麦麦自动生成）。

## 3. 聊天里四条通路各试一次

| 通路 | 你可以发 | 期望 |
| --- | --- | --- |
| ① 文搜文 | `帮我查一下初音未来是谁` | 回复里带上来源链接 |
| ② 文搜图 | `来张布偶猫的图` | 真的收到一张图片 |
| ③ 图搜图 | 发一张图 + `还有类似的吗` | 她提取关键词后再搜图（取决于模型是否支持看图） |
| ④ 图搜文 | 发一张图 + `这是什么` | 她描述图片内容（同样取决于模型是否支持看图） |

**关于 ③④**：这两条依赖**宿主的视觉能力**——如果 `planner`/`replyer` 配的是
`visual = true` 的多模态模型，宿主会把图片直接喂给模型；否则会退化为纯文本。
本插件**不调用 `vlm` 任务**（该任务留空时宿主不会回退，硬编码它反而会失败）。

命令：

```text
/websearch status        # 或 /ws
```

应回一段状态：版本、代理、引擎开关、阅读抽取链路、图片开关、以图搜源开关。

## 4. 重载与卸载

在 WebUI 里**重载插件**、再**禁用插件**，观察日志：

* 重载后 `/websearch status` 仍正常 → 说明配置热更新没有泄漏连接池；
* 禁用后日志出现「麦麦联网搜索已卸载」且**没有**残留报错 → 说明清理正常。

## 5. 常见失败与原因

| 现象 | 多半是 |
| --- | --- |
| 插件列表里**根本没有**这个插件 | `_manifest.json` 校验失败（字段多余 / 版本号格式 / 声明了未注册的能力名）或依赖冲突，看日志里 `ManifestValidator` / 依赖解析的报错 |
| 加载了但**搜不到东西** | 机器本身出不了网。先 `curl -I https://www.baidu.com`；需要代理就在 `[network]` 里设 `manual` + 填地址 |
| 模型**不调用**工具 | 工具默认在 deferred 池里需要被发现。可以先用 `/websearch status` 确认组件注册数，再尝试更直白的问法 |
| 工具被**截断**（跑到一半失败） | 检查组件元数据顶层的 `timeout_ms` 是否生效（`[limits] rpc_timeout_seconds`） |
| 和 `google_search_plugin` **同时装了** | 两边都注册了 `web_search`，启动日志里会有冲突告警。建议只留一个 |
| 发不出图片 | 看是"没找到图片"还是"找到了但发送失败"——后者通常是适配器限制或图片超过平台上限，可调小 `[images] max_bytes` |

## 6. 出问题了给我什么

把下面这些贴给我，基本能定位：

1. `python tests/smoke_live.py` 的**完整输出**；
2. 麦麦版本、`maibot-plugin-sdk` 版本、Python 版本；
3. 麦麦日志里插件相关的**报错片段**（含 traceback）；
4. `/websearch status` 的输出。

## 7. 验收通过之后

确认无误后，就可以向插件中心提交收录申请：

打开 [plugin-repo 的 New Issue](https://github.com/Mai-with-u/plugin-repo/issues/new/choose)，
选「**Add Plugin / 添加插件**」模板，填：

* **插件 ID**：`com.teriver.mai-websearch`
* **仓库地址**：`https://github.com/Te-River/Mai-Bot-WebSearch`

CI 会拉取仓库根目录的 `_manifest.json` 自动校验并把结果评论在 Issue 里，
通过后维护者 `/approve`，插件就会出现在 WebUI 插件市场的搜索结果中。
