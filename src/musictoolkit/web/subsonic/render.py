"""The Subsonic reply: one structure, written as XML or as JSON.

A reply is built as nested dicts and lists (what JSON is). The XML writer follows the rule Subsonic's own XML and JSON follow: the plain values of a dict
(strings, numbers, booleans) are the attributes of its element; a dict or a list inside it is a child element (a list is one child element for each of its
items, all named by the key); and the key `value` is the element's text. A value that is None is left out of both, so that a client that decodes strictly never
meets a null where it expects a string.
"""

from __future__ import annotations

import json
import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from musictoolkit import __version__

API_VERSION = "1.16.1"  # the version of the Subsonic API this server speaks
NAMESPACE = "http://subsonic.org/restapi"
SERVER_TYPE = "musictoolkit"

# What XML 1.0 does not allow in a document at all (a tag in a song's title can hold anything): dropped, not escaped.
_NOT_XML = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")
_CALLBACK = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$")


def envelope(payload: dict[str, Any] | None = None, *, failed: bool = False) -> dict[str, Any]:
    """The `subsonic-response` itself: its status and versions, and what the call answers with."""
    return {
        "status": "failed" if failed else "ok",
        "version": API_VERSION,
        "type": SERVER_TYPE,
        "serverVersion": __version__,
        "openSubsonic": True,
        **(payload or {}),
    }


def failure(code: int, message: str) -> dict[str, Any]:
    return envelope({"error": {"code": code, "message": message}}, failed=True)


def _text(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return _NOT_XML.sub("", str(value))


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value if item is not None]
    return value


def _element(tag: str, value: Any, out: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _element(tag, item, out)
        return
    if not isinstance(value, dict):
        out.append(f"<{tag}>{escape(_text(value))}</{tag}>")
        return
    attributes = "".join(f" {name}={quoteattr(_text(item))}" for name, item in value.items() if name != "value" and not isinstance(item, (dict, list)))
    children = [(name, item) for name, item in value.items() if isinstance(item, (dict, list))]
    text = value.get("value")
    if text is None and not children:
        out.append(f"<{tag}{attributes}/>")
        return
    out.append(f"<{tag}{attributes}>")
    if text is not None:
        out.append(escape(_text(text)))
    for name, item in children:
        _element(name, item, out)
    out.append(f"</{tag}>")


def to_xml(reply: dict[str, Any]) -> bytes:
    out: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    clean = _without_none(reply)
    body: list[str] = []
    _element("subsonic-response", clean, body)
    out.append(body[0].replace("<subsonic-response", f'<subsonic-response xmlns="{NAMESPACE}"', 1))
    out.extend(body[1:])
    return "".join(out).encode("utf-8")


def to_json(reply: dict[str, Any], callback: str | None = None) -> bytes:
    text = json.dumps({"subsonic-response": _without_none(reply)}, ensure_ascii=False, separators=(",", ":"))
    if callback:
        if not _CALLBACK.match(callback):
            raise ValueError("not a function name")
        text = f"{callback}({text})"
    return text.encode("utf-8")
