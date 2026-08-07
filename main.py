from __future__ import annotations

import math
import platform
import ctypes
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Iterable

from image_matcher import ImageMatch, ScreenImageMatcher
from macro_core import (
    APP_NAME,
    APP_VERSION,
    EXAMPLE_SCRIPT,
    IfBlock,
    MAX_EVENTS,
    MAX_RECORDED_EVENTS,
    MacroFormatError,
    RECORDING_WARNING_EVENTS,
    RepeatBlock,
    ScriptCommand,
    ScriptError,
    ScriptNode,
    VARIABLE_PATTERN,
    VARIABLE_TEMPLATE_PATTERN,
    compact_repeated_key_events,
    describe_event,
    events_to_script,
    load_macro,
    macro_duration,
    parse_script,
    save_macro,
)
from ocr_reader import OcrError, ScreenOcrReader, extract_first_number
from project_config import AUTHOR_NAME, AUTHOR_GITHUB, PROJECT_REPOSITORY, PROJECT_URL, SUPPORT_URL
from update_service import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateError,
    cleanup_previous_update_stage,
    choose_release_asset,
    download_release_asset,
    fetch_latest_release,
    handle_update_command,
    inspect_update_archive,
    is_newer_version,
    launch_update_installer,
    temporary_update_path,
)
from windows_input import (
    KEYDOWN_MESSAGES,
    KEYUP_MESSAGES,
    LLKHF_EXTENDED,
    VK_F9,
    VK_F10,
    VK_F12,
    WINDOWS_NATIVE_AVAILABLE,
    ScanKey,
    WindowsKeyboardController,
    WindowsMouseController,
    WindowsPhysicalMouseBlocker,
    WindowsRawMouseListener,
    enable_windows_dpi_awareness,
    get_pressed_scan_keys,
    parse_scan_token,
    scan_key_from_descriptor,
)


try:
    from pynput import keyboard, mouse
except Exception as import_error:  # pragma: no cover - depends on the desktop OS
    keyboard = None  # type: ignore[assignment]
    mouse = None  # type: ignore[assignment]
    PYNPUT_IMPORT_ERROR: Exception | None = import_error
else:
    PYNPUT_IMPORT_ERROR = None


MAX_TABLE_ROWS = 20_000
DEFAULT_MINIMIZE_ACTION_WINDOW = True
RECORDING_PRECISION_OPTIONS = {
    "Экономная · 20/с": 0.050,
    "Обычная · 40/с": 0.025,
    "Высокая · 100/с": 0.010,
    "Максимальная · 200/с": 0.005,
}
DEFAULT_RECORDING_PRECISION = "Обычная · 40/с"
DEFAULT_RECORD_MOUSE_MOVES = True
DEFAULT_BLOCK_PHYSICAL_MOUSE = False
DRAG_PRECISION_MULTIPLIER = 0.64
# Keep both button states visible long enough for games that sample the mouse
# only once per rendered frame. The real-time minimum is deliberately not
# divided by playback speed; the absolute recording timeline catches up after
# the pulse without accumulating drift.
MIN_MOUSE_HOLD_SECONDS = 0.060
MIN_MOUSE_GAP_SECONDS = 0.040
IMAGE_SEARCH_INTERVAL_SECONDS = 0.20
TK_CONTROL_MASK = 0x0004
TK_SHIFT_MASK = 0x0001
UI_COLORS = {
    "bg": "#0b1020",
    "surface": "#111827",
    "card": "#172033",
    "card_hover": "#202c43",
    "border": "#2a3852",
    "text": "#e8eef8",
    "muted": "#93a4bd",
    "accent": "#5b8cff",
    "accent_hover": "#739cff",
    "record": "#ef5b67",
    "record_hover": "#ff7180",
    "success": "#3ecf8e",
    "support": "#e96fad",
    "support_hover": "#f285bd",
    "editor": "#0d1424",
}
SCRIPT_HELP = """КОМАНДЫ

WAIT секунды
MOVE x y [секунды]
MOVE_BY dx dy [секунды]
CLICK [кнопка] [раз] [интервал]
CLICK_AT x y [кнопка] [раз] [интервал]
WAIT_IMAGE "файл.png" [тайм-аут] [сходство]
CLICK_IMAGE "файл.png" [кнопка] [тайм-аут] [сходство]
OCR_TEXT имя x y ширина высота [язык]
OCR_NUMBER имя x y ширина высота [язык]
DOWN кнопка
UP кнопка
SCROLL dy
SCROLL dx dy

PRESS клавиша
KEY_DOWN клавиша
KEY_UP клавиша
HOTKEY клавиша ...
TYPE "текст" [интервал]

IF_TEXT имя ==|!=|CONTAINS|NOT_CONTAINS "текст"
IF_NUMBER имя ==|!=|<|<=|>|>= число
ELSE
END

Переменную можно вставить в TYPE: ${имя}

REPEAT число
    команды
END

КНОПКИ МЫШИ
left, right, middle

ПОИСК ИЗОБРАЖЕНИЯ
Тайм-аут 0 ожидает без ограничения.
Сходство: от 0.5 до 1; по умолчанию 0.9.
Относительный путь считается от файла сценария.

OCR И ПЕРЕМЕННЫЕ
«Выбрать OCR-область…» вставляет координаты.
OCR_TEXT сохраняет текст, OCR_NUMBER — первое число.
Язык auto использует языки профиля Windows;
можно явно указать ru-RU или en-US.
IF_TEXT не учитывает регистр.
Результат можно вставить в TYPE: ${имя}

ПРИМЕРЫ КЛАВИШ
enter, tab, space, esc, backspace,
delete, home, end, page_up,
page_down, up, down, left, right,
ctrl, alt, shift, cmd, f1 ... f20
Windows: scan:11, scan:e0-4d

# Это комментарий

F9 — начать или продолжить запись
F12 — остановить выполнение
F10 — закончить запись
"""


def serialize_key(value: Any) -> dict[str, Any]:
    if keyboard is None:
        raise RuntimeError("pynput недоступен")
    if isinstance(value, keyboard.Key):
        return {"kind": "special", "value": value.name}
    char = getattr(value, "char", None)
    if char is not None:
        return {"kind": "char", "value": char}
    vk = getattr(value, "vk", None)
    if vk is not None:
        return {"kind": "vk", "value": int(vk)}
    raise ValueError(f"Не удалось определить клавишу: {value!r}")


def deserialize_key(value: dict[str, Any]) -> Any:
    if keyboard is None:
        raise RuntimeError("pynput недоступен")
    kind, raw = value["kind"], value["value"]
    if kind == "scan":
        if WINDOWS_NATIVE_AVAILABLE:
            return scan_key_from_descriptor(value)
        vk = int(value.get("vk", 0))
        if vk:
            return keyboard.KeyCode.from_vk(vk)
        raise ValueError("Макрос со scan-кодами клавиатуры можно воспроизвести только в Windows")
    if kind == "char":
        return keyboard.KeyCode.from_char(raw)
    if kind == "vk":
        return keyboard.KeyCode.from_vk(raw)
    result = getattr(keyboard.Key, raw, None)
    if result is None:
        raise ValueError(f"Неизвестная специальная клавиша: {raw}")
    return result


