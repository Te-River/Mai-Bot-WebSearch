"""插件配置模型。

Runner 依据本模型生成并维护插件目录下的 ``config.toml``，并在 WebUI 中渲染配置表单。
注意：``PluginConfigBase`` 通过构造默认实例来生成默认配置，因此**每个字段都必须有默认值**。

**关于中文标签**：WebUI 的表单标签取自 schema 的 ``label``（见 SDK ``config.py`` 的
``"label": str(json_extra.get("label") or field_name)`` 与 ``dashboard/src/lib/config-label.ts``
的 ``resolveFieldLabel``）——**取不到就回退成字段名**。所以光写中文 ``description`` 不够，
每个字段都必须显式给 ``json_schema_extra={"label": ...}``，这就是本模块统一走 ``_field()`` 的原因。
（``test_release_readiness.py`` 里有一条门禁盯着这件事，别绕过 ``_field`` 直接写 ``Field``。）
"""

from __future__ import annotations

from typing import Any, Literal

from maibot_sdk import Field, PluginConfigBase

__all__ = [
    "BingEngineConfig",
    "DuckDuckGoEngineConfig",
    "EnginesConfig",
    "ImagesSection",
    "LimitsSection",
    "LlmSection",
    "MoegirlEngineConfig",
    "NetworkSection",
    "PerfSection",
    "PluginSection",
    "ReadingSection",
    "ReverseSection",
    "SearchSection",
    "SearxngEngineConfig",
    "WebSearchConfig",
]


def _field(label: str, **kwargs: Any) -> Any:
    """构造带中文标签的配置字段。

    Args:
        label: WebUI 上显示的**中文标签**（不是变量名）。
        **kwargs: 透传给 ``Field``（``default`` / ``description`` / 取值范围等）。
    """
    return Field(json_schema_extra={"label": label}, **kwargs)


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = _field("启用插件", default=True, description="关掉后所有搜索工具都不可用")
    config_version: str = _field("配置版本", default="1.0.0", description="配置版本，由 Runner 用于兼容性校验，一般不用改")


class NetworkSection(PluginConfigBase):
    """出站网络配置。联网失败时优先检查这一节。"""

    __ui_label__ = "网络"
    __ui_icon__ = "globe"
    __ui_order__ = 1

    proxy_mode: Literal["auto", "manual", "off"] = _field(
        "代理模式",
        default="auto",
        description="auto 读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 环境变量，manual 使用下面的地址，off 完全不使用代理",
    )
    proxy: str = _field("代理地址", default="", description="manual 模式下的代理地址，例如 http://127.0.0.1:7890")
    no_proxy: str = _field("不走代理的地址", default="", description="逗号分隔，例如 127.0.0.1,localhost")
    timeout_seconds: float = _field("单次请求超时（秒）", default=10.0, gt=0, le=120, description="单次 HTTP 请求的上限")
    verify_tls: bool = _field("校验 TLS 证书", default=True, description="仅在自建实例使用自签证书时关闭")
    user_agents: list[str] = _field(
        "自定义 User-Agent",
        default_factory=list,
        description="抓取网页时随机选用；留空则使用内置列表",
    )


class BingEngineConfig(PluginConfigBase):
    """Bing 搜索（免密钥）。实测在中文 HTML SERP 中仍能返回真实结果的少数来源之一。"""

    __ui_label__ = "Bing"
    __ui_icon__ = "search"
    __ui_order__ = 11

    enabled: bool = _field("启用 Bing", default=True, description="默认引擎，免密钥、开箱即用")
    region: str = _field("区域代码", default="zh-CN", description="影响结果语言与排序，例如 zh-CN / en-US")
    language: str = _field("界面语言", default="zh-Hans", description="例如 zh-Hans / en-US")


class SearxngEngineConfig(PluginConfigBase):
    """自建 SearXNG 实例（免密钥、无反爬，结果最干净，但需要自行部署）。"""

    __ui_label__ = "SearXNG"
    __ui_icon__ = "server"
    __ui_order__ = 12

    enabled: bool = _field("启用 SearXNG", default=False, description="有自建实例时开启，结果最干净、无反爬")
    base_url: str = _field("实例地址", default="", description="例如 http://127.0.0.1:8888")
    categories: str = _field("搜索分类", default="general", description="逗号分隔，例如 general,images")
    language: str = _field("搜索语言", default="zh-CN", description="例如 zh-CN / en-US")


