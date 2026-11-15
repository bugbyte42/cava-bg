#!/usr/bin/env python3
"""Test prebuilt installation with local archives and fake download commands."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "install.sh"
VERSION = subprocess.check_output(
    ["bash", str(REPOSITORY / "scripts/version.sh")], text=True
).strip()
ARCHIVE = f"cava-bg-{VERSION}-x86_64-unknown-linux-gnu"

# Keep HOME unchanged. Redirect installation and record each protected operation.
WRAPPER = r'''
cargo() { echo build >> "$TEST_CALLS"; }
ldd() { echo linkage >> "$TEST_CALLS"; }
mkdir() {
    if [ "$TEST_CASE" = mkdir-failure ] && [ "$*" = "-p $TEST_DOWNLOAD_DIR/extract" ]; then return 1; fi
    if [ "$*" != "-p $HOME/.local/bin" ]; then command mkdir "$@"; fi
}
install() {
    echo install >> "$TEST_CALLS"
    if [ "$TEST_CASE" = install-failure ]; then return 1; fi
    INSTALL_BIN_DIR="$TEST_INSTALL_DIR"
    command cp "$BIN_PATH" "$INSTALL_BIN_DIR/cava-bg"
}
sudo() { echo 'Unexpected sudo call' >&2; return 1; }
mktemp() {
    if [ "$TEST_CASE" = temp-failure ]; then return 1; fi
    command mkdir -p "$TEST_DOWNLOAD_DIR"
    printf '%s\n' "$TEST_DOWNLOAD_DIR"
}
tar() {
    echo extract >> "$TEST_CALLS"
    command tar "$@"
}
if [ "$TEST_CASE" != missing-verifier ]; then
    sha256sum() {
        echo verify >> "$TEST_CALLS"
        if [ "$TEST_CASE" = verifier-failure ]; then return 1; fi
        command sha256sum "$@"
    }
fi
source "$1" "${@:2}"
'''

CURL = r'''#!/bin/bash
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) destination="$2"; shift 2 ;;
        *) url="$1"; shift ;;
    esac
done
printf 'download %s\n' "$url" >> "$TEST_CALLS"
if [ "$TEST_CASE" = missing-archive ]; then exit 22; fi
if [ "$TEST_CASE" = prefixed-tag ] && [[ "$url" != */download/v* ]]; then exit 22; fi
if [[ "$url" = *.sha256 ]]; then
    if [ "$TEST_CASE" = missing-checksum ]; then exit 22; fi
    if [ "$TEST_CASE" = missing-checksum-file ]; then exit 0; fi
    cp "$TEST_CHECKSUM" "$destination"
else
    cp "$TEST_ARCHIVE" "$destination"
fi
'''


class PrebuiltInstallerTests(unittest.TestCase):
    def run_installer(self, case="valid", binary=True, version=VERSION):
        with tempfile.TemporaryDirectory(prefix="cava-bg-prebuilt-") as directory:
            root = Path(directory)
            commands = root / "bin"
            commands.mkdir()
            installed = root / "installed"
            installed.mkdir()
            for name in ("cat", "cp", "grep", "gzip", "mkdir", "rm", "tar", "sha256sum"):
                if name == "sha256sum" and case == "missing-verifier":
                    continue
                commands.joinpath(name).symlink_to(shutil.which(name))
            if case != "missing-curl":
                commands.joinpath("curl").write_text(CURL)
                commands.joinpath("curl").chmod(0o755)

            (root / "src").mkdir()
            (root / "src/main.rs").touch()
            (root / "Cargo.toml").touch()
            source_binary = root / "target/release/cava-bg"
            source_binary.parent.mkdir(parents=True)
            source_binary.write_text('#!/bin/bash\necho "run source" >> "$TEST_CALLS"\n')
            source_binary.chmod(0o755)

            archive = root / f"{ARCHIVE}.tar.gz"
            payload = b'#!/bin/bash\necho "run prebuilt" >> "$TEST_CALLS"\n'
            with tarfile.open(archive, "w:gz") as fixture:
                name = "unexpected" if case == "missing-binary" else "cava-bg"
                entry = tarfile.TarInfo(f"{ARCHIVE}/{name}")
                entry.size = len(payload)
                entry.mode = 0o755
                fixture.addfile(entry, io.BytesIO(payload))
            if case == "invalid-archive":
                archive.write_bytes(b"not a tar archive")

            # Generate the checksum with the existing packaging helper.
            subprocess.run(
                ["bash", "-c", 'source "$1"; cava_bg_sha256 "$2"', "test-checksum",
                 str(REPOSITORY / "scripts/version.sh"), str(archive)], check=True,
            )
            record = archive.with_suffix(".gz.sha256").read_text()
            digest = record.split()[0]
            records = {
                "empty-checksum": "",
                "malformed-checksum": "not a checksum\n",
                "wrong-checksum": f"{'0' * 64}  {archive.name}\n",
                "wrong-filename": f"{digest}  unrelated.tar.gz\n",
                "absolute-filename": f"{digest}  {archive}\n",
                "extra-record": record + f"{digest}  unrelated.tar.gz\n",
                "trailing-junk": record + "not a checksum\n",
            }
            checksum = root / "fixture.sha256"
            checksum.write_text(records.get(case, record))
            calls_file = root / "calls"
            download_dir = root / "download"
            environment = os.environ.copy()
            environment.update(
                PATH=str(commands), BASH_ENV="/dev/null", CAVA_BG_SOURCE_DIR=str(root),
                TEST_CASE=case, TEST_CALLS=str(calls_file), TEST_ARCHIVE=str(archive),
                TEST_CHECKSUM=str(checksum), TEST_INSTALL_DIR=str(installed),
                TEST_DOWNLOAD_DIR=str(download_dir),
            )
            args = [shutil.which("bash"), "--noprofile", "--norc", "-c", WRAPPER,
                    "test-installer-binary", str(INSTALLER), "--user", "--no-cava",
                    "--no-config", "--no-completions", "--version", version]
            if binary:
                args.append("--binary")
            result = subprocess.run(args, cwd=root, env=environment, input=b"",
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=5)
            calls = calls_file.read_text().splitlines() if calls_file.exists() else []
            self.assertFalse(download_dir.exists(), f"Temporary download directory remains; operations: {calls}")
            return result.returncode, result.stdout.decode(), calls

    def test_valid_prebuilt_is_verified_before_use(self):
        for case, version in (("valid", VERSION), ("prefixed-tag", f"v{VERSION}")):
            with self.subTest(case=case):
                status, output, calls = self.run_installer(case, version=version)
                self.assertEqual(status, 0, output)
                operations = [c for c in calls if not c.startswith("download ")]
                self.assertEqual(operations, ["verify", "extract", "linkage", "install", "run prebuilt"])
                downloads = [c for c in calls if c.startswith("download ")]
                self.assertTrue(downloads[-1].endswith(downloads[-2].removeprefix("download ") + ".sha256"))

    def test_checksum_failures_stop_before_extraction(self):
        cases = ("missing-checksum", "missing-checksum-file", "empty-checksum", "malformed-checksum",
                 "wrong-checksum", "wrong-filename", "absolute-filename", "extra-record",
                 "trailing-junk", "missing-verifier", "verifier-failure")
        for case in cases:
            with self.subTest(case=case):
                status, output, calls = self.run_installer(case)
                self.assertNotEqual(status, 0, output)
                self.assertIn("Stopping installation.", output)
                for operation in ("build", "extract", "linkage", "install", "run prebuilt", "run source"):
                    self.assertNotIn(operation, calls)

    def test_unavailable_prebuilt_uses_source(self):
        for case, binary, version in (("missing-archive", True, VERSION),
                                      ("missing-curl", True, VERSION),
                                      ("valid", True, "main"), ("valid", False, VERSION)):
            with self.subTest(case=case, binary=binary, version=version):
                status, output, calls = self.run_installer(case, binary=binary, version=version)
                self.assertEqual(status, 0, output)
                operations = [c for c in calls if not c.startswith("download ")]
                self.assertEqual(operations, ["build", "linkage", "install", "run source"])

    def test_broken_prebuilt_does_not_use_source(self):
        for case in ("invalid-archive", "missing-binary", "temp-failure", "mkdir-failure"):
            with self.subTest(case=case):
                status, output, calls = self.run_installer(case)
                self.assertNotEqual(status, 0, output)
                if case != "temp-failure":
                    self.assertIn("Checksum OK", output)
                    if case != "mkdir-failure":
                        self.assertIn("extract", calls)
                if case == "missing-binary":
                    self.assertIn("Archive did not contain expected binary", output)
                for operation in ("build", "linkage", "install", "run prebuilt", "run source"):
                    self.assertNotIn(operation, calls)

    def test_install_failure_cleans_verified_archive(self):
        status, output, calls = self.run_installer("install-failure")
        self.assertNotEqual(status, 0, output)
        self.assertEqual([c for c in calls if not c.startswith("download ")],
                         ["verify", "extract", "linkage", "install"])
        self.assertNotIn("has been installed successfully", output)


if __name__ == "__main__":
    unittest.main()
