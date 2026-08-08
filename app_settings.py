from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SETTINGS_FORMAT_VERSION = 1
FUNCTION_HOTKEYS = tuple(f"F{number}" for number in range(1, 21))
_FUNCTION_KEY_BASE_VK = 0x70


def normalize_hotkey_name(value: Any) -> str | None:
    """Return an F1..F20 name from UI text, pynput keys, or a VK code."""

    if isinstance(value, int):
        number = int(value) - _FUNCTION_KEY_BASE_VK + 1
        return f"F{number}" if 1 <= number <= 20 else None

    vk = getattr(value, "vk", None)
    if vk is None:
        vk = getattr(getattr(value, "value", None), "vk", None)
    if vk is not None:
        normalized = normalize_hotkey_name(int(vk))
        if normalized is not None:
            return normalized

    name = getattr(value, "name", None)
    text = str(name if name is not None else value).strip().upper()
    for prefix in ("KEY:", "KEY."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if text in FUNCTION_HOTKEYS:
        return text
    return None


def hotkey_vk(name: str) -> int:
    normalized = normalize_hotkey_name(name)
    if normalized is None:
        raise ValueError("Горячая клавиша должна быть от F1 до F20")
    return _FUNCTION_KEY_BASE_VK + int(normalized[1:]) - 1


@dataclass(frozen=True, slots=True)
class HotkeySettings:
    play: str = "F8"
    record: str = "F9"
    finish_recording: str = "F10"
    stop: str = "F12"

    def __post_init__(self) -> None:
        normalized = {
            field: normalize_hotkey_name(getattr(self, field))
            for field in ("play", "record", "finish_recording", "stop")
        }
        if any(value is None for value in normalized.values()):
            raise ValueError("Горячие клавиши должны быть от F1 до F20")
        values = tuple(value for value in normalized.values() if value is not None)
        if len(set(values)) != len(values):
            raise ValueError("Для разных действий нужны разные горячие клавиши")
        for field, value in normalized.items():
            object.__setattr__(self, field, value)

    @property
    def reserved_names(self) -> frozenset[str]:
        return frozenset(
            (self.play, self.record, self.finish_recording, self.stop)
        )

    @property
    def reserved_vks(self) -> frozenset[int]:
        return frozenset(hotkey_vk(name) for name in self.reserved_names)

    @property
    def recording_stop_names(self) -> frozenset[str]:
        return frozenset((self.finish_recording, self.stop))

    @property
    def recording_stop_vks(self) -> frozenset[int]:
        return frozenset(hotkey_vk(name) for name in self.recording_stop_names)

    def to_document(self) -> dict[str, Any]:
        return {
            "version": SETTINGS_FORMAT_VERSION,
            "hotkeys": {
                "play": self.play,
                "record": self.record,
                "finish_recording": self.finish_recording,
                "stop": self.stop,
            },
        }


def default_settings_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / "MacroPilot" / "settings.json"


def load_hotkey_settings(path: Path | None = None) -> HotkeySettings:
    source = path or default_settings_path()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        if document.get("version") != SETTINGS_FORMAT_VERSION:
            return HotkeySettings()
        hotkeys = document.get("hotkeys")
        if not isinstance(hotkeys, dict):
            return HotkeySettings()
        return HotkeySettings(
            play=hotkeys.get("play", "F8"),
            record=hotkeys.get("record", "F9"),
            finish_recording=hotkeys.get("finish_recording", "F10"),
            stop=hotkeys.get("stop", "F12"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return HotkeySettings()


def save_hotkey_settings(
    settings: HotkeySettings,
    path: Path | None = None,
) -> Path:
    destination = path or default_settings_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(settings.to_document(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination
