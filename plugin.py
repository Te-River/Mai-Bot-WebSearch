"""麦麦联网搜索插件入口。

本文件只负责装配与组件声明（SDK 只扫描插件类上的组件装饰器），
业务逻辑放在 ``core`` / ``search`` / ``reading`` / ``images`` 子包中。

延迟约束：``on_load`` 不做网络 I/O、不导入重依赖；HTTP 客户端惰性创建。
"""

from __future__ import annotations

import asyncio
from typing import Any

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .commands.status import build_status_text
from .config import WebSearchConfig
from .core.budget import ConcurrencyGate
from .core.errors import SearchError, describe_error
from .core.http import HttpClient, resolve_proxy
from .core.ratelimit import Cooldown
from .core.ssrf import looks_like_url
from .images.inbound import acquire_inbound_image
from .images.picker import ImageHistory
from .images.pipeline import ImageSearchPipeline, build_image_pipeline
from .images.reverse import build_reverse_providers
from .reading.fetcher import read_url as read_page
from .reading.types import ReadingResult, SpecialSource
from .search.providers.moegirl import MoegirlProvider, moegirl_special_source
from .search.router import SearchPipeline, build_search_pipeline
from .tools.image_lookup import render_lookup, run_reverse_lookup
from .tools.image_search import render_image_outcome
from .tools.read_url import render_reading
from .tools.web_search import render_outcome
from .version import __version__

__all__ = ["WebSearchPlugin", "create_plugin"]

_FALLBACK_RPC_TIMEOUT_MS = 25_000

# 本插件注册的工具名。宿主侧工具是全局的，重名会让 planner 的选择产生歧义，
# 因此启动时用 tool.get_definitions() 自检并告警（只告警，不擅自改行为）。
_OWN_TOOL_NAMES = ("web_search", "read_url", "image_search", "image_lookup")

_SUMMARY_SYSTEM_PROMPT = (
    "你是搜索助手。请依据给定的搜索结果，用简洁的中文回答用户的问题，"
    "并保留关键结论与来源链接。不要编造结果里没有的信息。"
)


