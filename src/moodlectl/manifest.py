from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import moodlectlError
from .models import ResourceSpec


@dataclass(frozen=True)
class Batch:
    course: int
    section: str | int
    resources: tuple[ResourceSpec, ...]


def load_manifest(path: str | Path) -> Batch:
    try:
        root = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise moodlectlError(f"Cannot read YAML manifest: {exc}") from exc
    if not isinstance(root, dict):
        raise moodlectlError("Manifest must be a YAML mapping")
    if not isinstance(root.get("course"), int) or root["course"] <= 0:
        raise moodlectlError("Manifest requires numeric 'course' Moodle ID")
    section = root.get("section")
    if not isinstance(section, (str, int)) or str(section).strip() == "":
        raise moodlectlError("Manifest requires 'section' name or number")
    values = root.get("resources")
    if not isinstance(values, list):
        raise moodlectlError("Manifest requires a 'resources' list")
    fields = set(ResourceSpec.__dataclass_fields__)
    specs = []
    for i, item in enumerate(values):
        if not isinstance(item, dict):
            raise moodlectlError(f"Resource {i} must be a mapping")
        unknown = set(item) - fields
        if unknown:
            raise moodlectlError(f"Resource {i}: unknown fields: {sorted(unknown)}")
        if isinstance(item.get("file"), str):
            item = {**item, "file": Path(path).parent / item["file"]}  # relative to the manifest
        try:
            spec = ResourceSpec(**item)
            spec.validate()
        except (TypeError, ValueError) as exc:
            raise moodlectlError(f"Invalid resource {i}: {exc}") from exc
        specs.append(spec)
    seen = set()
    for spec in specs:
        key = (spec.kind, spec.name.casefold())
        if key in seen:
            raise moodlectlError(f"Duplicate resource declared in manifest: {spec.name}")
        seen.add(key)
    return Batch(course=root["course"], section=section, resources=tuple(specs))
