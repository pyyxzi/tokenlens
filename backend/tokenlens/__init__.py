# -*- coding: utf-8 -*-
"""tokenlens · 本机 AI 编程 token 用量全景统计

版本号以 config.py 为单一来源，避免两处不一致。
"""
from .config import APP_NAME as __appname__       # noqa: F401
from .config import APP_TAGLINE as __tagline__    # noqa: F401
from .config import APP_VERSION as __version__    # noqa: F401

__all__ = ["__appname__", "__tagline__", "__version__"]
