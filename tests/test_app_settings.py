import json
import tempfile
import unittest
from pathlib import Path

from app_settings import (
    HotkeySettings,
    hotkey_vk,
    load_hotkey_settings,
    normalize_hotkey_name,
    save_hotkey_settings,
)


class HotkeySettingsTests(unittest.TestCase):
    def test_normalizes_names_pynput_like_keys_and_vks(self) -> None:
        class Key:
            name = "f7"

        self.assertEqual(normalize_hotkey_name("f12"), "F12")
        self.assertEqual(normalize_hotkey_name("Key.f9"), "F9")
        self.assertEqual(normalize_hotkey_name(Key()), "F7")
        self.assertEqual(normalize_hotkey_name(0x77), "F8")
        self.assertEqual(hotkey_vk("F20"), 0x83)
        self.assertIsNone(normalize_hotkey_name("space"))

    def test_requires_distinct_function_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "разные"):
            HotkeySettings(play="F8", record="F8")
        with self.assertRaisesRegex(ValueError, "F1 до F20"):
            HotkeySettings(stop="space")

    def test_round_trip_and_corrupt_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            expected = HotkeySettings(
                play="F6",
                record="F7",
                finish_recording="F11",
                stop="F12",
            )
            save_hotkey_settings(expected, path)
            self.assertEqual(load_hotkey_settings(path), expected)

            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["version"], 1)
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_hotkey_settings(path), HotkeySettings())


if __name__ == "__main__":
    unittest.main()
