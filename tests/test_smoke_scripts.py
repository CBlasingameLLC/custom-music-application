"""The pure helpers inside the Windows-only smoke scripts, so a slip in them fails here and not after a 10-minute build."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def smoke_update():
    spec = importlib.util.spec_from_file_location("smoke_update_under_test", SCRIPTS / "smoke_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("stamped", "expected"),
    [
        ("0.3.0.0\r\n", "0.3.0"),  # Windows writes four parts; the app and latest.yml say three
        ("0.0.1.0", "0.0.1"),
        ("0.3.0", "0.3.0"),
        ("12.40.7.3", "12.40.7"),
        ("", ""),  # the exe is missing or PowerShell failed: report nothing, don't crash
    ],
)
def test_the_stamped_file_version_is_compared_as_semver(smoke_update, monkeypatch, stamped, expected) -> None:
    monkeypatch.setattr(smoke_update.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stamped))
    assert smoke_update.installed_version(Path("Music Toolkit.exe")) == expected


def test_the_app_is_running_when_tasklist_lists_its_image(smoke_update, monkeypatch) -> None:
    listing = '"Music Toolkit.exe","1234","Console","1","90,000 K"\r\n'
    monkeypatch.setattr(smoke_update.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=listing))
    assert smoke_update.running("Music Toolkit.exe") is True
    monkeypatch.setattr(
        smoke_update.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout="INFO: No tasks are running which match the specified criteria.\r\n"),
    )
    assert smoke_update.running("Music Toolkit.exe") is False
