#!/usr/bin/env python3
"""Generate release SBOM and checksums without adding a build dependency."""

import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path

_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_FIELD = r'^{}\s*=\s*"([^"]+)"\s*$'


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_packages(lock_path, project_name):
    text = lock_path.read_text(encoding="utf-8")
    packages = {}
    for block in text.split("[[package]]")[1:]:
        name_match = re.search(_FIELD.format("name"), block, re.MULTILINE)
        version_match = re.search(_FIELD.format("version"), block, re.MULTILINE)
        if name_match is None or version_match is None:
            continue
        name = name_match.group(1)
        version = version_match.group(1)
        if name == project_name or re.search(
            r'^source\s*=\s*\{\s*virtual\s*=', block, re.MULTILINE
        ):
            continue
        digest_match = re.search(r'hash\s*=\s*"sha256:([0-9a-f]{64})"', block)
        packages[(name, version)] = (
            digest_match.group(1) if digest_match is not None else None
        )
    return packages


def _library_component(name, version, digest):
    purl_name = name.lower().replace("_", "-")
    purl = f"pkg:pypi/{purl_name}@{version}"
    component = {
        "type": "library",
        "bom-ref": purl,
        "name": name,
        "version": version,
        "purl": purl,
        "scope": "optional",
        "properties": [
            {"name": "agent-os:lock-scope", "value": "full-uv-lock"}
        ],
    }
    if digest is not None:
        component["hashes"] = [{"alg": "SHA-256", "content": digest}]
    return component


def _artifact_component(path):
    digest = _sha256(path)
    return {
        "type": "file",
        "bom-ref": f"artifact:{path.name}:{digest}",
        "name": path.name,
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "agent-os:release-artifact", "value": "true"}
        ],
    }


def generate(
    *,
    lock_path,
    dist_dir,
    output_path,
    checksums_path,
    project_name,
    project_version,
    source_commit,
    source_repository,
):
    lock_path = Path(lock_path)
    dist_dir = Path(dist_dir)
    output_path = Path(output_path)
    checksums_path = Path(checksums_path)
    if _COMMIT.fullmatch(source_commit) is None:
        raise ValueError("source commit must be a 40- or 64-character lowercase hex digest")
    if not lock_path.is_file():
        raise ValueError("uv lockfile does not exist")
    if not dist_dir.is_dir():
        raise ValueError("distribution directory does not exist")

    artifact_paths = sorted(
        {
            *dist_dir.glob("*.whl"),
            *dist_dir.glob("*.tar.gz"),
        },
        key=lambda path: path.name,
    )
    if not artifact_paths:
        raise ValueError("distribution directory has no wheel or source archive")
    packages = _locked_packages(lock_path, project_name)
    libraries = [
        _library_component(name, version, digest)
        for (name, version), digest in sorted(packages.items())
    ]
    artifacts = [_artifact_component(path) for path in artifact_paths]
    root_ref = f"pkg:pypi/{project_name.lower().replace('_', '-')}@{project_version}"
    identity = "|".join(
        [source_repository, source_commit]
        + [item["bom-ref"] for item in libraries + artifacts]
    )
    document = {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": project_name,
                "version": project_version,
                "purl": root_ref,
                "externalReferences": [
                    {"type": "vcs", "url": source_repository}
                ],
                "properties": [
                    {"name": "agent-os:source-commit", "value": source_commit},
                    {"name": "agent-os:lockfile", "value": lock_path.name},
                ],
            },
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "agent-os-sbom-generator",
                        "version": "1",
                    }
                ]
            },
        },
        "components": libraries + artifacts,
        "dependencies": [
            {
                "ref": root_ref,
                "dependsOn": [item["bom-ref"] for item in libraries],
            }
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksum_paths = [*artifact_paths, output_path]
    checksums_path.write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in checksum_paths),
        encoding="utf-8",
    )
    return {
        "sbom": str(output_path),
        "checksums": str(checksums_path),
        "source_commit": source_commit,
        "artifacts": len(artifact_paths),
        "locked_dependencies": len(libraries),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--project-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-repository", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            generate(
                lock_path=args.lock,
                dist_dir=args.dist,
                output_path=args.output,
                checksums_path=args.checksums,
                project_name=args.project_name,
                project_version=args.project_version,
                source_commit=args.source_commit,
                source_repository=args.source_repository,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
