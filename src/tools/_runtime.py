"""
========================================
tools/_runtime.py — 工具模块共享的运行时上下文
========================================

这个文件解决一个工程问题：拆分后每个工具子模块都需要访问
config / bucket_mgr / dehydrator / decay_engine / embedding_engine /
logger 这些 server.py 创建的全局对象，但子模块不能反向 import
server.py（会循环 import）。

做法：server.py 在初始化所有组件后调用 init(...) 把引用塞进来；
工具模块全部 `from . import _runtime as rt` 然后用 `rt.bucket_mgr` 即可。

关键行为：
- 提供一个轻量级容器，保存共享对象的引用
- init() 一次性写入；后续工具模块直接读，不修改

不做什么（边界）：
- 不创建任何对象，不做配置加载，不做日志初始化
- 不做线程安全保护：写入只发生在 server.py 启动期，单次

对外暴露：init() / config / bucket_mgr / dehydrator / decay_engine /
         embedding_engine / import_engine / logger / fire_webhook / mark_op
========================================
"""

from typing import Any, Awaitable, Callable, Optional

# --- 共享对象引用，由 server.py 在启动时通过 init(...) 注入 ---
config: Any = None
bucket_mgr: Any = None
dehydrator: Any = None
decay_engine: Any = None
embedding_engine: Any = None
import_engine: Any = None
logger: Any = None

# --- 共享辅助回调（也由 server.py 注入，避免反向 import）---
fire_webhook: Optional[Callable[[str, dict], Awaitable[None]]] = None
mark_op: Optional[Callable[..., None]] = None


def init(**kwargs: Any) -> None:
    """server.py 在创建好所有组件后调用一次，把引用写到本模块全局上。
    测试 fixture 可以再次调用本函数覆盖个别字段，行为同 monkeypatch。"""
    g = globals()
    for k, v in kwargs.items():
        g[k] = v
