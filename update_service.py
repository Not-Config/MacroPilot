from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from project_config import SOURCE_RELEASE_ASSET, WINDOWS_RELEASE_ASSET


GITHUB_API = "https://api.github.com"
MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_UPDATE_BYTES = 500 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
UPDATE_HELPER_FLAG = "--apply-update"
UPDATE_CLEANUP_ENV = "MACROPILOT_UPDATE_CLEANUP"
UPDATE_STAGE_PREFIX = "MacroPilot-stage-"
WINDOWS_ANTIVIRUS_ERROR_CODES = {225, 226}


class UpdateError(RuntimeError):
    """A user-facing update failure."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    title: str
    notes: str
    page_url: str
    assets: tuple[ReleaseAsset, ...]


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise UpdateError(f"Неподдерживаемый номер версии: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _read_limited(response: Any, maximum: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError) as exc:
            raise UpdateError("Сервер обновлений вернул некорректный размер ответа") from exc
        if declared_size < 0:
            raise UpdateError("Сервер обновлений вернул некорректный размер ответа")
        if declared_size > maximum:
            raise UpdateError("Ответ сервера обновлений слишком большой")
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise UpdateError("Ответ сервера обновлений слишком большой")
    return payload


def _github_limit_message(error: urllib.error.HTTPError) -> str:
    headers = error.headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            seconds = max(1, int(retry_after))
        except (TypeError, ValueError):
            pass
        else:
            minutes = max(1, (seconds + 59) // 60)
            return (
                "GitHub временно ограничил проверку обновлений. "
                f"Повторите примерно через {minutes} мин."
            )

    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if remaining == "0" and reset:
        try:
            reset_time = datetime.fromtimestamp(int(reset)).astimezone()
        except (OSError, OverflowError, TypeError, ValueError):
            pass
        else:
            return (
                "GitHub временно исчерпал лимит проверок для вашего IP. "
                f"Повторите после {reset_time:%H:%M}."
            )

    return f"GitHub временно отклонил запрос проверки обновлений (HTTP {error.code})."


def _fetch_latest_release_from_page(
    repository: str,
    timeout: float,
    opener: Callable[..., Any],
) -> ReleaseInfo:
    latest_url = f"https://github.com/{repository}/releases/latest"
    request = urllib.request.Request(
        latest_url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "MacroPilot-Updater",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            final_url = str(response.geturl())
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"Запасная проверка релиза вернула HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Не удалось открыть страницу последнего релиза: {exc}") from exc

    parsed = urllib.parse.urlparse(final_url)
    expected_path = f"/{repository}/releases/tag/"
    if (
        parsed.scheme.casefold() != "https"
        or parsed.netloc.casefold() not in {"github.com", "www.github.com"}
        or not parsed.path.casefold().startswith(expected_path.casefold())
    ):
        raise UpdateError("GitHub не указал версию последнего релиза")

    tag = urllib.parse.unquote(parsed.path[len(expected_path) :]).strip("/")
    version = ".".join(str(part) for part in parse_version(tag))
    encoded_tag = urllib.parse.quote(tag, safe="")
    page_url = f"https://github.com/{repository}/releases/tag/{encoded_tag}"
    asset_base = f"https://github.com/{repository}/releases/download/{encoded_tag}"
    assets = tuple(
        ReleaseAsset(
            name=name,
            download_url=f"{asset_base}/{urllib.parse.quote(name, safe='')}",
            size=0,
        )
        for name in (WINDOWS_RELEASE_ASSET, SOURCE_RELEASE_ASSET)
    )
    return ReleaseInfo(
        version=version,
        title=f"MacroPilot {version}",
        notes="",
        page_url=page_url,
        assets=assets,
    )


def fetch_latest_release(
    repository: str,
    timeout: float = 10.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> ReleaseInfo:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise UpdateError("Репозиторий обновлений настроен неверно")

    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{repository}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MacroPilot-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = _read_limited(response, MAX_RELEASE_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("В репозитории пока нет опубликованных релизов") from exc
        if exc.code in {403, 429}:
            try:
                return _fetch_latest_release_from_page(repository, timeout, opener)
            except UpdateError as fallback_error:
                raise UpdateError(
                    f"{_github_limit_message(exc)} {fallback_error}"
                ) from exc
        raise UpdateError(f"GitHub вернул ошибку HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Не удалось связаться с GitHub: {exc}") from exc

    try:
        document = json.loads(payload.decode("utf-8"))
        tag = str(document["tag_name"])
        version = ".".join(str(part) for part in parse_version(tag))
        raw_assets = document.get("assets", [])
        if not isinstance(raw_assets, list):
            raise TypeError("assets")
        assets = tuple(
            ReleaseAsset(
                name=str(item["name"]),
                download_url=str(item["browser_download_url"]),
                size=int(item.get("size", 0)),
                digest=str(item["digest"]) if item.get("digest") else None,
            )
            for item in raw_assets
            if isinstance(item, dict)
        )
        return ReleaseInfo(
            version=version,
            title=str(document.get("name") or f"MacroPilot {version}"),
            notes=str(document.get("body") or ""),
            page_url=str(document["html_url"]),
            assets=assets,
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub вернул неполное описание релиза") from exc


def choose_release_asset(release: ReleaseInfo, frozen: bool) -> ReleaseAsset:
    preferred = WINDOWS_RELEASE_ASSET if frozen else SOURCE_RELEASE_ASSET
    fallbacks = (preferred, "MacroPilot.zip")
    for name in fallbacks:
        for asset in release.assets:
            if asset.name.casefold() == name.casefold():
                if asset.size > MAX_UPDATE_BYTES:
                    raise UpdateError("Архив обновления превышает допустимый размер")
                return asset
    raise UpdateError(f"В релизе отсутствует файл {preferred}")


def download_release_asset(
    asset: ReleaseAsset,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Path:
    request = urllib.request.Request(
        asset.download_url,
        headers={"Accept": "application/octet-stream", "User-Agent": "MacroPilot-Updater"},
    )
    temporary = destination.with_suffix(destination.suffix + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(request, timeout=timeout) as response, temporary.open("wb") as output:
            raw_header_size = response.headers.get("Content-Length")
            try:
                header_size = int(raw_header_size) if raw_header_size else 0
            except (TypeError, ValueError) as exc:
                raise UpdateError("Сервер вернул некорректный размер обновления") from exc
            if header_size < 0:
                raise UpdateError("Сервер вернул некорректный размер обновления")
            expected_size = asset.size or header_size
            if expected_size > MAX_UPDATE_BYTES:
                raise UpdateError("Архив обновления превышает допустимый размер")
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_UPDATE_BYTES:
                    raise UpdateError("Архив обновления превышает допустимый размер")
                output.write(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress(received, expected_size)
        if expected_size and received != expected_size:
            raise UpdateError("Архив обновления загрузился не полностью")
        if asset.digest:
            algorithm, separator, expected_digest = asset.digest.partition(":")
            if separator and algorithm.casefold() == "sha256":
                if digest.hexdigest().casefold() != expected_digest.casefold():
                    raise UpdateError("Контрольная сумма обновления не совпала")
        os.replace(temporary, destination)
        return destination
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"Не удалось скачать обновление: HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Не удалось скачать обновление: {exc}") from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _validated_archive_members(
    package: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    items = package.infolist()
    if len(items) > MAX_ARCHIVE_FILES:
        raise UpdateError("Архив обновления содержит слишком много файлов")
    extracted_size = 0
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    for item in items:
        normalized_name = item.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or any(":" in part for part in path.parts)
        ):
            raise UpdateError("Архив обновления содержит небезопасный путь")
        if item.is_dir():
            continue
        # Release archives contain regular files only. Symlinks could escape
        # the staging folder later, so reject them before extraction.
        if (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise UpdateError("Архив обновления содержит символическую ссылку")
        if item.flag_bits & 0x1:
            raise UpdateError("Архив обновления содержит зашифрованный файл")
        extracted_size += item.file_size
        if extracted_size > MAX_EXTRACTED_BYTES:
            raise UpdateError("Распакованный архив обновления слишком большой")
        members.append((item, path))
    return members


def inspect_update_archive(archive: Path, frozen: bool) -> str:
    required = "MacroPilot.exe" if frozen else "main.py"
    try:
        with zipfile.ZipFile(archive) as package:
            files = {path for _item, path in _validated_archive_members(package)}
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("Загруженный файл не является корректным ZIP-архивом") from exc

    direct = PurePosixPath(required)
    nested = PurePosixPath("MacroPilot") / required
    if direct in files:
        return ""
    if nested in files:
        return "MacroPilot"
    raise UpdateError(f"В архиве обновления отсутствует {required}")


def _friendly_update_error(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    winerror = getattr(exc, "winerror", None)
    if winerror in WINDOWS_ANTIVIRUS_ERROR_CODES or any(
        marker in lowered
        for marker in (
            "содержит вирус",
            "potentially unwanted",
            "virus or potentially unwanted",
        )
    ):
        return (
            "Защитник Windows заблокировал файл обновления. MacroPilot не будет "
            "просить отключать защиту или добавлять исключение. Установите переносимую "
            "ZIP-версию с официальной страницы релиза либо дождитесь исправленной сборки."
        )
    return message


def _is_managed_stage(path: Path) -> bool:
    try:
        resolved = path.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return resolved.parent == temporary_root and resolved.name.startswith(
        UPDATE_STAGE_PREFIX
    )


def _remove_managed_stage(path: Path) -> None:
    if _is_managed_stage(path):
        shutil.rmtree(path, ignore_errors=True)


def extract_update_archive(archive: Path, payload_subdir: str) -> tuple[Path, Path]:
    if payload_subdir not in {"", "MacroPilot"}:
        raise UpdateError("Архив обновления содержит неизвестную структуру")
    stage = Path(tempfile.mkdtemp(prefix=UPDATE_STAGE_PREFIX))
    try:
        with zipfile.ZipFile(archive) as package:
            for item, relative in _validated_archive_members(package):
                destination = stage.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(item) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        payload = stage / payload_subdir if payload_subdir else stage
        if not payload.is_dir():
            raise UpdateError("В архиве обновления отсутствует папка приложения")
        return stage, payload
    except UpdateError:
        _remove_managed_stage(stage)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        _remove_managed_stage(stage)
        raise UpdateError(
            f"Не удалось распаковать обновление: {_friendly_update_error(exc)}"
        ) from exc


def _wait_for_process_exit(process_id: int, timeout: float = 120.0) -> None:
    if process_id <= 0:
        raise UpdateError("Установщик получил неверный идентификатор процесса")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_object_0 = 0
        wait_timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(synchronize, False, process_id)
        if not handle:
            return
        try:
            result = int(kernel32.WaitForSingleObject(handle, round(timeout * 1000)))
        finally:
            kernel32.CloseHandle(handle)
        if result == wait_timeout:
            raise UpdateError("MacroPilot не завершился перед обновлением")
        if result != wait_object_0:
            raise UpdateError("Windows не дождался завершения MacroPilot")
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.1)
    raise UpdateError("MacroPilot не завершился перед обновлением")


def _copy_update_payload(payload: Path, install_path: Path) -> None:
    if not payload.is_dir():
        raise UpdateError("Распакованные файлы обновления не найдены")
    install_path.mkdir(parents=True, exist_ok=True)
    for source in sorted(payload.rglob("*")):
        relative = source.relative_to(payload)
        destination = install_path / relative
        if source.is_symlink():
            raise UpdateError("Обновление содержит недопустимую ссылку")
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.update-{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _start_installed_app(
    launch_file: Path,
    launch_argument: str,
    install_path: Path,
    stage: Path,
) -> None:
    command = [str(launch_file)]
    if launch_argument:
        command.append(launch_argument)
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    environment[UPDATE_CLEANUP_ENV] = str(stage)
    subprocess.Popen(
        command,
        cwd=install_path,
        close_fds=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        env=environment,
    )


def apply_staged_update(
    process_id: int,
    stage: Path,
    payload: Path,
    install_path: Path,
    launch_file: Path,
    launch_argument: str,
) -> int:
    log = install_path / "update-error.log"
    success = False
    try:
        if not _is_managed_stage(stage):
            raise UpdateError("Установщик получил небезопасный временный путь")
        resolved_stage = stage.resolve()
        resolved_payload = payload.resolve()
        if resolved_payload != resolved_stage and resolved_stage not in resolved_payload.parents:
            raise UpdateError("Файлы обновления находятся вне временной папки")
        _wait_for_process_exit(process_id)
        _copy_update_payload(resolved_payload, install_path.resolve())
        log.unlink(missing_ok=True)
        success = True
    except Exception as exc:
        try:
            install_path.mkdir(parents=True, exist_ok=True)
            log.write_text(
                "Не удалось установить обновление: " + _friendly_update_error(exc),
                encoding="utf-8",
            )
        except OSError:
            pass
    try:
        _start_installed_app(
            launch_file.resolve(),
            launch_argument,
            install_path.resolve(),
            stage.resolve(),
        )
    except OSError as exc:
        try:
            log.write_text(
                "Не удалось перезапустить MacroPilot: " + _friendly_update_error(exc),
                encoding="utf-8",
            )
        except OSError:
            pass
        return 1
    return 0 if success else 1


def handle_update_command(arguments: list[str] | None = None) -> int | None:
    arguments = sys.argv if arguments is None else arguments
    if len(arguments) < 2 or arguments[1] != UPDATE_HELPER_FLAG:
        return None
    if len(arguments) != 8:
        return 2
    try:
        process_id = int(arguments[2])
    except ValueError:
        return 2
    return apply_staged_update(
        process_id,
        Path(arguments[3]),
        Path(arguments[4]),
        Path(arguments[5]),
        Path(arguments[6]),
        arguments[7],
    )


def cleanup_previous_update_stage() -> None:
    raw_stage = os.environ.pop(UPDATE_CLEANUP_ENV, "")
    if not raw_stage:
        return
    stage = Path(raw_stage)
    if not _is_managed_stage(stage):
        return

    def worker() -> None:
        for _attempt in range(40):
            try:
                shutil.rmtree(stage)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.25)

    threading.Thread(
        target=worker,
        name="MacroPilotUpdateCleanup",
        daemon=True,
    ).start()


def launch_update_installer(archive: Path, payload_subdir: str) -> None:
    frozen = bool(getattr(sys, "frozen", False))
    install_path = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
    launch_file = Path(sys.executable).resolve()
    launch_argument = "" if frozen else str(install_path / "main.py")
    stage, payload = extract_update_archive(archive, payload_subdir)
    if frozen:
        staged_launcher = payload / "MacroPilot.exe"
        command = [str(staged_launcher)]
    else:
        staged_launcher = payload / "main.py"
        command = [str(sys.executable), str(staged_launcher)]
    if not staged_launcher.is_file():
        _remove_managed_stage(stage)
        raise UpdateError("В обновлении отсутствует файл запуска MacroPilot")
    command.extend(
        [
            UPDATE_HELPER_FLAG,
            str(os.getpid()),
            str(stage.resolve()),
            str(payload.resolve()),
            str(install_path),
            str(launch_file),
            launch_argument,
        ]
    )
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        subprocess.Popen(
            command,
            cwd=payload,
            close_fds=True,
            creationflags=creation_flags,
            env=environment,
        )
    except OSError as exc:
        _remove_managed_stage(stage)
        raise UpdateError(
            "Не удалось запустить установщик обновления: "
            + _friendly_update_error(exc)
        ) from exc
    try:
        archive.unlink(missing_ok=True)
    except OSError:
        pass


def temporary_update_path(version: str) -> Path:
    safe_version = re.sub(r"[^0-9A-Za-z_.-]", "-", version)
    return Path(tempfile.gettempdir()) / f"MacroPilot-{safe_version}-{uuid.uuid4().hex}.zip"
