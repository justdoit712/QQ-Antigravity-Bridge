"""Local configuration. AppSecret is encrypted with Windows user DPAPI."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / ".local" / "config.json"
DEFAULT_PERSONA = (
    "你叫 doro，是用户的 QQ 聊天伙伴。用自然、简洁的中文交流，"
    "友善、有一点幽默，记住本次聊天提供的上下文。"
    "不知道就直说，不虚构经历、联网结果或图片内容。"
)


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
            raise ValueError("请填写机器人管理页上的数字 AppID。")
        if not self.app_secret.strip():
            raise ValueError("请填写机器人管理页上的 AppSecret。")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("此桌面版使用 Windows DPAPI 保存密钥，请在 Windows 上运行。")
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
        raise RuntimeError("Windows 无法保护或解密 AppSecret，请用保存配置的 Windows 账号运行。")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        secret = data.get("app_secret_dpapi", "")
        groups = data.get("group_ids", [])
        if not isinstance(groups, list) or any(not isinstance(group, str) or not group for group in groups):
            raise ValueError()
        return Config(
            app_id=str(data.get("app_id", "")),
            app_secret=_crypt(base64.b64decode(secret, validate=True), decrypt=True).decode("utf-8")
            if secret else "",
            model=str(data.get("model", "")),
            persona=str(data.get("persona", DEFAULT_PERSONA)),
            owner_id=str(data.get("owner_id", "")),
            group_ids=list(dict.fromkeys(groups)),
        )
    except (ValueError, TypeError, UnicodeError, KeyError) as exc:
        raise RuntimeError("本地配置格式无效，请重新填写并保存配置。") from exc


def save_config(config: Config) -> None:
    # Only encrypted secret bytes ever reach a configuration or temporary file.
    secret = base64.b64encode(_crypt(config.app_secret.encode("utf-8"))).decode("ascii") \
        if config.app_secret else ""
    data = {
        "version": 2,
        "app_id": config.app_id.strip(),
        "app_secret_dpapi": secret,
        "model": config.model.strip(),
        "persona": config.persona,
        "owner_id": config.owner_id,
        "group_ids": list(dict.fromkeys(config.group_ids)),
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, CONFIG_PATH)
