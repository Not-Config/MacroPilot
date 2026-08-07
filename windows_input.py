from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


WINDOWS_NATIVE_AVAILABLE = sys.platform == "win32"

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
KEYDOWN_MESSAGES = {WM_KEYDOWN, WM_SYSKEYDOWN}
KEYUP_MESSAGES = {WM_KEYUP, WM_SYSKEYUP}

VK_F9 = 0x78
VK_F10 = 0x79
VK_F12 = 0x7B
LLKHF_EXTENDED = 0x01

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC_EX = 4

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2


@dataclass(frozen=True, slots=True)
class ScanKey:
    """A physical Windows key as seen by the low-level keyboard hook."""

    scan_code: int
    vk: int = 0
    extended: bool = False


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = (("type", wintypes.DWORD), ("value", INPUTUNION))


def _bind_private_send_input(user32: Any) -> Any:
    """Bind SendInput to this module's INPUT type, isolated from pynput."""

    send_input_type = ctypes.WINFUNCTYPE(
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
        use_last_error=True,
    )
    return send_input_type(("SendInput", user32))


def enable_windows_dpi_awareness(
    user32: Any | None = None,
    shcore: Any | None = None,
) -> bool:
    """Keep recorded and replayed mouse coordinates in physical pixels."""

    owns_user32 = user32 is None
    if owns_user32:
        if not WINDOWS_NATIVE_AVAILABLE:
            return False
        user32 = ctypes.WinDLL("user32", use_last_error=True)

    try:
        setter = user32.SetProcessDpiAwarenessContext
        if owns_user32:
            setter.argtypes = (ctypes.c_void_p,)
            setter.restype = wintypes.BOOL
        if setter(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return True
    except (AttributeError, OSError):
        pass

    try:
        owns_shcore = shcore is None
        if owns_shcore:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
        setter = shcore.SetProcessDpiAwareness
        if owns_shcore:
            setter.argtypes = (ctypes.c_int,)
            setter.restype = ctypes.c_long
        result = int(setter(PROCESS_PER_MONITOR_DPI_AWARE))
        # E_ACCESSDENIED means another component already selected the process
        # DPI mode, so coordinates are no longer in the default unaware mode.
        if result in {0, 0x80070005, -2147024891}:
            return True
    except (AttributeError, OSError):
        pass

    try:
        setter = user32.SetProcessDPIAware
        if owns_user32:
            setter.argtypes = ()
            setter.restype = wintypes.BOOL
        return bool(setter())
    except (AttributeError, OSError):
        return False


def scan_key_from_descriptor(descriptor: dict[str, Any]) -> ScanKey:
    return ScanKey(
        scan_code=int(descriptor["value"]),
        vk=int(descriptor.get("vk", 0)),
        extended=bool(descriptor.get("extended", False)),
    )


def scan_token(key: ScanKey) -> str:
    prefix = "e0-" if key.extended else ""
    return f"scan:{prefix}{key.scan_code:02x}"


def parse_scan_token(token: str) -> ScanKey:
    if not token.lower().startswith("scan:"):
        raise ValueError(f"Не scan-код: {token!r}")
    value = token[5:].lower()
    extended = False
    if value.startswith(("e0-", "e1-")):
        extended = True
        value = value[3:]
    try:
        scan_code = int(value, 16)
    except ValueError as exc:
        raise ValueError(f"Неверный scan-код {token!r}") from exc
    if not 0 <= scan_code <= 0xFFFF:
        raise ValueError(f"Scan-код вне диапазона: {token!r}")
    return ScanKey(scan_code=scan_code, extended=extended)


def get_pressed_scan_keys(user32: Any | None = None) -> list[ScanKey]:
    """Return physical keyboard keys already held when recording starts."""

    if user32 is None:
        if not WINDOWS_NATIVE_AVAILABLE:
            return []
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAsyncKeyState.argtypes = (wintypes.INT,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        user32.MapVirtualKeyW.restype = wintypes.UINT

    pressed: list[ScanKey] = []
    # 1..6 are mouse buttons. Generic Shift/Ctrl/Alt would duplicate their
    # left/right virtual keys, so only retain the side-specific variants.
    skipped = {0x10, 0x11, 0x12}
    for vk in range(7, 256):
        if vk in skipped or not (int(user32.GetAsyncKeyState(vk)) & 0x8000):
            continue
        mapped = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX))
        if not mapped:
            continue
        prefix = (mapped >> 8) & 0xFF
        pressed.append(
            ScanKey(
                scan_code=mapped & 0xFF,
                vk=vk,
                extended=prefix in {0xE0, 0xE1},
            )
        )
    return pressed


