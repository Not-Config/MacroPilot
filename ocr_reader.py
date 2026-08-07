from __future__ import annotations

import asyncio
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


WINDOWS_OCR_AVAILABLE = sys.platform == "win32"
DEFAULT_OCR_LANGUAGE = "auto"
MAX_OCR_REGION_DIMENSION = 16_384
MAX_OCR_REGION_PIXELS = 16_777_216
MAX_OCR_TEXT_LENGTH = 10_000

_NUMBER_PATTERN = re.compile(
    r"[+\-−–—]?\d(?:[\d\s\u00a0\u202f.,]*\d)?",
    re.UNICODE,
)


class OcrError(RuntimeError):
    """A user-facing Windows screen OCR failure."""


def normalize_ocr_text(text: str) -> str:
    """Trim OCR whitespace while preserving meaningful line boundaries."""

    lines = [" ".join(line.split()) for line in str(text).splitlines()]
    normalized = "\n".join(line for line in lines if line).strip()
    if len(normalized) > MAX_OCR_TEXT_LENGTH:
        raise OcrError(
            f"OCR вернул более {MAX_OCR_TEXT_LENGTH:,} символов; уменьшите область"
        )
    return normalized


def _normalize_number_token(token: str) -> str:
    value = (
        token.replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace(" ", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
    )
    dot_count = value.count(".")
    comma_count = value.count(",")
    if dot_count and comma_count:
        decimal_separator = "." if value.rfind(".") > value.rfind(",") else ","
        thousands_separator = "," if decimal_separator == "." else "."
        value = value.replace(thousands_separator, "")
        value = value.replace(decimal_separator, ".")
    elif dot_count or comma_count:
        separator = "." if dot_count else ","
        groups = value.lstrip("+-").split(separator)
        if len(groups) > 2 and all(len(group) == 3 for group in groups[1:]):
            value = value.replace(separator, "")
        elif len(groups) == 2:
            value = value.replace(separator, ".")
        else:
            # Multiple separators with a non-thousands tail: the last one is
            # treated as decimal and the preceding ones as group separators.
            head, tail = value.rsplit(separator, 1)
            value = head.replace(separator, "") + "." + tail
    return value


def extract_first_number(text: str) -> float:
    """Extract the first decimal number from OCR text, including RU separators."""

    match = _NUMBER_PATTERN.search(str(text))
    if match is None:
        raise OcrError("в распознанном тексте не найдено число")
    token = _normalize_number_token(match.group(0))
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise OcrError(f"не удалось разобрать число {match.group(0)!r}") from exc
    value = float(number)
    if not math.isfinite(value):
        raise OcrError("распознанное число находится вне допустимого диапазона")
    return value


class ScreenOcrReader:
    """Read text from a physical-pixel screen region using local Windows OCR."""

    def __init__(self, capture_factory: Callable[[], Any] | None = None) -> None:
        self.capture_factory = capture_factory
        self._engines: dict[str, Any] = {}
        self._apartment_initialized = False
        self._runtime: Any = None

    @staticmethod
    def _validate_region(x: int, y: int, width: int, height: int) -> dict[str, int]:
        values = (int(x), int(y), int(width), int(height))
        x, y, width, height = values
        if width <= 0 or height <= 0:
            raise OcrError("ширина и высота OCR-области должны быть больше нуля")
        if width > MAX_OCR_REGION_DIMENSION or height > MAX_OCR_REGION_DIMENSION:
            raise OcrError("OCR-область слишком велика")
        if width * height > MAX_OCR_REGION_PIXELS:
            raise OcrError("OCR-область содержит слишком много пикселей")
        return {"left": x, "top": y, "width": width, "height": height}

    def _ensure_apartment(self) -> None:
        if self._apartment_initialized:
            return
        try:
            from winrt import runtime
        except ImportError as exc:
            raise OcrError(
                "компоненты Windows OCR не установлены; переустановите MacroPilot"
            ) from exc
        runtime.init_apartment(runtime.ApartmentType.MULTI_THREADED)
        self._runtime = runtime
        self._apartment_initialized = True

    def _get_engine(self, language: str) -> Any:
        self._ensure_apartment()
        requested = str(language or DEFAULT_OCR_LANGUAGE).strip()
        cache_key = requested.casefold()
        if cache_key in self._engines:
            return self._engines[cache_key]
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
        except ImportError as exc:
            raise OcrError(
                "модули Windows OCR не загрузились; переустановите MacroPilot"
            ) from exc

        if cache_key in {"", "auto", "default", "авто"}:
            engine = OcrEngine.try_create_from_user_profile_languages()
        else:
            try:
                language_object = Language(requested)
            except Exception as exc:
                raise OcrError(f"неверный язык OCR: {requested!r}") from exc
            if not OcrEngine.is_language_supported(language_object):
                available = ", ".join(
                    item.language_tag for item in OcrEngine.available_recognizer_languages
                )
                suffix = f" Доступны: {available}." if available else ""
                raise OcrError(
                    f"язык OCR {requested!r} не установлен в Windows.{suffix}"
                )
            engine = OcrEngine.try_create_from_language(language_object)

        if engine is None:
            raise OcrError(
                "Windows не нашла установленный язык распознавания текста; "
                "добавьте языковой пакет в параметрах Windows"
            )
        self._engines[cache_key] = engine
        return engine

    @staticmethod
    async def _await_result(operation: Any) -> Any:
        return await operation

    def _recognize_bgra(
        self,
        pixels: bytes | bytearray | memoryview,
        width: int,
        height: int,
        language: str = DEFAULT_OCR_LANGUAGE,
    ) -> str:
        engine = self._get_engine(language)
        from winrt.windows.media.ocr import OcrEngine

        max_dimension = int(OcrEngine.max_image_dimension)
        if width > max_dimension or height > max_dimension:
            raise OcrError(
                "OCR-область больше предела Windows; выберите меньший прямоугольник"
            )
        expected_size = int(width) * int(height) * 4
        if len(pixels) != expected_size:
            raise OcrError("снимок OCR-области имеет неверный размер")

        try:
            from winrt.windows.graphics.imaging import (
                BitmapAlphaMode,
                BitmapPixelFormat,
                SoftwareBitmap,
            )
            from winrt.windows.storage.streams import DataWriter

            writer = DataWriter()
            try:
                writer.write_bytes(memoryview(pixels))
                buffer = writer.detach_buffer()
            finally:
                writer.close()
            bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
                buffer,
                BitmapPixelFormat.BGRA8,
                int(width),
                int(height),
                BitmapAlphaMode.PREMULTIPLIED,
            )
            try:
                result = asyncio.run(self._await_result(engine.recognize_async(bitmap)))
            finally:
                bitmap.close()
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(f"Windows OCR завершился с ошибкой: {exc}") from exc
        return normalize_ocr_text(result.text)

    def read_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        language: str = DEFAULT_OCR_LANGUAGE,
    ) -> str:
        if not WINDOWS_OCR_AVAILABLE:
            raise OcrError("распознавание области экрана доступно только в Windows")
        region = self._validate_region(x, y, width, height)
        try:
            if self.capture_factory is None:
                import mss

                capture = mss.mss()
            else:
                capture = self.capture_factory()
            try:
                screenshot = capture.grab(region)
                pixels = bytes(screenshot.bgra)
            finally:
                close = getattr(capture, "close", None)
                if close is not None:
                    close()
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(f"не удалось снять OCR-область экрана: {exc}") from exc
        return self._recognize_bgra(
            pixels,
            region["width"],
            region["height"],
            language,
        )

    def close(self) -> None:
        self._engines.clear()
        if self._apartment_initialized and self._runtime is not None:
            self._runtime.uninit_apartment()
        self._apartment_initialized = False
        self._runtime = None

    def __enter__(self) -> ScreenOcrReader:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()
