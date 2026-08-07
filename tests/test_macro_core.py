import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from macro_core import (
    MACRO_FORMAT,
    MacroFormatError,
    RepeatBlock,
    ScriptCommand,
    ScriptError,
    compact_repeated_key_events,
    events_to_script,
    load_macro,
    macro_duration,
    parse_script,
    save_macro,
    validate_events,
)


class ScriptParserTests(unittest.TestCase):
    def test_parses_commands_and_nested_repeat(self) -> None:
        program = parse_script(
            '''
            # comment
            MOVE 100 200 0.5
            TYPE "Hello world" 0.02
            REPEAT 2
                CLICK left 2 0.1
                REPEAT 3
                    PRESS enter
                END
            END
            '''
        )
        self.assertEqual(program.estimated_steps, 10)
        self.assertIsInstance(program.nodes[0], ScriptCommand)
        self.assertIsInstance(program.nodes[2], RepeatBlock)

    def test_supports_decimal_comma(self) -> None:
        program = parse_script("WAIT 0,25")
        command = program.nodes[0]
        self.assertIsInstance(command, ScriptCommand)
        self.assertEqual(command.args, (0.25,))

    def test_click_defaults(self) -> None:
        command = parse_script("CLICK").nodes[0]
        self.assertIsInstance(command, ScriptCommand)
        self.assertEqual(command.args, ("left", 1, 0.1))

    def test_reports_source_line(self) -> None:
        with self.assertRaises(ScriptError) as caught:
            parse_script("WAIT 1\nCLICK fourth")
        self.assertEqual(caught.exception.line_no, 2)
        self.assertIn("кнопка мыши", str(caught.exception))

    def test_rejects_unclosed_repeat(self) -> None:
        with self.assertRaisesRegex(ScriptError, "отсутствует END"):
            parse_script("REPEAT 2\nCLICK")

    def test_rejects_empty_repeat(self) -> None:
        with self.assertRaisesRegex(ScriptError, "не может быть пустым"):
            parse_script("REPEAT 2\nEND")

    def test_rejects_unknown_or_python_code(self) -> None:
        with self.assertRaisesRegex(ScriptError, "неизвестная команда"):
            parse_script("__import__('os').system('echo no')")


class MacroFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = [
            {
                "t": 0.4,
                "type": "mouse_button",
                "x": 400,
                "y": 300,
                "button": "left",
                "pressed": False,
            },
            {
                "t": 0.1,
                "type": "mouse_button",
                "x": 400,
                "y": 300,
                "button": "left",
                "pressed": True,
            },
            {
                "t": 0.5,
                "type": "key_down",
                "key": {"kind": "char", "value": "a"},
            },
            {
                "t": 0.6,
                "type": "key_up",
                "key": {"kind": "char", "value": "a"},
            },
        ]

    def test_round_trip_and_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.macro.json"
            save_macro(path, self.events)
            loaded = load_macro(path)
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["format"], MACRO_FORMAT)
        self.assertEqual([item["t"] for item in loaded], [0.1, 0.4, 0.5, 0.6])
        self.assertEqual(macro_duration(loaded), 0.6)

    def test_save_uses_compact_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compact.macro.json"
            save_macro(path, self.events)
            text = path.read_text(encoding="utf-8")

        self.assertEqual(text.count("\n"), 1)
        self.assertNotIn("\n ", text)
        self.assertEqual(json.loads(text)["format"], MACRO_FORMAT)

    def test_load_keeps_legacy_pretty_json_compatible(self) -> None:
        document = {
            "format": MACRO_FORMAT,
            "version": 1,
            "created_at": "2026-08-07T00:00:00+00:00",
            "events": self.events,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.macro.json"
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            loaded = load_macro(path)

        self.assertEqual([item["t"] for item in loaded], [0.1, 0.4, 0.5, 0.6])

    def test_oversized_save_keeps_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.macro.json"
            path.write_text("original", encoding="utf-8")
            with patch("macro_core.MAX_MACRO_BYTES", 64):
                with self.assertRaisesRegex(MacroFormatError, "безопасного лимита"):
                    save_macro(path, self.events)

            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_conversion_creates_valid_script(self) -> None:
        text = events_to_script(self.events)
        program = parse_script(text)
        self.assertGreater(program.estimated_steps, 4)
        self.assertIn("DOWN left", text)
        self.assertIn('KEY_DOWN "a"', text)

    def test_rejects_unknown_event(self) -> None:
        with self.assertRaisesRegex(MacroFormatError, "неизвестный тип"):
            validate_events([{"t": 0, "type": "launch_missile"}])

    def test_rejects_bad_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.json"
            path.write_text('{"format": "other", "version": 1, "events": []}', encoding="utf-8")
            with self.assertRaisesRegex(MacroFormatError, "не файл макроса"):
                load_macro(path)

    def test_windows_scan_codes_survive_validation_and_script_conversion(self) -> None:
        events = [
            {
                "t": 0.1,
                "type": "key_down",
                "key": {"kind": "scan", "value": 0x11, "vk": 0x57, "extended": False},
            },
            {
                "t": 0.2,
                "type": "key_up",
                "key": {"kind": "scan", "value": 0x4D, "vk": 0x27, "extended": True},
            },
        ]
        normalized = validate_events(events)
        self.assertEqual(normalized, events)
        script = events_to_script(events)
        self.assertIn("KEY_DOWN scan:11", script)
        self.assertIn("KEY_UP scan:e0-4d", script)
        self.assertEqual(parse_script(script).estimated_steps, 4)

    def test_relative_mouse_drag_survives_validation_and_becomes_move_by(self) -> None:
        events = [
            {
                "t": 0.0,
                "type": "mouse_button",
                "x": 500,
                "y": 400,
                "button": "right",
                "pressed": True,
                "relative": True,
            },
            {"t": 0.1, "type": "mouse_move_relative", "dx": 12, "dy": -7},
            {
                "t": 0.2,
                "type": "mouse_button",
                "x": 500,
                "y": 400,
                "button": "right",
                "pressed": False,
                "relative": True,
            },
        ]

        normalized = validate_events(events)
        script = events_to_script(normalized)

        self.assertTrue(normalized[0]["relative"])
        self.assertEqual((normalized[1]["dx"], normalized[1]["dy"]), (12, -7))
        self.assertEqual(script.count("MOVE 500 400"), 1)
        self.assertIn("DOWN right", script)
        self.assertIn("MOVE_BY 12 -7", script)
        self.assertIn("UP right", script)

    def test_keyboard_autorepeat_is_compacted_without_shortening_hold(self) -> None:
        key = {"kind": "scan", "value": 0x20, "vk": 0x44, "extended": False}
        events = [
            {"t": 2.0371, "type": "key_down", "key": key},
            {"t": 2.0699, "type": "key_down", "key": key},
            {"t": 2.1038, "type": "key_down", "key": key},
            {"t": 2.1303, "type": "key_up", "key": key},
        ]

        compacted = compact_repeated_key_events(events)
        self.assertEqual([event["type"] for event in compacted], ["key_down", "key_up"])
        self.assertEqual([event["t"] for event in compacted], [2.0371, 2.1303])
        script = events_to_script(events)
        self.assertEqual(script.count("KEY_DOWN scan:20"), 1)
        self.assertIn("WAIT 0.0932\nKEY_UP scan:20", script)


if __name__ == "__main__":
    unittest.main()
