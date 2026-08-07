import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from update_service import (
    POWERSHELL_INSTALLER,
    ReleaseAsset,
    UpdateError,
    choose_release_asset,
    download_release_asset,
    fetch_latest_release,
    inspect_update_archive,
    is_newer_version,
    parse_version,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, content_length=None):
        super().__init__(payload)
        self.headers = {
            "Content-Length": str(len(payload)) if content_length is None else content_length
        }

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

    def test_installer_waits_copies_and_restarts(self):
        self.assertIn("Get-Process -Id $ProcessId", POWERSHELL_INSTALLER)
        self.assertIn("robocopy.exe", POWERSHELL_INSTALLER)
        self.assertIn("Start-Process", POWERSHELL_INSTALLER)


if __name__ == "__main__":
    unittest.main()
