"""Check a Subsonic answer against the JSON schemas that the OpenSubsonic project publishes for its API (a copy of them is in tests/fixtures/opensubsonic)."""

from __future__ import annotations

import json
import posixpath
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

TREE = Path(__file__).parent / "fixtures" / "opensubsonic"
PREFIX = "file:///openapi/"  # (the schemas refer to each other by relative paths: this is where those are taken from)
FAILURE = PREFIX + "schemas/SubsonicResponse/SubsonicFailureResponse.json"


def _retrieve(uri: str) -> Resource:
    path = TREE / uri.removeprefix(PREFIX)
    return Resource.from_contents(json.loads(path.read_text(encoding="utf-8")), default_specification=DRAFT202012)


REGISTRY = Registry(retrieve=_retrieve)  # type: ignore[call-arg]


@lru_cache(maxsize=None)
def schema_of(name: str) -> str | None:
    """Where the schema of the JSON answer to call `name` is (None for a call that answers with a file, or that the schemas do not know)."""
    path = TREE / "endpoints" / f"{name}.json"
    if not path.exists():
        return None
    response = json.loads(path.read_text(encoding="utf-8"))["get"]["responses"]["200"]
    if "$ref" in response:  # (an answer the schemas share between calls: the one with nothing in it, the one that is a file)
        target = posixpath.normpath(posixpath.join("endpoints", response["$ref"]))
        shared = json.loads((TREE / target).read_text(encoding="utf-8"))
        return PREFIX + target + "#/content/application~1json/schema" if "application/json" in shared.get("content", {}) else None
    return PREFIX + posixpath.normpath(posixpath.join("endpoints", response["content"]["application/json"]["schema"]["$ref"]))


def problems(reference: str, document: dict[str, Any]) -> list[str]:
    """What is wrong with this answer, by the schema at `reference`; nothing when it is as the protocol says."""
    validator = Draft202012Validator({"$ref": reference}, registry=REGISTRY)
    found = []
    for error in sorted(validator.iter_errors(document), key=lambda e: [str(part) for part in e.absolute_path]):
        found.append(f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message[:200]}")
    return found
