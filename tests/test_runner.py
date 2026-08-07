import threading
import time
import types
import unittest
import tempfile
from pathlib import Path
from unittest import mock

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

    def move(self, dx, dy):
        self._position = (self._position[0] + dx, self._position[1] + dy)
        self.log.append(("move_by", (dx, dy)))


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
    f9 = "key:f9"
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
        self.original_windows_native_available = main.WINDOWS_NATIVE_AVAILABLE
        self.mouse_controller = FakeMouseController()
        self.keyboard_controller = FakeKeyboardController()
        # Generic runner tests must use the injected fake controllers on every
        # operating system. Native SendInput selection has its own focused test.
        main.WINDOWS_NATIVE_AVAILABLE = False
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
        main.WINDOWS_NATIVE_AVAILABLE = self.original_windows_native_available

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

    def test_game_clicks_keep_minimum_real_hold_and_release_gap(self) -> None:
        waits = []
        runner = main.AutomationRunner(
            speed=100,
            on_progress=lambda _text: None,
            on_finished=lambda _stopped, _error: None,
        )
        runner.stop_event.wait = lambda seconds: waits.append(seconds) or False

        runner.run_script(parse_script("CLICK left 2 0").nodes)

        self.assertTrue(any(seconds >= 0.035 for seconds in waits), waits)
        self.assertTrue(any(0.015 <= seconds <= 0.025 for seconds in waits), waits)
        self.assertEqual(self.mouse_controller.log.count(("press", "mouse:left")), 2)
        self.assertEqual(self.mouse_controller.log.count(("release", "mouse:left")), 2)

    def test_script_editor_shortcuts_use_windows_physical_keycodes(self) -> None:
        generated = []
        widget = types.SimpleNamespace(event_generate=generated.append)
        app = object.__new__(main.MacroPilotApp)

        result_copy = app._on_script_editor_control(
            types.SimpleNamespace(state=main.TK_CONTROL_MASK, keycode=67, keysym="Cyrillic_es", widget=widget)
        )
        result_paste = app._on_script_editor_control(
            types.SimpleNamespace(state=main.TK_CONTROL_MASK, keycode=86, keysym="Cyrillic_em", widget=widget)
        )

        self.assertEqual(generated, ["<<Copy>>", "<<Paste>>"])
        self.assertEqual((result_copy, result_paste), ("break", "break"))

    def test_waits_for_image_and_clicks_its_center(self) -> None:
        requested = []

        class FakeMatcher:
            def __init__(self, path):
                requested.append(Path(path))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def find(self, confidence):
                requested.append(confidence)
                return main.ImageMatch(100, 200, 40, 20, 0.97)

        with tempfile.TemporaryDirectory() as directory:
            script_directory = Path(directory).resolve()
            runner = main.AutomationRunner(
                speed=1,
                on_progress=lambda _text: None,
                on_finished=lambda _stopped, _error: None,
                script_directory=script_directory,
            )
            with mock.patch("main.ScreenImageMatcher", FakeMatcher):
                runner.run_script(
                    parse_script(
                        'WAIT_IMAGE "ready.png" 2 0.9\n'
                        'CLICK_IMAGE "play.png" right 2 0.95'
                    ).nodes
                )

        self.assertIn(script_directory / "ready.png", requested)
        self.assertIn(script_directory / "play.png", requested)
        self.assertIn(("move", (120, 210)), self.mouse_controller.log)
        self.assertIn(("press", "mouse:right"), self.mouse_controller.log)
        self.assertIn(("release", "mouse:right"), self.mouse_controller.log)

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
        try:
            self.assertTrue(entered_wait.wait(1), "runner did not reach WAIT")
            runner.stop()
            worker.join(1)

            self.assertFalse(worker.is_alive())
            self.assertEqual(finished, [(True, None)])
            self.assertIn(("release", "mouse:left"), self.mouse_controller.log)
            self.assertIn(("release", "key:ctrl"), self.keyboard_controller.log)
        finally:
            runner.stop()
            worker.join(1)

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

    def test_recording_precision_changes_mouse_sampling_interval(self) -> None:
        recorder = main.EventRecorder(
            True,
            lambda _reason: None,
            lambda _error: None,
            move_interval=0.05,
        )
        recorder.active = True
        clock = [0.0]
        recorder._timestamp = lambda: clock[0]

        for timestamp in (0.0, 0.02, 0.05):
            clock[0] = timestamp
            recorder._on_move(round(timestamp * 1000), 0)

        self.assertEqual(
            [event["x"] for event in recorder.snapshot()],
            [0, 50],
        )

    def test_recorder_warns_once_and_stops_at_safe_capacity(self) -> None:
        stops = []
        warnings = []
        recorder = main.EventRecorder(
            True,
            stops.append,
            lambda _error: None,
            warnings.append,
        )
        recorder.active = True
        recorder.started_at = time.perf_counter()
        recorder.max_recorded_events = 3
        recorder.warning_event_count = 2

        for x in range(4):
            recorder._append({"type": "mouse_move", "x": x, "y": 0})

        self.assertEqual(len(recorder.snapshot()), 3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Осталось около 1", warnings[0])
        self.assertEqual(len(stops), 1)
        self.assertIn("безопасный предел 3 событий", stops[0])
        self.assertTrue(recorder.stop_requested.is_set())

    def test_continued_recorder_counts_existing_events_toward_capacity(self) -> None:
        stops = []
        warnings = []
        recorder = main.EventRecorder(
            True,
            stops.append,
            lambda _error: None,
            warnings.append,
        )
        recorder.active = True
        recorder.started_at = time.perf_counter()
        recorder.capacity_base_count = 7
        recorder.max_recorded_events = 3
        recorder.warning_event_count = 8

        for x in range(3):
            recorder._append({"type": "mouse_move", "x": x, "y": 0})

        self.assertIn("8 событий", warnings[0])
        self.assertIn("Осталось около 2", warnings[0])
        self.assertIn("безопасный предел 10 событий", stops[0])

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

    def test_raw_mouse_drag_records_relative_motion_without_recenter_jump(self) -> None:
        recorder = main.EventRecorder(True, lambda _reason: None, lambda _error: None)
        recorder.active = True
        recorder.relative_mouse_enabled = True
        clock = [0.0]
        recorder._timestamp = lambda: clock[0]
        button = types.SimpleNamespace(name="right")

        recorder._on_click(500, 400, button, True, False)
        clock[0] = 0.01
        recorder._on_raw_mouse_move(8, -3)
        # The low-level hook also sees the game's absolute recenter, but it is
        # deliberately ignored while this Raw Input drag is active.
        recorder._on_move(500, 400, False)
        clock[0] = 0.02
        recorder._on_raw_mouse_move(4, 2)
        clock[0] = 0.03
        recorder._on_click(500, 400, button, False, False)

        events = recorder.snapshot()
        self.assertEqual(
            [event["type"] for event in events],
            [
                "mouse_button",
                "mouse_move_relative",
                "mouse_move_relative",
                "mouse_button",
            ],
        )
        self.assertTrue(events[0]["relative"])
        self.assertEqual((events[1]["dx"], events[1]["dy"]), (8, -3))
        self.assertEqual((events[2]["dx"], events[2]["dy"]), (4, 2))
        self.assertEqual(events[2]["t"], 0.02)
        self.assertTrue(events[3]["relative"])
        self.assertEqual(recorder.relative_mouse_buttons, set())

    def test_raw_mouse_motion_is_ignored_until_a_button_is_held(self) -> None:
        recorder = main.EventRecorder(True, lambda _reason: None, lambda _error: None)
        recorder.active = True
        recorder.relative_mouse_enabled = True
        recorder._on_raw_mouse_move(20, 10)
        self.assertEqual(recorder.snapshot(), [])

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

    def test_replays_raw_drag_as_relative_sendinput_motion(self) -> None:
        finished = []
        runner = main.AutomationRunner(
            speed=100,
            on_progress=lambda _text: None,
            on_finished=lambda stopped, error: finished.append((stopped, error)),
        )
        runner.run_recording(
            [
                {
                    "t": 0.0,
                    "type": "mouse_button",
                    "x": 500,
                    "y": 400,
                    "button": "right",
                    "pressed": True,
                    "relative": True,
                },
                {"t": 0.1, "type": "mouse_move_relative", "dx": 15, "dy": -6},
                {
                    "t": 0.2,
                    "type": "mouse_button",
                    "x": 500,
                    "y": 400,
                    "button": "right",
                    "pressed": False,
                    "relative": True,
                },
            ],
            repeats=1,
        )
        self.assertEqual(finished, [(False, None)])
        self.assertEqual(
            self.mouse_controller.log,
            [
                ("move", (500, 400)),
                ("press", "mouse:right"),
                ("move_by", (15, -6)),
                ("release", "mouse:right"),
            ],
        )

    def test_script_move_by_uses_relative_controller_motion(self) -> None:
        finished = []
        runner = main.AutomationRunner(
            speed=1,
            on_progress=lambda _text: None,
            on_finished=lambda stopped, error: finished.append((stopped, error)),
        )

        runner.run_script(parse_script("MOVE_BY 15 -6").nodes)

        self.assertEqual(finished, [(False, None)])
        self.assertEqual(self.mouse_controller.log, [("move_by", (15, -6))])

    def test_mouse_movements_are_recorded_by_default(self) -> None:
        self.assertTrue(main.DEFAULT_RECORD_MOUSE_MOVES)

    def test_physical_mouse_blocking_is_opt_in(self) -> None:
        self.assertFalse(main.DEFAULT_BLOCK_PHYSICAL_MOUSE)

    def test_physical_mouse_blocker_wraps_playback_and_is_always_stopped(self) -> None:
        lifecycle = []
        finished = []
        mouse_controller = FakeMouseController()
        original_release = mouse_controller.release

        def tracked_release(button):
            lifecycle.append(("release", button))
            original_release(button)

        mouse_controller.release = tracked_release

        class FakeBlocker:
            def __init__(self, on_error=None):
                self.on_error = on_error

            def start(self):
                lifecycle.append("start")

            def stop(self):
                lifecycle.append("stop")

        with (
            mock.patch.object(main, "WINDOWS_NATIVE_AVAILABLE", True),
            mock.patch.object(main, "WindowsMouseController", lambda: mouse_controller),
            mock.patch.object(
                main,
                "WindowsKeyboardController",
                lambda: self.keyboard_controller,
            ),
            mock.patch.object(main, "WindowsPhysicalMouseBlocker", FakeBlocker),
        ):
            runner = main.AutomationRunner(
                speed=100,
                on_progress=lambda _text: None,
                on_finished=lambda stopped, error: finished.append((stopped, error)),
                block_physical_mouse=True,
            )
            runner.run_script(parse_script("DOWN left").nodes)

        self.assertEqual(finished, [(False, None)])
        self.assertEqual(lifecycle[0], "start")
        self.assertEqual(lifecycle[-1], "stop")
        self.assertEqual(
            lifecycle[1:4],
            [
                ("release", "mouse:left"),
                ("release", "mouse:right"),
                ("release", "mouse:middle"),
            ],
        )
        self.assertEqual(lifecycle[-2], ("release", "mouse:left"))

    def test_mouse_blocker_startup_failure_is_reported_and_cleaned_up(self) -> None:
        lifecycle = []
        finished = []

        class FailedBlocker:
            def __init__(self, on_error=None):
                self.on_error = on_error

            def start(self):
                lifecycle.append("start")
                raise RuntimeError("hook denied")

            def stop(self):
                lifecycle.append("stop")

        with (
            mock.patch.object(main, "WINDOWS_NATIVE_AVAILABLE", True),
            mock.patch.object(
                main,
                "WindowsMouseController",
                lambda: self.mouse_controller,
            ),
            mock.patch.object(
                main,
                "WindowsKeyboardController",
                lambda: self.keyboard_controller,
            ),
            mock.patch.object(main, "WindowsPhysicalMouseBlocker", FailedBlocker),
        ):
            runner = main.AutomationRunner(
                speed=1,
                on_progress=lambda _text: None,
                on_finished=lambda stopped, error: finished.append((stopped, error)),
                block_physical_mouse=True,
            )
            runner.run_script(parse_script("WAIT 0").nodes)

        self.assertEqual(lifecycle, ["start", "stop"])
        self.assertEqual(finished, [(False, "hook denied")])

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

        f9_data = types.SimpleNamespace(vkCode=main.VK_F9, scanCode=0x43, flags=0)
        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYDOWN, f9_data))
        self.assertFalse(recorder._on_windows_keyboard_event(WM_KEYUP, f9_data))
        self.assertEqual(stops, [])
        self.assertEqual(len(recorder.snapshot()), 2)

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
        app.mode = "idle"
        app.events = []
        app._begin_recording = lambda append=False: calls.append(append)
        app.start_recording()
        self.assertEqual(calls, [False])

    def test_existing_recording_prompts_to_continue_or_overwrite(self) -> None:
        for choice, expected_append in ((True, True), (False, False)):
            with self.subTest(choice=choice):
                calls = []
                app = object.__new__(main.MacroPilotApp)
                app.mode = "idle"
                app.events = [{"t": 1.0, "type": "mouse_move", "x": 1, "y": 2}]
                app._begin_recording = lambda append=False: calls.append(append)
                with mock.patch.object(main.messagebox, "askyesnocancel", return_value=choice):
                    app.start_recording()
                self.assertEqual(calls, [expected_append])

    def test_existing_recording_cancel_keeps_macro(self) -> None:
        statuses = []
        app = object.__new__(main.MacroPilotApp)
        app.mode = "idle"
        app.events = [{"t": 1.0, "type": "mouse_move", "x": 1, "y": 2}]
        app.status_var = types.SimpleNamespace(set=statuses.append)
        app._begin_recording = lambda append=False: self.fail("recording must not start")

        with mock.patch.object(main.messagebox, "askyesnocancel", return_value=None):
            app.start_recording()

        self.assertEqual(len(app.events), 1)
        self.assertIn("отменено", statuses[0])

    def test_continued_recording_appends_shifted_timestamps(self) -> None:
        app = object.__new__(main.MacroPilotApp)
        app.events = [
            {"t": 1.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 2.0, "type": "mouse_move", "x": 2, "y": 2},
            {"t": 0.25, "type": "mouse_move", "x": 99, "y": 99},
        ]
        app.recording_append_mode = True
        app.recording_base_count = 2
        app.recording_base_duration = 2.0
        new_events = [
            {"t": 0.25, "type": "mouse_move", "x": 3, "y": 3},
            {"t": 0.75, "type": "mouse_move", "x": 4, "y": 4},
        ]

        app._merge_recorded_segment(new_events)

        self.assertEqual(len(app.events), 4)
        self.assertEqual([event["t"] for event in app.events], [1.0, 2.0, 2.25, 2.75])
        self.assertEqual([event["x"] for event in app.events[-2:]], [3, 4])

    def test_global_f9_starts_once_per_press_and_f12_stops(self) -> None:
        callbacks = {}
        starts = []
        stops = []

        class FakeListener:
            def __init__(self, **received):
                callbacks.update(received)

            def start(self):
                pass

        main.keyboard = types.SimpleNamespace(Key=FakeKey, Listener=FakeListener)
        app = object.__new__(main.MacroPilotApp)
        app.start_hotkey_held = False
        app.start_recording = lambda: starts.append("start")
        app.stop_current = lambda: stops.append("stop")
        app._ui = lambda callback, *args: callback(*args)
        app.status_var = types.SimpleNamespace(set=lambda _text: None)

        app._start_safety_listener()
        callbacks["on_press"](FakeKey.f9)
        callbacks["on_press"](FakeKey.f9)
        callbacks["on_release"](FakeKey.f9)
        callbacks["on_press"](FakeKey.f9)
        callbacks["on_press"](FakeKey.f12)

        self.assertEqual(starts, ["start", "start"])
        self.assertEqual(stops, ["stop"])

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
        try:
            self.assertTrue(second_click.wait(1), "infinite repeat did not start a second cycle")
            runner.stop()
            worker.join(1)

            self.assertFalse(worker.is_alive())
            self.assertGreaterEqual(press_count, 2)
            self.assertEqual(finished, [(True, None)])
        finally:
            runner.stop()
            worker.join(1)


if __name__ == "__main__":
    unittest.main()
