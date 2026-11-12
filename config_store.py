"""Local state with Windows DPAPI or environment-provided server credentials."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / ".local" / "config.json"
IS_WINDOWS = os.name == "nt"
DEFAULT_PERSONA = (
    "你叫 doro，是用户的 QQ 聊天伙伴。用自然、简洁的中文交流，"
    "友善、有一点幽默，记住本次聊天提供的上下文。"
    "不知道就直说，不虚构经历、联网结果或图片内容。"
)


class ConfigError(RuntimeError):
    """A configuration error whose message is safe to show to the operator."""


@dataclass
class Config:
    app_id: str = ""
    app_secret: str = ""
    model: str = ""
    persona: str = DEFAULT_PERSONA
    owner_id: str = ""
    group_ids: list[str] = field(default_factory=list)

    def validate_qq(self) -> None:
        if not self.app_id.strip().isascii() or not self.app_id.strip().isdigit():
            raise ValueError("请填写机器人管理页上的数字 AppID，或设置 QQBOT_APP_ID。")
        if not self.app_secret.strip():
            raise ValueError("请填写机器人管理页上的 AppSecret，或设置 QQBOT_APP_SECRET。")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if not IS_WINDOWS:
        raise ConfigError("此桌面版使用 Windows DPAPI 保存密钥，请在 Windows 上运行。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    target = _Blob()
    if decrypt:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
                              ctypes.POINTER(_Blob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        description = None
    else:
        operation = crypt32.CryptProtectData
        operation.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR,
                              ctypes.POINTER(_Blob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        description = "QQ Antigravity Bridge"
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), description, None, None, None, 1,
                     ctypes.byref(target)):
        raise ConfigError("Windows 无法保护或解密 AppSecret，请用保存配置的 Windows 账号运行。")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def load_config() -> Config:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        if not isinstance(data, dict):
            raise ValueError()
        secret = data.get("app_secret_dpapi", "")
        secret_override = os.environ.get("QQBOT_APP_SECRET")
        if secret_override is not None:
            app_secret = secret_override
        elif secret:
            if not IS_WINDOWS:
                raise ConfigError("此配置的 AppSecret 由 Windows 加密。请在服务器设置 QQBOT_APP_SECRET。")
            app_secret = _crypt(base64.b64decode(secret, validate=True), decrypt=True).decode("utf-8")
        else:
            app_secret = ""
        groups = data.get("group_ids", [])
        if not isinstance(groups, list) or any(not isinstance(group, str) or not group for group in groups):
            raise ValueError()
        saved_app_id = str(data.get("app_id", ""))
        app_id = os.environ.get("QQBOT_APP_ID", saved_app_id)
        changed_bot = app_id.strip() != saved_app_id.strip()
        return Config(
            app_id=app_id,
            app_secret=app_secret,
            model=os.environ.get("QQBOT_MODEL", str(data.get("model", ""))),
            persona=os.environ.get("QQBOT_PERSONA", str(data.get("persona", DEFAULT_PERSONA))),
            owner_id="" if changed_bot else str(data.get("owner_id", "")),
            group_ids=[] if changed_bot else list(dict.fromkeys(groups)),
        )
    except (ValueError, TypeError, UnicodeError, KeyError) as exc:
        raise ConfigError("本地配置格式无效，请重新填写并保存配置。") from exc


def save_config(config: Config) -> None:
    data = {
        "version": 2,
        "app_id": config.app_id.strip(),
        "model": config.model.strip(),
        "persona": config.persona,
        "owner_id": config.owner_id,
        "group_ids": list(dict.fromkeys(config.group_ids)),
    }
    if IS_WINDOWS:
        data["app_secret_dpapi"] = base64.b64encode(_crypt(config.app_secret.encode("utf-8"))).decode("ascii") \
            if config.app_secret else ""
    # Server secrets stay in the environment. Only state and settings reach disk.
    CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not IS_WINDOWS:
        CONFIG_PATH.parent.chmod(0o700)
    descriptor, filename = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=CONFIG_PATH.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)