class WindowsKeyboardController:
    """Keyboard sender based on SendInput scan codes for game compatibility."""

    def __init__(self, user32: Any | None = None) -> None:
        if user32 is None:
            if not WINDOWS_NATIVE_AVAILABLE:
                raise RuntimeError("Windows SendInput доступен только в Windows")
            # pynput also declares user32.SendInput, but with its own INPUT
            # structure. Reusing that global ctypes function can make Windows
            # reject our pointer even though both structures have the same
            # binary layout. Bind a private prototype to avoid type leakage.
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._send_input = _bind_private_send_input(user32)
            user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
            user32.MapVirtualKeyW.restype = wintypes.UINT
            user32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
            user32.VkKeyScanW.restype = ctypes.c_short
        else:
            self._send_input = user32.SendInput
        self.user32 = user32
        self._map_virtual_key = user32.MapVirtualKeyW
        self._vk_key_scan = user32.VkKeyScanW

    def _send(self, vk: int, scan_code: int, flags: int) -> None:
        payload = INPUT(
            type=INPUT_KEYBOARD,
            value=INPUTUNION(
                ki=KEYBDINPUT(
                    wVk=vk,
                    wScan=scan_code,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        payload_pointer = ctypes.pointer(payload)
        sent = int(self._send_input(1, payload_pointer, ctypes.sizeof(INPUT)))
        if sent != 1:
            error_code = int(getattr(ctypes, "get_last_error", lambda: 0)())
            raise OSError(error_code, "Windows SendInput не принял событие клавиатуры")

    def _scan_from_vk(self, vk: int) -> ScanKey:
        mapped = int(self._map_virtual_key(vk, MAPVK_VK_TO_VSC_EX))
        prefix = (mapped >> 8) & 0xFF
        return ScanKey(
            scan_code=mapped & 0xFF,
            vk=vk,
            extended=prefix in {0xE0, 0xE1},
        )

    def _coerce_key(self, key: Any) -> ScanKey:
        if isinstance(key, ScanKey):
            return key
        if isinstance(key, dict) and key.get("kind") == "scan":
            return scan_key_from_descriptor(key)

        candidate = getattr(key, "value", key)
        vk = getattr(candidate, "vk", None)
        char = getattr(candidate, "char", None)
        if vk is None and char:
            mapped = int(self._vk_key_scan(char[0]))
            if mapped != -1:
                vk = mapped & 0xFF
        if vk is None and isinstance(key, str) and len(key) == 1:
            mapped = int(self._vk_key_scan(key))
            if mapped != -1:
                vk = mapped & 0xFF
        if vk is None:
            raise ValueError(f"Не удалось определить Windows scan-код для {key!r}")
        return self._scan_from_vk(int(vk))

    def _send_key(self, key: Any, pressed: bool) -> None:
        physical = self._coerce_key(key)
        flags = 0 if pressed else KEYEVENTF_KEYUP
        if physical.scan_code:
            flags |= KEYEVENTF_SCANCODE
            if physical.extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            self._send(0, physical.scan_code, flags)
        elif physical.vk:
            self._send(physical.vk, 0, flags)
        else:
            raise ValueError("У клавиши отсутствуют scan-код и virtual-key")

    def press(self, key: Any) -> None:
        self._send_key(key, True)

    def release(self, key: Any) -> None:
        self._send_key(key, False)

    def type(self, text: str) -> None:
        """Send Unicode text; physical game controls should use press/hotkey."""

        encoded = text.encode("utf-16-le")
        for offset in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[offset : offset + 2], "little")
            self._send(0, unit, KEYEVENTF_UNICODE)
            self._send(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)


class WindowsMouseController:
    """Mouse sender using real SendInput events for game UI compatibility."""

    _BUTTON_FLAGS = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }

    def __init__(self, user32: Any | None = None) -> None:
        owns_user32 = user32 is None
        if owns_user32:
            if not WINDOWS_NATIVE_AVAILABLE:
                raise RuntimeError("Windows SendInput доступен только в Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._send_input = _bind_private_send_input(user32)
            user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
            user32.GetCursorPos.restype = wintypes.BOOL
            user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
            user32.GetSystemMetrics.restype = ctypes.c_int
        else:
            self._send_input = user32.SendInput
        self.user32 = user32
        self._get_cursor_pos = user32.GetCursorPos
        self._get_system_metrics = user32.GetSystemMetrics

    def _send(self, dx: int, dy: int, mouse_data: int, flags: int) -> None:
        payload = INPUT(
            type=INPUT_MOUSE,
            value=INPUTUNION(
                mi=MOUSEINPUT(
                    dx=int(dx),
                    dy=int(dy),
                    mouseData=ctypes.c_uint32(int(mouse_data)).value,
                    dwFlags=int(flags),
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        payload_pointer = ctypes.pointer(payload)
        sent = int(self._send_input(1, payload_pointer, ctypes.sizeof(INPUT)))
        if sent != 1:
            error_code = int(getattr(ctypes, "get_last_error", lambda: 0)())
            raise OSError(error_code, "Windows SendInput не принял событие мыши")

    @property
    def position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not self._get_cursor_pos(ctypes.pointer(point)):
            error_code = int(getattr(ctypes, "get_last_error", lambda: 0)())
            raise OSError(error_code, "Windows не вернул позицию курсора")
        return int(point.x), int(point.y)

    @position.setter
    def position(self, value: tuple[int, int]) -> None:
        x, y = int(value[0]), int(value[1])
        left = int(self._get_system_metrics(SM_XVIRTUALSCREEN))
        top = int(self._get_system_metrics(SM_YVIRTUALSCREEN))
        width = int(self._get_system_metrics(SM_CXVIRTUALSCREEN))
        height = int(self._get_system_metrics(SM_CYVIRTUALSCREEN))
        if width <= 1 or height <= 1:
            raise RuntimeError("Windows вернул неверный размер рабочего стола")

        x = min(max(x, left), left + width - 1)
        y = min(max(y, top), top + height - 1)
        normalized_x = round((x - left) * 65535 / (width - 1))
        normalized_y = round((y - top) * 65535 / (height - 1))
        self._send(
            normalized_x,
            normalized_y,
            0,
            MOUSEEVENTF_MOVE
            | MOUSEEVENTF_MOVE_NOCOALESCE
            | MOUSEEVENTF_ABSOLUTE
            | MOUSEEVENTF_VIRTUALDESK,
        )

    def move(self, dx: int, dy: int) -> None:
        self._send(
            int(dx),
            int(dy),
            0,
            MOUSEEVENTF_MOVE | MOUSEEVENTF_MOVE_NOCOALESCE,
        )

    @staticmethod
    def _button_name(button: Any) -> str:
        name = getattr(button, "name", None)
        if name is None:
            name = str(button).lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1]
        if name not in WindowsMouseController._BUTTON_FLAGS:
            raise ValueError(f"Неизвестная кнопка мыши: {button!r}")
        return name

    def press(self, button: Any) -> None:
        down_flag, _up_flag = self._BUTTON_FLAGS[self._button_name(button)]
        self._send(0, 0, 0, down_flag)

    def release(self, button: Any) -> None:
        _down_flag, up_flag = self._BUTTON_FLAGS[self._button_name(button)]
        self._send(0, 0, 0, up_flag)

    def scroll(self, dx: int, dy: int) -> None:
        if dx:
            self._send(0, 0, int(dx) * WHEEL_DELTA, MOUSEEVENTF_HWHEEL)
        if dy:
            self._send(0, 0, int(dy) * WHEEL_DELTA, MOUSEEVENTF_WHEEL)
