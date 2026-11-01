"""Exercise the Windows packaging arguments without building an executable."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows packaging script")
class DriveBuildTests(unittest.TestCase):
    def test_build_embeds_only_validated_desktop_app_fields(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="gpdf build test ") as directory:
            project = Path(directory)
            shutil.copyfile(root / "build.ps1", project / "build.ps1")
            stub = project / "PyInstaller"
            stub.mkdir()
            (stub / "__init__.py").write_text("", encoding="utf-8")
            (stub / "__main__.py").write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "Path('captured.json').write_text(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            config = project / "client with spaces.json"
            config.write_text(json.dumps({"installed": {
                "client_id": "test-client.apps.googleusercontent.com",
                "client_secret": "desktop-public-config",
                "refresh_token": "must-not-be-bundled",
                "token_uri": "https://untrusted.example/token",
            }}), encoding="utf-8")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project) + os.pathsep + str(root)
            result = subprocess.run([
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(project / "build.ps1"), "-Python", sys.executable,
                "-GoogleOAuthClient", str(config),
            ], cwd=project, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = json.loads((project / "captured.json").read_text())
            staged, destination = arguments[arguments.index("--add-data") + 1].split(";")
            self.assertEqual(destination, "gpdf")
            self.assertEqual(Path(staged).name, "google_oauth_client.json")
            payload = json.loads(Path(staged).read_text())
            self.assertEqual(payload, {"installed": {
                "client_id": "test-client.apps.googleusercontent.com",
                "client_secret": "desktop-public-config",
            }})


if __name__ == "__main__":
    unittest.main()
