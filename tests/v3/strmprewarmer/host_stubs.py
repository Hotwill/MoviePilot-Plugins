"""为 STRM 媒体信息预热插件提供的宿主桩模块。

插件运行时依赖 MoviePilot 宿主包，测试环境没有宿主源码，
因此在导入插件前注入最小可用的假模块，使纯逻辑可被单测覆盖。
"""

import enum
import sys
import types
from typing import Any, Dict, Optional


class _FakeEnum(enum.Enum):
    """用于替代宿主事件与消息类型的枚举。"""

    Plugin = "插件"
    TransferComplete = "transfer.complete"
    WebhookMessage = "webhook.message"
    PluginAction = "plugin.action"


class _FakeEventManager:
    """记录事件注册关系的假事件管理器。"""

    def __init__(self) -> None:
        self.registered = []

    def register(self, etype: Any):
        """返回装饰器，仅记录注册信息。"""

        def decorator(func):
            self.registered.append((etype, func.__name__))
            return func

        return decorator


class _FakeResponse:
    """可配置的假 HTTP 响应。"""

    def __init__(self, status_code: int = 200, payload: Any = None, content: bytes = b"{}"):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        """返回预置的 JSON 数据。"""
        return self._payload


class _FakeRequestUtils:
    """把请求记录到类变量的假 HTTP 客户端。"""

    calls = []
    responses = {}

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def get_res(self, url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any):
        """记录 GET 请求并返回预置响应。"""
        _FakeRequestUtils.calls.append(("GET", url, params))
        return _FakeRequestUtils.responses.get(("GET", url)) or _FakeRequestUtils.responses.get("default")

    def post_res(self, url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any):
        """记录 POST 请求并返回预置响应。"""
        _FakeRequestUtils.calls.append(("POST", url, params))
        return _FakeRequestUtils.responses.get(("POST", url)) or _FakeRequestUtils.responses.get("default")


class _FakePluginBase:
    """提供插件基类必需的配置与数据接口。"""

    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}
        self._config: Dict[str, Any] = {}
        self.messages = []

    def update_config(self, config: dict, plugin_id: str = None) -> bool:
        """保存配置。"""
        self._config = dict(config)
        return True

    def get_config(self, plugin_id: str = None) -> dict:
        """读取配置。"""
        return self._config

    def save_data(self, key: str, value: Any, plugin_id: str = None) -> None:
        """保存插件数据。"""
        self._store[key] = value

    def get_data(self, key: str = None, plugin_id: str = None) -> Any:
        """读取插件数据。"""
        return self._store.get(key)

    def post_message(self, **kwargs: Any) -> None:
        """记录通知消息。"""
        self.messages.append(kwargs)


class _FakeMediaServerHelper:
    """返回测试注入的媒体服务器服务。"""

    services: Dict[str, Any] = {}
    configs: Dict[str, Any] = {}

    def get_services(self, type_filter: str = None, name_filters=None) -> Dict[str, Any]:
        """按名称过滤返回服务。"""
        if not name_filters:
            return dict(self.services)
        return {name: service for name, service in self.services.items() if name in name_filters}

    def get_configs(self) -> Dict[str, Any]:
        """返回服务配置。"""
        return dict(self.configs)


def install_stubs() -> types.SimpleNamespace:
    """注入宿主桩模块并返回测试可用的句柄。"""
    event_manager = _FakeEventManager()

    def module(name: str, **attrs: Any) -> types.ModuleType:
        """创建并注册一个桩模块。"""
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    module("app")
    module("app.sdk")
    module("app.sdk.events", Event=object, eventmanager=event_manager)
    module("app.sdk.logging", logger=types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None, warn=lambda *a, **k: None))
    module("app.sdk.network", RequestUtils=_FakeRequestUtils)
    module("app.sdk.services", MediaServerHelper=_FakeMediaServerHelper)
    module("app.plugins", _PluginBase=_FakePluginBase)
    module("app.schemas", ServiceInfo=object)
    module("app.schemas.types", EventType=_FakeEnum, MessageType=_FakeEnum)
    return types.SimpleNamespace(
        event_manager=event_manager,
        request_utils=_FakeRequestUtils,
        media_server_helper=_FakeMediaServerHelper,
        response=_FakeResponse,
    )
