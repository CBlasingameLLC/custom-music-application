from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_complete_tags_fixture_reads_expected_tags() -> None:
    audio = EasyID3(FIXTURES_DIR / "complete_tags.mp3")
    assert audio["title"] == ["Fixture Title"]
    assert audio["artist"] == ["Fixture Artist"]
    assert audio["album"] == ["Fixture Album"]
    assert audio["albumartist"] == ["Fixture Album Artist"]
    assert audio["tracknumber"] == ["3"]
    assert audio["genre"] == ["Test Genre"]


def test_sparse_tags_fixture_has_no_id3_header() -> None:
    # Represents the realistic worst case a real personal library can contain:
    # a file with zero ID3v2 structure at all, not just empty/missing frames.
    audio = mutagen.File(FIXTURES_DIR / "sparse_tags.mp3", easy=True)
    assert audio is not None
    assert audio.tags is None


def test_tag_round_trip_write_and_reread(tmp_path: Path) -> None:
    """Exercises the same load-or-create pattern Phase 1's tagger.py will use
    for real-world files that have no existing ID3 header."""
    working_copy = tmp_path / "roundtrip.mp3"
    working_copy.write_bytes((FIXTURES_DIR / "sparse_tags.mp3").read_bytes())

    try:
        audio = EasyID3(working_copy)
    except ID3NoHeaderError:
        audio = EasyID3()
        audio.save(working_copy)
        audio = EasyID3(working_copy)

    audio["title"] = "Round Trip Title"
    audio["artist"] = "Round Trip Artist"
    audio.save()

    reread = EasyID3(working_copy)
    assert reread["title"] == ["Round Trip Title"]
    assert reread["artist"] == ["Round Trip Artist"]
