"""为 STRM 媒体信息预热插件提供的宿主桩模块。

插件运行时依赖 MoviePilot 宿主包，测试环境没有宿主源码，
因此在导入插件前注入最小可用的假模块，使纯逻辑可被单测覆盖。
"""

import enum
import sys
import types
from pathlib import Path
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


class _FakeFileItem:
    """最小文件项替身，字段与宿主 FileItem 的常用部分一致。"""

    def __init__(self, storage: str = "local", type: str = "file", path: str = None,
                 name: str = None, size: int = None, **kwargs: Any) -> None:
        self.storage = storage
        self.type = type
        self.path = path
        self.name = name
        self.size = size
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        """便于断言失败时阅读。"""
        return f"FileItem(storage={self.storage!r}, path={self.path!r}, size={self.size!r})"


class _FakeStorageChain:
    """记录云盘操作的假存储链，测试可预置文件与失败行为。"""

    # 云盘上已存在的文件：path -> size
    remote_files: Dict[str, int] = {}
    # 调用记录
    calls: list = []
    # 上传是否失败
    upload_fails: int = 0
    # get_folder 是否失败
    folder_fails: bool = False

    @classmethod
    def reset(cls) -> None:
        """清空状态，供每个用例独立使用。"""
        cls.remote_files = {}
        cls.remote_dirs = set()
        cls.calls = []
        cls.upload_fails = 0
        cls.folder_fails = False
        cls.get_folder_unsupported = False

    def get_file_item(self, storage: str, path: Any) -> Optional[_FakeFileItem]:
        """查询云盘文件或目录。"""
        key = str(path)
        self.calls.append(("get_file_item", storage, key))
        if key in self.remote_dirs or key == "/":
            return _FakeFileItem(storage=storage, type="dir", path=key,
                                 name=key.rsplit("/", 1)[-1] or "/")
        if key not in self.remote_files:
            return None
        return _FakeFileItem(storage=storage, path=key, name=key.rsplit("/", 1)[-1],
                             size=self.remote_files[key])

    # 模拟 MoviePilot v2.11.3：链上有 get_folder，但没有系统模块实现，恒返回 None
    get_folder_unsupported: bool = False
    # 已存在的云盘目录集合
    remote_dirs: set = set()

    def get_folder(self, storage: str, path: Any) -> Optional[_FakeFileItem]:
        """获取或创建云盘目录。"""
        key = str(path)
        self.calls.append(("get_folder", storage, key))
        if self.get_folder_unsupported or self.folder_fails:
            return None
        _FakeStorageChain.remote_dirs.add(key)
        return _FakeFileItem(storage=storage, type="dir", path=key, name=key.rsplit("/", 1)[-1])

    def create_folder(self, fileitem: Any, name: str) -> Optional[_FakeFileItem]:
        """在父目录下创建子目录。"""
        parent = str(fileitem.path).rstrip("/")
        key = f"{parent}/{name}"
        self.calls.append(("create_folder", fileitem.storage, key))
        if self.folder_fails:
            return None
        _FakeStorageChain.remote_dirs.add(key)
        return _FakeFileItem(storage=fileitem.storage, type="dir", path=key, name=name)

    def upload_file(self, fileitem: Any, path: Any, new_name: str = None) -> Optional[_FakeFileItem]:
        """上传文件到云盘目录。"""
        target = f"{fileitem.path.rstrip('/')}/{new_name or Path(str(path)).name}"
        self.calls.append(("upload_file", fileitem.storage, target, str(path)))
        if _FakeStorageChain.upload_fails > 0:
            _FakeStorageChain.upload_fails -= 1
            return None
        try:
            size = Path(str(path)).stat().st_size
        except OSError:
            size = 0
        self.remote_files[target] = size
        return _FakeFileItem(storage=fileitem.storage, path=target,
                             name=target.rsplit("/", 1)[-1], size=size)

    def download_file(self, fileitem: Any, path: Any = None) -> Optional[Any]:
        """把远端文件下载到本地临时路径。"""
        self.calls.append(("download_file", fileitem.storage, str(fileitem.path)))
        target = Path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"remote-content")
        return target


class _FakeStorageHelper:
    """返回测试注入的存储配置。"""

    storagies: list = []

    def get_storagies(self) -> list:
        """返回存储配置列表。"""
        return list(self.storagies)


class _FakeDirectoryHelper:
    """返回测试注入的目录配置。"""

    dirs: list = []

    def get_dirs(self) -> list:
        """返回目录配置列表。"""
        return list(self.dirs)


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


# 进程内只安装一次：重复安装会替换事件管理器，导致已导入的插件注册信息丢失
_INSTALLED: Optional[types.SimpleNamespace] = None


def install_stubs() -> types.SimpleNamespace:
    """注入宿主桩模块并返回测试可用的句柄（幂等）。"""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
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
    module("app.sdk.services", MediaServerHelper=_FakeMediaServerHelper,
           StorageHelper=_FakeStorageHelper)
    module("app.plugins", _PluginBase=_FakePluginBase)
    module("app.chain", )
    module("app.chain.storage", StorageChain=_FakeStorageChain)
    module("app.application", )
    module("app.application.directory", DirectoryHelper=_FakeDirectoryHelper)
    module("app.schemas", ServiceInfo=object, FileItem=_FakeFileItem)
    module("app.schemas.types", EventType=_FakeEnum, MessageType=_FakeEnum)
    _INSTALLED = types.SimpleNamespace(
        event_manager=event_manager,
        request_utils=_FakeRequestUtils,
        media_server_helper=_FakeMediaServerHelper,
        storage_chain=_FakeStorageChain,
        storage_helper=_FakeStorageHelper,
        directory_helper=_FakeDirectoryHelper,
        file_item=_FakeFileItem,
        response=_FakeResponse,
    )
    return _INSTALLED
