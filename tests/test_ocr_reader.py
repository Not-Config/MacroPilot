import importlib.util
import unittest
from unittest import mock

from ocr_reader import (
    OcrError,
    ScreenOcrReader,
    WINDOWS_OCR_AVAILABLE,
    extract_first_number,
    normalize_ocr_text,
)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError):
        return False


WINRT_OCR_AVAILABLE = WINDOWS_OCR_AVAILABLE and all(
    _module_available(name)
    for name in (
        "winrt.windows.globalization",
        "winrt.windows.graphics.imaging",
        "winrt.windows.media.ocr",
        "winrt.windows.storage.streams",
    )
)


class OcrReaderTests(unittest.TestCase):
    def test_normalizes_text_and_extracts_localized_numbers(self) -> None:
        self.assertEqual(
            normalize_ocr_text("  Score:   42 \n\n Ready   now  "),
            "Score: 42\nReady now",
        )
        self.assertEqual(extract_first_number("HP: 1 234,50 / 2000"), 1234.5)
        self.assertEqual(extract_first_number("Температура −12.75 °C"), -12.75)
        self.assertEqual(extract_first_number("1.234.567 очков"), 1234567.0)
        with self.assertRaisesRegex(OcrError, "не найдено число"):
            extract_first_number("READY")

    def test_region_validation_allows_negative_screen_coordinates(self) -> None:
        self.assertEqual(
            ScreenOcrReader._validate_region(-1920, -20, 300, 80),
            {"left": -1920, "top": -20, "width": 300, "height": 80},
        )
        with self.assertRaisesRegex(OcrError, "больше нуля"):
            ScreenOcrReader._validate_region(0, 0, 0, 10)
        with self.assertRaisesRegex(OcrError, "слишком много пикселей"):
            ScreenOcrReader._validate_region(0, 0, 5000, 5000)

    def test_reads_exact_region_and_closes_capture(self) -> None:
        requests = []

        class FakeScreenshot:
            bgra = bytes(range(16))

        class FakeCapture:
            closed = False

            def grab(self, region):
                requests.append(region)
                return FakeScreenshot()

            def close(self):
                self.closed = True

        capture = FakeCapture()
        reader = ScreenOcrReader(capture_factory=lambda: capture)
        with (
            mock.patch("ocr_reader.WINDOWS_OCR_AVAILABLE", True),
            mock.patch.object(
                reader,
                "_recognize_bgra",
                return_value="42",
            ) as recognize,
        ):
            result = reader.read_region(-5, 7, 2, 2, "auto")

        self.assertEqual(result, "42")
        self.assertEqual(
            requests,
            [{"left": -5, "top": 7, "width": 2, "height": 2}],
        )
        recognize.assert_called_once_with(bytes(range(16)), 2, 2, "auto")
        self.assertTrue(capture.closed)

    @unittest.skipUnless(WINRT_OCR_AVAILABLE, "requires Windows OCR packages")
    def test_windows_ocr_recognizes_generated_digits(self) -> None:
        import cv2
        import numpy

        image = numpy.full((150, 520, 4), 255, dtype=numpy.uint8)
        cv2.putText(
            image,
            "12345",
            (18, 112),
            cv2.FONT_HERSHEY_SIMPLEX,
            3.1,
            (0, 0, 0, 255),
            6,
            cv2.LINE_AA,
        )
        reader = ScreenOcrReader()
        try:
            text = reader._recognize_bgra(
                image.tobytes(),
                image.shape[1],
                image.shape[0],
                "en-US",
            )
        finally:
            reader.close()

        self.assertIn("12345", text.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
