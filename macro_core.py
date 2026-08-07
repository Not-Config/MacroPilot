from __future__ import annotations

import json
import math
import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from windows_input import scan_key_from_descriptor, scan_token


APP_NAME = "MacroPilot"
APP_VERSION = "1.6.1"
MACRO_FORMAT = "MacroPilot macro"
MACRO_VERSION = 1
MAX_MACRO_BYTES = 128 * 1024 * 1024
MAX_EVENTS = 1_000_000
RECORDING_RELEASE_RESERVE = 1_024
MAX_RECORDED_EVENTS = MAX_EVENTS - RECORDING_RELEASE_RESERVE
RECORDING_WARNING_EVENTS = 800_000
MAX_SCRIPT_STEPS = 100_000
MAX_REPEAT = 10_000
MAX_NESTING = 20

BUTTONS = {"left", "right", "middle"}
EVENT_TYPES = {
    "mouse_move",
    "mouse_button",
    "mouse_scroll",
    "key_down",
    "key_up",
}


class MacroFormatError(ValueError):
    """Raised when a macro file is malformed or unsupported."""


class ScriptError(ValueError):
    """A script validation error with a source line."""

    def __init__(self, line_no: int, message: str) -> None:
        self.line_no = line_no
        self.message = message
        super().__init__(f"Строка {line_no}: {message}")


@dataclass(slots=True)
class ScriptCommand:
    name: str
    args: tuple[Any, ...]
    line_no: int


@dataclass(slots=True)
class RepeatBlock:
    count: int
    line_no: int
    body: list[ScriptNode] = field(default_factory=list)


ScriptNode = ScriptCommand | RepeatBlock


@dataclass(slots=True)
class ScriptProgram:
    nodes: tuple[ScriptNode, ...]
    estimated_steps: int


def _finite_float(token: str, line_no: int, label: str) -> float:
    try:
        value = float(token.replace(",", "."))
    except ValueError as exc:
        raise ScriptError(line_no, f"{label}: требуется число, получено {token!r}") from exc
    if not math.isfinite(value):
        raise ScriptError(line_no, f"{label}: число должно быть конечным")
    return value


