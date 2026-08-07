from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable


WINDOWS_NATIVE_AVAILABLE = sys.platform == "win32"

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
KEYDOWN_MESSAGES = {WM_KEYDOWN, WM_SYSKEYDOWN}
KEYUP_MESSAGES = {WM_KEYUP, WM_SYSKEYUP}

VK_F9 = 0x78
VK_F10 = 0x79
VK_F12 = 0x7B
LLKHF_EXTENDED = 0x01
WH_MOUSE_LL = 14
PM_NOREMOVE = 0x0000

# Every mouse event created by MacroPilot carries this marker in dwExtraInfo.
# The playback-only low-level hook lets these events through and suppresses
# unmarked events produced by the physical mouse or other applications.
MACROPILOT_MOUSE_EXTRA_INFO = 0x4D504C54  # "MPLT"

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

RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
MOUSE_MOVE_ABSOLUTE = 0x0001
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
HWND_MESSAGE = -3
UINT_ERROR = 0xFFFFFFFF

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


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = (
        ("usUsagePage", ctypes.c_uint16),
        ("usUsage", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("hwndTarget", ctypes.c_void_p),
    )


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = (
        ("dwType", ctypes.c_uint32),
        ("dwSize", ctypes.c_uint32),
        ("hDevice", ctypes.c_void_p),
        ("wParam", ctypes.c_size_t),
    )


class _RAWMOUSEBUTTONDATA(ctypes.Structure):
    _fields_ = (
        ("usButtonFlags", ctypes.c_uint16),
        ("usButtonData", ctypes.c_uint16),
    )


class _RAWMOUSEBUTTONS(ctypes.Union):
    _anonymous_ = ("data",)
    _fields_ = (
        ("ulButtons", ctypes.c_uint32),
        ("data", _RAWMOUSEBUTTONDATA),
    )


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("buttons",)
    _fields_ = (
        ("usFlags", ctypes.c_uint16),
        ("buttons", _RAWMOUSEBUTTONS),
        ("ulRawButtons", ctypes.c_uint32),
        ("lLastX", ctypes.c_int32),
        ("lLastY", ctypes.c_int32),
        ("ulExtraInformation", ctypes.c_uint32),
    )


class _RAWINPUTDATA(ctypes.Union):
    _fields_ = (("mouse", RAWMOUSE),)


class RAWINPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = (("header", RAWINPUTHEADER), ("data", _RAWINPUTDATA))


class _HOOKPOINT(ctypes.Structure):
    _fields_ = (("x", ctypes.c_int32), ("y", ctypes.c_int32))


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("pt", _HOOKPOINT),
        ("mouseData", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    )


_window_function_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _window_function_type(
    ctypes.c_ssize_t,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
)
LOW_LEVEL_MOUSE_PROC = _window_function_type(
    ctypes.c_ssize_t,
    ctypes.c_int,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", ctypes.c_uint32),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int32),
        ("cbWndExtra", ctypes.c_int32),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    )


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


class WindowsRawMouseListener:
    """Receive physical relative mouse motion through the Windows Raw Input API."""

    def __init__(
        self,
        on_move: Callable[[int, int], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.on_move = on_move
        self.on_error = on_error or (lambda _text: None)
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.startup_error: Exception | None = None
        self.hwnd: int | None = None
        self.class_name = f"MacroPilotRawMouse_{id(self):x}"
        self._wndproc: Any = None
        self._user32: Any = None
        self._kernel32: Any = None
        self._hinstance: int | None = None
        self._registered = False
        self._runtime_error_reported = False

    @staticmethod
    def _last_error(message: str) -> OSError:
        error_code = int(getattr(ctypes, "get_last_error", lambda: 0)())
        return OSError(error_code, message)

    def _bind_api(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._kernel32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
        self._kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        self._user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        self._user32.RegisterClassW.restype = ctypes.c_uint16
        self._user32.UnregisterClassW.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p)
        self._user32.UnregisterClassW.restype = ctypes.c_int
        self._user32.CreateWindowExW.argtypes = (
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._user32.CreateWindowExW.restype = ctypes.c_void_p
        self._user32.DestroyWindow.argtypes = (ctypes.c_void_p,)
        self._user32.DestroyWindow.restype = ctypes.c_int
        self._user32.DefWindowProcW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self._user32.PostMessageW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._user32.PostMessageW.restype = ctypes.c_int
        self._user32.PostQuitMessage.argtypes = (ctypes.c_int32,)
        self._user32.PostQuitMessage.restype = None
        self._user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.TranslateMessage.restype = ctypes.c_int
        self._user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self._user32.RegisterRawInputDevices.argtypes = (
            ctypes.POINTER(RAWINPUTDEVICE),
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        self._user32.RegisterRawInputDevices.restype = ctypes.c_int
        self._user32.GetRawInputData.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        )
        self._user32.GetRawInputData.restype = ctypes.c_uint32

    def _report_runtime_error(self, exc: Exception) -> None:
        if self._runtime_error_reported:
            return
        self._runtime_error_reported = True
        self.on_error(f"{exc.__class__.__name__}: {exc}")

    def _dispatch_motion(self, flags: int, dx: int, dy: int) -> None:
        # SetCursorPos-style recentering does not create relative Raw Input.
        # Absolute HID devices are ignored because their values are normalized
        # screen coordinates rather than physical mouse counts.
        if int(flags) & MOUSE_MOVE_ABSOLUTE:
            return
        dx, dy = int(dx), int(dy)
        if dx or dy:
            self.on_move(dx, dy)

    def _read_raw_input(self, raw_handle: int) -> None:
        user32 = self._user32
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        data_size = ctypes.c_uint32(0)
        result = int(
            user32.GetRawInputData(
                ctypes.c_void_p(raw_handle),
                RID_INPUT,
                None,
                ctypes.byref(data_size),
                header_size,
            )
        )
        if result != 0 or data_size.value < ctypes.sizeof(RAWINPUT):
            raise self._last_error("Windows не вернул размер Raw Input")

        buffer = ctypes.create_string_buffer(data_size.value)
        copied = int(
            user32.GetRawInputData(
                ctypes.c_void_p(raw_handle),
                RID_INPUT,
                buffer,
                ctypes.byref(data_size),
                header_size,
            )
        )
        if copied == UINT_ERROR or copied < ctypes.sizeof(RAWINPUT):
            raise self._last_error("Windows не прочитал Raw Input мыши")

        raw = ctypes.cast(buffer, ctypes.POINTER(RAWINPUT)).contents
        if int(raw.header.dwType) == RIM_TYPEMOUSE:
            self._dispatch_motion(
                int(raw.mouse.usFlags),
                int(raw.mouse.lLastX),
                int(raw.mouse.lLastY),
            )

    def _window_proc(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        if int(message) == WM_INPUT:
            try:
                self._read_raw_input(int(lparam))
            except Exception as exc:
                self._report_runtime_error(exc)
            return int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))
        if int(message) == WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
            return 0
        if int(message) == WM_DESTROY:
            self.hwnd = None
            self._user32.PostQuitMessage(0)
            return 0
        return int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _remove_registration(self) -> None:
        if not self._registered or self._user32 is None:
            return
        device = RAWINPUTDEVICE(
            HID_USAGE_PAGE_GENERIC,
            HID_USAGE_GENERIC_MOUSE,
            RIDEV_REMOVE,
            None,
        )
        self._user32.RegisterRawInputDevices(
            ctypes.byref(device),
            1,
            ctypes.sizeof(device),
        )
        self._registered = False

    def _thread_main(self) -> None:
        try:
            self._bind_api()
            self._hinstance = int(self._kernel32.GetModuleHandleW(None) or 0)
            if not self._hinstance:
                raise self._last_error("Windows не вернул модуль приложения")

            self._wndproc = WNDPROC(self._window_proc)
            window_class = WNDCLASSW(
                0,
                self._wndproc,
                0,
                0,
                ctypes.c_void_p(self._hinstance),
                None,
                None,
                None,
                None,
                self.class_name,
            )
            if not self._user32.RegisterClassW(ctypes.byref(window_class)):
                raise self._last_error("Windows не зарегистрировал окно Raw Input")

            hwnd = self._user32.CreateWindowExW(
                0,
                self.class_name,
                "MacroPilot Raw Input",
                0,
                0,
                0,
                0,
                0,
                ctypes.c_void_p(HWND_MESSAGE),
                None,
                ctypes.c_void_p(self._hinstance),
                None,
            )
            if not hwnd:
                raise self._last_error("Windows не создал окно Raw Input")
            self.hwnd = int(hwnd)

            device = RAWINPUTDEVICE(
                HID_USAGE_PAGE_GENERIC,
                HID_USAGE_GENERIC_MOUSE,
                RIDEV_INPUTSINK,
                ctypes.c_void_p(self.hwnd),
            )
            if not self._user32.RegisterRawInputDevices(
                ctypes.byref(device),
                1,
                ctypes.sizeof(device),
            ):
                raise self._last_error("Windows не включил Raw Input мыши")
            self._registered = True
            self.ready.set()

            message = wintypes.MSG()
            while True:
                result = int(self._user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    raise self._last_error("Windows прервал цикл Raw Input")
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            if not self.ready.is_set():
                self.startup_error = exc
            else:
                self._report_runtime_error(exc)
        finally:
            self._remove_registration()
            if self.hwnd and self._user32 is not None:
                self._user32.DestroyWindow(ctypes.c_void_p(self.hwnd))
                self.hwnd = None
            if self._user32 is not None and self._hinstance:
                self._user32.UnregisterClassW(
                    self.class_name,
                    ctypes.c_void_p(self._hinstance),
                )
            self.ready.set()
            self.stopped.set()

    def start(self, timeout: float = 3.0) -> None:
        if not WINDOWS_NATIVE_AVAILABLE:
            raise RuntimeError("Windows Raw Input доступен только в Windows")
        if self.thread is not None and self.thread.is_alive():
            return
        self.ready.clear()
        self.stopped.clear()
        self.startup_error = None
        self._runtime_error_reported = False
        self.thread = threading.Thread(
            target=self._thread_main,
            name="MacroPilotRawMouse",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(timeout):
            self.stop()
            raise TimeoutError("Windows Raw Input не запустился вовремя")
        if self.startup_error is not None:
            raise RuntimeError(f"Raw Input: {self.startup_error}") from self.startup_error

    def stop(self, timeout: float = 3.0) -> None:
        thread = self.thread
        if thread is None:
            return
        hwnd = self.hwnd
        if hwnd and self._user32 is not None:
            self._user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
        if thread is not threading.current_thread():
            thread.join(timeout)


class WindowsPhysicalMouseBlocker:
    """Suppress unmarked mouse input while allowing MacroPilot SendInput events."""

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self.on_error = on_error or (lambda _text: None)
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.stop_requested = threading.Event()
        self.startup_error: Exception | None = None
        self.thread_id: int | None = None
        self.hook_handle: int | None = None
        self._hook_proc: Any = None
        self._user32: Any = None
        self._kernel32: Any = None
        self._runtime_error_reported = False

    @staticmethod
    def _last_error(message: str) -> OSError:
        error_code = int(getattr(ctypes, "get_last_error", lambda: 0)())
        return OSError(error_code, message)

    @staticmethod
    def should_block(extra_info: int) -> bool:
        """Return whether a low-level event is not one of MacroPilot's events."""

        return int(extra_info) != MACROPILOT_MOUSE_EXTRA_INFO

    def _bind_api(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._kernel32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
        self._kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        self._kernel32.GetCurrentThreadId.argtypes = ()
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        self._user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int,
            LOW_LEVEL_MOUSE_PROC,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._user32.CallNextHookEx.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self._user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.TranslateMessage.restype = wintypes.BOOL
        self._user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self._user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._user32.PostThreadMessageW.restype = wintypes.BOOL

    def _call_next(self, n_code: int, wparam: int, lparam: int) -> int:
        if self._user32 is None:
            return 0
        return int(
            self._user32.CallNextHookEx(
                ctypes.c_void_p(self.hook_handle or 0),
                int(n_code),
                int(wparam),
                int(lparam),
            )
        )

    def _hook_callback(self, n_code: int, wparam: int, lparam: int) -> int:
        if int(n_code) < 0:
            return self._call_next(n_code, wparam, lparam)
        try:
            event = ctypes.cast(
                int(lparam),
                ctypes.POINTER(MSLLHOOKSTRUCT),
            ).contents
            if self.should_block(int(event.dwExtraInfo)):
                return 1
        except Exception:
            # A malformed callback must fail open so the user never loses the
            # mouse because of an unexpected structure or ctypes error.
            return self._call_next(n_code, wparam, lparam)
        return self._call_next(n_code, wparam, lparam)

    def _report_runtime_error(self, exc: Exception) -> None:
        if self._runtime_error_reported:
            return
        self._runtime_error_reported = True
        self.on_error(f"{exc.__class__.__name__}: {exc}")

    def _unhook(self) -> None:
        handle = self.hook_handle
        if handle and self._user32 is not None:
            self._user32.UnhookWindowsHookEx(ctypes.c_void_p(handle))
            self.hook_handle = None

    def _thread_main(self) -> None:
        try:
            self._bind_api()
            self.thread_id = int(self._kernel32.GetCurrentThreadId())

            # PostThreadMessage works only after the receiving thread owns a
            # message queue. PeekMessage creates it before start() returns.
            message = wintypes.MSG()
            self._user32.PeekMessageW(
                ctypes.byref(message),
                None,
                0,
                0,
                PM_NOREMOVE,
            )
            if self.stop_requested.is_set():
                return

            hinstance = self._kernel32.GetModuleHandleW(None)
            self._hook_proc = LOW_LEVEL_MOUSE_PROC(self._hook_callback)
            hook = self._user32.SetWindowsHookExW(
                WH_MOUSE_LL,
                self._hook_proc,
                hinstance,
                0,
            )
            if not hook:
                raise self._last_error("Windows не включил блокировку физической мыши")
            self.hook_handle = int(hook)
            self.ready.set()

            if self.stop_requested.is_set():
                self._user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

            while True:
                result = int(self._user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    raise self._last_error("Windows прервал блокировку физической мыши")
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            if not self.ready.is_set():
                self.startup_error = exc
            else:
                self._report_runtime_error(exc)
        finally:
            self._unhook()
            self.ready.set()
            self.stopped.set()

    def start(self, timeout: float = 3.0) -> None:
        if not WINDOWS_NATIVE_AVAILABLE:
            raise RuntimeError("Блокировка физической мыши доступна только в Windows")
        if self.thread is not None and self.thread.is_alive():
            return
        self.ready.clear()
        self.stopped.clear()
        self.stop_requested.clear()
        self.startup_error = None
        self.thread_id = None
        self.hook_handle = None
        self._runtime_error_reported = False
        self.thread = threading.Thread(
            target=self._thread_main,
            name="MacroPilotMouseBlocker",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(timeout):
            self.stop()
            raise TimeoutError("Блокировка физической мыши не запустилась вовремя")
        if self.startup_error is not None:
            self.stop()
            raise RuntimeError(
                f"Блокировка физической мыши: {self.startup_error}"
            ) from self.startup_error

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_requested.set()
        thread = self.thread
        if thread is None:
            return
        if self.thread_id and self._user32 is not None:
            self._user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if thread is not threading.current_thread():
            thread.join(timeout)
        if thread.is_alive():
            # Even if the message loop is unexpectedly stuck, remove the hook
            # from this thread so physical input is immediately restored.
            self._unhook()
            raise TimeoutError("Windows не остановил блокировку физической мыши")


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
                    dwExtraInfo=MACROPILOT_MOUSE_EXTRA_INFO,
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
