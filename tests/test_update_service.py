import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import update_service
from update_service import (
    UPDATE_HELPER_FLAG,
    UPDATE_STAGE_PREFIX,
    ReleaseAsset,
    UpdateError,
    apply_staged_update,
    choose_release_asset,
    download_release_asset,
    extract_update_archive,
    fetch_latest_release,
    inspect_update_archive,
    is_newer_version,
    launch_update_installer,
    parse_version,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, content_length=None, url="https://example.test"):
        super().__init__(payload)
        self.headers = {
            "Content-Length": str(len(payload)) if content_length is None else content_length
        }
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def make_zip(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


class UpdateServiceTests(unittest.TestCase):
    def test_semantic_version_comparison(self):
        self.assertEqual(parse_version("v1.5.0"), (1, 5, 0))
        self.assertTrue(is_newer_version("1.5.1", "1.5.0"))
        self.assertFalse(is_newer_version("1.5.0", "1.5.0"))
        with self.assertRaises(UpdateError):
            parse_version("latest")

    def test_reads_latest_github_release_and_selects_assets(self):
        document = {
            "tag_name": "v1.6.0",
            "name": "MacroPilot 1.6.0",
            "body": "Changes",
            "html_url": "https://github.com/Not-Config/MacroPilot/releases/tag/v1.6.0",
            "assets": [
                {
                    "name": "MacroPilot-windows.zip",
                    "browser_download_url": "https://example.test/windows.zip",
                    "size": 100,
                    "digest": "sha256:abc",
                },
                {
                    "name": "MacroPilot-source.zip",
                    "browser_download_url": "https://example.test/source.zip",
                    "size": 80,
                },
            ],
        }
        release = fetch_latest_release(
            "Not-Config/MacroPilot",
            opener=lambda _request, timeout: FakeResponse(json.dumps(document).encode()),
        )
        self.assertEqual(release.version, "1.6.0")
        self.assertEqual(choose_release_asset(release, frozen=True).name, "MacroPilot-windows.zip")
        self.assertEqual(choose_release_asset(release, frozen=False).name, "MacroPilot-source.zip")

    def test_rate_limit_uses_public_release_page_fallback(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    403,
                    "Forbidden",
                    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"},
                    io.BytesIO(b""),
                )
            return FakeResponse(
                b"",
                url="https://github.com/Not-Config/MacroPilot/releases/tag/v1.5.1",
            )

        release = fetch_latest_release("Not-Config/MacroPilot", opener=opener)

        self.assertEqual(release.version, "1.5.1")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            choose_release_asset(release, frozen=True).download_url,
            "https://github.com/Not-Config/MacroPilot/releases/download/"
            "v1.5.1/MacroPilot-windows.zip",
        )

    def test_rate_limit_reports_wait_when_fallback_is_unavailable(self):
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                headers = {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "4102444800",
                }
            else:
                headers = {}
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                headers,
                io.BytesIO(b""),
            )

        with self.assertRaisesRegex(UpdateError, "лимит проверок"):
            fetch_latest_release("Not-Config/MacroPilot", opener=opener)
        self.assertEqual(calls, 2)

    def test_download_verifies_digest_and_archive_layout(self):
        payload = make_zip({"MacroPilot/main.py": "print('ok')", "MacroPilot/README.md": "readme"})
        asset = ReleaseAsset(
            name="MacroPilot-source.zip",
            download_url="https://example.test/source.zip",
            size=len(payload),
            digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        )
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.zip"
            download_release_asset(
                asset,
                path,
                progress=lambda received, total: progress.append((received, total)),
                opener=lambda _request, timeout: FakeResponse(payload),
            )
            self.assertEqual(inspect_update_archive(path, frozen=False), "MacroPilot")
            self.assertEqual(path.read_bytes(), payload)
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (len(payload), len(payload)))

    def test_rejects_bad_digest_and_unsafe_archive(self):
        payload = make_zip({"MacroPilot/main.py": "ok"})
        asset = ReleaseAsset(
            name="MacroPilot-source.zip",
            download_url="https://example.test/source.zip",
            size=len(payload),
            digest="sha256:" + "0" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.zip"
            with self.assertRaisesRegex(UpdateError, "Контрольная сумма"):
                download_release_asset(
                    asset,
                    path,
                    opener=lambda _request, timeout: FakeResponse(payload),
                )

            unsafe = Path(directory) / "unsafe.zip"
            unsafe.write_bytes(make_zip({"../main.py": "bad"}))
            with self.assertRaisesRegex(UpdateError, "небезопасный путь"):
                inspect_update_archive(unsafe, frozen=False)

            windows_unsafe = Path(directory) / "windows-unsafe.zip"
            windows_unsafe.write_bytes(make_zip({r"..\main.py": "bad"}))
            with self.assertRaisesRegex(UpdateError, "небезопасный путь"):
                inspect_update_archive(windows_unsafe, frozen=False)

            oversized = Path(directory) / "oversized.zip"
            with zipfile.ZipFile(oversized, "w") as archive:
                info = zipfile.ZipInfo("MacroPilot/main.py")
                info.file_size = 1024 * 1024 * 1024 + 1
                archive.writestr(info, b"")
            # zipfile rewrites the real size, so patch the central-directory
            # metadata as a focused test of the inspection limit.
            with zipfile.ZipFile(oversized) as archive:
                archive.infolist()[0].file_size = 1024 * 1024 * 1024 + 1
                with self.assertRaisesRegex(UpdateError, "слишком большой"):
                    # Exercise the same checks with a mocked ZipFile instance.
                    with mock.patch("update_service.zipfile.ZipFile", return_value=archive):
                        inspect_update_archive(oversized, frozen=False)

    def test_rejects_malformed_content_length(self):
        with self.assertRaisesRegex(UpdateError, "некорректный размер"):
            fetch_latest_release(
                "Not-Config/MacroPilot",
                opener=lambda _request, timeout: FakeResponse(b"{}", "not-a-number"),
            )

    def test_download_rejects_truncated_content_length_without_asset_size(self):
        asset = ReleaseAsset(
            name="MacroPilot-windows.zip",
            download_url="https://example.test/windows.zip",
            size=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.zip"
            with self.assertRaisesRegex(UpdateError, "не полностью"):
                download_release_asset(
                    asset,
                    path,
                    opener=lambda _request, timeout: FakeResponse(b"short", "20"),
                )

    def test_staged_installer_waits_copies_and_restarts(self):
        stage = Path(tempfile.mkdtemp(prefix=UPDATE_STAGE_PREFIX))
        self.addCleanup(shutil.rmtree, stage, True)
        payload = stage / "MacroPilot"
        payload.mkdir()
        (payload / "main.py").write_text("print('new')", encoding="utf-8")
        (payload / "data.txt").write_text("payload", encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory:
            install_path = Path(directory) / "installed"
            launch_file = Path(sys.executable)
            with (
                mock.patch("update_service._wait_for_process_exit") as wait,
                mock.patch("update_service._start_installed_app") as restart,
            ):
                result = apply_staged_update(
                    1234,
                    stage,
                    payload,
                    install_path,
                    launch_file,
                    str(install_path / "main.py"),
                )

            self.assertEqual(result, 0)
            wait.assert_called_once_with(1234)
            self.assertEqual((install_path / "data.txt").read_text(), "payload")
            restart.assert_called_once()

    def test_updater_does_not_invoke_powershell_or_bypass_policy(self):
        source = Path(update_service.__file__).read_text(encoding="utf-8")
        self.assertNotIn("powershell.exe", source.casefold())
        self.assertNotIn("executionpolicy", source.casefold())

    def test_installer_resets_inherited_pyinstaller_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "update.zip"
            archive.write_bytes(make_zip({"MacroPilot/main.py": "print('new')"}))
            with mock.patch("update_service.subprocess.Popen") as popen:
                launch_update_installer(archive, "MacroPilot")

            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertEqual(command[2], UPDATE_HELPER_FLAG)
            self.assertEqual(Path(command[1]).name, "main.py")
            self.assertFalse(archive.exists())
            stage = Path(command[4])
            self.assertTrue(stage.name.startswith(UPDATE_STAGE_PREFIX))
            self.addCleanup(shutil.rmtree, stage, True)

    def test_extract_update_archive_rejects_unknown_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "update.zip"
            archive.write_bytes(make_zip({"MacroPilot/main.py": "ok"}))
            with self.assertRaisesRegex(UpdateError, "неизвестную структуру"):
                extract_update_archive(archive, "other")


if __name__ == "__main__":
    unittest.main()