class MoegirlEngineConfig(PluginConfigBase):
    """萌娘百科专用通路。

    实测（2026）：文章 HTML / ``action=raw`` 返回 403，``list=search`` / ``action=parse`` / ``rest.php``
    全部被拒；只有 ``action=opensearch``（标题匹配）与 ``prop=extracts&explaintext=1``（纯文本正文）可用。
    因此本通路**只走 JSON API，不做 HTML 抓取**。
    """

    __ui_label__ = "萌娘百科"
    __ui_icon__ = "book-open"
    __ui_order__ = 13

    enabled: bool = _field("启用萌娘百科", default=True, description="ACG 语境下自动提权，走 JSON 接口")
    api_base: str = _field(
        "API 地址",
        default="https://zh.moegirl.org.cn/api.php",
        description="镜像可填 https://mzh.moegirl.org.cn/api.php",
    )
    extract_mode: Literal["intro", "full"] = _field(
        "正文模式",
        default="intro",
        description="intro 只取简介（快，推荐）；full 取全文（按截断长度）",
    )
    polite_ua: str = _field(
        "礼貌 User-Agent",
        default="MaiBotWebSearch/0.1 (https://github.com/Te-River/Mai-Bot-WebSearch)",
        description="建议保留可联系的仓库地址，降低被限制的风险",
    )
    site_fallback: bool = _field(
        "site: 兜底",
        default=True,
        description="opensearch 是标题前缀匹配、对自然语言零召回；开启后用通用引擎检索 site: 定位标题再取正文",
    )


class DuckDuckGoEngineConfig(PluginConfigBase):
    """DuckDuckGo（html 端点，免密钥）。

    **默认关闭**：实网探针（2026，直连环境）对 ``html.duckduckgo.com`` 三次请求全部
    ``ConnectTimeout``（各 15 秒），即该域名在部分网络下完全不可达。
    留着它是为了在网络可达的环境里多一路结果；不可达时开着只会白白拖慢并污染诊断。
    """

    __ui_label__ = "DuckDuckGo"
    __ui_icon__ = "search"
    __ui_order__ = 14

    enabled: bool = _field("启用 DuckDuckGo", default=False, description="实测在部分网络下不可达，默认关闭")
    region: str = _field("区域代码", default="cn-zh", description="例如 cn-zh / wt-wt")


class EnginesConfig(PluginConfigBase):
    """搜索引擎集合。每个引擎都可在运行时单独开关。"""

    __ui_label__ = "搜索引擎"
    __ui_icon__ = "search"
    __ui_order__ = 2

    bing: BingEngineConfig = _field("Bing", default_factory=BingEngineConfig)
    searxng: SearxngEngineConfig = _field("SearXNG", default_factory=SearxngEngineConfig)
    moegirl: MoegirlEngineConfig = _field("萌娘百科", default_factory=MoegirlEngineConfig)
    duckduckgo: DuckDuckGoEngineConfig = _field("DuckDuckGo", default_factory=DuckDuckGoEngineConfig)


class SearchSection(PluginConfigBase):
    """搜索行为与延迟预算。默认值是"高频聊天场景下的最快可用组合"。"""

    __ui_label__ = "搜索"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 3

    max_results: int = _field("返回条数", default=8, ge=1, le=30, description="返回给模型的结果条数")
    per_engine_timeout_seconds: float = _field("单引擎超时（秒）", default=3.0, gt=0, le=30, description="单个引擎的上限")
    concurrency: int = _field("并发引擎数", default=4, ge=1, le=16, description="同时查询的引擎数量")
    global_deadline_seconds: float = _field("总硬截止（秒）", default=6.0, gt=0, le=60, description="整次搜索的上限")
    early_return_quorum: int = _field("提前返回阈值", default=2, ge=1, le=16, description="有几个引擎返回就可以不等剩下的了")
    grace_window_ms: int = _field(
        "提前返回宽限（毫秒）",
        default=800,
        ge=0,
        le=5000,
        description="达到阈值后再给更优质的引擎一点追加时间，避免只拿到最差引擎的结果",
    )
    fetch_top_n: int = _field("抓取正文条数", default=0, ge=0, le=10, description="抓取前 N 条的正文；0 = 不抓（默认，最快）")
    cache_ttl_seconds: int = _field("缓存有效期（秒）", default=600, ge=0, le=86400, description="0 = 不缓存")
    safe_search: bool = _field("安全搜索", default=True, description="过滤成人与敏感内容")