def _bounded_float(
    token: str,
    line_no: int,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    value = _finite_float(token, line_no, label)
    if not minimum <= value <= maximum:
        raise ScriptError(line_no, f"{label}: допустимо от {minimum:g} до {maximum:g}")
    return value


def _bounded_int(
    token: str,
    line_no: int,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(token)
    except ValueError as exc:
        raise ScriptError(line_no, f"{label}: требуется целое число, получено {token!r}") from exc
    if not minimum <= value <= maximum:
        raise ScriptError(line_no, f"{label}: допустимо от {minimum} до {maximum}")
    return value


def _coordinate(token: str, line_no: int, label: str) -> int:
    return _bounded_int(token, line_no, label, -1_000_000, 1_000_000)


def _button(token: str, line_no: int) -> str:
    value = token.lower()
    aliases = {
        "лкм": "left",
        "пкм": "right",
        "скм": "middle",
        "левая": "left",
        "правая": "right",
        "средняя": "middle",
    }
    value = aliases.get(value, value)
    if value not in BUTTONS:
        raise ScriptError(line_no, "кнопка мыши: left, right или middle")
    return value


def _arity(args: list[str], line_no: int, command: str, allowed: set[int]) -> None:
    if len(args) not in allowed:
        variants = ", ".join(str(item) for item in sorted(allowed))
        raise ScriptError(
            line_no,
            f"{command}: неверное число аргументов (допустимо: {variants})",
        )


def _parse_command(name: str, args: list[str], line_no: int) -> ScriptCommand:
    if name == "WAIT":
        _arity(args, line_no, name, {1})
        return ScriptCommand(name, (_bounded_float(args[0], line_no, "пауза", 0, 86_400),), line_no)

    if name in {"MOVE", "MOVE_BY"}:
        _arity(args, line_no, name, {2, 3})
        duration = _bounded_float(args[2], line_no, "длительность", 0, 3_600) if len(args) == 3 else 0.0
        return ScriptCommand(
            name,
            (
                _coordinate(args[0], line_no, "X"),
                _coordinate(args[1], line_no, "Y"),
                duration,
            ),
            line_no,
        )

    if name == "CLICK":
        _arity(args, line_no, name, {0, 1, 2, 3})
        button = _button(args[0], line_no) if args else "left"
        count = _bounded_int(args[1], line_no, "число кликов", 1, 100) if len(args) >= 2 else 1
        interval = _bounded_float(args[2], line_no, "интервал", 0, 60) if len(args) == 3 else 0.1
        return ScriptCommand(name, (button, count, interval), line_no)

    if name == "CLICK_AT":
        _arity(args, line_no, name, {2, 3, 4, 5})
        button = _button(args[2], line_no) if len(args) >= 3 else "left"
        count = _bounded_int(args[3], line_no, "число кликов", 1, 100) if len(args) >= 4 else 1
        interval = _bounded_float(args[4], line_no, "интервал", 0, 60) if len(args) == 5 else 0.1
        return ScriptCommand(
            name,
            (
                _coordinate(args[0], line_no, "X"),
                _coordinate(args[1], line_no, "Y"),
                button,
                count,
                interval,
            ),
            line_no,
        )

    if name in {"DOWN", "UP"}:
        _arity(args, line_no, name, {1})
        return ScriptCommand(name, (_button(args[0], line_no),), line_no)

    if name == "SCROLL":
        _arity(args, line_no, name, {1, 2})
        if len(args) == 1:
            dx, dy = 0, _bounded_int(args[0], line_no, "прокрутка", -10_000, 10_000)
        else:
            dx = _bounded_int(args[0], line_no, "горизонтальная прокрутка", -10_000, 10_000)
            dy = _bounded_int(args[1], line_no, "вертикальная прокрутка", -10_000, 10_000)
        return ScriptCommand(name, (dx, dy), line_no)

    if name in {"PRESS", "KEY_DOWN", "KEY_UP"}:
        _arity(args, line_no, name, {1})
        if not args[0]:
            raise ScriptError(line_no, f"{name}: имя клавиши не может быть пустым")
        return ScriptCommand(name, (args[0],), line_no)

    if name == "HOTKEY":
        if not args:
            raise ScriptError(line_no, "HOTKEY: укажите хотя бы одну клавишу")
        if len(args) > 10:
            raise ScriptError(line_no, "HOTKEY: допускается не более 10 клавиш")
        return ScriptCommand(name, tuple(args), line_no)

    if name == "TYPE":
        _arity(args, line_no, name, {1, 2})
        if len(args[0]) > 10_000:
            raise ScriptError(line_no, "TYPE: допускается не более 10 000 символов")
        interval = _bounded_float(args[1], line_no, "интервал", 0, 60) if len(args) == 2 else 0.03
        return ScriptCommand(name, (args[0], interval), line_no)

    raise ScriptError(line_no, f"неизвестная команда {name!r}")


def _count_steps(nodes: Iterable[ScriptNode], limit: int = MAX_SCRIPT_STEPS) -> int:
    total = 0
    for node in nodes:
        if isinstance(node, ScriptCommand):
            total += 1
        else:
            nested = _count_steps(node.body, limit)
            total += node.count * nested
        if total > limit:
            raise ScriptError(
                node.line_no,
                f"сценарий разворачивается более чем в {limit:,} команд",
            )
    return total


def parse_script(text: str) -> ScriptProgram:
    """Parse and validate MacroPilot's small, non-Python scripting language."""

    root: list[ScriptNode] = []
    bodies: list[list[ScriptNode]] = [root]
    blocks: list[RepeatBlock] = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        try:
            parts = shlex.split(raw_line, comments=True, posix=True)
        except ValueError as exc:
            raise ScriptError(line_no, f"ошибка кавычек: {exc}") from exc
        if not parts:
            continue

        name = parts[0].upper()
        args = parts[1:]

        if name == "REPEAT":
            _arity(args, line_no, name, {1})
            if len(blocks) >= MAX_NESTING:
                raise ScriptError(line_no, f"вложенность циклов больше {MAX_NESTING}")
            block = RepeatBlock(
                count=_bounded_int(args[0], line_no, "число повторов", 1, MAX_REPEAT),
                line_no=line_no,
            )
            bodies[-1].append(block)
            blocks.append(block)
            bodies.append(block.body)
            continue

        if name == "END":
            _arity(args, line_no, name, {0})
            if not blocks:
                raise ScriptError(line_no, "END без соответствующего REPEAT")
            if not bodies[-1]:
                raise ScriptError(blocks[-1].line_no, "блок REPEAT не может быть пустым")
            blocks.pop()
            bodies.pop()
            continue

        bodies[-1].append(_parse_command(name, args, line_no))

    if blocks:
        raise ScriptError(blocks[-1].line_no, "для REPEAT отсутствует END")

    estimated_steps = _count_steps(root)
    return ScriptProgram(tuple(root), estimated_steps)


def _json_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MacroFormatError(f"{label}: требуется число")
    number = float(value)
    if not math.isfinite(number):
        raise MacroFormatError(f"{label}: число должно быть конечным")
    return number


def _json_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MacroFormatError(f"{label}: требуется целое число")
    return value


def _validate_key(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MacroFormatError(f"{label}: неверное описание клавиши")
    kind = value.get("kind")
    raw = value.get("value")
    if kind in {"char", "special"}:
        if not isinstance(raw, str) or not raw:
            raise MacroFormatError(f"{label}: пустое значение клавиши")
        return {"kind": kind, "value": raw}
    if kind == "vk":
        return {"kind": kind, "value": _json_int(raw, f"{label}.value")}
    if kind == "scan":
        scan_code = _json_int(raw, f"{label}.value")
        vk = _json_int(value.get("vk", 0), f"{label}.vk")
        extended = value.get("extended", False)
        if not 0 <= scan_code <= 0xFFFF:
            raise MacroFormatError(f"{label}.value: scan-код вне диапазона")
        if not 0 <= vk <= 0xFFFF:
            raise MacroFormatError(f"{label}.vk: virtual-key вне диапазона")
        if not isinstance(extended, bool):
            raise MacroFormatError(f"{label}.extended: требуется true или false")
        return {
            "kind": kind,
            "value": scan_code,
            "vk": vk,
            "extended": extended,
        }
    raise MacroFormatError(f"{label}: неизвестный тип клавиши {kind!r}")


def validate_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for index, event in enumerate(events):
        label = f"Событие {index + 1}"
        if index >= MAX_EVENTS:
            raise MacroFormatError(f"Макрос содержит более {MAX_EVENTS:,} событий")
        if not isinstance(event, dict):
            raise MacroFormatError(f"{label}: ожидался объект")

        timestamp = _json_number(event.get("t"), f"{label}.t")
        if timestamp < 0:
            raise MacroFormatError(f"{label}.t: время не может быть отрицательным")
        event_type = event.get("type")
        if event_type not in EVENT_TYPES:
            raise MacroFormatError(f"{label}: неизвестный тип {event_type!r}")

        item: dict[str, Any] = {"t": round(timestamp, 6), "type": event_type}
        if event_type == "mouse_move":
            item.update(
                x=_json_int(event.get("x"), f"{label}.x"),
                y=_json_int(event.get("y"), f"{label}.y"),
            )
        elif event_type == "mouse_button":
            button = event.get("button")
            if button not in BUTTONS:
                raise MacroFormatError(f"{label}.button: left, right или middle")
            pressed = event.get("pressed")
            if not isinstance(pressed, bool):
                raise MacroFormatError(f"{label}.pressed: требуется true или false")
            item.update(
                x=_json_int(event.get("x"), f"{label}.x"),
                y=_json_int(event.get("y"), f"{label}.y"),
                button=button,
                pressed=pressed,
            )
        elif event_type == "mouse_scroll":
            item.update(
                x=_json_int(event.get("x"), f"{label}.x"),
                y=_json_int(event.get("y"), f"{label}.y"),
                dx=_json_int(event.get("dx"), f"{label}.dx"),
                dy=_json_int(event.get("dy"), f"{label}.dy"),
            )
        else:
            item["key"] = _validate_key(event.get("key"), f"{label}.key")
        normalized.append(item)

    normalized.sort(key=lambda item: item["t"])
    return normalized


def compact_repeated_key_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop keyboard autorepeat downs while preserving the real hold timing."""

    normalized = validate_events(events)
    held: set[tuple[Any, ...]] = set()
    write_index = 0
    for event in normalized:
        event_type = event["type"]
        keep = True
        if event_type in {"key_down", "key_up"}:
            key = event["key"]
            identity = (
                ("scan", int(key["value"]), bool(key.get("extended", False)))
                if key["kind"] == "scan"
                else (str(key["kind"]), key["value"])
            )
            if event_type == "key_down":
                if identity in held:
                    keep = False
                else:
                    held.add(identity)
            else:
                held.discard(identity)
        if keep:
            normalized[write_index] = event
            write_index += 1

    del normalized[write_index:]
    return normalized


def save_macro(path: str | os.PathLike[str], events: Iterable[dict[str, Any]]) -> None:
    destination = Path(path)
    normalized = compact_repeated_key_events(events)
    document = {
        "format": MACRO_FORMAT,
        "version": MACRO_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "events": normalized,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        # Keep the existing JSON schema, but omit formatting whitespace.  A
        # long mouse recording becomes roughly twice as small while old,
        # pretty-printed v1 files remain fully compatible with load_macro().
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        if temporary.stat().st_size > MAX_MACRO_BYTES:
            limit_mb = MAX_MACRO_BYTES // (1024 * 1024)
            raise MacroFormatError(f"Макрос больше безопасного лимита {limit_mb} МБ")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_macro(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    source = Path(path)
    if source.stat().st_size > MAX_MACRO_BYTES:
        limit_mb = MAX_MACRO_BYTES // (1024 * 1024)
        raise MacroFormatError(f"Файл больше безопасного лимита {limit_mb} МБ")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MacroFormatError(f"Не удалось прочитать JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise MacroFormatError("В корне файла должен быть объект")
    if document.get("format") != MACRO_FORMAT:
        raise MacroFormatError("Это не файл макроса MacroPilot")
    if document.get("version") != MACRO_VERSION:
        raise MacroFormatError(f"Версия макроса {document.get('version')!r} не поддерживается")
    events = document.get("events")
    if not isinstance(events, list):
        raise MacroFormatError("Поле events должно быть списком")
    return compact_repeated_key_events(events)


def macro_duration(events: Iterable[dict[str, Any]]) -> float:
    return max((float(event["t"]) for event in events), default=0.0)


def describe_event(event: dict[str, Any]) -> tuple[str, str]:
    event_type = event["type"]
    if event_type == "mouse_move":
        return "Движение мыши", f"x={event['x']}, y={event['y']}"
    if event_type == "mouse_button":
        action = "нажата" if event["pressed"] else "отпущена"
        return "Кнопка мыши", f"{event['button']}, {action}, x={event['x']}, y={event['y']}"
    if event_type == "mouse_scroll":
        return "Прокрутка", f"dx={event['dx']}, dy={event['dy']}, x={event['x']}, y={event['y']}"
    action = "Клавиша нажата" if event_type == "key_down" else "Клавиша отпущена"
    key = event["key"]
    if key["kind"] == "scan":
        suffix = ", extended" if key.get("extended") else ""
        return action, f"scan=0x{key['value']:02X}, vk=0x{key.get('vk', 0):02X}{suffix}"
    return action, f"{key['kind']}:{key['value']}"


def _format_number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _key_script_token(key: dict[str, Any]) -> str:
    kind, value = key["kind"], key["value"]
    if kind == "scan":
        return scan_token(scan_key_from_descriptor(key))
    if kind == "special":
        return str(value)
    if kind == "vk":
        return f"vk:{value}"
    if value == " ":
        return "space"
    if len(value) == 1 and value.isprintable():
        return json.dumps(value, ensure_ascii=False)
    return "char:" + str(value).encode("utf-8").hex()


def events_to_script(events: Iterable[dict[str, Any]]) -> str:
    normalized = compact_repeated_key_events(events)
    lines = [
        "# Сценарий создан из записи MacroPilot.",
        "# F12 — аварийная остановка.",
    ]
    previous_time = 0.0

    for event in normalized:
        delay = max(0.0, float(event["t"]) - previous_time)
        if delay >= 0.01:
            lines.append(f"WAIT {_format_number(delay)}")
        previous_time = float(event["t"])

        event_type = event["type"]
        if event_type == "mouse_move":
            lines.append(f"MOVE {event['x']} {event['y']}")
        elif event_type == "mouse_button":
            lines.append(f"MOVE {event['x']} {event['y']}")
            action = "DOWN" if event["pressed"] else "UP"
            lines.append(f"{action} {event['button']}")
        elif event_type == "mouse_scroll":
            lines.append(f"MOVE {event['x']} {event['y']}")
            lines.append(f"SCROLL {event['dx']} {event['dy']}")
        else:
            action = "KEY_DOWN" if event_type == "key_down" else "KEY_UP"
            lines.append(f"{action} {_key_script_token(event['key'])}")

    return "\n".join(lines) + "\n"


EXAMPLE_SCRIPT = '''# Пример сценария MacroPilot
# После запуска будет обратный отсчёт. F12 останавливает выполнение.

MOVE 500 350 0.4
CLICK left
WAIT 0.5
TYPE "Привет из MacroPilot!" 0.04
PRESS enter

REPEAT 3
    MOVE_BY 80 0 0.2
    CLICK left
    WAIT 0.3
END
'''
