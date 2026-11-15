#!/usr/bin/env python3
"""Test CAVA installation consent with Bash and the Python standard library."""

import errno
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
PROMPT = b"Install cava with the system package manager (requires sudo)? [y/N] "
COMMANDS = {
    "pacman": ["pacman -S cava"],
    "apt": ["apt update", "apt install cava"],
    "dnf": ["dnf install cava"],
    "zypper": ["zypper install cava"],
    "apk": ["apk add cava"],
    "xbps-install": ["xbps-install cava"],
}

# Run the complete installer with fake build and installation commands.
# Keep HOME unchanged. Redirect binary installation to the temporary directory.
WRAPPER = r'''
cargo() { :; }
ldd() { :; }
mkdir() { :; }
install() {
    INSTALL_BIN_DIR="$TEST_INSTALL_DIR"
    command cp "$BIN_PATH" "$INSTALL_BIN_DIR/cava-bg"
}
sudo() {
    printf '%s\n' "$*" >> "$TEST_CALLS"
    "$@"
}
source "$1" "${@:2}"
'''


class InstallerConsentTests(unittest.TestCase):
    def run_installer(self, mode="--user", reply=b"n\n", interactive=True,
                      manager="pacman", present=False, skip=False, failure=False):
        with tempfile.TemporaryDirectory(prefix="cava-bg-installer-") as directory:
            root = Path(directory)
            commands = root / "bin"
            commands.mkdir()
            installed = root / "installed"
            installed.mkdir()
            (root / "src").mkdir()
            (root / "src/main.rs").touch()
            (root / "Cargo.toml").touch()
            binary = root / "target/release/cava-bg"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/bash\necho 'test binary'\n")
            binary.chmod(0o755)

            # Exclude all host package managers and the host CAVA executable.
            for name in ("cat", "cp", "grep"):
                commands.joinpath(name).symlink_to(shutil.which(name))
            if manager:
                package_manager = commands / manager
                package_manager.write_text(
                    '#!/bin/bash\nexit "$TEST_PACKAGE_STATUS"\n'
                )
                package_manager.chmod(0o755)
            if present:
                commands.joinpath("cava").symlink_to(binary)

            calls_file = root / "calls"
            environment = os.environ.copy()
            environment.update(
                PATH=str(commands),
                BASH_ENV="/dev/null",
                CAVA_BG_SOURCE_DIR=str(root),
                TEST_INSTALL_DIR=str(installed),
                TEST_CALLS=str(calls_file),
                TEST_PACKAGE_STATUS="1" if failure else "0",
            )
            args = [shutil.which("bash"), "--noprofile", "--norc", "-c",
                    WRAPPER, "test-installer", str(INSTALLER)]
            if mode:
                args.append(mode)
            args.extend(["--no-config", "--no-completions"])
            if skip:
                args.append("--no-cava")

            def calls():
                return calls_file.read_text().splitlines() if calls_file.exists() else []

            if interactive:
                master, slave = pty.openpty()
                process = subprocess.Popen(args, cwd=root, env=environment,
                                           stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                output = b""
                answered = False
                destination_answered = False
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        self.assertLess(time.monotonic(), deadline, output.decode())
                        if not select.select([master], [], [], 0.1)[0]:
                            continue
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        output += chunk
                        if not mode and not destination_answered and b"[y/N] " in output:
                            os.write(master, b"n")
                            destination_answered = True
                        if PROMPT in output and not answered:
                            self.assertFalse([c for c in calls() if not c.startswith("install ")],
                                             "Package operation occurred before consent")
                            os.write(master, reply)
                            answered = True
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
            else:
                result = subprocess.run(args, cwd=root, env=environment, input=b"",
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        timeout=5)
                process = result
                output = result.stdout

            self.assertEqual(process.returncode, 0, output.decode())
            operations = [c for c in calls() if not c.startswith("install ")]
            self.assertEqual(sum(c.startswith("install ") for c in calls()),
                             int(mode == "--system"))
            return output.decode(), operations

    def test_consent_is_separate_from_destination(self):
        for mode in ("--user", "--system", None):
            for reply in (b"y\n", b"Y\n", b"yes\n", b"YES\n"):
                with self.subTest(mode=mode, reply=reply):
                    output, operations = self.run_installer(mode=mode, reply=reply)
                    self.assertIn(PROMPT.decode(), output)
                    self.assertEqual(operations, COMMANDS["pacman"])

    def test_refusal_and_end_of_input_skip_cava(self):
        for mode in ("--user", "--system", None):
            for reply in (b"n\n", b"\n", b"invalid\n", b"\x04"):
                with self.subTest(mode=mode, reply=reply):
                    output, operations = self.run_installer(mode=mode, reply=reply)
                    self.assertIn("Skipping cava.", output)
                    self.assertEqual(operations, [])

    def test_noninteractive_input_skips_cava(self):
        for mode in ("--user", "--system", None):
            with self.subTest(mode=mode):
                output, operations = self.run_installer(mode=mode, interactive=False)
                self.assertNotIn(PROMPT.decode(), output)
                self.assertIn("Not a terminal: skipping cava installation.", output)
                self.assertEqual(operations, [])

    def test_installed_cava_and_skip_option_do_not_prompt(self):
        for mode in ("--user", "--system"):
            for options in ({"present": True}, {"skip": True}):
                with self.subTest(mode=mode, options=options):
                    output, operations = self.run_installer(mode=mode, **options)
                    self.assertNotIn(PROMPT.decode(), output)
                    self.assertEqual(operations, [])

    def test_package_managers_keep_confirmation(self):
        for manager, expected in COMMANDS.items():
            with self.subTest(manager=manager):
                _, operations = self.run_installer(manager=manager, reply=b"y\n")
                self.assertEqual(operations, expected)

    def test_package_failure_and_unknown_manager(self):
        for manager in (*COMMANDS, None):
            with self.subTest(manager=manager):
                output, operations = self.run_installer(manager=manager, reply=b"y\n",
                                                        failure=True)
                expected = COMMANDS.get(manager, [])
                # Stop after a failed apt update.
                self.assertEqual(operations, expected[:1])
                self.assertIn("Warning: cava installation failed", output)
                self.assertNotIn("cava is already installed", output)
                if manager is None:
                    self.assertIn("Could not detect a supported package manager", output)


if __name__ == "__main__":
    unittest.main()