class ReadingSection(PluginConfigBase):
    """``read_url`` 深路径（读已知 URL 的正文）。"""

    __ui_label__ = "网页阅读"
    __ui_icon__ = "file-text"
    __ui_order__ = 4

    enabled: bool = _field("启用网页阅读", default=True, description="是否启用 read_url 工具")
    max_bytes: int = _field("单页读取上限（字节）", default=1048576, ge=65536, le=10485760, description="页面太大就放弃")
    content_timeout_seconds: float = _field("页面抓取超时（秒）", default=8.0, gt=0, le=60, description="抓取单页的上限")
    max_content_length: int = _field("正文截断长度（字符）", default=3000, ge=200, le=20000, description="送给模型的正文长度")
    block_private_hosts: bool = _field(
        "拦截内网地址",
        default=True,
        description="拒绝内网 / 回环 / 云元数据地址（SSRF 防护，强烈建议保持开启）",
    )


class LlmSection(PluginConfigBase):
    """插件内调用宿主 LLM 的策略。

    ``task_name`` 刻意只允许文本任务：图像理解一律通过 Tool 的 ``content_items`` 交回宿主，
    由宿主按模型 ``visual`` 能力自动判定，**本插件永不调用 ``vlm`` 任务**
    （``vlm`` 留空时宿主不会自动回退，硬编码它会在"只配了多模态 planner/replyer"的部署上失败）。
    """

    __ui_label__ = "模型"
    __ui_icon__ = "sparkles"
    __ui_order__ = 5

    summarize: bool = _field(
        "二次 LLM 总结",
        default=False,
        description="默认关闭——结果本来就会回到麦麦自己的对话模型，再总结一次是重复消耗",
    )
    task_name: Literal["utils", "replyer", "tool_use", "planner"] = _field(
        "使用的 LLM 任务",
        default="utils",
        description="utils 是唯一有回退保障的通用任务；不包含 vlm（本插件不走图像模型）",
    )
    temperature: float = _field("总结温度", default=0.3, ge=0.0, le=2.0, description="仅影响二次总结")
    timeout_seconds: float = _field("LLM 超时（秒）", default=20.0, gt=0, le=120, description="单次总结调用上限")
    max_prompt_chars: int = _field("送入 LLM 的字符上限", default=8000, ge=500, le=60000, description="正文过长会被截断")


class LimitsSection(PluginConfigBase):
    """RPC 与资源上限。"""

    __ui_label__ = "限制"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    rpc_timeout_seconds: int = _field(
        "组件 RPC 超时（秒）",
        default=25,
        ge=5,
        le=300,
        description="会写入组件元数据顶层，覆盖宿主默认的 60 秒；这是天花板而不是目标，真正的控制靠内部硬截止",
    )


class PerfSection(PluginConfigBase):
    """高频调用保护。插件与其它插件共享同一个 Runner 子进程，不能独占资源。"""

    __ui_label__ = "性能"
    __ui_icon__ = "gauge"
    __ui_order__ = 7

    max_inflight_searches: int = _field(
        "全局并发上限",
        default=3,
        ge=1,
        le=16,
        description="插件与其它插件共享同一个 Runner 子进程，所以设得比较保守",
    )
    per_stream_cooldown_seconds: float = _field(
        "同群最小间隔（秒）",
        default=5.0,
        ge=0.0,
        le=120.0,
        description="同一聊天流两次搜索的最小间隔，0 = 不限制",
    )
    queue_overflow_policy: Literal["reject_with_notice"] = _field(
        "超出并发时",
        default="reject_with_notice",
        description="快速返回提示，而不是把请求堆在队列里让用户等更久",
    )


