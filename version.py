"""插件版本号（与 ``_manifest.json`` 的 ``version`` 保持一致，由测试断言）。

正式发布前一律 ``1.0.0``：版本号只在**对外发布**的节点上推进，
避免"修一个 bug 就跳一个版本"让早期 Tag 和 Release 变得零碎。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
