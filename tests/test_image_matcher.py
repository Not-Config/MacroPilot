import importlib.util
import unittest

from image_matcher import ScreenImageMatcher


IMAGE_DEPENDENCIES_AVAILABLE = all(
    importlib.util.find_spec(name) is not None for name in ("cv2", "numpy")
)


@unittest.skipUnless(IMAGE_DEPENDENCIES_AVAILABLE, "OpenCV is not installed")
class ScreenImageMatcherTests(unittest.TestCase):
    def test_finds_template_and_applies_virtual_desktop_origin(self):
        import cv2
        import numpy

        template = numpy.zeros((8, 10, 3), dtype=numpy.uint8)
        template[:, :5] = (20, 80, 210)
        template[:, 5:] = (220, 40, 30)
        screenshot = numpy.zeros((50, 70, 4), dtype=numpy.uint8)
        screenshot[17:25, 23:33, :3] = template

        class FakeCapture:
            monitors = [{"left": -120, "top": 40, "width": 70, "height": 50}]

            @staticmethod
            def grab(_monitor):
                return screenshot

        matcher = object.__new__(ScreenImageMatcher)
        matcher.cv2 = cv2
        matcher.numpy = numpy
        matcher.template = template
        matcher.height, matcher.width = template.shape[:2]
        matcher.constant_template = False
        matcher.capture = FakeCapture()

        match = matcher.find(0.95)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual((match.left, match.top), (-97, 57))
        self.assertEqual(match.center, (-92, 61))
        self.assertGreaterEqual(match.confidence, 0.95)


if __name__ == "__main__":
    unittest.main()
