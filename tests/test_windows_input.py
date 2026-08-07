import ctypes
import types
import unittest
from ctypes import wintypes

from windows_input import (
    INPUT,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MAPVK_VK_TO_VSC_EX,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_MOVE_NOCOALESCE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    ScanKey,
    WindowsKeyboardController,
    WindowsMouseController,
    get_pressed_scan_keys,
    parse_scan_token,
    scan_token,
)


class FakeUser32:
    def __init__(self):
        self.sent = []
        self.held = set()
        self.cursor = (120, 240)
        self.metrics = {
            76: -1920,
            77: 0,
            78: 3840,
            79: 1080,
        }

    def SendInput(self, count, pointer, size):
        if not isinstance(pointer, ctypes.POINTER(INPUT)):
            raise TypeError("expected LP_INPUT instance")
        self.assertions = (count, size)
        payload = ctypes.cast(pointer, ctypes.POINTER(INPUT)).contents
        if int(payload.type) == INPUT_MOUSE:
            self.sent.append(
                (
                    "mouse",
                    int(payload.mi.dx),
                    int(payload.mi.dy),
                    int(payload.mi.mouseData),
                    int(payload.mi.dwFlags),
                )
            )
        else:
            self.sent.append((int(payload.ki.wVk), int(payload.ki.wScan), int(payload.ki.dwFlags)))
        return 1

    def MapVirtualKeyW(self, vk, mode):
        if mode != MAPVK_VK_TO_VSC_EX:
            return 0
        return {
            0x41: 0x001E,
            0x44: 0x0020,
            0x57: 0x0011,
            0x27: 0xE04D,
        }.get(int(vk), 0)

    def VkKeyScanW(self, char):
        return {"a": 0x41, "w": 0x57}.get(char, -1)

    def GetAsyncKeyState(self, vk):
        return 0x8000 if int(vk) in self.held else 0

    def GetCursorPos(self, pointer):
        if not isinstance(pointer, ctypes.POINTER(wintypes.POINT)):
            raise TypeError("expected LP_POINT instance")
        pointer.contents.x, pointer.contents.y = self.cursor
        return 1

    def GetSystemMetrics(self, index):
        return self.metrics[int(index)]


class CharacterKey:
    vk = None
    char = "w"


class WindowsInputTests(unittest.TestCase):
    def test_scan_token_round_trip(self):
        self.assertEqual(parse_scan_token("scan:11"), ScanKey(0x11, extended=False))
        self.assertEqual(parse_scan_token("scan:e0-4d"), ScanKey(0x4D, extended=True))
        self.assertEqual(scan_token(ScanKey(0x4D, extended=True)), "scan:e0-4d")

    def test_sendinput_uses_scan_and_keyup_flags(self):
        user32 = FakeUser32()
        controller = WindowsKeyboardController(user32=user32)
        key = ScanKey(scan_code=0x11, vk=0x57, extended=False)
        controller.press(key)
        controller.release(key)
        self.assertEqual(
            user32.sent,
            [
                (0, 0x11, KEYEVENTF_SCANCODE),
                (0, 0x11, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP),
            ],
        )

    def test_sendinput_preserves_extended_key(self):
        user32 = FakeUser32()
        controller = WindowsKeyboardController(user32=user32)
        controller.press(ScanKey(scan_code=0x4D, vk=0x27, extended=True))
        self.assertEqual(
            user32.sent[0],
            (0, 0x4D, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY),
        )

    def test_character_key_is_mapped_to_physical_scan_code(self):
        user32 = FakeUser32()
        controller = WindowsKeyboardController(user32=user32)
        controller.press(CharacterKey())
        self.assertEqual(user32.sent[0], (0, 0x11, KEYEVENTF_SCANCODE))

    def test_finds_key_already_held_when_recording_starts(self):
        user32 = FakeUser32()
        user32.held = {0x44, 0x10}
        self.assertEqual(
            get_pressed_scan_keys(user32),
            [ScanKey(scan_code=0x20, vk=0x44, extended=False)],
        )

    def test_native_mouse_drag_uses_sendinput_move_events(self):
        user32 = FakeUser32()
        controller = WindowsMouseController(user32=user32)
        left = types.SimpleNamespace(name="left")

        self.assertEqual(controller.position, (120, 240))
        controller.position = (-1920, 0)
        controller.press(left)
        controller.position = (1919, 1079)
        controller.release(left)

        move_flags = (
            MOUSEEVENTF_MOVE
            | MOUSEEVENTF_MOVE_NOCOALESCE
            | MOUSEEVENTF_ABSOLUTE
            | MOUSEEVENTF_VIRTUALDESK
        )
        self.assertEqual(
            user32.sent,
            [
                ("mouse", 0, 0, 0, move_flags),
                ("mouse", 0, 0, 0, MOUSEEVENTF_LEFTDOWN),
                ("mouse", 65535, 65535, 0, move_flags),
                ("mouse", 0, 0, 0, MOUSEEVENTF_LEFTUP),
            ],
        )

    def test_native_mouse_sends_relative_move_and_both_wheels(self):
        user32 = FakeUser32()
        controller = WindowsMouseController(user32=user32)
        controller.move(-7, 9)
        controller.scroll(2, 1)

        self.assertEqual(
            user32.sent,
            [
                ("mouse", -7, 9, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_MOVE_NOCOALESCE),
                ("mouse", 0, 0, 240, MOUSEEVENTF_HWHEEL),
                ("mouse", 0, 0, 120, MOUSEEVENTF_WHEEL),
            ],
        )

    def test_native_mouse_supports_right_button_hold(self):
        user32 = FakeUser32()
        controller = WindowsMouseController(user32=user32)
        right = types.SimpleNamespace(name="right")
        controller.press(right)
        controller.position = (1919, 1079)
        controller.release(right)

        self.assertEqual(user32.sent[0][-1], MOUSEEVENTF_RIGHTDOWN)
        self.assertTrue(user32.sent[1][-1] & MOUSEEVENTF_MOVE)
        self.assertEqual(user32.sent[2][-1], MOUSEEVENTF_RIGHTUP)


if __name__ == "__main__":
    unittest.main()
