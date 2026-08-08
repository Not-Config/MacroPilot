import unittest

from macro_core import IfBlock, ScriptCommand, parse_script, script_nodes_to_text
from visual_script import action_values, build_action, build_condition, describe_node


class VisualScriptModelTests(unittest.TestCase):
    def test_builds_every_action_from_defaults(self) -> None:
        defaults = {
            "WAIT": {"seconds": "0.5"},
            "MOVE": {"x": "100", "y": "200", "duration": "0.2"},
            "MOVE_BY": {"x": "5", "y": "-3", "duration": "0"},
            "CLICK": {"button": "left", "count": "2", "interval": "0.1"},
            "CLICK_AT": {"x": "10", "y": "20", "button": "right", "count": "1", "interval": "0.1"},
            "WAIT_IMAGE": {"path": "ready image.png", "timeout": "30", "confidence": "0.9"},
            "CLICK_IMAGE": {"path": "start.png", "button": "left", "timeout": "10", "confidence": "0.85"},
            "OCR_TEXT": {"variable": "state", "x": "0", "y": "0", "width": "100", "height": "40", "language": "auto"},
            "OCR_NUMBER": {"variable": "score", "x": "0", "y": "0", "width": "100", "height": "40", "language": "ru-RU"},
            "DOWN": {"button": "left"},
            "UP": {"button": "left"},
            "SCROLL": {"x": "0", "y": "-2"},
            "PRESS": {"key": "enter"},
            "KEY_DOWN": {"key": "scan:11"},
            "KEY_UP": {"key": "scan:11"},
            "HOTKEY": {"keys": "ctrl+shift+a"},
            "TYPE": {"text": "Привет мир", "interval": "0.03"},
        }

        for name, values in defaults.items():
            with self.subTest(name=name):
                command = build_action(name, values)
                self.assertIsInstance(command, ScriptCommand)
                self.assertEqual(command.name, name)
                self.assertEqual(parse_script(script_nodes_to_text((command,))).nodes[0].name, name)

    def test_action_values_can_be_edited_and_rebuilt(self) -> None:
        original = parse_script('CLICK_AT 12 34 right 3 0.25').nodes[0]
        self.assertIsInstance(original, ScriptCommand)
        assert isinstance(original, ScriptCommand)
        rebuilt = build_action(original.name, action_values(original))
        self.assertEqual(rebuilt.args, original.args)

    def test_builds_text_and_number_conditions(self) -> None:
        text = build_condition("text", "state", "CONTAINS", "ready now")
        number = build_condition("number", "health", "<", "30")
        self.assertIsInstance(text, IfBlock)
        self.assertEqual(text.expected, "ready now")
        self.assertEqual(number.expected, 30.0)
        self.assertEqual(len(text.true_body), 1)

    def test_describes_action_without_losing_parameters(self) -> None:
        command = build_action("TYPE", {"text": "hello world", "interval": "0.04"})
        title, details = describe_node(command)
        self.assertIn("Напечатать", title)
        self.assertIn("hello world", details)


if __name__ == "__main__":
    unittest.main()
