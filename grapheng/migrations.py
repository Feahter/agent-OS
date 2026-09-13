import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Tuple

from ._store import atomic_json_write
from .errors import ContractViolation
from .reuse import VerifiedReuseRecord

BUNDLE_SCHEMA_VERSION = 2
_BUNDLE_KIND = "grapheng-agent-os-bundle"


def _canonical(value: Any) -> Tuple[Any, str]:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ContractViolation(
            f"bundle migration value must be JSON serializable: {error}"
        ) from error
    return json.loads(encoded), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationStep:
    converter_id: str
    from_version: int
    to_version: int
    changed_paths: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["changed_paths"] = list(self.changed_paths)
        return value


@dataclass(frozen=True)
class MigrationReport:
    source_version: int
    target_version: int
    steps: Tuple[MigrationStep, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "steps": [step.to_dict() for step in self.steps],
        }


BundleConverter = Callable[[Path, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class _RegisteredConverter:
    converter_id: str
    from_version: int
    to_version: int
    convert: BundleConverter


class BundleMigrationRegistry:
    """Runs an unambiguous, observable bundle conversion chain in staging."""

    def __init__(self):
        self._converters: Dict[int, _RegisteredConverter] = {}

    def register(
        self,
        converter_id: str,
        from_version: int,
        to_version: int,
        converter: BundleConverter,
    ) -> None:
        if not isinstance(converter_id, str) or not converter_id.strip():
            raise ContractViolation("bundle converter_id cannot be empty")
        if (
            isinstance(from_version, bool)
            or not isinstance(from_version, int)
            or isinstance(to_version, bool)
            or not isinstance(to_version, int)
            or from_version < 1
            or to_version != from_version + 1
        ):
            raise ContractViolation("bundle converters must advance exactly one version")
        if from_version in self._converters:
            raise ContractViolation(
                f"bundle schema {from_version} already has a converter"
            )
        self._converters[from_version] = _RegisteredConverter(
            converter_id.strip(), from_version, to_version, converter
        )

    def source_version(
        self, manifest: Mapping[str, Any], target_version: int
    ) -> int:
        if manifest.get("kind") != _BUNDLE_KIND:
            raise ContractViolation("invalid Agent OS bundle kind")
        version = manifest.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ContractViolation(f"unsupported Agent OS bundle schema: {version}")
        if version > target_version:
            raise ContractViolation(f"unsupported Agent OS bundle schema: {version}")
        current = version
        while current < target_version:
            converter = self._converters.get(current)
            if converter is None:
                raise ContractViolation(
                    f"no Agent OS bundle converter from schema {current}"
                )
            current = converter.to_version
        return version

    def compatible_source_versions(self, target_version: int) -> Tuple[int, ...]:
        if (
            isinstance(target_version, bool)
            or not isinstance(target_version, int)
            or target_version < 1
        ):
            raise ContractViolation("bundle target schema must be a positive integer")
        supported = []
        for source_version in range(1, target_version + 1):
            current = source_version
            while current < target_version:
                converter = self._converters.get(current)
                if converter is None:
                    break
                current = converter.to_version
            if current == target_version:
                supported.append(source_version)
        return tuple(supported)

    def migrate(
        self,
        staging: Path,
        manifest: Mapping[str, Any],
        target_version: int,
    ) -> Tuple[Mapping[str, Any], MigrationReport]:
        source_version = self.source_version(manifest, target_version)
        current = dict(manifest)
        version = source_version
        steps = []
        while version < target_version:
            registered = self._converters[version]
            before = self._snapshot(staging)
            try:
                converted = registered.convert(staging, current)
            except ContractViolation:
                raise
            except Exception as error:
                raise ContractViolation(
                    f"bundle converter {registered.converter_id} failed"
                ) from error
            if not isinstance(converted, Mapping):
                raise ContractViolation(
                    f"bundle converter {registered.converter_id} returned no manifest"
                )
            current = dict(converted)
            if (
                current.get("kind") != _BUNDLE_KIND
                or current.get("schema_version") != registered.to_version
            ):
                raise ContractViolation(
                    f"bundle converter {registered.converter_id} produced invalid schema"
                )
            after = self._snapshot(staging)
            changed_paths = tuple(
                sorted(
                    path
                    for path in set(before) | set(after)
                    if before.get(path) != after.get(path)
                )
            )
            step = MigrationStep(
                registered.converter_id,
                registered.from_version,
                registered.to_version,
                changed_paths,
            )
            history = current.get("migration_history", ())
            if not isinstance(history, (list, tuple)) or any(
                not isinstance(item, dict) for item in history
            ):
                raise ContractViolation("bundle migration_history must be an array")
            current["migration_history"] = [*history, step.to_dict()]
            steps.append(step)
            version = registered.to_version

        history = current.get("migration_history", ())
        if not isinstance(history, (list, tuple)) or any(
            not isinstance(item, dict) for item in history
        ):
            raise ContractViolation("bundle migration_history must be an array")
        current["migration_history"] = list(history)
        return current, MigrationReport(source_version, target_version, tuple(steps))

    @staticmethod
    def _snapshot(root: Path) -> Mapping[str, str]:
        result = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ContractViolation("bundle converter staging cannot contain symlinks")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result


def default_bundle_migrations() -> BundleMigrationRegistry:
    registry = BundleMigrationRegistry()
    registry.register("bundle-v1-to-v2", 1, 2, _bundle_v1_to_v2)
    return registry


def _bundle_v1_to_v2(
    staging: Path, manifest: Mapping[str, Any]
) -> Mapping[str, Any]:
    for path in sorted((staging / "reuse" / "entries").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(
                f"cannot migrate verified reuse entry {path.name}: {error}"
            ) from error
        if not isinstance(value, dict):
            raise ContractViolation("verified reuse entry must be an object")
        VerifiedReuseRecord.from_dict(value)
        key_fields = value.get("key_fields")
        if not isinstance(key_fields, dict):
            raise ContractViolation("verified reuse key_fields must be an object")
        if "max_cost_usd" in key_fields:
            continue
        migrated_fields = dict(key_fields)
        migrated_fields["max_cost_usd"] = None
        _, migrated_key = _canonical(migrated_fields)
        migrated = dict(value)
        migrated["key"] = migrated_key
        migrated["key_fields"] = migrated_fields
        migrated.pop("checksum", None)
        _, checksum = _canonical(migrated)
        migrated["checksum"] = checksum
        VerifiedReuseRecord.from_dict(migrated)
        target = path.with_name(f"{migrated_key}.json")
        if target != path and target.exists():
            raise ContractViolation(
                f"bundle migration reuse entry collision: {target.name}"
            )
        atomic_json_write(target, migrated)
        if target != path:
            path.unlink()

    excludes = manifest.get("excludes", ())
    if not isinstance(excludes, (list, tuple)) or any(
        not isinstance(item, str) for item in excludes
    ):
        raise ContractViolation("bundle excludes must be an array of strings")
    migrated_manifest = dict(manifest)
    migrated_manifest["schema_version"] = 2
    migrated_manifest["state_schema_version"] = 1
    migrated_manifest["excludes"] = list(
        dict.fromkeys(
            [
                *excludes,
                "runtime-leases",
                "publication-receipts",
                "routing-governance-runtime",
            ]
        )
    )
    return migrated_manifest
