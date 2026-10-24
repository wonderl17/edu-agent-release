"""Mock the build boundary while checking actual Production archive metadata."""
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import tencent_production as target


class ProductionBundleTests(unittest.TestCase):
    def test_build_uses_formal_environment_and_fixed_image_namespace(self):
        sha = "a" * 40
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory); source = private / "source"; source.mkdir()
            def run(command, _child, _log, **_kwargs):
                commands.append(command)
                if command[:3] == ["docker", "image", "inspect"]:
                    return ("sha256:" + "b" * 64).encode()
                if command[:2] == ["docker", "save"]:
                    Path(command[3]).write_bytes(b"synthetic docker save")
                if command[0] == "zstd":
                    Path(command[-1]).write_bytes(b"synthetic compressed image")
                if command[:2] == ["docker", "create"]:
                    return ("c" * 64).encode()
                if command[:2] == ["docker", "cp"]:
                    web = Path(command[-1]); web.mkdir()
                    (web / "version.json").write_text(json.dumps({"version": "0.0.125", "buildSha": sha}))
                    (web / "index.html").write_text("synthetic public web")
                return b""
            with patch.object(target, "prepare_build_disk"), patch.object(target, "run", side_effect=run), \
                    patch.object(target, "saved_config_ids", side_effect=lambda _p, images, _sha: {k: images[k] for k in ("api", "worker")}):
                bundle = target.build_bundle(source, sha, "0.0.125", private, {}, None)
            webbuild = [command for command in commands if command[:2] == ["docker", "build"] and "Dockerfile.web" in command][0]
            self.assertIn("VITE_API_BASE_URL=/api", webbuild)
            self.assertIn("VITE_DEPLOY_ENV=shukezhinao-prod", webbuild)
            self.assertIn("VITE_BUILD_SHA=" + sha, webbuild)
            self.assertNotIn("VITE_DEPLOY_ENV=tencent-validation", webbuild)
            for command in (c for c in commands if c[:2] == ["docker", "build"]):
                kind = command[command.index("--file") + 1].split(".")[1]
                self.assertEqual(command[command.index("--tag") + 1], f"teaching-ai-production/{kind}:{sha}")
            with tarfile.open(bundle, "r:") as archive:
                self.assertEqual(archive.getnames(), ["manifest.json", "images.tar.zst", "web.tar.gz"])
                manifest = json.loads(archive.extractfile("manifest.json").read())
                self.assertEqual(manifest["deployment"], "teaching-production")
                self.assertEqual((manifest["sourceRepository"], manifest["sourceSha"], manifest["version"]),
                                 (target.SOURCE, sha, "0.0.125"))
                self.assertTrue(all(not member.pax_headers for member in archive.getmembers()))
            self.assertEqual(set(manifest["images"]), {"api", "worker"})
            for name, metadata in manifest["files"].items():
                self.assertEqual(metadata, target.digest(private / "bundle" / name))


if __name__ == "__main__":
    unittest.main()
