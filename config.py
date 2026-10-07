"""插件配置模型。

Runner 依据本模型生成并维护插件目录下的 ``config.toml``，并在 WebUI 中渲染配置表单。
注意：``PluginConfigBase`` 通过构造默认实例来生成默认配置，因此**每个字段都必须有默认值**。
"""

from __future__ import annotations

from typing import Literal

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


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本，由 Runner 用于兼容性校验")


class NetworkSection(PluginConfigBase):
    """出站网络配置。联网失败时优先检查这一节。"""

    __ui_label__ = "网络"
    __ui_icon__ = "globe"
    __ui_order__ = 1

    proxy_mode: Literal["auto", "manual", "off"] = Field(
        default="auto",
        description="代理模式：auto 读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 环境变量，manual 使用下面的地址，off 完全不使用代理",
    )
    proxy: str = Field(default="", description="manual 模式下的代理地址，例如 http://127.0.0.1:7890")
    no_proxy: str = Field(default="", description="不走代理的地址列表，逗号分隔")
    timeout_seconds: float = Field(default=10.0, gt=0, le=120, description="单次 HTTP 请求超时（秒）")
    verify_tls: bool = Field(default=True, description="是否校验 TLS 证书；仅在自建实例使用自签证书时关闭")
    user_agents: list[str] = Field(
        default_factory=list,
        description="抓取网页时随机选用的 User-Agent；留空则使用内置列表",
    )


class BingEngineConfig(PluginConfigBase):
    """Bing 搜索（免密钥）。实测在中文 HTML SERP 中仍能返回真实结果的少数来源之一。"""

    __ui_label__ = "Bing"
    __ui_icon__ = "search"
    __ui_order__ = 11

    enabled: bool = Field(default=True, description="是否启用 Bing 搜索")
    region: str = Field(default="zh-CN", description="区域代码，影响结果语言与排序")
    language: str = Field(default="zh-Hans", description="界面语言")


class SearxngEngineConfig(PluginConfigBase):
    """自建 SearXNG 实例（免密钥、无反爬，结果最干净，但需要自行部署）。"""

    __ui_label__ = "SearXNG"
    __ui_icon__ = "server"
    __ui_order__ = 12

    enabled: bool = Field(default=False, description="是否启用自建 SearXNG 实例")
    base_url: str = Field(default="", description="实例地址，例如 http://127.0.0.1:8888")
    categories: str = Field(default="general", description="搜索分类，逗号分隔")
    language: str = Field(default="zh-CN", description="搜索语言")


