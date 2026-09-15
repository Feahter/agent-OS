import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_generator():
    path = PROJECT_ROOT / "scripts" / "generate_sbom.py"
    spec = importlib.util.spec_from_file_location("generate_sbom", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SupplyChainMetadataTests(unittest.TestCase):
    def test_sbom_binds_locked_dependencies_artifacts_and_source_commit(self):
        generator = load_generator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "uv.lock"
            lock.write_text(
                """version = 1

[[package]]
name = "example-dependency"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.invalid/example.tar.gz", hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }

[[package]]
name = "graph-engineering-agent-os"
version = "0.0.1"
source = { virtual = "." }
""",
                encoding="utf-8",
            )
            dist = root / "dist"
            dist.mkdir()
            wheel = dist / "graph_engineering_agent_os-0.0.1-py3-none-any.whl"
            source = dist / "graph_engineering_agent_os-0.0.1.tar.gz"
            wheel.write_bytes(b"wheel")
            source.write_bytes(b"sdist")
            output = dist / "agent-os.cdx.json"
            checksums = dist / "SHA256SUMS"

            result = generator.generate(
                lock_path=lock,
                dist_dir=dist,
                output_path=output,
                checksums_path=checksums,
                project_name="graph-engineering-agent-os",
                project_version="0.0.1",
                source_commit="a" * 40,
                source_repository="https://github.com/example/agent-os",
            )

            sbom = json.loads(output.read_text(encoding="utf-8"))
            component = sbom["metadata"]["component"]
            packages = [
                item for item in sbom["components"] if item["type"] == "library"
            ]
            artifacts = [
                item for item in sbom["components"] if item["type"] == "file"
            ]
            checksum_lines = checksums.read_text(encoding="utf-8").splitlines()

        self.assertEqual("CycloneDX", sbom["bomFormat"])
        self.assertEqual("1.6", sbom["specVersion"])
        self.assertIn(
            {"name": "agent-os:source-commit", "value": "a" * 40},
            component["properties"],
        )
        self.assertEqual(
            ["pkg:pypi/example-dependency@1.2.3"],
            [item["purl"] for item in packages],
        )
        self.assertEqual(
            {
                "graph_engineering_agent_os-0.0.1-py3-none-any.whl",
                "graph_engineering_agent_os-0.0.1.tar.gz",
            },
            {item["name"] for item in artifacts},
        )
        self.assertTrue(all(item["hashes"] for item in artifacts))
        self.assertEqual(3, len(checksum_lines))
        self.assertEqual(
            {
                "agent-os.cdx.json",
                "graph_engineering_agent_os-0.0.1-py3-none-any.whl",
                "graph_engineering_agent_os-0.0.1.tar.gz",
            },
            {line.split("  ", 1)[1] for line in checksum_lines},
        )
        self.assertEqual(2, result["artifacts"])
        self.assertEqual(1, result["locked_dependencies"])

    def test_invalid_source_commit_is_rejected(self):
        generator = load_generator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (root / "dist").mkdir()
            with self.assertRaisesRegex(ValueError, "source commit"):
                generator.generate(
                    lock_path=root / "uv.lock",
                    dist_dir=root / "dist",
                    output_path=root / "dist" / "sbom.json",
                    checksums_path=root / "dist" / "SHA256SUMS",
                    project_name="project",
                    project_version="1.0.0",
                    source_commit="not-a-commit",
                    source_repository="https://example.invalid/project",
                )

    def test_release_workflow_separates_test_signing_and_publish_permissions(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        threat_model = (PROJECT_ROOT / "docs" / "threat-model.md").read_text(
            encoding="utf-8"
        )
        security = (PROJECT_ROOT / "SECURITY.md").read_text(encoding="utf-8")

        self.assertIn("permissions: {}", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("generate_sbom.py", workflow)
        self.assertIn(
            "uv sync --frozen --group build --python 3.12",
            workflow,
        )
        self.assertNotIn("--only-group dev --group build", workflow)
        self.assertIn("不是 OS 沙箱", threat_model)
        self.assertIn("不是凭据保险库", threat_model)
        self.assertIn("多租户", threat_model)
        self.assertIn("private vulnerability reporting", security)


if __name__ == "__main__":
    unittest.main()
