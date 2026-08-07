from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_TEMPLATE_BYTES = 50 * 1024 * 1024


class ImageMatchError(RuntimeError):
    """A user-facing screen capture or image matching failure."""


@dataclass(frozen=True, slots=True)
class ImageMatch:
    left: int
    top: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


class ScreenImageMatcher:
    """Find one reference image on the Windows virtual desktop."""

    def __init__(self, template_path: str | Path) -> None:
        self.template_path = Path(template_path)
        try:
            size = self.template_path.stat().st_size
        except OSError as exc:
            raise ImageMatchError(
                f"Не удалось открыть эталонное изображение: {self.template_path}"
            ) from exc
        if not self.template_path.is_file():
            raise ImageMatchError(f"Эталон не является файлом: {self.template_path}")
        if size > MAX_TEMPLATE_BYTES:
            raise ImageMatchError("Эталонное изображение больше 50 МБ")

        try:
            import cv2
            import mss
            import numpy
        except ImportError as exc:
            raise ImageMatchError(
                "Поиск изображений недоступен: установите зависимости "
                "командой python -m pip install -r requirements.txt"
            ) from exc

        self.cv2: Any = cv2
        self.numpy: Any = numpy
        encoded = numpy.fromfile(str(self.template_path), dtype=numpy.uint8)
        self.template = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if self.template is None or not self.template.size:
            raise ImageMatchError(
                f"Не удалось прочитать PNG/JPG/BMP: {self.template_path}"
            )
        self.height, self.width = self.template.shape[:2]
        self.constant_template = float(self.template.std()) < 0.000001
        try:
            self.capture = mss.mss()
        except Exception as exc:
            raise ImageMatchError(f"Не удалось начать захват экрана: {exc}") from exc

    def close(self) -> None:
        close = getattr(self.capture, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> ScreenImageMatcher:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def find(self, minimum_confidence: float) -> ImageMatch | None:
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ImageMatchError("Точность поиска должна быть от 0 до 1")
        try:
            monitor = self.capture.monitors[0]
            screenshot = self.numpy.asarray(self.capture.grab(monitor))[:, :, :3]
        except Exception as exc:
            raise ImageMatchError(f"Не удалось сделать снимок экрана: {exc}") from exc

        screen_height, screen_width = screenshot.shape[:2]
        if self.width > screen_width or self.height > screen_height:
            raise ImageMatchError("Эталонное изображение больше рабочего стола")

        if self.constant_template:
            result = self.cv2.matchTemplate(
                screenshot,
                self.template,
                self.cv2.TM_SQDIFF_NORMED,
            )
            minimum, _maximum, minimum_at, _maximum_at = self.cv2.minMaxLoc(result)
            score = 1.0 - float(minimum)
            location = minimum_at
        else:
            result = self.cv2.matchTemplate(
                screenshot,
                self.template,
                self.cv2.TM_CCOEFF_NORMED,
            )
            result = self.numpy.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
            _minimum, maximum, _minimum_at, maximum_at = self.cv2.minMaxLoc(result)
            score = float(maximum)
            location = maximum_at

        if score < minimum_confidence:
            return None
        return ImageMatch(
            left=int(monitor["left"]) + int(location[0]),
            top=int(monitor["top"]) + int(location[1]),
            width=self.width,
            height=self.height,
            confidence=max(0.0, min(1.0, score)),
        )