class MoegirlEngineConfig(PluginConfigBase):
    """萌娘百科专用通路。

    实测（2026）：文章 HTML / ``action=raw`` 返回 403，``list=search`` / ``action=parse`` / ``rest.php``
    全部被拒；只有 ``action=opensearch``（标题匹配）与 ``prop=extracts&explaintext=1``（纯文本正文）可用。
    因此本通路**只走 JSON API，不做 HTML 抓取**。
    """

    __ui_label__ = "萌娘百科"
    __ui_icon__ = "book-open"
    __ui_order__ = 13

    enabled: bool = Field(default=True, description="是否启用萌娘百科（ACG 语境下自动提升权重）")
    api_base: str = Field(
        default="https://zh.moegirl.org.cn/api.php",
        description="MediaWiki API 地址；镜像可填 https://mzh.moegirl.org.cn/api.php",
    )
    extract_mode: Literal["intro", "full"] = Field(
        default="intro",
        description="正文模式：intro 只取简介（快），full 取全文（按长度截断）",
    )
    polite_ua: str = Field(
        default="MaiBotWebSearch/0.1 (https://github.com/Te-River/Mai-Bot-WebSearch)",
        description="礼貌 User-Agent，建议保留可联系的仓库地址，降低被限制的风险",
    )
    site_fallback: bool = Field(
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

    enabled: bool = Field(default=False, description="是否启用 DuckDuckGo（实测在部分网络下不可达，默认关闭）")
    region: str = Field(default="cn-zh", description="区域代码，例如 cn-zh / wt-wt")


class EnginesConfig(PluginConfigBase):
    """搜索引擎集合。每个引擎都可在运行时单独开关。"""

    __ui_label__ = "搜索引擎"
    __ui_icon__ = "search"
    __ui_order__ = 2

    bing: BingEngineConfig = Field(default_factory=BingEngineConfig)
    searxng: SearxngEngineConfig = Field(default_factory=SearxngEngineConfig)
    moegirl: MoegirlEngineConfig = Field(default_factory=MoegirlEngineConfig)
    duckduckgo: DuckDuckGoEngineConfig = Field(default_factory=DuckDuckGoEngineConfig)


class SearchSection(PluginConfigBase):
    """搜索行为与延迟预算。默认值是"高频聊天场景下的最快可用组合"。"""

    __ui_label__ = "搜索"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 3

    max_results: int = Field(default=8, ge=1, le=30, description="返回给模型的结果条数")
    per_engine_timeout_seconds: float = Field(default=3.0, gt=0, le=30, description="单个引擎的超时（秒）")
    concurrency: int = Field(default=4, ge=1, le=16, description="同时查询的引擎数量")
    global_deadline_seconds: float = Field(default=6.0, gt=0, le=60, description="整次搜索的硬截止（秒）")
    early_return_quorum: int = Field(default=2, ge=1, le=16, description="达到该数量的引擎返回后即可提前返回")
    grace_window_ms: int = Field(
        default=800,
        ge=0,
        le=5000,
        description="提前返回的宽限窗口（毫秒）：达到 quorum 后再给更优质的引擎一点追加时间，避免只拿到最差引擎的结果",
    )
    fetch_top_n: int = Field(default=0, ge=0, le=10, description="抓取前 N 条结果的正文；0 = 不抓（默认，最快）")
    cache_ttl_seconds: int = Field(default=600, ge=0, le=86400, description="查询缓存有效期（秒），0 = 不缓存")
    safe_search: bool = Field(default=True, description="是否开启安全搜索")


class ReadingSection(PluginConfigBase):
    """``read_url`` 深路径（读已知 URL 的正文）。"""

    __ui_label__ = "网页阅读"
    __ui_icon__ = "file-text"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="是否启用 read_url 工具")
    max_bytes: int = Field(default=1048576, ge=65536, le=10485760, description="单个页面最多读取的字节数")
    content_timeout_seconds: float = Field(default=8.0, gt=0, le=60, description="页面抓取超时（秒）")
    max_content_length: int = Field(default=3000, ge=200, le=20000, description="正文截断长度（字符）")
    block_private_hosts: bool = Field(
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

    summarize: bool = Field(
        default=False,
        description="是否在插件内二次调用 LLM 总结；默认关闭——结果本来就会回到麦麦自己的对话模型，再总结一次是重复消耗",
    )
    task_name: Literal["utils", "replyer", "tool_use", "planner"] = Field(
        default="utils",
        description="插件调用 LLM 使用的宿主任务；utils 是唯一有回退保障的通用任务",
    )
    temperature: float = Field(default=0.3, ge=0.0, le=2.0, description="总结时的温度")
    timeout_seconds: float = Field(default=20.0, gt=0, le=120, description="单次 LLM 调用超时（秒）")
    max_prompt_chars: int = Field(default=8000, ge=500, le=60000, description="送入 LLM 的正文最长字符数")


class LimitsSection(PluginConfigBase):
    """RPC 与资源上限。"""

    __ui_label__ = "限制"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    rpc_timeout_seconds: int = Field(
        default=25,
        ge=5,
        le=300,
        description="组件 RPC 超时（会写入组件元数据顶层，覆盖宿主默认的 60 秒）；这是天花板而不是目标，真正的控制靠内部硬截止",
    )


class PerfSection(PluginConfigBase):
    """高频调用保护。插件与其它插件共享同一个 Runner 子进程，不能独占资源。"""

    __ui_label__ = "性能"
    __ui_icon__ = "gauge"
    __ui_order__ = 7

    max_inflight_searches: int = Field(
        default=3,
        ge=1,
        le=16,
        description="全局并发搜索上限（插件与其它插件共享同一个 Runner 子进程）",
    )
    per_stream_cooldown_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=120.0,
        description="同一聊天流两次搜索之间的最小间隔（秒），0 = 不限制",
    )
    queue_overflow_policy: Literal["reject_with_notice"] = Field(
        default="reject_with_notice",
        description="超出并发上限时的行为：快速返回提示，而不是把请求堆在队列里让用户等更久",
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

    enabled: bool = Field(default=True, description="是否启用文搜图")
    bing_enabled: bool = Field(default=True, description="使用 Bing 图片搜索（免密钥）")
    searxng_enabled: bool = Field(default=False, description="使用自建 SearXNG 的 images 分类")
    safe_search: bool = Field(default=True, description="安全搜索；群聊场景建议保持开启")
    prefer_thumbnail: bool = Field(default=True, description="优先下载缩略图（通常比原图快一个数量级）")
    min_width: int = Field(default=300, ge=0, le=10000, description="丢弃宽度小于该值的候选（0 = 不过滤）")
    min_height: int = Field(default=200, ge=0, le=10000, description="丢弃高度小于该值的候选（0 = 不过滤）")
    max_bytes: int = Field(
        default=2097152,
        ge=65536,
        le=20971520,
        description=(
            "单张图片下载上限（字节）；超过就换下一张候选。"
            "注意 base64 会再放大约 1.33 倍，这个值同时也是 RPC 载荷的上界"
        ),
    )
    max_images_per_call: int = Field(default=1, ge=1, le=5, description="每次最多发送几张图片")
    repeat_window_minutes: int = Field(
        default=30,
        ge=0,
        le=1440,
        description="同一关键词在该时间窗内不重复发送同一张图片（按内容哈希判定）",
    )
    preview_to_model: bool = Field(
        default=False,
        description="是否把图片同时回传给模型观察（走 content_items，会明显增加载荷，默认关闭）",
    )
    deadline_seconds: float = Field(default=10.0, gt=0, le=60, description="整次图片搜索的硬截止（秒）")


class ReverseSection(PluginConfigBase):
    """以图搜源（反查）。

    实测（详见 ``images/reverse/__init__.py`` 的表格）：多数网络下**没有任何可用的反查引擎**
    （ascii2d / IQDB / SauceNAO 域名不可达，Yandex 返回壳页面，Bing 视觉搜索需要浏览器会话，
    百度识图返回 Reject）。因此本层默认不启用引擎：

    * **图搜文** 主要靠宿主的官方多模态通道（把图片经 ``content_items`` 交回宿主）；
    * **图搜图** 主要靠"模型看图 → 提取关键词 → 调用 ``image_search``"；
    * 反查引擎是可选的增益，能在可用网络下补上"出自哪部作品/哪个角色"。
    """

    __ui_label__ = "以图搜源"
    __ui_icon__ = "scan-search"
    __ui_order__ = 9

    enabled: bool = Field(default=True, description="是否启用 image_lookup 工具（图搜图 / 图搜文的入口）")
    preview_to_model: bool = Field(
        default=False,
        description=(
            "是否把用户图片经 content_items 回传模型观察。"
            "模型通常已经在上下文里看到用户发的图，默认关闭以免重复占用载荷"
        ),
    )
    saucenao_enabled: bool = Field(
        default=False,
        description="是否启用 SauceNAO 反查（需要 API Key；该项目实测网络下该域名不可达，联网行为未验证）",
    )
    saucenao_api_key: str = Field(default="", description="SauceNAO API Key")
    saucenao_min_similarity: float = Field(default=50.0, ge=0.0, le=100.0, description="低于该相似度的结果丢弃")
    deadline_seconds: float = Field(default=8.0, gt=0, le=60, description="反查硬截止（秒）")


class WebSearchConfig(PluginConfigBase):
    """插件配置根模型。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    network: NetworkSection = Field(default_factory=NetworkSection)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    search: SearchSection = Field(default_factory=SearchSection)
    reading: ReadingSection = Field(default_factory=ReadingSection)
    llm: LlmSection = Field(default_factory=LlmSection)
    limits: LimitsSection = Field(default_factory=LimitsSection)
    perf: PerfSection = Field(default_factory=PerfSection)
    images: ImagesSection = Field(default_factory=ImagesSection)
    reverse: ReverseSection = Field(default_factory=ReverseSection)