class ImagesSection(PluginConfigBase):
    """文搜图（``image_search``）。

    ``send.image`` **只接受 base64**（宿主直接把参数传给 ``image_to_stream_with_message``），
    所以图片必须下载。由此，**缩略图优先**不是优化而是主要的延迟手段：
    缩略图通常 20–60KB，原图常常数 MB。
    """

    __ui_label__ = "图片搜索"
    __ui_icon__ = "image"
    __ui_order__ = 8

    enabled: bool = _field("启用文搜图", default=True, description="关掉后 image_search 不可用")
    bing_enabled: bool = _field("使用 Bing 图片", default=True, description="免密钥，默认引擎")
    searxng_enabled: bool = _field("使用 SearXNG 图片", default=False, description="需要自建实例")
    safe_search: bool = _field("安全搜索", default=True, description="群聊场景建议保持开启")
    prefer_thumbnail: bool = _field("缩略图优先", default=True, description="缩略图通常比原图快一个数量级")
    min_width: int = _field("最小宽度", default=300, ge=0, le=10000, description="丢弃过窄的候选，0 = 不过滤")
    min_height: int = _field("最小高度", default=200, ge=0, le=10000, description="丢弃过矮的候选，0 = 不过滤")
    max_bytes: int = _field(
        "单图上限（字节）",
        default=2097152,
        ge=65536,
        le=20971520,
        description=(
            "超过就换下一张候选。注意 base64 会再放大约 1.33 倍，这个值同时也是 RPC 载荷的上界"
        ),
    )
    max_images_per_call: int = _field("每次最多发送", default=1, ge=1, le=5, description="一次调用最多发几张图")
    repeat_window_minutes: int = _field(
        "重复窗口（分钟）",
        default=30,
        ge=0,
        le=1440,
        description="同一关键词在该时间窗内不重复发同一张图（按内容哈希判定），0 = 不限制",
    )
    preview_to_model: bool = _field(
        "回传模型观察",
        default=False,
        description="把图片同时经 content_items 回传给模型（会明显增加载荷），默认关闭",
    )
    deadline_seconds: float = _field("图片搜索硬截止（秒）", default=10.0, gt=0, le=60, description="整次图片搜索的上限")


class ReverseSection(PluginConfigBase):
    """以图搜源（反查）：**用图片去互联网上搜"相应的信息"**（来源页 / 相似图）。

    实测各引擎可达性差异极大（同一台机器上 ascii2d / IQDB 域名不可达、Yandex 返回壳页、
    Bing 视觉搜索要浏览器会话、百度识图返回 Reject），所以设计成：

    * 免密钥引擎**并发请求，谁先回先用谁**，全失败会明确告知而不是静默；
    * 连续失败的引擎按主机熔断跳过，不拖累整体；
    * 解析器对各家改版做了多形态兜底，认不出结构时返回"没搜到"而不是抛异常。

    各引擎的独立开关都在下面，跑 ``python tests/live_probe.py`` 可在你自己的网络上
    自查可达性与实际解析效果。
    """

    __ui_label__ = "以图搜源"
    __ui_icon__ = "scan-search"
    __ui_order__ = 9

    enabled: bool = _field("启用图搜源", default=True, description="关掉后 image_lookup 不可用")
    preview_to_model: bool = _field(
        "回传模型观察",
        default=False,
        description=(
            "把用户图片经 content_items 回传给模型。"
            "模型通常已经在上下文里看到用户发的图，默认关闭以免重复占用载荷"
        ),
    )
    ascii2d_enabled: bool = _field("启用 ascii2d（免密钥）", default=True, description="ACG 图源识别；需可达 ascii2d.net")
    iqdb_enabled: bool = _field("启用 IQDB（免密钥）", default=True, description="ACG 图库聚合；需可达 iqdb.org")
    yandex_enabled: bool = _field(
        "启用 Yandex（免密钥）",
        default=False,
        description="通用以图搜；需可达 yandex.com，且该站对脚本常需会话，默认关闭",
    )
    bing_visual_enabled: bool = _field(
        "启用 Bing 视觉搜索（免密钥）",
        default=True,
        description="通用以图搜；对脚本请求常需浏览器会话，失败会自动跳过",
    )
    saucenao_enabled: bool = _field(
        "启用 SauceNAO（需 Key）",
        default=False,
        description="需要 API Key；AC 来源识别最准，但该域名在很多网络下不可达",
    )
    saucenao_api_key: str = _field("SauceNAO API Key", default="", description="留空则无法反查")
    saucenao_min_similarity: float = _field("最低相似度", default=50.0, ge=0.0, le=100.0, description="低于该值的结果丢弃")
    deadline_seconds: float = _field("反查硬截止（秒）", default=8.0, gt=0, le=60, description="整次反查的上限")


class WebSearchConfig(PluginConfigBase):
    """插件配置根模型。"""

    plugin: PluginSection = _field("插件", default_factory=PluginSection)
    network: NetworkSection = _field("网络", default_factory=NetworkSection)
    engines: EnginesConfig = _field("搜索引擎", default_factory=EnginesConfig)
    search: SearchSection = _field("搜索", default_factory=SearchSection)
    reading: ReadingSection = _field("网页阅读", default_factory=ReadingSection)
    llm: LlmSection = _field("模型", default_factory=LlmSection)
    limits: LimitsSection = _field("限制", default_factory=LimitsSection)
    perf: PerfSection = _field("性能", default_factory=PerfSection)
    images: ImagesSection = _field("图片搜索", default_factory=ImagesSection)
    reverse: ReverseSection = _field("以图搜源", default_factory=ReverseSection)
