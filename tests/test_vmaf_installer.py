import hashlib
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


class VmafInstallerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.runner = self.tmp / "runner"
        self.runner.mkdir()
        self.github_path = self.runner / "github-path"
        self.github_path.touch()
        release = self.tmp / "release" / "bin"
        release.mkdir(parents=True)
        executable = release / "ffmpeg"
        executable.write_text(
            "#!/bin/sh\ncase \"$*\" in\n"
            "  *-filters*) echo libvmaf ;;\n"
            "  *-version*) touch \"$(dirname \"$0\")/validated\"; echo fake-ffmpeg ;;\n"
            "esac\n"
        )
        executable.chmod(0o755)
        (release / "ffprobe").write_text("#!/bin/sh\necho fake-ffprobe\n")
        (release / "ffprobe").chmod(0o755)
        self.archive = self.tmp / "fixture.tar.xz"
        with tarfile.open(self.archive, "w:xz") as archive:
            archive.add(release.parent, arcname="package")
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        fake_gh = self.tmp / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = api ]; then\n"
            f"  printf '%s\\n' 'sha256:{self.digest}'\n"
            "elif [ \"$1\" = release ]; then\n"
            "  cp \"$FAKE_ARCHIVE\" \"$RUNNER_TEMP/vmaf-ffmpeg.tar.xz\"\n"
            "  if [ \"${CORRUPT:-0}\" = 1 ]; then printf corrupt >> \"$RUNNER_TEMP/vmaf-ffmpeg.tar.xz\"; fi\n"
            "else\n  exit 2\nfi\n"
        )
        fake_gh.chmod(0o755)
        self.script = Path(__file__).resolve().parents[1] / "scripts/install_vmaf_ffmpeg.sh"
        self.environment = os.environ.copy()
        self.environment.update({
            "GH_TOKEN": "fake-test-token",
            "RUNNER_TEMP": str(self.runner),
            "GITHUB_PATH": str(self.github_path),
            "PATH": str(self.tmp) + os.pathsep + self.environment["PATH"],
            "FAKE_ARCHIVE": str(self.archive),
        })

    def run_installer(self):
        return subprocess.run(
            ["bash", str(self.script)],
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

    def test_verified_archive_is_extracted_and_added_to_path(self):
        output = self.run_installer()
        installed = self.runner / "vmaf-ffmpeg" / "bin"
        self.assertTrue((installed / "ffmpeg").is_file())
        self.assertTrue((installed / "ffprobe").is_file())
        self.assertTrue((installed / "validated").is_file())
        self.assertEqual(
            self.github_path.read_text().splitlines()[0],
            f"{installed}:{self.environment['PATH']}",
        )
        self.assertIn("vmaf-ffmpeg.tar.xz: OK", output.stdout)

    def test_corrupted_archive_never_changes_the_path(self):
        self.environment["CORRUPT"] = "1"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer()
        self.assertEqual(self.github_path.read_text(), "")
        self.assertFalse((self.runner / "vmaf-ffmpeg" / "bin" / "ffmpeg").exists())


if __name__ == "__main__":
    unittest.main()
