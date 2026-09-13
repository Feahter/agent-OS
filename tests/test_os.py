import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import (
    BUNDLE_SCHEMA_VERSION,
    AgentOS,
    AgentRequest,
    AgentResult,
    BundleMigrationRegistry,
    ContractViolation,
    ProviderPolicy,
)


def _digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rewrite_manifest_files(bundle, manifest):
    files = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(bundle).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    manifest["files"] = files
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _downgrade_bundle_to_v1(bundle):
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    renamed = []
    for path in sorted((bundle / "reuse" / "entries").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        fields = dict(value["key_fields"])
        fields.pop("max_cost_usd", None)
        old_key = _digest(fields)
        value["key"] = old_key
        value["key_fields"] = fields
        value.pop("checksum")
        value["checksum"] = _digest(value)
        target = path.with_name(f"{old_key}.json")
        target.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        if target != path:
            path.unlink()
        renamed.append((old_key, path.stem))
    manifest["schema_version"] = 1
    manifest.pop("state_schema_version", None)
    manifest.pop("migration_history", None)
    manifest["excludes"] = [
        item
        for item in manifest.get("excludes", ())
        if item not in ("runtime-leases", "publication-receipts")
    ]
    _rewrite_manifest_files(bundle, manifest)
    return tuple(renamed)


class AgentOSTests(unittest.TestCase):
    def test_root_layout_status_and_portable_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source", clock=lambda: 100.0)
            workspace = root / "workspace"
            workspace.mkdir()
            request = AgentRequest(
                "run-1:node:1",
                "private prompt",
                {"private_input": "secret-input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            result = AgentResult(
                "memory", {"answer": "verified-result"}, "done", 7, 0.2
            )
            source.reuse_store().publish_verified(
                request, result, "run-1", "reality-anchor-1", 0.9
            )
            source_loop = source.router()
            self.assertIsNotNone(source_loop)
            status = source.status()
            layout = {path.name for path in source.root.iterdir()}
            bundle = source.export_bundle(root / "bundle")
            target = AgentOS(root / "target", clock=lambda: 200.0)

            imported = target.import_bundle(bundle)
            target_status = target.status()
            bundle_manifest = json.loads(
                (bundle / "manifest.json").read_text(encoding="utf-8")
            )
            bundle_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in bundle.rglob("*")
                if path.is_file()
            )

        self.assertEqual(
            {
                "learning",
                "optimization",
                "reuse",
                "approvals",
                "routing",
                "manifest.json",
            },
            layout,
        )
        self.assertEqual(1, status["reuse"]["entries"]["valid"])
        self.assertEqual(1, target_status["reuse"]["entries"]["valid"])
        self.assertEqual(BUNDLE_SCHEMA_VERSION, bundle_manifest["schema_version"])
        self.assertEqual(1, bundle_manifest["state_schema_version"])
        self.assertIn("runtime-leases", bundle_manifest["excludes"])
        self.assertIn("publication-receipts", bundle_manifest["excludes"])
        self.assertIn("routing-governance-runtime", bundle_manifest["excludes"])
        self.assertIn("controlled-merge-runtime", bundle_manifest["excludes"])
        self.assertEqual(2, imported["imported_files"])
        self.assertEqual([], imported["migration"]["steps"])
        self.assertNotIn("private prompt", bundle_text)
        self.assertNotIn("secret-input", bundle_text)
        self.assertIn("verified-result", bundle_text)

    def test_export_rejects_unmanaged_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "state")
            (agent_os.root / "credentials.json").write_text(
                '{"token":"do-not-export"}', encoding="utf-8"
            )

            with self.assertRaisesRegex(ContractViolation, "unmanaged file"):
                agent_os.export_bundle(root / "bundle")

    def test_export_excludes_active_singleflight_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "state")
            workspace = root / "workspace"
            workspace.mkdir()
            cache = agent_os.reuse_store()
            request = AgentRequest(
                "active-task",
                "transient prompt",
                {"private_input": "transient input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            started = threading.Event()
            release = threading.Event()
            results = []

            def load():
                started.set()
                release.wait(2)
                return AgentResult("memory", {"answer": "done"}, "done", 3, 0.1)

            worker = threading.Thread(
                target=lambda: results.append(cache.resolve(request, "memory", load))
            )
            worker.start()
            self.assertTrue(started.wait(1))
            try:
                status = agent_os.status()
                bundle = agent_os.export_bundle(root / "bundle")
                bundle_text = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in bundle.rglob("*")
                    if path.is_file()
                )
            finally:
                release.set()
                worker.join(2)

        self.assertEqual(1, status["reuse"]["singleflight"]["running"])
        self.assertEqual("miss", results[0].reuse_status)
        self.assertNotIn("flights", bundle_text)
        self.assertNotIn("transient prompt", bundle_text)
        self.assertNotIn("transient input", bundle_text)

    def test_routing_governance_is_projected_but_excluded_from_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "state", clock=lambda: 100.0)
            agent_os.governance_store().admit(
                "provider",
                ProviderPolicy(requests_per_minute=2),
                reserve=True,
            )

            status = agent_os.status()
            bundle = agent_os.export_bundle(root / "bundle")
            manifest = json.loads(
                (bundle / "manifest.json").read_text(encoding="utf-8")
            )
            paths = {item["path"] for item in manifest["files"]}

        self.assertEqual(
            1,
            status["routing"]["providers"]["provider"][
                "requests_last_minute"
            ],
        )
        self.assertFalse(any(path.startswith("routing/") for path in paths))
        self.assertIn("routing-governance-runtime", manifest["excludes"])

    def test_import_rejects_nonempty_target_and_checksum_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source")
            source.router()
            (source.learning_root / "history.json").write_text("[]", encoding="utf-8")
            bundle = source.export_bundle(root / "bundle")
            target = AgentOS(root / "target")
            (target.learning_root / "history.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ContractViolation, "no existing state"):
                target.import_bundle(bundle)

            clean = AgentOS(root / "clean")
            (bundle / "learning" / "history.json").write_text(
                '[{"tampered":true}]', encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractViolation, "checksum mismatch"):
                clean.import_bundle(bundle)

    def test_import_rejects_unsafe_and_future_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            manifest = {
                "kind": "grapheng-agent-os-bundle",
                "schema_version": 1,
                "files": [
                    {"path": "../escape.json", "sha256": "0" * 64, "size": 0}
                ],
            }
            (bundle / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            target = AgentOS(root / "target")

            with self.assertRaisesRegex(ContractViolation, "unsafe"):
                target.import_bundle(bundle)

            manifest["files"] = []
            manifest["schema_version"] = 99
            (bundle / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractViolation, "unsupported"):
                target.import_bundle(bundle)

            manifest["schema_version"] = BUNDLE_SCHEMA_VERSION
            manifest["state_schema_version"] = 99
            manifest["migration_history"] = []
            (bundle / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractViolation, "state schema"):
                target.import_bundle(bundle)

    def test_import_migrates_v1_reuse_key_and_persists_path_free_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source", clock=lambda: 100.0)
            workspace = root / "workspace"
            workspace.mkdir()
            request = AgentRequest(
                "run-1:node:1",
                "private prompt",
                {"private_input": "secret-input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            result = AgentResult(
                "memory", {"answer": "verified-result"}, "done", 7, 0.2
            )
            current = source.reuse_store().publish_verified(
                request, result, "run-1", "reality-anchor-1", 0.9
            )
            bundle = source.export_bundle(root / "bundle")
            renamed = _downgrade_bundle_to_v1(bundle)
            target = AgentOS(root / "target", clock=lambda: 200.0)

            imported = target.import_bundle(bundle)
            migrated_path = target.reuse_root / "entries" / f"{current.key}.json"
            migrated = json.loads(migrated_path.read_text(encoding="utf-8"))
            root_manifest = json.loads(
                (target.root / "manifest.json").read_text(encoding="utf-8")
            )
            old_entry_exists = (
                target.reuse_root / "entries" / f"{renamed[0][0]}.json"
            ).exists()

        self.assertEqual(1, imported["source_bundle_schema_version"])
        self.assertEqual(2, imported["bundle_schema_version"])
        self.assertEqual("bundle-v1-to-v2", imported["migration"]["steps"][0]["converter_id"])
        self.assertEqual(None, migrated["key_fields"]["max_cost_usd"])
        self.assertEqual(current.key, migrated["key"])
        self.assertFalse(old_entry_exists)
        audit_text = json.dumps(root_manifest["last_import"], sort_keys=True)
        self.assertNotIn(str(root), audit_text)
        self.assertEqual(imported["migration"], {
            key: value
            for key, value in root_manifest["last_import"].items()
            if key != "imported_at"
        })

    def test_import_rejects_converter_gap_and_converter_failure_without_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source")
            bundle = source.export_bundle(root / "bundle")
            _downgrade_bundle_to_v1(bundle)
            missing = AgentOS(
                root / "missing", migrations=BundleMigrationRegistry()
            )

            with self.assertRaisesRegex(ContractViolation, "no Agent OS bundle converter"):
                missing.import_bundle(bundle)

            failing_registry = BundleMigrationRegistry()

            def fail_converter(staging, manifest):
                (staging / "learning" / "partial.json").write_text(
                    "{}", encoding="utf-8"
                )
                raise RuntimeError("private converter failure")

            failing_registry.register("failing-v1-to-v2", 1, 2, fail_converter)
            failing = AgentOS(root / "failing", migrations=failing_registry)
            with self.assertRaisesRegex(ContractViolation, "converter .* failed"):
                failing.import_bundle(bundle)

            self.assertEqual((), failing._state_files())
            self.assertNotIn("last_import", json.loads(
                (failing.root / "manifest.json").read_text(encoding="utf-8")
            ))

    def test_import_commit_failure_restores_original_empty_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source")
            (source.learning_root / "history.json").write_text("[]", encoding="utf-8")
            bundle = source.export_bundle(root / "bundle")
            target = AgentOS(root / "target", clock=lambda: 50.0)
            original_manifest = (target.root / "manifest.json").read_bytes()
            replacements = 0

            def fail_second_replace(source_path, target_path):
                nonlocal replacements
                replacements += 1
                if replacements == 2:
                    raise OSError("simulated directory commit failure")
                os.replace(str(source_path), str(target_path))

            with patch.object(
                target, "_replace_directory", side_effect=fail_second_replace
            ):
                with self.assertRaisesRegex(ContractViolation, "directory commit failed"):
                    target.import_bundle(bundle)

            self.assertEqual(original_manifest, (target.root / "manifest.json").read_bytes())
            self.assertEqual((), target._state_files())
            self.assertIsNone(target.status()["last_import"])

    def test_import_rejects_active_and_invalid_singleflight_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source")
            bundle = source.export_bundle(root / "bundle")
            target = AgentOS(root / "target")
            workspace = root / "workspace"
            workspace.mkdir()
            request = AgentRequest(
                "active-task",
                "transient prompt",
                {"private_input": "transient input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            started = threading.Event()
            release = threading.Event()
            result = []

            def load():
                started.set()
                release.wait(2)
                return AgentResult("memory", {"answer": "done"}, "done", 3, 0.1)

            cache = target.reuse_store()
            worker = threading.Thread(
                target=lambda: result.append(cache.resolve(request, "memory", load))
            )
            worker.start()
            self.assertTrue(started.wait(1))
            try:
                with self.assertRaisesRegex(ContractViolation, "active or invalid"):
                    target.import_bundle(bundle)
            finally:
                release.set()
                worker.join(2)

            invalid_target = AgentOS(root / "invalid-target")
            states = invalid_target.reuse_root / "flights" / "states"
            states.mkdir(parents=True, exist_ok=True)
            (states / f"{'0' * 64}.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ContractViolation, "active or invalid"):
                invalid_target.import_bundle(bundle)
            self.assertEqual("done", result[0].outputs["answer"])

    def test_import_rejects_active_or_invalid_routing_governance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = AgentOS(root / "source")
            bundle = source.export_bundle(root / "bundle")

            active = AgentOS(root / "active", clock=lambda: 100.0)
            active.governance_store().admit(
                "provider", ProviderPolicy(), reserve=True
            )
            with self.assertRaisesRegex(
                ContractViolation, "active provider governance"
            ):
                active.import_bundle(bundle)

            invalid = AgentOS(root / "invalid")
            state_path = invalid.routing_root / "state.json"
            state_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                ContractViolation, "provider governance"
            ):
                invalid.import_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