def resolve_script_key(token: str) -> Any:
    if keyboard is None:
        raise RuntimeError("pynput недоступен")
    aliases = {
        "control": "ctrl",
        "escape": "esc",
        "return": "enter",
        "windows": "cmd",
        "win": "cmd",
        "super": "cmd",
        "pgup": "page_up",
        "pgdn": "page_down",
        "del": "delete",
        "ins": "insert",
    }
    lowered = token.lower()
    lowered = aliases.get(lowered, lowered)
    if lowered.startswith("scan:"):
        return parse_scan_token(token)
    if lowered.startswith("vk:"):
        try:
            return keyboard.KeyCode.from_vk(int(lowered[3:]))
        except ValueError as exc:
            raise ValueError(f"Неверный код клавиши {token!r}") from exc
    if lowered.startswith("char:"):
        try:
            raw = bytes.fromhex(token[5:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"Неверный код символа {token!r}") from exc
        return keyboard.KeyCode.from_char(raw)
    if len(token) == 1:
        return keyboard.KeyCode.from_char(token)
    special = getattr(keyboard.Key, lowered, None)
    if special is None:
        raise ValueError(f"Неизвестная клавиша {token!r}")
    return special


def resolve_button(name: str) -> Any:
    if mouse is None:
        raise RuntimeError("pynput недоступен")
    return {
        "left": mouse.Button.left,
        "right": mouse.Button.right,
        "middle": mouse.Button.middle,
    }[name]


class EventRecorder:
    def __init__(
        self,
        record_moves: bool,
        request_stop: Callable[[str], None],
        report_error: Callable[[str], None],
        report_warning: Callable[[str], None] | None = None,
        move_interval: float = RECORDING_PRECISION_OPTIONS[DEFAULT_RECORDING_PRECISION],
    ) -> None:
        self.record_moves = record_moves
        self.request_stop = request_stop
        self.report_error = report_error
        self.report_warning = report_warning or (lambda _text: None)
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.started_at = 0.0
        self.last_move_at = -1.0
        self.last_relative_move_at = -1.0
        self.pending_relative_dx = 0
        self.pending_relative_dy = 0
        self.pending_relative_at: float | None = None
        self.held_mouse_buttons: set[str] = set()
        self.relative_mouse_buttons: set[str] = set()
        self.held_keyboard_keys: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.last_mouse_position: tuple[int, int] | None = None
        self.active = False
        self.stop_requested = threading.Event()
        self.last_error: str | None = None
        self.max_recorded_events = MAX_RECORDED_EVENTS
        self.capacity_base_count = 0
        self.warning_event_count = RECORDING_WARNING_EVENTS
        self.capacity_warning_sent = False
        self.move_interval = max(0.001, float(move_interval))
        self.drag_move_interval = max(
            0.001,
            self.move_interval * DRAG_PRECISION_MULTIPLIER,
        )
        self.mouse_listener: Any = None
        self.keyboard_listener: Any = None
        self.raw_mouse_listener: WindowsRawMouseListener | None = None
        self.relative_mouse_enabled = False

    def start(self) -> None:
        if keyboard is None or mouse is None:
            raise RuntimeError("Библиотека pynput не загружена")
        self.mouse_listener = mouse.Listener(
            on_move=self._guard(self._on_move),
            on_click=self._guard(self._on_click),
            on_scroll=self._guard(self._on_scroll),
        )
        if WINDOWS_NATIVE_AVAILABLE:
            self.keyboard_listener = keyboard.Listener(
                win32_event_filter=self._guard(self._on_windows_keyboard_event),
            )
        else:
            self.keyboard_listener = keyboard.Listener(
                on_press=self._guard(self._on_key_down),
                on_release=self._guard(self._on_key_up),
            )
        if WINDOWS_NATIVE_AVAILABLE:
            self.raw_mouse_listener = WindowsRawMouseListener(
                on_move=self._guard(self._on_raw_mouse_move),
                on_error=self._on_raw_mouse_error,
            )
        try:
            self.mouse_listener.start()
            self.keyboard_listener.start()
            # A listener thread can take a moment to install its system hook.
            # Do not minimize the app or start the recording clock until both
            # hooks explicitly report that they are ready.
            self.mouse_listener.wait()
            self.keyboard_listener.wait()
            if self.raw_mouse_listener is not None:
                try:
                    self.raw_mouse_listener.start()
                except Exception as exc:
                    self.raw_mouse_listener.stop()
                    self.raw_mouse_listener = None
                    self.report_warning(
                        "Raw Input мыши недоступен — перетаскивания будут записаны "
                        f"по экранным координатам ({exc})"
                    )
                else:
                    self.relative_mouse_enabled = True
            self.started_at = time.perf_counter()
            self.active = True

            # The user may already be holding a movement key while the game
            # gets focus. A hook would otherwise see only autorepeat events or
            # the eventual release and lose the intended hold duration.
            if WINDOWS_NATIVE_AVAILABLE:
                for key in get_pressed_scan_keys():
                    if key.vk not in {VK_F9, VK_F10, VK_F12}:
                        self._record_key_change(self._scan_descriptor(key), True)
        except Exception:
            self.active = False
            for listener in (
                self.mouse_listener,
                self.keyboard_listener,
                self.raw_mouse_listener,
            ):
                if listener is None:
                    continue
                try:
                    listener.stop()
                except Exception:
                    pass
            raise

    def _guard(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(*args: Any) -> Any:
            try:
                return callback(*args)
            except Exception as exc:  # listener callbacks must not fail silently
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self.report_error(self.last_error)
                self._signal_stop("Запись остановлена из-за ошибки")
                return False

        return guarded

    def _timestamp(self) -> float:
        return max(0.0, time.perf_counter() - self.started_at)

    @staticmethod
    def _format_event_count(value: int) -> str:
        return f"{value:,}".replace(",", " ")

    def _append_locked(
        self,
        event: dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> tuple[bool, str | None, bool]:
        if not self.active or self.stop_requested.is_set():
            return False, None, False
        if len(self.events) >= self.max_recorded_events:
            return False, None, True

        event["t"] = round(
            self._timestamp() if timestamp is None else max(0.0, timestamp),
            6,
        )
        self.events.append(event)
        recorded_event_count = len(self.events)
        total_event_count = self.capacity_base_count + recorded_event_count
        total_capacity = self.capacity_base_count + self.max_recorded_events
        warning: str | None = None
        if not self.capacity_warning_sent and total_event_count >= self.warning_event_count:
            self.capacity_warning_sent = True
            percent = min(100, round(total_event_count * 100 / total_capacity))
            remaining = max(0, total_capacity - total_event_count)
            warning = (
                f"Большая запись: {self._format_event_count(total_event_count)} событий "
                f"({percent}%). Осталось около {self._format_event_count(remaining)}"
            )
        return True, warning, recorded_event_count >= self.max_recorded_events

    def _notify_capacity(self, warning: str | None, limit_reached: bool) -> None:
        if warning is not None:
            self.report_warning(warning)
        if limit_reached:
            limit = self._format_event_count(
                self.capacity_base_count + self.max_recorded_events
            )
            self._signal_stop(
                f"Запись автоматически остановлена: достигнут безопасный предел {limit} событий"
            )

    def _append(self, event: dict[str, Any]) -> bool:
        with self.lock:
            appended, warning, limit_reached = self._append_locked(event)
        self._notify_capacity(warning, limit_reached)
        return appended

    @staticmethod
    def _scan_descriptor(key: ScanKey) -> dict[str, Any]:
        return {
            "kind": "scan",
            "value": key.scan_code,
            "vk": key.vk,
            "extended": key.extended,
        }

    @staticmethod
    def _key_identity(descriptor: dict[str, Any]) -> tuple[Any, ...]:
        if descriptor.get("kind") == "scan":
            return (
                "scan",
                int(descriptor["value"]),
                bool(descriptor.get("extended", False)),
            )
        return (str(descriptor.get("kind")), descriptor.get("value"))

    def _record_key_change(self, descriptor: dict[str, Any], pressed: bool) -> None:
        identity = self._key_identity(descriptor)
        with self.lock:
            if not self.active or self.stop_requested.is_set():
                return
            if pressed:
                if identity in self.held_keyboard_keys:
                    return
            else:
                if identity not in self.held_keyboard_keys:
                    return
            appended, warning, limit_reached = self._append_locked(
                {
                    "type": "key_down" if pressed else "key_up",
                    "key": descriptor.copy(),
                }
            )
            if appended:
                if pressed:
                    self.held_keyboard_keys[identity] = descriptor.copy()
                else:
                    self.held_keyboard_keys.pop(identity, None)
        self._notify_capacity(warning, limit_reached)

    def _on_raw_mouse_error(self, text: str) -> None:
        self.relative_mouse_enabled = False
        self.report_warning(
            "Raw Input мыши отключён; новые перетаскивания используют экранные "
            f"координаты ({text})"
        )

    def _flush_relative_move_locked(
        self,
        *,
        force: bool,
    ) -> tuple[str | None, bool]:
        if not (self.pending_relative_dx or self.pending_relative_dy):
            return None, False
        now = self._timestamp()
        if (
            not force
            and self.last_relative_move_at >= 0
            and now - self.last_relative_move_at < self.drag_move_interval
        ):
            return None, False

        dx, dy = self.pending_relative_dx, self.pending_relative_dy
        event_time = self.pending_relative_at
        self.pending_relative_dx = 0
        self.pending_relative_dy = 0
        self.pending_relative_at = None
        appended, warning, limit_reached = self._append_locked(
            {"type": "mouse_move_relative", "dx": dx, "dy": dy},
            timestamp=event_time,
        )
        if appended:
            self.last_relative_move_at = now
        return warning, limit_reached

    def _on_raw_mouse_move(self, dx: int, dy: int) -> None:
        if not self.active or self.stop_requested.is_set():
            return
        with self.lock:
            if not self.relative_mouse_buttons:
                return
            self.pending_relative_dx += int(dx)
            self.pending_relative_dy += int(dy)
            self.pending_relative_at = self._timestamp()
            warning, limit_reached = self._flush_relative_move_locked(force=False)
        self._notify_capacity(warning, limit_reached)

    def _on_move(self, x: int, y: int, _injected: bool = False) -> None:
        if not self.active or self.stop_requested.is_set():
            return
        with self.lock:
            self.last_mouse_position = (int(x), int(y))
            is_dragging = bool(self.held_mouse_buttons)
            relative_drag = bool(self.relative_mouse_buttons)
        if relative_drag:
            # Physical dx/dy arrives separately through Raw Input. Ignoring
            # screen positions here also removes the game's SetCursorPos
            # recenter jumps from the recording.
            return
        if not self.record_moves and not is_dragging:
            return
        now = self._timestamp()
        minimum_interval = self.drag_move_interval if is_dragging else self.move_interval
        if self.last_move_at >= 0 and now - self.last_move_at < minimum_interval:
            return
        self.last_move_at = now
        self._append({"type": "mouse_move", "x": int(x), "y": int(y)})

    def _on_click(
        self,
        x: int,
        y: int,
        button: Any,
        pressed: bool,
        _injected: bool = False,
    ) -> None:
        if not self.active or self.stop_requested.is_set():
            return
        name = getattr(button, "name", None)
        if name not in {"left", "right", "middle"}:
            return
        notifications: list[tuple[str | None, bool]] = []
        with self.lock:
            self.last_mouse_position = (int(x), int(y))
            relative = (
                self.relative_mouse_enabled
                if pressed
                else name in self.relative_mouse_buttons
            )
            if pressed and relative and not self.relative_mouse_buttons:
                self.pending_relative_dx = 0
                self.pending_relative_dy = 0
                self.pending_relative_at = None
                self.last_relative_move_at = -1.0
            if not pressed and relative:
                notifications.append(self._flush_relative_move_locked(force=True))
            event = {
                "type": "mouse_button",
                "x": int(x),
                "y": int(y),
                "button": name,
                "pressed": bool(pressed),
            }
            if relative:
                event["relative"] = True
            appended, warning, limit_reached = self._append_locked(
                event
            )
            if appended and pressed:
                self.held_mouse_buttons.add(name)
                if relative:
                    self.relative_mouse_buttons.add(name)
            elif appended:
                self.held_mouse_buttons.discard(name)
                self.relative_mouse_buttons.discard(name)
                if not self.relative_mouse_buttons:
                    self.pending_relative_dx = 0
                    self.pending_relative_dy = 0
                    self.pending_relative_at = None
                    self.last_relative_move_at = -1.0
            notifications.append((warning, limit_reached))
        for warning, limit_reached in notifications:
            self._notify_capacity(warning, limit_reached)

    def _on_scroll(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        _injected: bool = False,
    ) -> None:
        if not self.active or self.stop_requested.is_set():
            return
        with self.lock:
            self.last_mouse_position = (int(x), int(y))
        self._append(
            {
                "type": "mouse_scroll",
                "x": int(x),
                "y": int(y),
                "dx": int(dx),
                "dy": int(dy),
            }
        )

    def _on_windows_keyboard_event(self, message: int, data: Any) -> bool:
        if not self.active or self.stop_requested.is_set():
            return True
        message = int(message)
        if message not in KEYDOWN_MESSAGES | KEYUP_MESSAGES:
            return True
        pressed = message in KEYDOWN_MESSAGES
        vk = int(data.vkCode)
        if vk in {VK_F9, VK_F10, VK_F12}:
            if pressed and vk in {VK_F10, VK_F12}:
                self._signal_stop("Запись остановлена горячей клавишей")
            return False
        flags = int(data.flags)
        self._record_key_change(
            {
                "kind": "scan",
                "value": int(data.scanCode),
                "vk": vk,
                "extended": bool(flags & LLKHF_EXTENDED),
            },
            pressed,
        )
        return False

    def _on_key_down(self, key: Any, _injected: bool = False) -> bool | None:
        if not self.active or self.stop_requested.is_set():
            return None
        if key is None:
            return None
        if key in {keyboard.Key.f9, keyboard.Key.f10, keyboard.Key.f12}:
            if key in {keyboard.Key.f10, keyboard.Key.f12}:
                self._signal_stop("Запись остановлена горячей клавишей")
            return False
        self._record_key_change(serialize_key(key), True)
        return None

    def _on_key_up(self, key: Any, _injected: bool = False) -> bool | None:
        if not self.active or self.stop_requested.is_set():
            return None
        if key is None:
            return None
        if key in {keyboard.Key.f9, keyboard.Key.f10, keyboard.Key.f12}:
            return False
        self._record_key_change(serialize_key(key), False)
        return None

    def _signal_stop(self, reason: str) -> None:
        if not self.stop_requested.is_set():
            self.stop_requested.set()
            self.request_stop(reason)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return sorted((event.copy() for event in self.events), key=lambda item: item["t"])

    def snapshot_range(self, start: int, stop: int) -> list[dict[str, Any]]:
        with self.lock:
            return [event.copy() for event in self.events[start:stop]]

    def recording_stats(self) -> tuple[int, float]:
        with self.lock:
            duration = float(self.events[-1]["t"]) if self.events else 0.0
            return len(self.events), duration

    def stop(self) -> list[dict[str, Any]]:
        self.stop_requested.set()
        if self.active:
            # Close unfinished holds at the exact stop time. This makes F10 a
            # valid way to finish a recording even while a game key or mouse
            # button is still held.
            release_time = round(self._timestamp(), 6)
            with self.lock:
                remaining_slots = max(
                    0,
                    MAX_EVENTS - self.capacity_base_count - len(self.events),
                )
                if (
                    remaining_slots > 0
                    and self.relative_mouse_buttons
                    and (self.pending_relative_dx or self.pending_relative_dy)
                ):
                    event_time = (
                        self.pending_relative_at
                        if self.pending_relative_at is not None
                        else release_time
                    )
                    self.events.append(
                        {
                            "t": round(event_time, 6),
                            "type": "mouse_move_relative",
                            "dx": self.pending_relative_dx,
                            "dy": self.pending_relative_dy,
                        }
                    )
                    remaining_slots -= 1
                self.pending_relative_dx = 0
                self.pending_relative_dy = 0
                self.pending_relative_at = None
                for descriptor in self.held_keyboard_keys.values():
                    if remaining_slots <= 0:
                        break
                    self.events.append(
                        {"t": release_time, "type": "key_up", "key": descriptor.copy()}
                    )
                    remaining_slots -= 1
                self.held_keyboard_keys.clear()
                if self.last_mouse_position is not None:
                    x, y = self.last_mouse_position
                    for button_name in sorted(self.held_mouse_buttons):
                        if remaining_slots <= 0:
                            break
                        event = {
                            "t": release_time,
                            "type": "mouse_button",
                            "x": x,
                            "y": y,
                            "button": button_name,
                            "pressed": False,
                        }
                        if button_name in self.relative_mouse_buttons:
                            event["relative"] = True
                        self.events.append(event)
                        remaining_slots -= 1
                self.held_mouse_buttons.clear()
                self.relative_mouse_buttons.clear()
                self.active = False
        for listener in (
            self.mouse_listener,
            self.keyboard_listener,
            self.raw_mouse_listener,
        ):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self.relative_mouse_enabled = False
        with self.lock:
            self.events.sort(key=lambda item: item["t"])
            return self.events


class AutomationRunner:
    def __init__(
        self,
        speed: float,
        on_progress: Callable[[str], None],
        on_finished: Callable[[bool, str | None], None],
        script_directory: Path | None = None,
        block_physical_mouse: bool = DEFAULT_BLOCK_PHYSICAL_MOUSE,
    ) -> None:
        self.speed = speed
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.stop_event = threading.Event()
        self.mouse_controller: Any = None
        self.keyboard_controller: Any = None
        self.ocr_reader: ScreenOcrReader | None = None
        self.variables: dict[str, str | float] = {}
        self.block_physical_mouse = bool(block_physical_mouse)
        self.mouse_blocker: WindowsPhysicalMouseBlocker | None = None
        self.mouse_blocker_error: str | None = None
        self.pressed_buttons: list[Any] = []
        self.pressed_keys: list[Any] = []
        self.mouse_pressed_at: dict[Any, float] = {}
        self.mouse_released_at: dict[Any, float] = {}
        self.script_directory = (
            script_directory.resolve()
            if script_directory is not None
            else Path.cwd().resolve()
        )

    def stop(self) -> None:
        self.stop_event.set()

    def _wait(self, seconds: float) -> bool:
        return not self.stop_event.wait(max(0.0, seconds / self.speed))

    def _wait_until(self, deadline: float) -> bool:
        """Wait for an absolute clock deadline without accumulating drift."""

        while not self.stop_event.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            if self.stop_event.wait(remaining):
                return False
        return False

    def _track_press(self, collection: list[Any], item: Any) -> None:
        if item not in collection:
            collection.append(item)

    @staticmethod
    def _track_release(collection: list[Any], item: Any) -> None:
        try:
            collection.remove(item)
        except ValueError:
            pass

    def _mouse_down(self, button: Any) -> bool:
        if button in self.pressed_buttons:
            return True
        released_at = self.mouse_released_at.get(button)
        if released_at is not None and not self.stop_event.is_set():
            remaining_gap = MIN_MOUSE_GAP_SECONDS - (time.perf_counter() - released_at)
            if remaining_gap > 0 and self.stop_event.wait(remaining_gap):
                return False
        self.mouse_controller.press(button)
        self._track_press(self.pressed_buttons, button)
        self.mouse_pressed_at[button] = time.perf_counter()
        return True

    def _mouse_up(self, button: Any) -> None:
        pressed_at = self.mouse_pressed_at.get(button)
        if pressed_at is not None and not self.stop_event.is_set():
            remaining_hold = MIN_MOUSE_HOLD_SECONDS - (time.perf_counter() - pressed_at)
            if remaining_hold > 0:
                self.stop_event.wait(remaining_hold)
        self.mouse_controller.release(button)
        self._track_release(self.pressed_buttons, button)
        self.mouse_pressed_at.pop(button, None)
        self.mouse_released_at[button] = time.perf_counter()

    def _key_down(self, key: Any) -> None:
        self.keyboard_controller.press(key)
        self._track_press(self.pressed_keys, key)

    def _key_up(self, key: Any) -> None:
        self.keyboard_controller.release(key)
        self._track_release(self.pressed_keys, key)

    def _move(self, target_x: int, target_y: int, duration: float) -> bool:
        scaled_duration = duration / self.speed
        if scaled_duration <= 0:
            self.mouse_controller.position = (target_x, target_y)
            return not self.stop_event.is_set()
        start_x, start_y = self.mouse_controller.position
        started = time.perf_counter()
        while not self.stop_event.is_set():
            progress = min(1.0, (time.perf_counter() - started) / scaled_duration)
            current_x = round(start_x + (target_x - start_x) * progress)
            current_y = round(start_y + (target_y - start_y) * progress)
            self.mouse_controller.position = (current_x, current_y)
            if progress >= 1.0:
                return True
            if self.stop_event.wait(min(0.016, scaled_duration / 10)):
                return False
        return False

    def _move_by(self, delta_x: int, delta_y: int, duration: float) -> bool:
        """Send relative mouse deltas without consulting the screen cursor."""

        scaled_duration = duration / self.speed
        if scaled_duration <= 0:
            self.mouse_controller.move(delta_x, delta_y)
            return not self.stop_event.is_set()

        sent_x = 0
        sent_y = 0
        started = time.perf_counter()
        while not self.stop_event.is_set():
            progress = min(1.0, (time.perf_counter() - started) / scaled_duration)
            target_x = round(delta_x * progress)
            target_y = round(delta_y * progress)
            step_x = target_x - sent_x
            step_y = target_y - sent_y
            if step_x or step_y:
                self.mouse_controller.move(step_x, step_y)
                sent_x, sent_y = target_x, target_y
            if progress >= 1.0:
                return True
            if self.stop_event.wait(min(0.016, scaled_duration / 10)):
                return False
        return False

    def _click(self, button: Any, count: int, interval: float) -> bool:
        for index in range(count):
            if self.stop_event.is_set():
                return False
            if not self._mouse_down(button):
                return False
            if not self._wait(0.025):
                self._mouse_up(button)
                return False
            self._mouse_up(button)
            if index + 1 < count and not self._wait(interval):
                return False
        return True

    def _resolve_script_image(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.script_directory / path
        return path.resolve()

    def _wait_for_image(
        self,
        raw_path: str,
        timeout: float,
        confidence: float,
    ) -> ImageMatch | None:
        image_path = self._resolve_script_image(raw_path)
        started = time.monotonic()
        with ScreenImageMatcher(image_path) as matcher:
            while not self.stop_event.is_set():
                match = matcher.find(confidence)
                if match is not None:
                    return match
                elapsed = time.monotonic() - started
                if timeout > 0 and elapsed >= timeout:
                    raise RuntimeError(
                        f'Изображение "{raw_path}" не появилось '
                        f"за {timeout:g} с (сходство {confidence:.0%})"
                    )
                delay = IMAGE_SEARCH_INTERVAL_SECONDS
                if timeout > 0:
                    delay = min(delay, max(0.0, timeout - elapsed))
                if self.stop_event.wait(delay):
                    return None
        return None

    def _execute_recorded_event(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        if event_type == "mouse_move":
            self.mouse_controller.position = (event["x"], event["y"])
        elif event_type == "mouse_move_relative":
            self.mouse_controller.move(event["dx"], event["dy"])
        elif event_type == "mouse_button":
            # Relative drags start at the recorded click coordinate, then keep
            # the actual position produced by dx/dy until release. Moving to
            # the game's recentered coordinate before UP would undo the drag.
            if event["pressed"] or not event.get("relative", False):
                self.mouse_controller.position = (event["x"], event["y"])
            button = resolve_button(event["button"])
            if event["pressed"]:
                self._mouse_down(button)
            else:
                self._mouse_up(button)
        elif event_type == "mouse_scroll":
            self.mouse_controller.position = (event["x"], event["y"])
            self.mouse_controller.scroll(event["dx"], event["dy"])
        elif event_type == "key_down":
            self._key_down(deserialize_key(event["key"]))
        elif event_type == "key_up":
            self._key_up(deserialize_key(event["key"]))

    def run_recording(self, events: Iterable[dict[str, Any]], repeats: int | None) -> None:
        normalized = compact_repeated_key_events(events)

        def task() -> None:
            if not normalized:
                return
            playback_started = time.perf_counter()
            recording_duration = max(0.0, float(normalized[-1]["t"]))
            repeat_index = 0
            last_progress_at = -1.0
            while repeats is None or repeat_index < repeats:
                previous_time = 0.0
                now = time.perf_counter()
                if last_progress_at < 0 or now - last_progress_at >= 0.25:
                    total = "∞" if repeats is None else str(repeats)
                    self.on_progress(f"Повтор {repeat_index + 1} из {total}")
                    last_progress_at = now
                repeat_offset = repeat_index * recording_duration
                for event in normalized:
                    event_time = max(previous_time, float(event["t"]))
                    deadline = playback_started + (
                        repeat_offset + event_time
                    ) / self.speed
                    if not self._wait_until(deadline):
                        return
                    previous_time = event_time
                    self._execute_recorded_event(event)
                repeat_index += 1
                if (
                    repeats is None
                    and recording_duration <= 0
                    and self.stop_event.wait(0.001)
                ):
                    return

        self._run(task)

    def _execute_nodes(self, nodes: Iterable[ScriptNode]) -> bool:
        for node in nodes:
            if self.stop_event.is_set():
                return False
            if isinstance(node, RepeatBlock):
                for index in range(node.count):
                    self.on_progress(f"Строка {node.line_no}: цикл {index + 1}/{node.count}")
                    if not self._execute_nodes(node.body):
                        return False
            elif isinstance(node, IfBlock):
                self.on_progress(f"Строка {node.line_no}: IF_{node.value_kind.upper()}")
                branch = node.true_body if self._evaluate_condition(node) else node.false_body
                if branch and not self._execute_nodes(branch):
                    return False
            else:
                self.on_progress(f"Строка {node.line_no}: {node.name}")
                if not self._execute_command(node):
                    return False
        return True

    @staticmethod
    def _format_variable(value: str | float) -> str:
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return format(value, ".15g")
        return value

    def _get_variable(self, name: str, line_no: int) -> str | float:
        try:
            return self.variables[name]
        except KeyError as exc:
            raise RuntimeError(
                f"Строка {line_no}: переменная {name!r} ещё не задана"
            ) from exc

    def _expand_variables(self, text: str, line_no: int) -> str:
        def replace(match: Any) -> str:
            name = match.group(1)
            return self._format_variable(self._get_variable(name, line_no))

        return VARIABLE_TEMPLATE_PATTERN.sub(replace, text)

    def _evaluate_condition(self, block: IfBlock) -> bool:
        actual = self._get_variable(block.variable, block.line_no)
        operator = block.operator
        if block.value_kind == "text":
            left = self._format_variable(actual).casefold()
            right = str(block.expected).casefold()
            if operator == "==":
                return left == right
            if operator == "!=":
                return left != right
            if operator == "CONTAINS":
                return right in left
            if operator == "NOT_CONTAINS":
                return right not in left
        else:
            if isinstance(actual, float):
                left_number = actual
            else:
                try:
                    left_number = extract_first_number(actual)
                except OcrError as exc:
                    raise RuntimeError(
                        f"Строка {block.line_no}: переменная {block.variable!r} "
                        f"не содержит число: {exc}"
                    ) from exc
            right_number = float(block.expected)
            if operator == "==":
                return math.isclose(left_number, right_number, rel_tol=1e-9, abs_tol=1e-9)
            if operator == "!=":
                return not math.isclose(
                    left_number,
                    right_number,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            if operator == "<":
                return left_number < right_number
            if operator == "<=":
                return left_number <= right_number
            if operator == ">":
                return left_number > right_number
            if operator == ">=":
                return left_number >= right_number
        raise RuntimeError(
            f"Строка {block.line_no}: неизвестный оператор {operator!r}"
        )

    def _read_ocr(self, command: ScriptCommand) -> bool:
        variable, x, y, width, height, language = command.args
        if self.ocr_reader is None:
            self.ocr_reader = ScreenOcrReader()
        try:
            text = self.ocr_reader.read_region(x, y, width, height, language)
            if command.name == "OCR_NUMBER":
                value: str | float = extract_first_number(text)
            else:
                value = text
        except OcrError as exc:
            raise RuntimeError(f"Строка {command.line_no}: OCR: {exc}") from exc
        self.variables[variable] = value
        preview = self._format_variable(value).replace("\n", " ↵ ")
        if len(preview) > 100:
            preview = preview[:97] + "…"
        self.on_progress(f"Строка {command.line_no}: {variable} = {preview or '∅'}")
        return True

    def _execute_command(self, command: ScriptCommand) -> bool:
        name, args = command.name, command.args
        if name == "WAIT":
            return self._wait(args[0])
        if name == "MOVE":
            return self._move(args[0], args[1], args[2])
        if name == "MOVE_BY":
            return self._move_by(args[0], args[1], args[2])
        if name == "CLICK":
            return self._click(resolve_button(args[0]), args[1], args[2])
        if name == "CLICK_AT":
            self.mouse_controller.position = (args[0], args[1])
            return self._click(resolve_button(args[2]), args[3], args[4])
        if name == "WAIT_IMAGE":
            return self._wait_for_image(args[0], args[1], args[2]) is not None
        if name == "CLICK_IMAGE":
            match = self._wait_for_image(args[0], args[2], args[3])
            if match is None:
                return False
            self.mouse_controller.position = match.center
            return self._click(resolve_button(args[1]), 1, 0.0)
        if name in {"OCR_TEXT", "OCR_NUMBER"}:
            return self._read_ocr(command)
        if name == "DOWN":
            return self._mouse_down(resolve_button(args[0]))
        if name == "UP":
            self._mouse_up(resolve_button(args[0]))
            return True
        if name == "SCROLL":
            self.mouse_controller.scroll(args[0], args[1])
            return True
        if name == "PRESS":
            key = resolve_script_key(args[0])
            self._key_down(key)
            if not self._wait(0.025):
                self._key_up(key)
                return False
            self._key_up(key)
            return True
        if name == "KEY_DOWN":
            self._key_down(resolve_script_key(args[0]))
            return True
        if name == "KEY_UP":
            self._key_up(resolve_script_key(args[0]))
            return True
        if name == "HOTKEY":
            keys = [resolve_script_key(item) for item in args]
            pressed: list[Any] = []
            try:
                for key in keys:
                    self._key_down(key)
                    pressed.append(key)
                return self._wait(0.04)
            finally:
                for key in reversed(pressed):
                    self._key_up(key)
        if name == "TYPE":
            text, interval = args
            text = self._expand_variables(text, command.line_no)
            for char in text:
                if self.stop_event.is_set():
                    return False
                self.keyboard_controller.type(char)
                if interval and not self._wait(interval):
                    return False
            return True
        raise RuntimeError(f"Команда {name} не реализована")

    def run_script(self, nodes: Iterable[ScriptNode]) -> None:
        self._run(lambda: self._execute_nodes(nodes))

    def _mouse_blocker_failed(self, error: str) -> None:
        self.mouse_blocker_error = error
        self.stop_event.set()

    def _start_mouse_blocker(self) -> None:
        if not self.block_physical_mouse:
            return
        if not WINDOWS_NATIVE_AVAILABLE:
            raise RuntimeError(
                "Блокировка физической мыши доступна только в Windows"
            )
        self.mouse_blocker = WindowsPhysicalMouseBlocker(
            on_error=self._mouse_blocker_failed,
        )
        self.mouse_blocker.start()

        # If playback started while a physical button was held, the target
        # application may already consider it pressed. Tagged UP events pass
        # through the blocker and reset that state before the macro begins.
        for button_name in ("left", "right", "middle"):
            self.mouse_controller.release(resolve_button(button_name))

    def _run(self, task: Callable[[], Any]) -> None:
        error: str | None = None
        self.variables.clear()
        try:
            if keyboard is None or mouse is None:
                raise RuntimeError("Библиотека pynput не загружена")
            self.mouse_controller = (
                WindowsMouseController() if WINDOWS_NATIVE_AVAILABLE else mouse.Controller()
            )
            self.keyboard_controller = (
                WindowsKeyboardController() if WINDOWS_NATIVE_AVAILABLE else keyboard.Controller()
            )
            self._start_mouse_blocker()
            task()
            if self.mouse_blocker_error is not None:
                raise RuntimeError(
                    "Блокировка физической мыши была отключена: "
                    f"{self.mouse_blocker_error}"
                )
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
        finally:
            self._release_all()
            if self.ocr_reader is not None:
                try:
                    self.ocr_reader.close()
                except Exception:
                    pass
                self.ocr_reader = None
            if self.mouse_blocker is not None:
                try:
                    self.mouse_blocker.stop()
                except Exception as exc:
                    if error is None:
                        error = str(exc) or exc.__class__.__name__
            self.on_finished(self.stop_event.is_set(), error)

    def _release_all(self) -> None:
        if self.mouse_controller is not None:
            for button in reversed(self.pressed_buttons[:]):
                try:
                    self.mouse_controller.release(button)
                except Exception:
                    pass
            self.pressed_buttons.clear()
            self.mouse_pressed_at.clear()
            self.mouse_released_at.clear()
        if self.keyboard_controller is not None:
            for key in reversed(self.pressed_keys[:]):
                try:
                    self.keyboard_controller.release(key)
                except Exception:
                    pass
            self.pressed_keys.clear()


class ScreenRegionPicker:
    """Let the user drag a physical-pixel rectangle across the virtual desktop."""

    def __init__(
        self,
        root: tk.Tk,
        on_selected: Callable[[int, int, int, int], None],
        on_cancelled: Callable[[str | None], None],
    ) -> None:
        self.root = root
        self.on_selected = on_selected
        self.on_cancelled = on_cancelled
        self.previous_state = root.state()
        self.window: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self.virtual_left = 0
        self.virtual_top = 0
        self.virtual_width = 0
        self.virtual_height = 0
        self.start_point: tuple[int, int] | None = None
        self.rectangle_id: int | None = None
        self.finished = False

    def start(self) -> None:
        self.root.withdraw()
        self.root.after(180, self._show_overlay)

    def _show_overlay(self) -> None:
        try:
            import mss

            with mss.mss() as capture:
                monitor = capture.monitors[0]
                self.virtual_left = int(monitor["left"])
                self.virtual_top = int(monitor["top"])
                self.virtual_width = int(monitor["width"])
                self.virtual_height = int(monitor["height"])
            if self.virtual_width <= 0 or self.virtual_height <= 0:
                raise RuntimeError("Windows вернула неверный размер рабочего стола")

            window = tk.Toplevel(self.root)
            self.window = window
            window.overrideredirect(True)
            window.configure(background="#07101f")
            window.attributes("-topmost", True)
            window.attributes("-alpha", 0.42)
            window.geometry(f"{self.virtual_width}x{self.virtual_height}+0+0")
            window.update_idletasks()

            if WINDOWS_NATIVE_AVAILABLE:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SetWindowPos.argtypes = (
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint32,
                )
                user32.SetWindowPos.restype = ctypes.c_int
                if not user32.SetWindowPos(
                    ctypes.c_void_p(window.winfo_id()),
                    ctypes.c_void_p(-1),
                    self.virtual_left,
                    self.virtual_top,
                    self.virtual_width,
                    self.virtual_height,
                    0x0040,
                ):
                    raise RuntimeError("Windows не разместила окно выбора области")
            else:
                window.geometry(
                    f"{self.virtual_width}x{self.virtual_height}"
                    f"{self.virtual_left:+d}{self.virtual_top:+d}"
                )

            canvas = tk.Canvas(
                window,
                background="#07101f",
                cursor="crosshair",
                highlightthickness=0,
                borderwidth=0,
            )
            self.canvas = canvas
            canvas.pack(fill="both", expand=True)
            canvas.create_text(
                self.virtual_width // 2,
                42,
                text="Зажмите ЛКМ и обведите область OCR · Esc или ПКМ — отмена",
                fill="#ffffff",
                font=("Segoe UI Semibold", 14),
            )
            canvas.bind("<ButtonPress-1>", self._on_press)
            canvas.bind("<B1-Motion>", self._on_drag)
            canvas.bind("<ButtonRelease-1>", self._on_release)
            canvas.bind("<ButtonPress-3>", lambda _event: self.cancel())
            window.bind("<Escape>", lambda _event: self.cancel())
            window.focus_force()
        except Exception as exc:
            self.cancel(f"Не удалось открыть выбор OCR-области: {exc}")

    def _clamp_point(self, x: int, y: int) -> tuple[int, int]:
        return (
            min(max(int(x), 0), self.virtual_width - 1),
            min(max(int(y), 0), self.virtual_height - 1),
        )

    def _on_press(self, event: Any) -> None:
        if self.canvas is None:
            return
        self.start_point = self._clamp_point(event.x, event.y)
        if self.rectangle_id is not None:
            self.canvas.delete(self.rectangle_id)
        x, y = self.start_point
        self.rectangle_id = self.canvas.create_rectangle(
            x,
            y,
            x,
            y,
            outline=UI_COLORS["accent"],
            width=3,
            fill="#284a86",
            stipple="gray25",
        )

    def _on_drag(self, event: Any) -> None:
        if self.canvas is None or self.start_point is None or self.rectangle_id is None:
            return
        x, y = self._clamp_point(event.x, event.y)
        self.canvas.coords(
            self.rectangle_id,
            self.start_point[0],
            self.start_point[1],
            x,
            y,
        )

    def _on_release(self, event: Any) -> None:
        if self.start_point is None:
            return
        end_x, end_y = self._clamp_point(event.x, event.y)
        left = min(self.start_point[0], end_x)
        top = min(self.start_point[1], end_y)
        width = abs(end_x - self.start_point[0]) + 1
        height = abs(end_y - self.start_point[1]) + 1
        if width < 4 or height < 4:
            self.start_point = None
            if self.canvas is not None and self.rectangle_id is not None:
                self.canvas.delete(self.rectangle_id)
                self.rectangle_id = None
            return
        self._finish()
        self.on_selected(
            self.virtual_left + left,
            self.virtual_top + top,
            width,
            height,
        )

    def _finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
        self.root.deiconify()
        if self.previous_state == "zoomed":
            self.root.state("zoomed")
        self.root.lift()

    def cancel(self, error: str | None = None) -> None:
        if self.finished:
            return
        self._finish()
        self.on_cancelled(error)


class MacroPilotApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1180x780")
        self.root.minsize(960, 640)
        self.root.configure(background=UI_COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.events: list[dict[str, Any]] = []
        self.recorder: EventRecorder | None = None
        self.runner: AutomationRunner | None = None
        self.worker: threading.Thread | None = None
        self.safety_listener: Any = None
        self.mode = "idle"
        self.countdown_job: str | None = None
        self.refresh_job: str | None = None
        self.window_was_minimized = False
        self.minimize_job: str | None = None
        self.start_hotkey_held = False
        self.record_prompt_active = False
        self.recording_append_mode = False
        self.recording_base_count = 0
        self.recording_base_duration = 0.0
        self.current_macro_path: Path | None = None
        self.current_script_path: Path | None = None
        self.table_event_count = 0
        self.available_release: ReleaseInfo | None = None
        self.update_busy = False
        self.region_picker: ScreenRegionPicker | None = None

        self.status_var = tk.StringVar(value="Готово")
        self.summary_var = tk.StringVar(value="Событий: 0 · длительность: 0.00 с")
        self.speed_var = tk.StringVar(value="1.0")
        self.repeats_var = tk.StringVar(value="1")
        self.infinite_repeats_var = tk.BooleanVar(value=False)
        self.record_moves_var = tk.BooleanVar(value=DEFAULT_RECORD_MOUSE_MOVES)
        self.block_physical_mouse_var = tk.BooleanVar(
            value=DEFAULT_BLOCK_PHYSICAL_MOUSE
        )
        self.recording_precision_var = tk.StringVar(value=DEFAULT_RECORDING_PRECISION)
        self.minimize_var = tk.BooleanVar(value=DEFAULT_MINIMIZE_ACTION_WINDOW)
        self.update_state_var = tk.StringVar(value="Обновления через GitHub Releases")

        self._configure_style()
        self._build_ui()
        self._start_safety_listener()
        self._refresh_controls()
        self.root.after(1800, lambda: self.check_for_updates(manual=False))

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        colors = UI_COLORS
        base_font = ("Segoe UI", 10)
        self.root.option_add("*Font", base_font)

        style.configure(".", background=colors["bg"], foreground=colors["text"], font=base_font)
        style.configure("App.TFrame", background=colors["bg"])
        style.configure("Header.TFrame", background=colors["surface"])
        style.configure("Card.TFrame", background=colors["card"], relief="flat")
        style.configure("TLabel", background=colors["bg"], foreground=colors["text"])
        style.configure(
            "Header.TLabel",
            background=colors["surface"],
            foreground=colors["text"],
            font=("Segoe UI Semibold", 19),
        )
        style.configure(
            "Subheader.TLabel",
            background=colors["surface"],
            foreground=colors["muted"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Version.TLabel",
            background=colors["card"],
            foreground=colors["muted"],
            padding=(9, 4),
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "CardTitle.TLabel",
            background=colors["card"],
            foreground=colors["text"],
            font=("Segoe UI Semibold", 13),
        )
        style.configure(
            "CardText.TLabel",
            background=colors["card"],
            foreground=colors["muted"],
        )
        style.configure(
            "Card.TLabel",
            background=colors["card"],
            foreground=colors["text"],
        )
        style.configure("Muted.TLabel", foreground=colors["muted"])
        style.configure(
            "Summary.TLabel",
            background=colors["card"],
            foreground=colors["muted"],
            font=("Segoe UI Semibold", 9),
        )

        button_common = {
            "borderwidth": 0,
            "focuscolor": "",
            "padding": (13, 8),
            "font": ("Segoe UI Semibold", 9),
        }
        style.configure("TButton", background=colors["card"], foreground=colors["text"], **button_common)
        style.map(
            "TButton",
            background=[("active", colors["card_hover"]), ("disabled", colors["surface"])],
            foreground=[("disabled", "#58667b")],
        )
        style.configure(
            "Accent.TButton", background=colors["accent"], foreground="#ffffff", **button_common
        )
        style.map(
            "Accent.TButton",
            background=[("active", colors["accent_hover"]), ("disabled", "#2d426f")],
            foreground=[("disabled", "#7f91b5")],
        )
        style.configure(
            "Record.TButton", background=colors["record"], foreground="#ffffff", **button_common
        )
        style.map(
            "Record.TButton",
            background=[("active", colors["record_hover"]), ("disabled", "#633942")],
            foreground=[("disabled", "#a87880")],
        )
        style.configure(
            "Danger.TButton", background="#3a2630", foreground="#ff93a0", **button_common
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#54313d"), ("disabled", colors["surface"])],
            foreground=[("disabled", "#70545a")],
        )
        style.configure(
            "Support.TButton", background=colors["support"], foreground="#ffffff", **button_common
        )
        style.map("Support.TButton", background=[("active", colors["support_hover"])])
        style.configure(
            "Ghost.TButton",
            background=colors["surface"],
            foreground=colors["muted"],
            **button_common,
        )
        style.map(
            "Ghost.TButton",
            background=[("active", colors["card"]), ("disabled", colors["surface"])],
            foreground=[("active", colors["text"]), ("disabled", "#58667b")],
        )

        style.configure("TNotebook", background=colors["bg"], borderwidth=0, tabmargins=(0, 8, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=colors["surface"],
            foreground=colors["muted"],
            borderwidth=0,
            padding=(18, 10),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", colors["card"]), ("active", colors["card_hover"])],
            foreground=[("selected", colors["text"]), ("active", colors["text"])],
        )
        style.configure(
            "Card.TLabelframe",
            background=colors["card"],
            bordercolor=colors["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=colors["card"],
            foreground=colors["muted"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TCheckbutton",
            background=colors["card"],
            foreground=colors["text"],
            indicatorbackground=colors["editor"],
            indicatorforeground=colors["accent"],
            padding=(2, 2),
        )
        style.map(
            "TCheckbutton",
            background=[("active", colors["card"])],
            foreground=[("disabled", "#58667b")],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=colors["editor"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            arrowcolor=colors["muted"],
            padding=5,
        )
        style.configure(
            "Treeview",
            background=colors["editor"],
            fieldbackground=colors["editor"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            rowheight=29,
            relief="flat",
        )
        style.map("Treeview", background=[("selected", "#284a86")], foreground=[("selected", "#ffffff")])
        style.configure(
            "Treeview.Heading",
            background=colors["card"],
            foreground=colors["muted"],
            bordercolor=colors["border"],
            relief="flat",
            padding=(7, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Treeview.Heading", background=[("active", colors["card_hover"])])
        style.configure(
            "Vertical.TScrollbar",
            background=colors["card_hover"],
            troughcolor=colors["editor"],
            bordercolor=colors["editor"],
            arrowcolor=colors["muted"],
        )
        style.configure(
            "Horizontal.TScrollbar",
            background=colors["card_hover"],
            troughcolor=colors["editor"],
            bordercolor=colors["editor"],
            arrowcolor=colors["muted"],
        )
        style.configure(
            "Status.TLabel",
            background=colors["surface"],
            foreground=colors["muted"],
            padding=(8, 8),
        )
        style.configure(
            "StatusDot.TLabel",
            background=colors["surface"],
            foreground=colors["success"],
            padding=(14, 8, 0, 8),
        )

    def _build_ui(self) -> None:
        colors = UI_COLORS
        header = ttk.Frame(self.root, padding=(18, 13), style="Header.TFrame")
        header.pack(fill="x")
        logo = tk.Label(
            header,
            text="M",
            bg=colors["accent"],
            fg="#ffffff",
            font=("Segoe UI Black", 16),
            width=2,
            height=1,
            padx=3,
            pady=3,
        )
        logo.pack(side="left", padx=(0, 11))
        brand = ttk.Frame(header, style="Header.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        subtitle = (
            "Игровые макросы · Windows SendInput · by Config"
            if WINDOWS_NATIVE_AVAILABLE
            else "Запись и воспроизведение действий · by Config"
        )
        ttk.Label(brand, text=subtitle, style="Subheader.TLabel").pack(anchor="w", pady=(1, 0))

        header_actions = ttk.Frame(header, style="Header.TFrame")
        header_actions.pack(side="right")
        ttk.Label(header_actions, text=f"v{APP_VERSION}", style="Version.TLabel").pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            header_actions,
            text="GitHub",
            command=self.open_project_page,
            style="Ghost.TButton",
        ).pack(side="left", padx=(0, 6))
        self.update_button = ttk.Button(
            header_actions,
            text="Проверить обновления",
            command=self._update_button_clicked,
            style="Accent.TButton",
        )
        self.update_button.pack(side="left")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.record_tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.script_tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.about_tab = ttk.Frame(self.notebook, padding=12, style="App.TFrame")
        self.notebook.add(self.record_tab, text="●  Запись")
        self.notebook.add(self.script_tab, text="{ }  Сценарий")
        self.notebook.add(self.about_tab, text="О проекте")

        self._build_record_tab()
        self._build_script_tab()
        self._build_about_tab()

        status_bar = ttk.Frame(self.root, style="Header.TFrame")
        status_bar.pack(fill="x", side="bottom")
        ttk.Label(status_bar, text="●", style="StatusDot.TLabel").pack(side="left")
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(status_bar, text="F9 — запись · F12 — стоп", style="Status.TLabel").pack(
            side="right", padx=(8, 10)
        )

    def _build_record_tab(self) -> None:
        toolbar = ttk.Frame(self.record_tab, padding=10, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        self.record_button = ttk.Button(
            toolbar,
            text="●  Начать / продолжить (F9)",
            command=self.start_recording,
            style="Record.TButton",
        )
        self.record_button.pack(side="left")
        self.play_button = ttk.Button(
            toolbar,
            text="▶  Воспроизвести",
            command=self.play_recording_countdown,
            style="Accent.TButton",
        )
        self.play_button.pack(side="left", padx=(7, 0))
        self.stop_button = ttk.Button(
            toolbar,
            text="■  Остановить (F12)",
            command=self.stop_current,
            style="Danger.TButton",
        )
        self.stop_button.pack(side="left", padx=(7, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=12)
        self.load_button = ttk.Button(
            toolbar, text="Открыть…", command=self.load_macro_file, style="Ghost.TButton"
        )
        self.load_button.pack(side="left")
        self.save_button = ttk.Button(
            toolbar, text="Сохранить…", command=self.save_macro_file, style="Ghost.TButton"
        )
        self.save_button.pack(side="left", padx=(6, 0))
        self.to_script_button = ttk.Button(
            toolbar,
            text="В сценарий",
            command=self.convert_to_script,
            style="Ghost.TButton",
        )
        self.to_script_button.pack(side="left", padx=(6, 0))

        settings = ttk.LabelFrame(
            self.record_tab,
            text=" Параметры записи и воспроизведения ",
            padding=(12, 9),
            style="Card.TLabelframe",
        )
        settings.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            settings,
            text="Записывать движения мыши",
            variable=self.record_moves_var,
        ).pack(side="left")
        ttk.Label(settings, text="Точность:", style="Card.TLabel").pack(
            side="left", padx=(14, 4)
        )
        self.recording_precision_combo = ttk.Combobox(
            settings,
            values=tuple(RECORDING_PRECISION_OPTIONS),
            textvariable=self.recording_precision_var,
            state="readonly",
            width=19,
        )
        self.recording_precision_combo.pack(side="left")
        ttk.Checkbutton(
            settings,
            text="Сворачивать окно при записи и запуске",
            variable=self.minimize_var,
        ).pack(side="left", padx=(14, 0))
        ttk.Label(settings, text="Скорость:", style="Card.TLabel").pack(
            side="left", padx=(14, 4)
        )
        self.speed_spin = ttk.Spinbox(settings, from_=0.1, to=10.0, increment=0.1, width=6, textvariable=self.speed_var)
        self.speed_spin.pack(side="left")
        ttk.Label(settings, text="Повторы:", style="Card.TLabel").pack(
            side="left", padx=(14, 4)
        )
        self.repeats_spin = ttk.Spinbox(settings, from_=1, to=999, increment=1, width=6, textvariable=self.repeats_var)
        self.repeats_spin.pack(side="left")
        self.infinite_checkbox = ttk.Checkbutton(
            settings,
            text="∞ До F12",
            variable=self.infinite_repeats_var,
            command=self._refresh_controls,
        )
        self.infinite_checkbox.pack(side="left", padx=(8, 0))

        playback_safety = ttk.Frame(
            self.record_tab,
            padding=(12, 8),
            style="Card.TFrame",
        )
        playback_safety.pack(fill="x", pady=(0, 10))
        self.block_physical_mouse_checkbox = ttk.Checkbutton(
            playback_safety,
            text="Блокировать физическую мышь при воспроизведении записи и сценария",
            variable=self.block_physical_mouse_var,
        )
        self.block_physical_mouse_checkbox.pack(side="left")
        block_hint = (
            "Клавиатура и аварийная остановка F12 останутся доступны"
            if WINDOWS_NATIVE_AVAILABLE
            else "Доступно только в Windows"
        )
        ttk.Label(
            playback_safety,
            text=block_hint,
            style="CardText.TLabel",
        ).pack(side="left", padx=(12, 0))

        table_frame = ttk.Frame(self.record_tab, padding=1, style="Card.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("number", "time", "event", "details")
        self.event_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.event_tree.heading("number", text="№")
        self.event_tree.heading("time", text="Время, с")
        self.event_tree.heading("event", text="Событие")
        self.event_tree.heading("details", text="Параметры")
        self.event_tree.column("number", width=55, minwidth=45, stretch=False, anchor="e")
        self.event_tree.column("time", width=90, minwidth=75, stretch=False, anchor="e")
        self.event_tree.column("event", width=165, minwidth=130, stretch=False)
        self.event_tree.column("details", width=550, minwidth=240)
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.event_tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.event_tree.xview)
        self.event_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.event_tree.tag_configure("even", background=UI_COLORS["editor"])
        self.event_tree.tag_configure("odd", background="#111b2e")
        self.event_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        footer = ttk.Frame(self.record_tab, padding=(11, 8), style="Card.TFrame")
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.summary_var, style="Summary.TLabel").pack(side="left")
        self.clear_button = ttk.Button(
            footer, text="Очистить", command=self.clear_events, style="Ghost.TButton"
        )
        self.clear_button.pack(side="right")
        self.delete_button = ttk.Button(
            footer,
            text="Удалить выбранные",
            command=self.delete_selected_events,
            style="Ghost.TButton",
        )
        self.delete_button.pack(side="right", padx=(0, 6))

    def _build_script_tab(self) -> None:
        toolbar = ttk.Frame(self.script_tab, padding=10, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        self.script_run_button = ttk.Button(
            toolbar,
            text="▶  Запустить",
            command=self.play_script_countdown,
            style="Accent.TButton",
        )
        self.script_run_button.pack(side="left")
        self.script_validate_button = ttk.Button(toolbar, text="Проверить", command=self.validate_script_text)
        self.script_validate_button.pack(side="left", padx=(6, 0))
        self.script_stop_button = ttk.Button(toolbar, text="■ Остановить (F12)", command=self.stop_current, style="Danger.TButton")
        self.script_stop_button.pack(side="left", padx=(6, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=12)
        self.script_open_button = ttk.Button(
            toolbar, text="Открыть…", command=self.load_script_file, style="Ghost.TButton"
        )
        self.script_open_button.pack(side="left")
        self.script_save_button = ttk.Button(
            toolbar, text="Сохранить…", command=self.save_script_file, style="Ghost.TButton"
        )
        self.script_save_button.pack(side="left", padx=(6, 0))
        self.script_example_button = ttk.Button(
            toolbar,
            text="Загрузить пример",
            command=self.load_example_script,
            style="Ghost.TButton",
        )
        self.script_example_button.pack(side="left", padx=(6, 0))
        ttk.Label(toolbar, text="Скорость:", style="Card.TLabel").pack(
            side="left", padx=(20, 4)
        )
        self.script_speed_spin = ttk.Spinbox(toolbar, from_=0.1, to=10.0, increment=0.1, width=6, textvariable=self.speed_var)
        self.script_speed_spin.pack(side="left")

        script_options = ttk.Frame(
            self.script_tab,
            padding=(12, 7),
            style="Card.TFrame",
        )
        script_options.pack(fill="x", pady=(0, 10))
        self.script_block_physical_mouse_checkbox = ttk.Checkbutton(
            script_options,
            text="Блокировать физическую мышь во время выполнения · F12 работает",
            variable=self.block_physical_mouse_var,
        )
        self.script_block_physical_mouse_checkbox.pack(side="left")
        self.script_ocr_region_button = ttk.Button(
            script_options,
            text="▣  Выбрать OCR-область…",
            command=self.select_ocr_region,
            style="Ghost.TButton",
        )
        self.script_ocr_region_button.pack(side="right")
        ttk.Label(
            script_options,
            text="Текст распознаётся локально средствами Windows",
            style="CardText.TLabel",
        ).pack(side="right", padx=(0, 10))

        pane = ttk.Panedwindow(self.script_tab, orient="horizontal")
        pane.pack(fill="both", expand=True)

        editor_frame = ttk.Frame(pane, padding=1, style="Card.TFrame")
        help_frame = ttk.LabelFrame(
            pane,
            text=" Справка по командам ",
            padding=7,
            style="Card.TLabelframe",
        )
        pane.add(editor_frame, weight=4)
        pane.add(help_frame, weight=2)

        font = ("Consolas", 11) if platform.system() == "Windows" else ("TkFixedFont", 11)
        self.script_text = tk.Text(
            editor_frame,
            wrap="none",
            undo=True,
            font=font,
            padx=8,
            pady=8,
            tabs=(32,),
            background=UI_COLORS["editor"],
            foreground=UI_COLORS["text"],
            insertbackground=UI_COLORS["accent"],
            selectbackground="#284a86",
            selectforeground="#ffffff",
            highlightbackground=UI_COLORS["border"],
            highlightcolor=UI_COLORS["accent"],
            highlightthickness=1,
            borderwidth=0,
            relief="flat",
        )
        self.script_text.tag_configure("script_error", background="#5a2935", foreground="#ffd8dd")
        script_y = ttk.Scrollbar(editor_frame, orient="vertical", command=self.script_text.yview)
        script_x = ttk.Scrollbar(editor_frame, orient="horizontal", command=self.script_text.xview)
        self.script_text.configure(yscrollcommand=script_y.set, xscrollcommand=script_x.set)
        self.script_text.bind("<Control-KeyPress>", self._on_script_editor_control)
        self.script_text.grid(row=0, column=0, sticky="nsew")
        script_y.grid(row=0, column=1, sticky="ns")
        script_x.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)

        help_text = tk.Text(
            help_frame,
            wrap="word",
            font=("Segoe UI", 9),
            padx=9,
            pady=9,
            borderwidth=0,
            relief="flat",
            background=UI_COLORS["editor"],
            foreground=UI_COLORS["muted"],
            selectbackground="#284a86",
            selectforeground="#ffffff",
        )
        help_scroll = ttk.Scrollbar(help_frame, orient="vertical", command=help_text.yview)
        help_text.configure(yscrollcommand=help_scroll.set)
        help_text.insert("1.0", SCRIPT_HELP)
        help_text.configure(state="disabled")
        help_text.pack(side="left", fill="both", expand=True)
        help_scroll.pack(side="right", fill="y")

        self.script_text.insert("1.0", EXAMPLE_SCRIPT)
        self.script_text.edit_modified(False)

    def _on_script_editor_control(self, event: Any) -> str | None:
        """Keep editor shortcuts physical-key based on non-English layouts."""

        if not int(getattr(event, "state", 0)) & TK_CONTROL_MASK:
            return None
        keycode = int(getattr(event, "keycode", 0))
        keysym = str(getattr(event, "keysym", "")).lower()
        action = {
            67: "<<Copy>>",
            86: "<<Paste>>",
            88: "<<Cut>>",
            65: "<<SelectAll>>",
            90: "<<Redo>>" if int(event.state) & TK_SHIFT_MASK else "<<Undo>>",
            89: "<<Redo>>",
        }.get(keycode)
        if action is None:
            action = {
                "c": "<<Copy>>",
                "v": "<<Paste>>",
                "x": "<<Cut>>",
                "a": "<<SelectAll>>",
                "z": "<<Redo>>" if int(event.state) & TK_SHIFT_MASK else "<<Undo>>",
                "y": "<<Redo>>",
            }.get(keysym)
        if action is None:
            return None
        event.widget.event_generate(action)
        return "break"

    def select_ocr_region(self) -> None:
        if self.mode != "idle" or self.region_picker is not None:
            return
        number_mode = messagebox.askyesnocancel(
            "OCR-область",
            "Что нужно считать из области?\n\n"
            "Да — число\n"
            "Нет — текст\n"
            "Отмена — ничего не делать",
            parent=self.root,
        )
        if number_mode is None:
            return
        default_name = "number" if number_mode else "text"
        variable = simpledialog.askstring(
            "OCR-область",
            "Имя переменной (латинскими буквами):",
            initialvalue=default_name,
            parent=self.root,
        )
        if variable is None:
            return
        variable = variable.strip()
        if not VARIABLE_PATTERN.fullmatch(variable):
            messagebox.showerror(
                "OCR-область",
                "Имя должно начинаться с латинской буквы или _. "
                "Далее разрешены латинские буквы, цифры и _.",
                parent=self.root,
            )
            return

        command_name = "OCR_NUMBER" if number_mode else "OCR_TEXT"

        def selected(x: int, y: int, width: int, height: int) -> None:
            self.region_picker = None
            self._insert_ocr_command(
                command_name,
                variable,
                x,
                y,
                width,
                height,
            )

        def cancelled(error: str | None) -> None:
            self.region_picker = None
            if error:
                self.status_var.set(error)
                messagebox.showerror("OCR-область", error, parent=self.root)
            else:
                self.status_var.set("Выбор OCR-области отменён")

        self.status_var.set("Обведите OCR-область на экране · Esc — отмена")
        self.region_picker = ScreenRegionPicker(self.root, selected, cancelled)
        self.region_picker.start()

    def _insert_ocr_command(
        self,
        command_name: str,
        variable: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        before_cursor = self.script_text.get("insert linestart", "insert")
        indent = before_cursor[: len(before_cursor) - len(before_cursor.lstrip())]
        prefix = "\n" + indent if before_cursor.strip() else ""
        command = f"{command_name} {variable} {x} {y} {width} {height}"
        self.script_text.insert("insert", prefix + command + "\n")
        self.script_text.see("insert")
        self.script_text.focus_set()
        self.status_var.set(
            f"Вставлено: {command_name} {variable} · "
            f"область {width}×{height} в ({x}, {y})"
        )

    def _build_about_tab(self) -> None:
        shell = ttk.Frame(self.about_tab, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        card = ttk.Frame(shell, padding=(42, 34), style="Card.TFrame")
        card.place(relx=0.5, rely=0.46, anchor="center", relwidth=0.72)

        logo = tk.Label(
            card,
            text="MP",
            bg=UI_COLORS["accent"],
            fg="#ffffff",
            font=("Segoe UI Black", 23),
            width=3,
            height=1,
            padx=5,
            pady=8,
        )
        logo.pack(pady=(0, 15))
        ttk.Label(card, text=f"{APP_NAME} {APP_VERSION}", style="CardTitle.TLabel").pack()
        ttk.Label(card, text=f"by {AUTHOR_NAME}", style="CardText.TLabel").pack(pady=(4, 14))
        ttk.Label(
            card,
            text=(
                "Игровой рекордер и редактор макросов с физическими scan-кодами, "
                "нативным Windows SendInput и безопасным языком сценариев."
            ),
            style="CardText.TLabel",
            justify="center",
            wraplength=650,
        ).pack(pady=(0, 18))

        facts = ttk.Frame(card, style="Card.TFrame")
        facts.pack(pady=(0, 20))
        ttk.Label(
            facts,
            text=f"Автор: {AUTHOR_NAME}  ·  GitHub: {AUTHOR_GITHUB}  ·  Лицензия: MIT",
            style="CardText.TLabel",
        ).pack()
        ttk.Label(facts, textvariable=self.update_state_var, style="CardText.TLabel").pack(
            pady=(6, 0)
        )

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.pack()
        ttk.Button(
            actions,
            text="Открыть GitHub",
            command=self.open_project_page,
            style="Ghost.TButton",
        ).pack(side="left")
        self.support_button = ttk.Button(
            actions,
            text="♥  Поддержать проект",
            command=self.open_support_page,
            style="Support.TButton",
        )
        self.support_button.pack(side="left", padx=8)
        self.about_update_button = ttk.Button(
            actions,
            text="Проверить обновления",
            command=self._update_button_clicked,
            style="Accent.TButton",
        )
        self.about_update_button.pack(side="left")

        ttk.Label(
            card,
            text="MacroPilot проверяет обновления только в публичном репозитории GitHub.",
            style="CardText.TLabel",
        ).pack(pady=(22, 0))

    @staticmethod
    def open_project_page() -> None:
        webbrowser.open_new_tab(PROJECT_URL)

    @staticmethod
    def open_support_page() -> None:
        webbrowser.open_new_tab(SUPPORT_URL)

    def _update_button_clicked(self) -> None:
        if self.available_release is not None:
            self._prompt_update(self.available_release)
        else:
            self.check_for_updates(manual=True)

    def check_for_updates(self, manual: bool = True) -> None:
        if self.update_busy:
            return
        if self.mode != "idle":
            if manual:
                messagebox.showinfo(APP_NAME, "Сначала остановите запись или воспроизведение.")
            return
        self.update_busy = True
        self.update_state_var.set("Проверяю актуальную версию…")
        if manual:
            self.status_var.set("Проверяю обновления на GitHub…")
        self._refresh_controls()

        def worker() -> None:
            try:
                release = fetch_latest_release(PROJECT_REPOSITORY)
            except UpdateError as exc:
                self._ui(self._finish_update_check, manual, None, str(exc))
            else:
                self._ui(self._finish_update_check, manual, release, None)

        threading.Thread(
            target=worker,
            name="MacroPilotUpdateCheck",
            daemon=True,
        ).start()

    def _finish_update_check(
        self,
        manual: bool,
        release: ReleaseInfo | None,
        error: str | None,
    ) -> None:
        self.update_busy = False
        if error is not None:
            self.update_state_var.set("Проверка обновлений сейчас недоступна")
            if manual:
                self.status_var.set(f"Не удалось проверить обновления: {error}")
                messagebox.showerror("Обновления", error)
            self._refresh_controls()
            return

        assert release is not None
        try:
            newer = is_newer_version(release.version, APP_VERSION)
        except UpdateError as exc:
            self.update_state_var.set("GitHub вернул неизвестную версию")
            if manual:
                messagebox.showerror("Обновления", str(exc))
            self._refresh_controls()
            return

        if newer:
            self.available_release = release
            self.update_state_var.set(f"Доступно обновление {release.version}")
            self.status_var.set(f"Доступна новая версия MacroPilot {release.version}")
            self._refresh_controls()
            if manual:
                self._prompt_update(release)
            return

        self.available_release = None
        self.update_state_var.set(f"Установлена актуальная версия {APP_VERSION}")
        if manual:
            self.status_var.set("Установлена актуальная версия")
            messagebox.showinfo("Обновления", f"MacroPilot {APP_VERSION} — актуальная версия.")
        self._refresh_controls()

    def _prompt_update(self, release: ReleaseInfo) -> None:
        if sys.platform != "win32":
            webbrowser.open_new_tab(release.page_url)
            return
        try:
            asset = choose_release_asset(release, frozen=bool(getattr(sys, "frozen", False)))
        except UpdateError as exc:
            if messagebox.askyesno(
                "Обновления",
                f"{exc}\n\nОткрыть страницу релиза в браузере?",
            ):
                webbrowser.open_new_tab(release.page_url)
            return

        notes = release.notes.strip()
        if len(notes) > 600:
            notes = notes[:597].rstrip() + "…"
        details = f"\n\n{notes}" if notes else ""
        if not messagebox.askyesno(
            "Доступно обновление",
            f"Установить MacroPilot {release.version}?{details}\n\n"
            "Приложение скачает архив, проверит его и автоматически перезапустится.",
        ):
            return

        self.update_busy = True
        self.status_var.set(f"Скачиваю MacroPilot {release.version}…")
        self.update_state_var.set(f"Загрузка версии {release.version}…")
        self._refresh_controls()
        threading.Thread(
            target=self._download_update_worker,
            args=(release, asset),
            name="MacroPilotUpdateDownload",
            daemon=True,
        ).start()

    def _download_update_worker(self, release: ReleaseInfo, asset: ReleaseAsset) -> None:
        archive = temporary_update_path(release.version)
        last_percent = -1

        def progress(received: int, total: int) -> None:
            nonlocal last_percent
            if total > 0:
                percent = min(100, int(received * 100 / total))
                if percent == last_percent:
                    return
                last_percent = percent
                text = f"Скачиваю MacroPilot {release.version}: {percent}%"
            else:
                text = f"Скачано обновления: {received / (1024 * 1024):.1f} МБ"
            self._ui(self.status_var.set, text)

        try:
            download_release_asset(asset, archive, progress=progress)
            payload_subdir = inspect_update_archive(
                archive,
                frozen=bool(getattr(sys, "frozen", False)),
            )
        except UpdateError as exc:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self._ui(self._finish_update_download, None, None, str(exc))
        except Exception as exc:  # pragma: no cover - final guard for a background thread
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self._ui(
                self._finish_update_download,
                None,
                None,
                f"Непредвиденная ошибка загрузки: {exc}",
            )
        else:
            self._ui(self._finish_update_download, archive, payload_subdir, None)

    def _finish_update_download(
        self,
        archive: Path | None,
        payload_subdir: str | None,
        error: str | None,
    ) -> None:
        self.update_busy = False
        self._refresh_controls()
        if error is not None:
            self.update_state_var.set("Не удалось загрузить обновление")
            self.status_var.set(f"Ошибка обновления: {error}")
            messagebox.showerror("Обновления", error)
            return
        assert archive is not None and payload_subdir is not None
        try:
            launch_update_installer(archive, payload_subdir)
        except UpdateError as exc:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
            self.update_state_var.set("Не удалось запустить установщик")
            self.status_var.set(f"Ошибка обновления: {exc}")
            messagebox.showerror("Обновления", str(exc))
            return
        self.status_var.set("Обновление загружено. Перезапускаю MacroPilot…")
        self.root.after(250, self.on_close)

    def _ui(self, callback: Callable[..., Any], *args: Any) -> None:
        try:
            self.root.after(0, callback, *args)
        except tk.TclError:
            pass

    def _start_safety_listener(self) -> None:
        if keyboard is None:
            return

        def on_press(key: Any, _injected: bool = False) -> None:
            if key == keyboard.Key.f9:
                if not self.start_hotkey_held:
                    self.start_hotkey_held = True
                    self._ui(self.start_recording)
            elif key == keyboard.Key.f12:
                self._ui(self.stop_current)

        def on_release(key: Any, _injected: bool = False) -> None:
            if key == keyboard.Key.f9:
                self.start_hotkey_held = False

        try:
            self.safety_listener = keyboard.Listener(
                on_press=on_press,
                on_release=on_release,
            )
            self.safety_listener.start()
        except Exception as exc:
            self.status_var.set(f"Не удалось включить глобальные F9/F12: {exc}")

    def _refresh_controls(self) -> None:
        idle = self.mode == "idle"
        has_events = bool(self.events)
        self._set_enabled(self.record_button, idle)
        self._set_enabled(self.play_button, idle and has_events)
        self._set_enabled(self.load_button, idle)
        self._set_enabled(self.save_button, idle and has_events)
        self._set_enabled(self.to_script_button, idle and has_events)
        self._set_enabled(self.clear_button, idle and has_events)
        self._set_enabled(self.delete_button, idle and has_events)
        self._set_enabled(self.recording_precision_combo, idle)
        self._set_enabled(self.speed_spin, idle)
        self._set_enabled(self.repeats_spin, idle and not self.infinite_repeats_var.get())
        self._set_enabled(self.infinite_checkbox, idle)
        self._set_enabled(
            self.block_physical_mouse_checkbox,
            idle and WINDOWS_NATIVE_AVAILABLE,
        )
        self._set_enabled(self.stop_button, not idle)

        self._set_enabled(self.script_run_button, idle)
        self._set_enabled(self.script_validate_button, idle)
        self._set_enabled(self.script_open_button, idle)
        self._set_enabled(self.script_save_button, idle)
        self._set_enabled(self.script_example_button, idle)
        self._set_enabled(self.script_speed_spin, idle)
        self._set_enabled(
            self.script_ocr_region_button,
            idle and WINDOWS_NATIVE_AVAILABLE,
        )
        self._set_enabled(
            self.script_block_physical_mouse_checkbox,
            idle and WINDOWS_NATIVE_AVAILABLE,
        )
        self._set_enabled(self.script_stop_button, not idle)
        updates_enabled = idle and not self.update_busy
        self._set_enabled(self.update_button, updates_enabled)
        self._set_enabled(self.about_update_button, updates_enabled)
        update_text = (
            f"Обновить до {self.available_release.version}"
            if self.available_release is not None
            else "Проверить обновления"
        )
        self.update_button.configure(text=update_text)
        self.about_update_button.configure(text=update_text)
        self.script_text.configure(state="normal" if idle else "disabled")

    @staticmethod
    def _set_enabled(widget: ttk.Widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _set_mode(self, mode: str, status: str) -> None:
        self.mode = mode
        self.status_var.set(status)
        self._refresh_controls()

    def _parse_speed(self) -> float | None:
        try:
            speed = float(self.speed_var.get().replace(",", "."))
        except ValueError:
            messagebox.showerror(APP_NAME, "Скорость должна быть числом от 0.1 до 10.")
            return None
        if not 0.1 <= speed <= 10:
            messagebox.showerror(APP_NAME, "Скорость должна быть от 0.1 до 10.")
            return None
        return speed

    def _parse_repeats(self) -> int | None:
        try:
            repeats = int(self.repeats_var.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Число повторов должно быть целым от 1 до 999.")
            return None
        if not 1 <= repeats <= 999:
            messagebox.showerror(APP_NAME, "Число повторов должно быть от 1 до 999.")
            return None
        return repeats

    def _start_countdown(self, label: str, callback: Callable[[], None]) -> None:
        self._set_mode("countdown", f"{label} через 3… F12 — отмена")

        def tick(value: int) -> None:
            if self.mode != "countdown":
                return
            if value <= 0:
                self.countdown_job = None
                callback()
                return
            self.status_var.set(f"{label} через {value}… F12 — отмена")
            self.countdown_job = self.root.after(1000, tick, value - 1)

        tick(3)

    def start_recording(self) -> None:
        if self.mode != "idle" or getattr(self, "record_prompt_active", False):
            return

        append = False
        if self.events:
            self.record_prompt_active = True
            try:
                choice = messagebox.askyesnocancel(
                    "В макросе уже есть запись",
                    f"Текущий макрос содержит {len(self.events)} событий.\n\n"
                    "Да — продолжить с конца текущей записи.\n"
                    "Нет — удалить текущую запись и начать заново.\n"
                    "Отмена — ничего не изменять.",
                    icon="warning",
                )
            finally:
                self.record_prompt_active = False
            if choice is None:
                self.status_var.set(
                    "Начало записи отменено — текущий макрос сохранён"
                )
                return
            append = bool(choice)
            if append and len(self.events) >= MAX_RECORDED_EVENTS:
                messagebox.showwarning(
                    "Продолжение невозможно",
                    "Текущая запись уже достигла безопасного предела событий. "
                    "Сохраните её отдельно или начните новую запись.",
                )
                return

        # Recording is immediate. A countdown here makes it very easy to press
        # a game movement key too early and lose the beginning of its hold.
        self._begin_recording(append=append)

    def _begin_recording(self, append: bool = False) -> None:
        base_count = len(self.events) if append else 0
        base_duration = macro_duration(self.events) if append else 0.0
        available_events = MAX_RECORDED_EVENTS - base_count
        if available_events <= 0:
            return
        precision_name = self.recording_precision_var.get()
        move_interval = RECORDING_PRECISION_OPTIONS.get(
            precision_name,
            RECORDING_PRECISION_OPTIONS[DEFAULT_RECORDING_PRECISION],
        )
        self.recorder = EventRecorder(
            record_moves=self.record_moves_var.get(),
            request_stop=lambda reason: self._ui(self.finish_recording, reason),
            report_error=lambda text: self._ui(self.status_var.set, f"Ошибка записи: {text}"),
            report_warning=lambda text: self._ui(self._show_recording_warning, text),
            move_interval=move_interval,
        )
        self.recorder.capacity_base_count = base_count
        self.recorder.max_recorded_events = available_events
        try:
            self.recorder.start()
        except Exception as exc:
            self._restore_window()
            self.recorder = None
            self._set_mode("idle", "Запись не запущена")
            messagebox.showerror(APP_NAME, f"Не удалось начать запись:\n{exc}")
            return
        self.recording_append_mode = append
        self.recording_base_count = base_count
        self.recording_base_duration = base_duration
        if not append:
            self.events = []
            self.current_macro_path = None
            self._refresh_event_table()
        action = "Продолжается запись" if append else "Идёт запись"
        input_mode = " игровых scan-кодов" if WINDOWS_NATIVE_AVAILABLE else ""
        recording_status = (
            f"{action}{input_mode} · {precision_name} · F10 или F12 — закончить"
        )
        self._set_mode("recording", recording_status)
        if self.minimize_var.get():
            self._minimize_for_action()
        self._schedule_recording_refresh()

    def _show_recording_warning(self, text: str) -> None:
        if self.mode != "recording":
            return
        self.status_var.set(text)
        try:
            self.root.bell()
        except tk.TclError:
            pass

    def _schedule_recording_refresh(self) -> None:
        if self.mode != "recording" or self.recorder is None:
            return
        new_event_count, new_duration = self.recorder.recording_stats()
        preview_space = max(0, MAX_TABLE_ROWS - self.recording_base_count)
        desired_new_preview = min(new_event_count, preview_space)
        previewed_new = max(0, len(self.events) - self.recording_base_count)
        if previewed_new < desired_new_preview:
            self.events.extend(
                self.recorder.snapshot_range(previewed_new, desired_new_preview)
            )
        total_event_count = self.recording_base_count + new_event_count
        total_duration = (
            self.recording_base_duration + new_duration
            if new_event_count
            else self.recording_base_duration
        )
        self._refresh_event_table(
            full=False,
            total_event_count=total_event_count,
            duration=total_duration,
        )
        self.refresh_job = self.root.after(350, self._schedule_recording_refresh)

    def _merge_recorded_segment(self, new_events: list[dict[str, Any]]) -> None:
        if not self.recording_append_mode:
            self.events = new_events
            return

        # Remove the lightweight UI preview added while recording, then append
        # the recorder-owned segment with timestamps shifted to the old end.
        del self.events[self.recording_base_count :]
        for event in new_events:
            event["t"] = round(self.recording_base_duration + float(event["t"]), 6)
        self.events.extend(new_events)

    def finish_recording(self, reason: str = "Запись остановлена") -> None:
        if self.mode != "recording" or self.recorder is None:
            return
        if self.refresh_job is not None:
            try:
                self.root.after_cancel(self.refresh_job)
            except tk.TclError:
                pass
            self.refresh_job = None
        recording_error = self.recorder.last_error
        new_events = self.recorder.stop()
        append_mode = self.recording_append_mode
        self._merge_recorded_segment(new_events)
        self.recorder = None
        self.recording_append_mode = False
        self.recording_base_count = 0
        self.recording_base_duration = 0.0
        self._restore_window()
        self._refresh_event_table()
        result = (
            f"{reason}. Добавлено: {len(new_events)} · всего: {len(self.events)}"
            if append_mode
            else f"{reason}. Событий: {len(self.events)}"
        )
        self._set_mode("idle", result)
        if recording_error:
            messagebox.showerror(
                "Ошибка записи",
                f"Перехватчик ввода сообщил ошибку:\n\n{recording_error}\n\n"
                "Текст ошибки оставлен в строке состояния. Если проблема повторится, "
                "пришлите его мне.",
            )

    def _minimize_for_action(self) -> None:
        self.window_was_minimized = True
        self.root.update_idletasks()
        self.root.iconify()
        if self.minimize_job is not None:
            try:
                self.root.after_cancel(self.minimize_job)
            except tk.TclError:
                pass
        self.minimize_job = self.root.after(150, self._ensure_window_minimized)

    def _ensure_window_minimized(self) -> None:
        self.minimize_job = None
        if not self.window_was_minimized or self.mode not in {"recording", "playing"}:
            return
        try:
            if self.root.state() != "iconic":
                self.root.iconify()
        except tk.TclError:
            pass

    def _restore_window(self) -> None:
        if self.minimize_job is not None:
            try:
                self.root.after_cancel(self.minimize_job)
            except tk.TclError:
                pass
            self.minimize_job = None
        if self.window_was_minimized:
            self.window_was_minimized = False
            self.root.deiconify()
            try:
                self.root.state("normal")
            except tk.TclError:
                pass
            self.root.lift()

    def play_recording_countdown(self) -> None:
        if not self.events:
            return
        speed = self._parse_speed()
        infinite = self.infinite_repeats_var.get()
        repeats = None if infinite else self._parse_repeats()
        if speed is None or (not infinite and repeats is None):
            return
        estimated_seconds = macro_duration(self.events) * repeats / speed if repeats is not None else 0.0
        if repeats is not None and (repeats > 20 or estimated_seconds > 300) and not messagebox.askyesno(
            "Длительное выполнение",
            f"Макрос будет повторён {repeats} раз.\n"
            f"Ориентировочная длительность: {estimated_seconds:.0f} с.\n\n"
            "Продолжить?",
        ):
            return
        self._start_countdown(
            "Воспроизведение начнётся",
            lambda: self._begin_recording_playback(speed, repeats),
        )

    def _begin_recording_playback(self, speed: float, repeats: int | None) -> None:
        if self.minimize_var.get():
            self._minimize_for_action()
        self._start_runner(speed)
        assert self.runner is not None
        status = (
            "Бесконечное воспроизведение · F12 — остановить"
            if repeats is None
            else "Воспроизведение записи · F12 — остановить"
        )
        self._set_mode("playing", status)
        self.worker = threading.Thread(
            target=self.runner.run_recording,
            args=(self.events.copy(), repeats),
            name="MacroPilotPlayback",
            daemon=True,
        )
        self.worker.start()

    def validate_script_text(self, show_dialog: bool = True) -> Any:
        self._clear_script_error()
        try:
            program = parse_script(self.script_text.get("1.0", "end-1c"))
            self._validate_script_keys(program.nodes)
        except ScriptError as exc:
            self._highlight_script_error(exc.line_no)
            self.status_var.set(str(exc))
            if show_dialog:
                messagebox.showerror("Ошибка сценария", str(exc))
            return None
        except ValueError as exc:
            self.status_var.set(str(exc))
            if show_dialog:
                messagebox.showerror("Ошибка сценария", str(exc))
            return None
        self.status_var.set(f"Сценарий корректен · команд с учётом циклов: {program.estimated_steps}")
        if show_dialog:
            messagebox.showinfo(APP_NAME, f"Сценарий корректен.\nКоманд с учётом циклов: {program.estimated_steps}")
        return program

    def _script_directory(self) -> Path:
        if self.current_script_path is not None:
            return self.current_script_path.resolve().parent
        if bool(getattr(sys, "frozen", False)):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _resolve_script_image_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self._script_directory() / path
        return path.resolve()

    def _validate_script_keys(self, nodes: Iterable[ScriptNode]) -> None:
        for node in nodes:
            if isinstance(node, RepeatBlock):
                self._validate_script_keys(node.body)
            elif isinstance(node, IfBlock):
                self._validate_script_keys(node.true_body)
                self._validate_script_keys(node.false_body)
            elif node.name in {"PRESS", "KEY_DOWN", "KEY_UP", "HOTKEY"}:
                for token in node.args:
                    try:
                        resolve_script_key(token)
                    except ValueError as exc:
                        raise ScriptError(node.line_no, str(exc)) from exc
            elif node.name in {"WAIT_IMAGE", "CLICK_IMAGE"}:
                image_path = self._resolve_script_image_path(node.args[0])
                if not image_path.is_file():
                    raise ScriptError(
                        node.line_no,
                        f"файл изображения не найден: {image_path}",
                    )

    def play_script_countdown(self) -> None:
        program = self.validate_script_text(show_dialog=False)
        if program is None:
            messagebox.showerror("Ошибка сценария", self.status_var.get())
            return
        speed = self._parse_speed()
        if speed is None:
            return
        if program.estimated_steps > 10_000 and not messagebox.askyesno(
            "Большой сценарий",
            f"Сценарий выполнит около {program.estimated_steps:,} команд.\n\nПродолжить?",
        ):
            return
        script_directory = self._script_directory()
        self._start_countdown(
            "Сценарий начнётся",
            lambda: self._begin_script_playback(
                speed,
                program.nodes,
                script_directory,
            ),
        )

    def _begin_script_playback(
        self,
        speed: float,
        nodes: Iterable[ScriptNode],
        script_directory: Path,
    ) -> None:
        if self.minimize_var.get():
            self._minimize_for_action()
        self._start_runner(speed, script_directory=script_directory)
        assert self.runner is not None
        self._set_mode("playing", "Выполняется сценарий · F12 — остановить")
        self.worker = threading.Thread(
            target=self.runner.run_script,
            args=(tuple(nodes),),
            name="MacroPilotScript",
            daemon=True,
        )
        self.worker.start()

    def _start_runner(
        self,
        speed: float,
        script_directory: Path | None = None,
    ) -> None:
        self.runner = AutomationRunner(
            speed=speed,
            on_progress=lambda text: self._ui(self._set_progress, text),
            on_finished=lambda stopped, error: self._ui(self._runner_finished, stopped, error),
            script_directory=script_directory,
            block_physical_mouse=self.block_physical_mouse_var.get(),
        )

    def _set_progress(self, text: str) -> None:
        if self.mode == "playing":
            self.status_var.set(f"{text} · F12 — остановить")

    def _runner_finished(self, stopped: bool, error: str | None) -> None:
        self.runner = None
        self.worker = None
        self._restore_window()
        self._set_mode("idle", "Остановлено" if stopped else "Выполнение завершено")
        if error:
            self.status_var.set(f"Ошибка выполнения: {error}")
            messagebox.showerror(APP_NAME, f"Ошибка выполнения:\n{error}")

    def stop_current(self) -> None:
        if self.mode == "countdown":
            if self.countdown_job is not None:
                try:
                    self.root.after_cancel(self.countdown_job)
                except tk.TclError:
                    pass
                self.countdown_job = None
            self._set_mode("idle", "Запуск отменён")
        elif self.mode == "recording":
            self.finish_recording()
        elif self.mode == "playing" and self.runner is not None:
            self.status_var.set("Останавливаю…")
            self.runner.stop()

    def _refresh_event_table(
        self,
        full: bool = True,
        total_event_count: int | None = None,
        duration: float | None = None,
    ) -> None:
        displayed = self.events[:MAX_TABLE_ROWS]
        if total_event_count is None:
            total_event_count = len(self.events)
        if duration is None:
            duration = macro_duration(self.events)
        if self.table_event_count > len(displayed):
            full = True
        if full:
            children = self.event_tree.get_children()
            if children:
                self.event_tree.delete(*children)
            self.table_event_count = 0
        for zero_based_index in range(self.table_event_count, len(displayed)):
            event = displayed[zero_based_index]
            index = zero_based_index + 1
            title, details = describe_event(event)
            self.event_tree.insert(
                "",
                "end",
                values=(index, f"{event['t']:.3f}", title, details),
                tags=("odd" if index % 2 else "even",),
            )
        self.table_event_count = len(displayed)
        extra = max(0, total_event_count - len(displayed))
        suffix = f" · в таблице не показано: {extra}" if extra > 0 else ""
        capacity_suffix = ""
        if self.mode == "recording" and self.recorder is not None:
            percent = min(100, total_event_count * 100 / MAX_RECORDED_EVENTS)
            capacity_suffix = f" · заполнено: {percent:.1f}%"
        self.summary_var.set(
            f"Событий: {total_event_count} · длительность: "
            f"{duration:.2f} с{capacity_suffix}{suffix}"
        )
        self._refresh_controls()

    def delete_selected_events(self) -> None:
        indices: list[int] = []
        for item_id in self.event_tree.selection():
            values = self.event_tree.item(item_id, "values")
            if values:
                indices.append(int(values[0]) - 1)
        for index in sorted(indices, reverse=True):
            if 0 <= index < len(self.events):
                self.events.pop(index)
        self.current_macro_path = None
        self._refresh_event_table()
        self.status_var.set(f"Удалено событий: {len(indices)}")

    def clear_events(self) -> None:
        if not self.events:
            return
        if messagebox.askyesno(APP_NAME, "Очистить текущую запись?"):
            self.events.clear()
            self.current_macro_path = None
            self._refresh_event_table()
            self.status_var.set("Запись очищена")

    def save_macro_file(self) -> None:
        if not self.events:
            return
        initial = self.current_macro_path.name if self.current_macro_path else "macro.macro.json"
        path = filedialog.asksaveasfilename(
            title="Сохранить макрос",
            defaultextension=".macro.json",
            initialfile=initial,
            filetypes=(("Макрос MacroPilot", "*.macro.json"), ("JSON", "*.json"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            save_macro(path, self.events)
        except (OSError, MacroFormatError) as exc:
            messagebox.showerror(APP_NAME, f"Не удалось сохранить макрос:\n{exc}")
            return
        self.current_macro_path = Path(path)
        self.status_var.set(f"Макрос сохранён: {self.current_macro_path.name}")

    def load_macro_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Открыть макрос",
            filetypes=(("Макрос MacroPilot", "*.macro.json"), ("JSON", "*.json"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            events = load_macro(path)
        except (OSError, MacroFormatError) as exc:
            messagebox.showerror(APP_NAME, f"Не удалось открыть макрос:\n{exc}")
            return
        self.events = events
        self.current_macro_path = Path(path)
        self._refresh_event_table()
        self.status_var.set(f"Открыт макрос: {self.current_macro_path.name}")

    def convert_to_script(self) -> None:
        if not self.events:
            return
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", events_to_script(self.events))
        self.script_text.edit_modified(True)
        self.current_script_path = None
        self.notebook.select(self.script_tab)
        self.status_var.set("Запись преобразована в редактируемый сценарий")

    def load_example_script(self) -> None:
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", EXAMPLE_SCRIPT)
        self.script_text.edit_modified(False)
        self.current_script_path = None
        self._clear_script_error()
        self.status_var.set("Загружен пример сценария")

    def save_script_file(self) -> None:
        initial = self.current_script_path.name if self.current_script_path else "scenario.macro.txt"
        path = filedialog.asksaveasfilename(
            title="Сохранить сценарий",
            defaultextension=".macro.txt",
            initialfile=initial,
            filetypes=(("Сценарий MacroPilot", "*.macro.txt"), ("Текст", "*.txt"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            Path(path).write_text(self.script_text.get("1.0", "end-1c") + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Не удалось сохранить сценарий:\n{exc}")
            return
        self.current_script_path = Path(path)
        self.script_text.edit_modified(False)
        self.status_var.set(f"Сценарий сохранён: {self.current_script_path.name}")

    def load_script_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Открыть сценарий",
            filetypes=(("Сценарий MacroPilot", "*.macro.txt"), ("Текст", "*.txt"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            source = Path(path)
            if source.stat().st_size > 2 * 1024 * 1024:
                raise OSError("Файл сценария больше 2 МБ")
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"Не удалось открыть сценарий:\n{exc}")
            return
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", text)
        self.script_text.edit_modified(False)
        self.current_script_path = source
        self._clear_script_error()
        self.status_var.set(f"Открыт сценарий: {source.name}")

    def _clear_script_error(self) -> None:
        self.script_text.tag_remove("script_error", "1.0", "end")

    def _highlight_script_error(self, line_no: int) -> None:
        self.script_text.tag_add("script_error", f"{line_no}.0", f"{line_no}.end")
        self.script_text.see(f"{line_no}.0")
        self.script_text.mark_set("insert", f"{line_no}.0")
        self.script_text.focus_set()

    def on_close(self) -> None:
        if self.countdown_job is not None:
            try:
                self.root.after_cancel(self.countdown_job)
            except tk.TclError:
                pass
        if self.recorder is not None:
            self.recorder.stop()
        if self.runner is not None:
            self.runner.stop()
        if self.safety_listener is not None:
            try:
                self.safety_listener.stop()
            except Exception:
                pass
        self.root.destroy()


def main() -> int:
    update_result = handle_update_command()
    if update_result is not None:
        return update_result
    cleanup_previous_update_stage()
    enable_windows_dpi_awareness()
    root = tk.Tk()
    if PYNPUT_IMPORT_ERROR is not None:
        root.withdraw()
        messagebox.showerror(
            APP_NAME,
            "Не удалось загрузить pynput.\n\n"
            "Установите зависимости командой:\n"
            "python -m pip install -r requirements.txt\n\n"
            f"Техническая информация: {PYNPUT_IMPORT_ERROR}",
        )
        root.destroy()
        return 1
    MacroPilotApp(root)

    install_directory = (
        Path(sys.executable).resolve().parent
        if bool(getattr(sys, "frozen", False))
        else Path(__file__).resolve().parent
    )
    update_error_log = install_directory / "update-error.log"
    if update_error_log.is_file():
        try:
            update_error = update_error_log.read_text(encoding="utf-8-sig").strip()
            update_error_log.unlink(missing_ok=True)
        except OSError:
            update_error = ""
        if update_error:
            root.after(
                100,
                lambda: messagebox.showerror(
                    "Обновление",
                    update_error,
                    parent=root,
                ),
            )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
