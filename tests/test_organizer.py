from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.ingest import organizer

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


def _insert_track(conn, file_path: str, **overrides) -> int:
    fields = {
        "album_artist": None, "artist": None, "album": None, "title": None,
        "track_number": None, "year": None, "is_missing": 0,
    }
    fields.update(overrides)
    columns = ", ".join(["file_path"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", [file_path] + list(fields.values()))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_sanitize_path_component_strips_invalid_chars() -> None:
    assert organizer.sanitize_path_component('AC/DC: Back in Black?') == "AC_DC_ Back in Black_"


def test_compute_target_path_uses_scheme(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    old_path = library / "raw.mp3"
    old_path.write_bytes(b"x")
    track_id = _insert_track(
        conn, str(old_path.resolve()), artist="The Artist", album="The Album", title="The Title", track_number=5
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()

    target = organizer.compute_target_path(row, library, SCHEME)
    assert target == (library / "The Artist" / "The Album" / "05 - The Title.mp3").resolve()
    conn.close()


def test_propose_organization_dry_run_does_not_move(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    old_path = library / "raw.mp3"
    old_path.write_bytes(b"x")
    _insert_track(conn, str(old_path.resolve()), artist="Artist", album="Album", title="Title", track_number=1)

    result = organizer.propose_organization(conn, library, SCHEME)

    assert len(result.proposals) == 1
    assert old_path.exists()
    conn.close()


def test_apply_organization_moves_file_and_updates_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    old_path = library / "raw.mp3"
    old_path.write_bytes(b"x")
    track_id = _insert_track(
        conn, str(old_path.resolve()), artist="Artist", album="Album", title="Title", track_number=1
    )

    result = organizer.propose_organization(conn, library, SCHEME)
    moved = organizer.apply_organization(conn, result.proposals)

    assert moved == 1
    assert not old_path.exists()
    new_path = library / "Artist" / "Album" / "01 - Title.mp3"
    assert new_path.exists()

    row = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    assert row["file_path"] == str(new_path.resolve())
    conn.close()


def test_propose_organization_flags_collisions(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    path_a = library / "a.mp3"
    path_b = library / "b.mp3"
    path_a.write_bytes(b"x")
    path_b.write_bytes(b"y")
    _insert_track(conn, str(path_a.resolve()), artist="Artist", album="Album", title="Same", track_number=1)
    _insert_track(conn, str(path_b.resolve()), artist="Artist", album="Album", title="Same", track_number=1)

    result = organizer.propose_organization(conn, library, SCHEME)

    # One of the two can legitimately claim the shared target; the other is
    # correctly flagged rather than silently overwriting it.
    assert len(result.proposals) == 1
    assert len(result.collisions) == 1
    conn.close()


def test_a_template_that_leaves_the_library_proposes_nothing(tmp_path: Path) -> None:
    """A rooted template ("/x/...") has no drive letter, so Windows doesn't call it absolute; joined onto the
    library folder it would still land at the drive root. Nothing may be proposed outside the library."""
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    (library / "raw.mp3").write_bytes(b"x")
    _insert_track(conn, str(library / "raw.mp3"), title="Song", artist="Artist")

    for scheme in ("/elsewhere/{title}.{ext}", "\\elsewhere\\{title}.{ext}", "../{title}.{ext}"):
        assert organizer.propose_organization(conn, library, scheme).proposals == []


def _plan(conn, library: Path, scheme: str = SCHEME):
    return organizer.propose_organization(conn, library, scheme)


def test_an_untagged_song_has_no_made_up_track_number(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = (tmp_path / "library").resolve()
    library.mkdir()
    (library / "mystery.mp3").write_bytes(b"x")
    _insert_track(conn, str(library / "mystery.mp3"))

    [proposal] = _plan(conn, library).proposals

    assert proposal.new_path == library / "Unknown Artist" / "Unknown Album" / "mystery.mp3"
    conn.close()


def test_organizing_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    """Untitled songs borrow their title from the file name; that must not grow a prefix on every run."""
    conn = connect(tmp_path / "test.db")
    library = (tmp_path / "library").resolve()
    library.mkdir()
    for name in ("mystery.mp3", "03 - Loose Song.mp3", "99 Problems.mp3"):
        (library / name).write_bytes(b"x")
        _insert_track(conn, str(library / name))

    first = _plan(conn, library)
    assert organizer.apply_organization(conn, first.proposals, library) == 3
    second = _plan(conn, library)

    assert second.proposals == [] and second.unchanged == 3
    names = sorted(p.name for p in library.rglob("*.mp3"))
    assert names == ["03 - Loose Song.mp3", "99 Problems.mp3", "mystery.mp3"]
    conn.close()


def test_the_file_name_supplies_what_the_tags_lack(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = (tmp_path / "library").resolve()
    library.mkdir()
    only_title = library / "07 - Named.mp3"
    only_title.write_bytes(b"x")
    _insert_track(conn, str(only_title), title="Named", artist="A", album="B")  # a title but no track number
    only_track = library / "x.mp3"
    only_track.write_bytes(b"y")
    _insert_track(conn, str(only_track), artist="A", album="B", track_number=4)  # a track number but no title

    targets = {p.old_path.name: p.new_path.relative_to(library).as_posix() for p in _plan(conn, library).proposals}

    assert targets == {"07 - Named.mp3": "A/B/07 - Named.mp3", "x.mp3": "A/B/04 - x.mp3"}
    assert organizer.propose_organization(conn, library, SCHEME).collisions == []
    conn.close()


def test_a_missing_year_leaves_no_zero_behind(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = (tmp_path / "library").resolve()
    library.mkdir()
    for name, year in (("a.mp3", None), ("b.mp3", 1999)):
        (library / name).write_bytes(b"x")
        _insert_track(conn, str(library / name), artist="Ar", album="Al", title=name[0].upper(), track_number=1, year=year)

    scheme = "{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}"
    got = {p.old_path.name: p.new_path.relative_to(library).as_posix() for p in _plan(conn, library, scheme).proposals}

    assert got == {"a.mp3": "Ar/Al/01 - A.mp3", "b.mp3": "Ar/1999 - Al/01 - B.mp3"}
    conn.close()


def test_punctuation_around_a_missing_number_is_tidied() -> None:
    blank = organizer._BLANK
    cases = {
        f"{blank} - Song.mp3": "Song.mp3",
        f"{blank}. Song.mp3": "Song.mp3",
        f"Album ({blank})": "Album",
        f"Album - {blank}": "Album",
        f"A - {blank} - B": "A - B",
        f"Song - {blank}.mp3": "Song.mp3",
        "Ordinary - Name.mp3": "Ordinary - Name.mp3",
    }
    for text, expected in cases.items():
        assert organizer._drop_blanks(text) == expected, text