class WebSearchPlugin(MaiBotPlugin):
    """麦麦联网搜索插件。"""

    config_model = WebSearchConfig

    def __init__(self) -> None:
        super().__init__()
        self._http: HttpClient | None = None
        self._pipeline: SearchPipeline | None = None
        self._image_pipeline: ImageSearchPipeline | None = None
        self._image_history = ImageHistory(window_seconds=0.0)
        self._reverse_providers: list[Any] = []
        self._special_sources: list[SpecialSource] = []
        self._gate = ConcurrencyGate(3)
        self._cooldown = Cooldown(interval_seconds=0.0)

    # ---------------------------------------------------------------- #
    # 生命周期
    # ---------------------------------------------------------------- #

    async def on_load(self) -> None:
        """插件加载时执行；此处**不得**做网络 I/O，也不得导入重依赖（预算 ≤300ms）。"""
        self._build_runtime()
        config = self.config
        pipeline = self._pipeline
        engines = ",".join(provider.name for provider in pipeline.providers) if pipeline else "(无)"
        self.ctx.logger.info(
            "麦麦联网搜索 v%s 已加载（enabled=%s, proxy=%s, 引擎=%s, 阅读=%s, 图片=%s）",
            __version__,
            config.plugin.enabled,
            self._describe_proxy(),
            engines,
            config.reading.enabled,
            config.images.enabled,
        )
        await self._warn_on_tool_name_conflicts()

    async def _warn_on_tool_name_conflicts(self) -> None:
        """自检工具名冲突。

        宿主侧工具是**全局**注册的：``google_search_plugin`` 也注册了 ``web_search``，
        同时安装会让 planner 的选择产生歧义。这里用 ``tool.get_definitions()`` 数名字出现次数——
        **同一个名字出现两次就说明有两个插件注册了它**（我们自己只注册一次）。

        只告警、不擅自禁用自己：取舍留给用户（README 建议二选一）。
        """
        try:
            definitions = await self.ctx.tool.get_definitions()
        except Exception as exc:  # noqa: BLE001 - 自检失败绝不能影响插件加载
            self.ctx.logger.debug("工具名自检跳过：%s", exc)
            return

        counts: dict[str, int] = {}
        for item in definitions or []:
            name = str(item.get("name") or "") if isinstance(item, dict) else ""
            if name in _OWN_TOOL_NAMES:
                counts[name] = counts.get(name, 0) + 1

        conflicts = sorted(name for name, count in counts.items() if count > 1)
        if conflicts:
            self.ctx.logger.warning(
                "检测到工具名冲突：%s 已被其它插件注册。planner 可能选错工具，"
                "建议只保留其中一个联网搜索插件。",
                "、".join(conflicts),
            )

    async def on_unload(self) -> None:
        """插件卸载时执行：取消在途请求、关闭连接池、清空缓存。"""
        await self._teardown()
        self.ctx.logger.info("麦麦联网搜索已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：必须关掉旧客户端，否则会泄漏连接池。"""
        del config_data
        await self._teardown()
        self._build_runtime()
        self.ctx.logger.info("麦麦联网搜索配置已更新（scope=%s, version=%s）", scope, version)

    def get_components(self) -> list[dict[str, Any]]:
        """在组件元数据的**顶层**注入 RPC 超时。

        宿主只读 metadata 顶层的 ``timeout_ms``（缺省 60 秒）；写在 ``@Tool`` 的额外
        kwargs 里会落进嵌套 metadata 而不被读取，慢查询会被整段截断成一次失败调用。
        """
        components = super().get_components()
        timeout_ms = self._rpc_timeout_ms()
        for component in components:
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata["timeout_ms"] = timeout_ms
        return components

    # ---------------------------------------------------------------- #
    # 装配
    # ---------------------------------------------------------------- #

    def _rpc_timeout_ms(self) -> int:
        """RPC 超时（毫秒）：这是天花板，不是目标；真正的控制靠内部硬截止。"""
        try:
            return int(self.config.limits.rpc_timeout_seconds * 1000)
        except Exception:  # noqa: BLE001 - 配置尚未注入时用兜底值
            return _FALLBACK_RPC_TIMEOUT_MS

    def _describe_proxy(self) -> str:
        """把生效的代理渲染成一句话，便于排障。"""
        try:
            proxy = resolve_proxy(
                self.config.network.proxy_mode,
                self.config.network.proxy,
            )
        except Exception:  # noqa: BLE001 - 诊断输出不应因为配置未就绪而失败
            return "(未初始化)"
        return proxy or "(直连)"

    def _build_runtime(self) -> None:
        """按当前配置装配运行时对象（不做任何 I/O）。"""
        config = self.config
        self._http = HttpClient(
            proxy=resolve_proxy(config.network.proxy_mode, config.network.proxy),
            timeout_seconds=config.network.timeout_seconds,
            verify_tls=config.network.verify_tls,
            user_agents=list(config.network.user_agents),
        )
        self._gate = ConcurrencyGate(config.perf.max_inflight_searches)
        self._cooldown = Cooldown(interval_seconds=config.perf.per_stream_cooldown_seconds)
        self._pipeline = build_search_pipeline(config, self._http, self._gate)
        self._special_sources = self._build_special_sources()
        self._image_history = ImageHistory(window_seconds=config.images.repeat_window_minutes * 60.0)
        self._image_pipeline = build_image_pipeline(config, self._http, self._image_history)
        self._reverse_providers = self._build_reverse_providers()

    def _build_reverse_providers(self) -> list[Any]:
        """装配反查引擎（免密钥引擎默认开，Key 引擎需显式开启）。"""
        return build_reverse_providers(self.config)

    def _build_special_sources(self) -> list[SpecialSource]:
        """站点专用阅读通路。

        萌娘百科的文章 HTML 是 403，抓网页只会白烧预算，因此走它的 JSON 接口。
        """
        pipeline = self._pipeline
        if pipeline is None:
            return []
        provider = pipeline.provider_by_name("moegirl")
        if isinstance(provider, MoegirlProvider):
            return [moegirl_special_source(provider)]
        return []

    async def _teardown(self) -> None:
        """释放运行时资源。"""
        http, self._http = self._http, None
        pipeline, self._pipeline = self._pipeline, None
        self._image_pipeline = None
        self._image_history.clear()
        self._reverse_providers = []
        self._special_sources = []
        if pipeline is not None:
            pipeline.cache.clear()
        self._cooldown.clear()
        if http is not None:
            await http.aclose()

    async def _read_page(self, url: str) -> ReadingResult:
        """按配置读一个 URL 的正文。"""
        http = self._http
        if http is None:
            raise SearchError("HTTP 客户端未就绪", user_message="阅读组件未就绪，请稍后再试")
        config = self.config
        return await read_page(
            url,
            http=http,
            max_bytes=config.reading.max_bytes,
            timeout_seconds=config.reading.content_timeout_seconds,
            max_content_length=config.reading.max_content_length,
            block_private_hosts=config.reading.block_private_hosts,
            special_sources=self._special_sources,
        )

    async def _maybe_summarize(self, text: str) -> str:
        """按配置在插件内二次调用 LLM 总结。

        默认关闭：工具结果本来就会回到麦麦自己的对话模型手里，再总结一次是重复消耗
        （同一段文字过两遍模型）。开启后失败要**退回原文**，而不是让工具失败。

        任务名固定取配置里的文本任务（``Literal`` 里没有 ``vlm``）——
        图像理解一律走 ``content_items`` 交宿主自动判定。
        """
        config = self.config
        if not config.llm.summarize or not text.strip():
            return text
        prompt = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text[: config.llm.max_prompt_chars]},
        ]
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt,
                    task_name=config.llm.task_name,
                    temperature=config.llm.temperature,
                ),
                timeout=config.llm.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 总结失败必须退回原文
            self.ctx.logger.warning("LLM 总结失败，返回原始内容：%s", exc)
            return text

        if isinstance(result, dict) and result.get("success") and str(result.get("response") or "").strip():
            return str(result["response"]).strip()
        self.ctx.logger.warning("LLM 总结未成功，返回原始内容：%s", result)
        return text

    # ---------------------------------------------------------------- #
    # Tool: web_search（文搜文）
    # ---------------------------------------------------------------- #

    @Tool(
        "web_search",
        brief_description="联网搜索：查询最新信息、事实核查、找资料",
        detailed_description=(
            "当你不知道答案、需要最新信息、或用户明确要求查询时使用。\n"
            "参数说明：\n"
            "- query：string，必填。搜索关键词，可以是自然语言问句或关键词组合。\n"
            "- 若已知具体网页地址，也可直接把 URL 传进来，会自动转为读取该网页正文。\n"
            "返回若干条结果的标题、链接与摘要。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="搜索关键词、完整问题，或一个网页地址",
                required=True,
            ),
        ],
    )
    async def handle_web_search(self, query: str = "", **kwargs: Any) -> dict[str, Any]:
        """执行网页搜索（收到 URL 时转为读正文）并把结果交回模型。"""
        del kwargs
        config = self.config
        if not config.plugin.enabled:
            return {"success": False, "content": "联网搜索插件已被禁用。"}

        text = (query or "").strip()
        if not text:
            return {"success": False, "content": "没有提供搜索关键词。"}

        if config.reading.enabled and looks_like_url(text):
            return await self._reading_tool_result(text)

        pipeline = self._pipeline
        if pipeline is None:
            return {"success": False, "content": "搜索组件未就绪，请稍后再试。"}

        try:
            outcome = await pipeline.search(text)
        except Exception as exc:  # noqa: BLE001 - 工具层绝不把异常泄漏给宿主，一律转成可读文案
            self.ctx.logger.warning("web_search 失败：%s", exc)
            return {"success": False, "content": describe_error(exc)}

        self.ctx.logger.info(
            "web_search query=%r engines=%s hits=%d elapsed=%dms cache=%s",
            text[:30],
            ",".join(f"{name}:{status}" for name, status in outcome.engine_status.items()),
            len(outcome.hits),
            outcome.elapsed_ms,
            outcome.from_cache,
        )
        content = render_outcome(outcome, max_chars=config.llm.max_prompt_chars)
        if outcome.ok:
            content = await self._maybe_summarize(content)
        return {"success": outcome.ok, "content": content}

    # ---------------------------------------------------------------- #
    # Tool: read_url（深路径）
    # ---------------------------------------------------------------- #

    @Tool(
        "read_url",
        brief_description="读取指定网页的正文并交给你阅读",
        detailed_description=(
            "当你已经知道具体网址、需要看页面内容时使用（例如搜索结果里的某一条）。\n"
            "参数说明：\n"
            "- url：string，必填。要读取的完整网址，必须以 http:// 或 https:// 开头。\n"
            "返回页面的标题与正文文本。正文过长会被截断。\n"
            "注意：这是慢路径，比搜索慢，只在确实需要看页面内容时使用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="要读取的完整网址",
                required=True,
            ),
        ],
    )
    async def handle_read_url(self, url: str = "", **kwargs: Any) -> dict[str, Any]:
        """读取指定网页正文。"""
        del kwargs
        if not self.config.plugin.enabled:
            return {"success": False, "content": "联网搜索插件已被禁用。"}
        if not self.config.reading.enabled:
            return {"success": False, "content": "网页阅读功能已在配置中关闭。"}

        target = (url or "").strip()
        if not target:
            return {"success": False, "content": "没有提供网址。"}
        return await self._reading_tool_result(target)

    async def _reading_tool_result(self, url: str) -> dict[str, Any]:
        """读正文并渲染成工具返回值（两条入口共用）。"""
        try:
            result = await self._read_page(url)
        except Exception as exc:  # noqa: BLE001 - 同上：转成可读文案而不是堆栈
            self.ctx.logger.warning("read_url 失败：%s", exc)
            return {"success": False, "content": describe_error(exc)}

        self.ctx.logger.info(
            "read_url url=%s strategy=%s chars=%d elapsed=%dms hops=%d",
            url[:60],
            result.strategy,
            len(result.text),
            result.elapsed_ms,
            result.hops,
        )
        content = render_reading(result, max_chars=self.config.llm.max_prompt_chars)
        if result.text.strip():
            content = await self._maybe_summarize(content)
        return {
            "success": bool(result.text.strip()),
            "content": content,
        }

    # ---------------------------------------------------------------- #
    # Tool: image_search（文搜图）
    # ---------------------------------------------------------------- #

    @Tool(
        "image_search",
        brief_description="搜索图片并直接发送到聊天里",
        detailed_description=(
            "当用户**明确想要一张图片**时使用，例如「来张猫图」「找几张赛博朋克的图」「发张风景照」。\n"
            "参数说明：\n"
            "- query：string，必填。图片的关键词，例如「布偶猫」「赛博朋克城市」。\n"
            "图片会由你直接发送到聊天里，你不需要也无法自己贴图。\n"
            "注意：如果用户只是在闲聊中提到某个事物、并没有要图片，就不要调用本工具。\n"
            "如果用户发来一张图并问「这是什么」，那要用图搜图/识图，而不是本工具。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="要搜索的图片关键词",
                required=True,
            ),
        ],
    )
    async def handle_image_search(self, query: str = "", **kwargs: Any) -> dict[str, Any]:
        """搜索图片并发送到当前聊天流。"""
        stream_id = str(kwargs.get("stream_id", "") or "")
        config = self.config
        if not config.plugin.enabled:
            return {"success": False, "content": "联网搜索插件已被禁用。"}
        if not config.images.enabled:
            return {"success": False, "content": "图片搜索功能已在配置中关闭。"}

        text = (query or "").strip()
        if not text:
            return {"success": False, "content": "没有提供图片关键词。"}

        pipeline = self._image_pipeline
        if pipeline is None:
            return {"success": False, "content": "图片搜索组件未就绪，请稍后再试。"}

        try:
            outcome = await pipeline.search(text, limit=config.images.max_images_per_call)
        except Exception as exc:  # noqa: BLE001 - 工具层绝不把异常泄漏给宿主
            self.ctx.logger.warning("image_search 失败：%s", exc)
            return {"success": False, "content": describe_error(exc)}

        sent = await self._send_images(outcome, stream_id)
        self.ctx.logger.info(
            "image_search query=%r status=%s candidates=%d sent=%d elapsed=%dms",
            text[:30],
            outcome.status,
            outcome.candidates_seen,
            sent,
            outcome.elapsed_ms,
        )

        result: dict[str, Any] = {
            "success": bool(sent),
            "content": render_image_outcome(outcome, sent=sent),
        }
        if config.images.preview_to_model and outcome.images:
            # 官方多模态通道：宿主会按模型 visual 能力决定是否真的带图
            result["content_items"] = [image.to_content_item() for image in outcome.images[:2]]
        return result

    async def _send_images(self, outcome: Any, stream_id: str) -> int:
        """把图片发到聊天流，返回成功张数。"""
        if not outcome.images or not stream_id:
            return 0
        sent = 0
        for image in outcome.images:
            try:
                if await self.ctx.send.image(image.to_base64(), stream_id):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - 单张失败不应影响其余图片
                self.ctx.logger.warning("发送图片失败：%s", exc)
        return sent

    # ---------------------------------------------------------------- #
    # Tool: image_lookup（图搜图 / 图搜文）
    # ---------------------------------------------------------------- #

    @Tool(
        "image_lookup",
        brief_description="用图片去互联网上搜相关的信息（来源、相似图）",
        detailed_description=(
            "当用户**发了一张图片**并问「这是什么」「出自哪」「这是谁」「找原图」"
            "「有没有一样的图」时使用。\n"
            "本工具会把图片交给多个免密钥以图搜源引擎（ascii2d / IQDB / Bing 视觉搜索等），"
            "返回图片来源、作者、相似度与相似图链接。\n"
            "参数说明：\n"
            "- message_id：string，选填。图片所在消息的 msg_id。\n"
            "  用户先发图、再发文字问「这是什么」时，**图片在另一条消息里**，"
            "把那条消息的 msg_id 填进来最准；不填则自动用最近一条带图的消息。\n"
            "使用要点：\n"
            "- 本工具就是用来**上网搜这张图**的；如果所有引擎都不可用，"
            "它会明确告诉你「搜不到」——这时**不要重复调用**，"
            "改为依据你已经在上下文里看到的图像内容直接回答；\n"
            "- 如果用户想要的是**新的相似图片**（而不是这张图的出处），"
            "请依据图像内容提取关键词后调用 image_search。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id",
                param_type=ToolParamType.STRING,
                description="图片所在消息的 msg_id（选填）",
                required=False,
            ),
        ],
    )
    async def handle_image_lookup(self, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        """取得用户图片并（在可用时）反查来源。"""
        config = self.config
        if not config.plugin.enabled:
            return {"success": False, "content": "联网搜索插件已被禁用。"}
        if not config.reverse.enabled:
            return {"success": False, "content": "以图搜图 / 图搜文功能已在配置中关闭。"}

        # 注意：@Tool 的载荷里**没有** message（那是 @Command 才注入的），
        # 只有 stream_id / chat_id / group_id / user_id / platform。
        stream_id = str(kwargs.get("stream_id", "") or "")
        chat_id = str(kwargs.get("chat_id", "") or "") or stream_id
        try:
            image = await acquire_inbound_image(
                message=kwargs.get("message"),
                ctx=self.ctx,
                chat_id=chat_id,
                stream_id=stream_id,
                message_id=str(message_id or ""),
                http=self._http,
                max_bytes=config.images.max_bytes,
                timeout_seconds=config.network.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 工具层绝不把异常泄漏给宿主
            self.ctx.logger.warning("image_lookup 取图失败：%s", exc)
            return {"success": False, "content": describe_error(exc)}

        try:
            results = await run_reverse_lookup(
                self._reverse_providers,
                image,
                self._http,
                deadline_seconds=config.reverse.deadline_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 反查只是增益，失败不影响"把图交给模型"
            self.ctx.logger.warning("image_lookup 反查异常：%s", exc)
            results = []

        previewed = bool(config.reverse.preview_to_model)
        self.ctx.logger.info(
            "image_lookup source=%s bytes=%d engines=%d hits=%d preview=%s",
            image.source,
            len(image.content),
            len(results),
            sum(1 for result in results if result.ok),
            previewed,
        )

        output: dict[str, Any] = {
            "success": True,
            "content": render_lookup(image=image, results=results, previewed=previewed),
        }
        if previewed and image.has_bytes:
            # 官方多模态通道：宿主按模型 visual 能力决定是否真的把图喂给模型
            output["content_items"] = [image.to_content_item()]
        return output

    # ---------------------------------------------------------------- #
    # Command: /websearch
    # ---------------------------------------------------------------- #

    @Command(
        "websearch",
        description="查看麦麦联网搜索插件的配置与通路状态",
        pattern=r"^/websearch(?:\s+(?P<action>\S+))?\s*$",
        aliases=["/ws"],
    )
    async def handle_websearch(self, **kwargs: Any) -> tuple[bool, str, int]:
        """``/websearch [status]`` —— 输出当前配置与四条通路的状态。"""
        stream_id = str(kwargs.get("stream_id", "") or "")
        matched = kwargs.get("matched_groups") or {}
        action = str(matched.get("action") or "status").lower()

        if action not in {"status", "s"}:
            text = f"未知子命令：{action}（当前仅支持 status）"
            if stream_id:
                await self.ctx.send.text(text, stream_id)
            return False, text, 1

        text = build_status_text(self.config)
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2


def create_plugin() -> WebSearchPlugin:
    """Runner 通过此工厂函数实例化插件。"""
    return WebSearchPlugin()
