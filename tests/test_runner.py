import threading
import time
import types
import unittest

import main
from macro_core import parse_script
from windows_input import ScanKey, WM_KEYDOWN, WM_KEYUP


class FakeMouseController:
    def __init__(self) -> None:
        self._position = (0, 0)
        self.log = []

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = tuple(value)
        self.log.append(("move", tuple(value)))

    def press(self, button):
        self.log.append(("press", button))

    def release(self, button):
        self.log.append(("release", button))

    def scroll(self, dx, dy):
        self.log.append(("scroll", dx, dy))


class FakeKeyboardController:
    def __init__(self) -> None:
        self.log = []

    def press(self, key):
        self.log.append(("press", key))

    def release(self, key):
        self.log.append(("release", key))

    def type(self, text):
        self.log.append(("type", text))


class FakeKey:
    enter = "key:enter"
    ctrl = "key:ctrl"
    shift = "key:shift"
    space = "key:space"
    f10 = "key:f10"
    f12 = "key:f12"


class FakeKeyCode:
    @staticmethod
    def from_char(value):
        return f"char:{value}"

    @staticmethod
    def from_vk(value):
        return f"vk:{value}"


class FakeCharacterKey:
    def __init__(self, char):
        self.char = char
        self.vk = ord(char)


class FakeRoot:
    def __init__(self):
        self.window_state = "normal"
        self.calls = []
        self.scheduled = {}

    def update_idletasks(self):
        self.calls.append("update_idletasks")

    def iconify(self):
        self.window_state = "iconic"
        self.calls.append("iconify")

    def after(self, delay, callback):
        job = f"job-{len(self.scheduled) + 1}"
        self.scheduled[job] = (delay, callback)
        return job

    def after_cancel(self, job):
        self.calls.append(("after_cancel", job))
        self.scheduled.pop(job, None)

    def state(self, value=None):
        if value is not None:
            self.window_state = value
            self.calls.append(("state", value))
        return self.window_state

    def deiconify(self):
        self.window_state = "normal"
        self.calls.append("deiconify")

    def lift(self):
        self.calls.append("lift")


class AutomationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_mouse = main.mouse
        self.original_keyboard = main.keyboard
        self.mouse_controller = FakeMouseController()
        self.keyboard_controller = FakeKeyboardController()
        main.mouse = types.SimpleNamespace(
            Button=types.SimpleNamespace(left="mouse:left", right="mouse:right", middle="mouse:middle"),
            Controller=lambda: self.mouse_controller,
        )
        main.keyboard = types.SimpleNamespace(
            Key=FakeKey,
            KeyCode=FakeKeyCode,
            Controller=lambda: self.keyboard_controller,
        )

    def tearDown(self) -> None:
        main.mouse = self.original_mouse
        main.keyboard = self.original_keyboard

    def test_executes_script_commands(self) -> None:
        finished = []
        runner = main.AutomationRunner(
            speed=10,
            on_progress=lambda _text: None,
            on_finished=lambda stopped, error: finished.append((stopped, error)),
        )
        program = parse_script(
            '''
            MOVE 10 20
            CLICK right 2 0
            SCROLL 1 -2
            PRESS enter
            HOTKEY ctrl s
            TYPE "ab" 0
            '''
        )
        runner.run_script(program.nodes)

        self.assertEqual(finished, [(False, None)])
        self.assertIn(("move", (10, 20)), self.mouse_controller.log)
        self.assertEqual(self.mouse_controller.log.count(("press", "mouse:right")), 2)
        self.assertIn(("scroll", 1, -2), self.mouse_controller.log)
        self.assertIn(("press", "key:enter"), self.keyboard_controller.log)
        self.assertIn(("press", "key:ctrl"), self.keyboard_controller.log)
        self.assertIn(("press", "char:s"), self.keyboard_controller.log)
        self.assertEqual(self.keyboard_controller.log.count(("type", "a")), 1)
        self.assertEqual(self.keyboard_controller.log.count(("type", "b")), 1)

    def test_stop_releases_held_inputs(self) -> None:
        entered_wait = threading.Event()
        finished = []

        def progress(text):
            if "WAIT" in text:
                entered_wait.set()

        runner = main.AutomationRunner(
            speed=1,
            on_progress=progress,
            on_finished=lambda stopped, error: finished.append((stopped, error)),
        )
        program = parse_script("DOWN left\nKEY_DOWN ctrl\nWAIT 100")
        worker = threading.Thread(target=runner.run_script, args=(program.nodes,))
        worker.start()
        self.assertTrue(entered_wait.wait(1), "runner did not reach WAIT")
        runner.stop()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(finished, [(True, None)])
        self.assertIn(("release", "mouse:left"), self.mouse_controller.log)
        self.assertIn(("release", "key:ctrl"), self.keyboard_controller.log)

    def test_recorder_accepts_pynput_18_injected_argument(self) -> None:
        stops = []
        errors = []
        recorder = main.EventRecorder(
            record_moves=True,
            request_stop=stops.append,
            report_error=errors.append,
        )
        recorder.active = True
        recorder.started_at = time.perf_counter()

        recorder._guard(recorder._on_move)(10, 20, False)
        recorder._guard(recorder._on_click)(
            10,
            20,
            types.SimpleNamespace(name="left"),
            True,
            False,
        )
        recorder._guard(recorder._on_scroll)(10, 20, 0, -1, False)
        recorder._guard(recorder._on_key_down)(FakeCharacterKey("a"), False)
        recorder._guard(recorder._on_key_up)(FakeCharacterKey("a"), False)

        self.assertEqual(errors, [])
        self.assertEqual(stops, [])
        self.assertEqual(
            [event["type"] for event in recorder.snapshot()],
            ["mouse_move", "mouse_button", "mouse_scroll", "key_down", "key_up"],
        )

    def test_recorder_still_accepts_pynput_17_arguments(self) -> None:
        recorder = main.EventRecorder(True, lambda _reason: None, lambda _error: None)
        recorder.active = True
        recorder.started_at = time.perf_counter()
        recorder._guard(recorder._on_click)(
            1,
            2,
            types.SimpleNamespace(name="right"),
            False,
        )
        self.assertEqual(recorder.snapshot()[0]["type"], "mouse_button")

    def test_records_left_and_right_drag_without_general_mouse_moves(self) -> None:
        for button_name in ("left", "right"):
            with self.subTest(button=button_name):
                recorder = main.EventRecorder(False, lambda _reason: None, lambda _error: None)
                recorder.active = True
                recorder.started_at = time.perf_counter()
                button = types.SimpleNamespace(name=button_name)

                recorder._on_move(1, 1, False)
                recorder._on_click(10, 10, button, True, False)
                recorder._on_move(20, 25, False)
                recorder._on_click(30, 35, button, False, False)
                recorder._on_move(40, 45, False)

                events = recorder.snapshot()
                self.assertEqual(
                    [event["type"] for event in events],
                    ["mouse_button", "mouse_move", "mouse_button"],
                )
                self.assertTrue(events[0]["pressed"])
                self.assertEqual((events[1]["x"], events[1]["y"]), (20, 25))
                self.assertFalse(events[2]["pressed"])
                self.assertEqual(recorder.held_mouse_buttons, set())

    def test_replays_recorded_drag_in_order(self) -> None:
        finished = []
        runner = main.AutomationRunner(
            speed=100,
            on_progress=lambda _text: None,
            on_finished=lambda stopped, error: finished.append((stopped, error)),
        )
        runner.run_recording(
            [
                {"t": 0.0, "type": "mouse_button", "x": 10, "y": 10, "button": "left", "pressed": True},
                {"t": 0.1, "type": "mouse_move", "x": 20, "y": 25},
                {"t": 0.2, "type": "mouse_button", "x": 30, "y": 35, "button": "left", "pressed": False},
            ],
            repeats=1,
        )
        self.assertEqual(finished, [(False, None)])
        self.assertEqual(
            self.mouse_controller.log,
            [
                ("move", (10, 10)),
                ("press", "mouse:left"),
                ("move", (20, 25)),
                ("move", (30, 35)),
                ("release", "mouse:left"),
            ],
        )

    def test_windows_playback_selects_native_mouse_controller(self) -> None:
        original_available = main.WINDOWS_NATIVE_AVAILABLE
        original_native_mouse = main.WindowsMouseController
        original_native_keyboard = main.WindowsKeyboardController
        try:
            main.WINDOWS_NATIVE_AVAILABLE = True
            main.WindowsMouseController = lambda: self.mouse_controller
            main.WindowsKeyboardController = lambda: self.keyboard_controller
            finished = []
            runner = main.AutomationRunner(
                speed=100,
                on_progress=lambda _text: None,
                on_finished=lambda stopped, error: finished.append((stopped, error)),
            )
            runner.run_recording(
                [
                    {"t": 0, "type": "mouse_button", "x": 10, "y": 10, "button": "left", "pressed": True},
                    {"t": 0.01, "type": "mouse_move", "x": 40, "y": 50},
                    {"t": 0.02, "type": "mouse_button", "x": 40, "y": 50, "button": "left", "pressed": False},
                ],
                repeats=1,
            )
            self.assertEqual(finished, [(False, None)])
            self.assertEqual(
                self.mouse_controller.log,
                [
                    ("move", (10, 10)),
                    ("press", "mouse:left"),
                    ("move", (40, 50)),
                    ("move", (40, 50)),
                    ("release", "mouse:left"),
                ],
            )
        finally:
            main.WINDOWS_NATIVE_AVAILABLE = original_available
            main.WindowsMouseController = original_native_mouse
            main.WindowsKeyboardController = original_native_keyboard

    def test_action_window_is_minimized_and_restored(self) -> None:
        self.assertTrue(main.DEFAULT_MINIMIZE_ACTION_WINDOW)
        app = object.__new__(main.MacroPilotApp)
        app.root = FakeRoot()
        app.mode = "recording"
        app.window_was_minimized = False
        app.minimize_job = None

        app._minimize_for_action()
        self.assertEqual(app.root.window_state, "iconic")
        self.assertTrue(app.window_was_minimized)
        self.assertIsNotNone(app.minimize_job)

        app._restore_window()
        self.assertEqual(app.root.window_state, "normal")
        self.assertFalse(app.window_was_minimized)
        self.assertIn("deiconify", app.root.calls)
        self.assertIn("lift", app.root.calls)

    def test_windows_hook_records_physical_scan_codes(self) -> None:
        stops = []
        recorder = main.EventRecorder(False, stops.append, lambda _error: None)
        recorder.active = True
        recorder.started_at = time.perf_counter()
        key_data = types.SimpleNamespace(vkCode=0x57, scanCode=0x11, flags=0)

        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYDOWN, key_data))
        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYDOWN, key_data))
        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYDOWN, key_data))
        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYUP, key_data))
        events = recorder.snapshot()
        self.assertEqual([event["type"] for event in events], ["key_down", "key_up"])
        self.assertEqual(
            events[0]["key"],
            {"kind": "scan", "value": 0x11, "vk": 0x57, "extended": False},
        )

        f10_data = types.SimpleNamespace(vkCode=main.VK_F10, scanCode=0x44, flags=0)
        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYDOWN, f10_data))
        self.assertEqual(stops, ["Запись остановлена горячей клавишей"])
        self.assertEqual(len(recorder.snapshot()), 2)

    def test_recorder_waits_for_hooks_and_closes_preheld_key_on_stop(self) -> None:
        order = []

        class FakeListener:
            def __init__(self, name, **callbacks):
                self.name = name
                self.callbacks = callbacks

            def start(self):
                order.append(f"{self.name}:start")

            def wait(self):
                order.append(f"{self.name}:wait")

            def stop(self):
                order.append(f"{self.name}:stop")

        original_available = main.WINDOWS_NATIVE_AVAILABLE
        original_pressed_keys = main.get_pressed_scan_keys
        try:
            main.WINDOWS_NATIVE_AVAILABLE = True
            main.get_pressed_scan_keys = lambda: [ScanKey(0x20, vk=0x44)]
            main.mouse = types.SimpleNamespace(
                Listener=lambda **callbacks: FakeListener("mouse", **callbacks)
            )
            main.keyboard = types.SimpleNamespace(
                Listener=lambda **callbacks: FakeListener("keyboard", **callbacks)
            )
            recorder = main.EventRecorder(False, lambda _reason: None, lambda _error: None)

            recorder.start()
            self.assertEqual(
                order,
                ["mouse:start", "keyboard:start", "mouse:wait", "keyboard:wait"],
            )
            self.assertEqual([event["type"] for event in recorder.snapshot()], ["key_down"])
            events = recorder.stop()
            self.assertEqual([event["type"] for event in events], ["key_down", "key_up"])
        finally:
            main.WINDOWS_NATIVE_AVAILABLE = original_available
            main.get_pressed_scan_keys = original_pressed_keys

    def test_recording_button_starts_without_countdown(self) -> None:
        calls = []
        app = object.__new__(main.MacroPilotApp)
        app._begin_recording = lambda: calls.append("recording")
        app.start_recording()
        self.assertEqual(calls, ["recording"])

    def test_infinite_recording_repeats_until_stop(self) -> None:
        second_click = threading.Event()
        press_count = 0
        original_press = self.mouse_controller.press

        def counted_press(button):
            nonlocal press_count
            original_press(button)
            press_count += 1
            if press_count >= 2:
                second_click.set()

        self.mouse_controller.press = counted_press
        finished = []
        runner = main.AutomationRunner(
            speed=100,
            on_progress=lambda _text: None,
            on_finished=lambda stopped, error: finished.append((stopped, error)),
        )
        events = [
            {"t": 0.0, "type": "mouse_button", "x": 10, "y": 10, "button": "left", "pressed": True},
            {"t": 0.01, "type": "mouse_button", "x": 10, "y": 10, "button": "left", "pressed": False},
        ]
        worker = threading.Thread(target=runner.run_recording, args=(events, None))
        worker.start()
        self.assertTrue(second_click.wait(1), "infinite repeat did not start a second cycle")
        runner.stop()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(press_count, 2)
        self.assertEqual(finished, [(True, None)])


if __name__ == "__main__":
    unittest.main()
