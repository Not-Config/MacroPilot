from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
        if asset.size and received != asset.size:
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


def inspect_update_archive(archive: Path, frozen: bool) -> str:
    required = "MacroPilot.exe" if frozen else "main.py"
    try:
        with zipfile.ZipFile(archive) as package:
            files: set[PurePosixPath] = set()
            items = package.infolist()
            if len(items) > MAX_ARCHIVE_FILES:
                raise UpdateError("Архив обновления содержит слишком много файлов")
            extracted_size = 0
            for item in items:
                normalized_name = item.filename.replace("\\", "/")
                path = PurePosixPath(normalized_name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or ":" in path.parts[0]
                ):
                    raise UpdateError("Архив обновления содержит небезопасный путь")
                if item.is_dir():
                    continue
                # Reject Unix symbolic links. Windows release archives contain
                # regular files only.
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise UpdateError("Архив обновления содержит символическую ссылку")
                if item.flag_bits & 0x1:
                    raise UpdateError("Архив обновления содержит зашифрованный файл")
                extracted_size += item.file_size
                if extracted_size > MAX_EXTRACTED_BYTES:
                    raise UpdateError("Распакованный архив обновления слишком большой")
                files.add(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("Загруженный файл не является корректным ZIP-архивом") from exc

    direct = PurePosixPath(required)
    nested = PurePosixPath("MacroPilot") / required
    if direct in files:
        return ""
    if nested in files:
        return "MacroPilot"
    raise UpdateError(f"В архиве обновления отсутствует {required}")


POWERSHELL_INSTALLER = r'''param(
    [int]$ProcessId,
    [string]$ArchivePath,
    [string]$InstallPath,
    [string]$PayloadSubdir,
    [string]$LaunchFile,
    [string]$LaunchArgument
)

$ErrorActionPreference = "Stop"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("MacroPilot-stage-" + [guid]::NewGuid())
$log = Join-Path $InstallPath "update-error.log"
try {
    while (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Start-Sleep -Milliseconds 250
    }
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $stage -Force
    $source = if ($PayloadSubdir) { Join-Path $stage $PayloadSubdir } else { $stage }
    & robocopy.exe $source $InstallPath /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) {
        throw "Robocopy завершился с кодом $LASTEXITCODE"
    }
    Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
    if ($LaunchArgument) {
        Start-Process -FilePath $LaunchFile -ArgumentList @($LaunchArgument) -WorkingDirectory $InstallPath
    } else {
        Start-Process -FilePath $LaunchFile -WorkingDirectory $InstallPath
    }
} catch {
    ("Не удалось установить обновление: " + $_.Exception.Message) | Set-Content -LiteralPath $log -Encoding UTF8
    if ($LaunchArgument) {
        Start-Process -FilePath $LaunchFile -ArgumentList @($LaunchArgument) -WorkingDirectory $InstallPath
    } else {
        Start-Process -FilePath $LaunchFile -WorkingDirectory $InstallPath
    }
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
'''


def launch_update_installer(archive: Path, payload_subdir: str) -> None:
    frozen = bool(getattr(sys, "frozen", False))
    install_path = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
    launch_file = Path(sys.executable).resolve()
    launch_argument = "" if frozen else str(install_path / "main.py")
    script = Path(tempfile.gettempdir()) / f"MacroPilot-update-{uuid.uuid4().hex}.ps1"
    script.write_text(POWERSHELL_INSTALLER, encoding="utf-8-sig")
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ProcessId",
        str(os.getpid()),
        "-ArchivePath",
        str(archive.resolve()),
        "-InstallPath",
        str(install_path),
        "-PayloadSubdir",
        payload_subdir,
        "-LaunchFile",
        str(launch_file),
        "-LaunchArgument",
        launch_argument,
    ]
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(command, close_fds=True, creationflags=creation_flags)
    except OSError as exc:
        try:
            script.unlink()
        except OSError:
            pass
        raise UpdateError(f"Не удалось запустить установщик обновления: {exc}") from exc


def temporary_update_path(version: str) -> Path:
    safe_version = re.sub(r"[^0-9A-Za-z_.-]", "-", version)
    return Path(tempfile.gettempdir()) / f"MacroPilot-{safe_version}-{uuid.uuid4().hex}.zip"
