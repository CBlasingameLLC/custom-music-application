"""Writing files onto a data disc or into an ISO image: what the files are called on the disc, what goes and what cannot, whether they fit, and
reading a written disc back to check it. Real tiny files in tmp_path; nothing here touches a drive."""

from __future__ import annotations

from pathlib import Path

import pytest

from musictoolkit.discs import datadisc
from musictoolkit.discs.datadisc import SECTOR, DataSong, plan_data, safe_name


def song(tmp_path: Path, relative: str, size: int = 10, title: str = "", artist: str = "", track_id: int | None = None, fill: bytes = b"x") -> DataSong:
    path = tmp_path / "music" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((fill * size)[:size])
    return DataSong(str(path), title or path.stem, artist, track_id, root=str(tmp_path / "music"))


def virtual(tmp_path: Path, relative: str) -> DataSong:
    """A song that is only a place in the library (no file is made: a path longer than Windows allows for a real file can still be planned)."""
    root = tmp_path / "music"
    return DataSong(str(root / relative), Path(relative).stem, root=str(root))


def media(**changes) -> dict:
    return {"writer": True, "state": "blank", "type": "CD-R", "rewritable": False, "blank": True, "free_sectors": 359846, **changes}


def dests(plan) -> list[str | None]:
    return [f.dest for f in plan.files]


# ---------------------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Normal Song.mp3", "Normal Song.mp3"),
        ('AC/DC: "Back in Black"?.mp3', "AC_DC_ _Back in Black__.mp3"),
        ("What; Why*.flac", "What_ Why_.flac"),
        ("trailing dots...", "trailing dots"),
        ("  padded name  .mp3", "padded name.mp3"),
        ("CON.mp3", "_CON.mp3"),
        ("com1", "_com1"),
        ("", "_"),
        ("...", "_"),
        ("tab\there.mp3", "tab_here.mp3"),
        ("no extension at all", "no extension at all"),
        ("archive.tar.gz", "archive.tar.gz"),
        (".hidden", ".hidden"),
        ("..", "_"),
        (".", "_"),
        ("a.thisisnotanextension", "a.thisisnotanextension"),
    ],
)
def test_a_name_is_made_safe_for_every_disc_format(raw: str, expected: str) -> None:
    assert safe_name(raw) == expected


def test_a_long_name_is_cut_but_keeps_its_extension() -> None:
    cut = safe_name("x" * 100 + ".flac")
    assert len(cut) == 64 and cut.endswith("x.flac") and cut.count(".") == 1
    assert safe_name("y" * 100) == "y" * 64
    assert len(safe_name("z" * 100, 32)) == 32
    assert safe_name("a" * 70 + " .mp3").endswith(".mp3") and not safe_name("a" * 59 + " " + "b" * 20 + ".mp3").split(".")[0].endswith(" ")


def test_accents_stay_and_are_composed() -> None:
    assert safe_name("Café del Mar.mp3") == "Café del Mar.mp3"


def test_the_name_of_the_disc_is_cleaned_and_short() -> None:
    assert datadisc.clean_label("Road Trip 2026") == "Road Trip 2026"
    assert datadisc.clean_label('  A "very" /long\\ title: really  ') == "A very long titl"
    assert datadisc.clean_label("") == "MUSIC" and datadisc.clean_label("???") == "MUSIC" and datadisc.clean_label("", "OTHER") == "OTHER"
    assert datadisc.clean_label("x" * 40) == "x" * 16
    assert datadisc.clean_label("0123456789abcde      fgh") == "0123456789abcde"  # (cut where a space would be left at the end)


def test_the_name_of_the_disc_for_iso_9660_has_only_capitals_digits_and_underscores() -> None:
    assert datadisc.iso_label("Road Trip 2026") == "ROAD_TRIP_2026"
    assert datadisc.iso_label("Café del Mar") == "CAFE_DEL_MAR"
    assert datadisc.iso_label("音楽") == "MUSIC" and datadisc.iso_label("---") == "MUSIC" and datadisc.iso_label("") == "MUSIC"
    assert datadisc.iso_label("a" * 50) == "A" * 32


def test_names_that_differ_only_in_capitals_are_told_apart(tmp_path: Path) -> None:
    songs = [song(tmp_path, "A/Same.mp3"), song(tmp_path, "B/x.mp3")]
    other = tmp_path / "music" / "A" / "SAME.mp3"
    other.write_bytes(b"y")
    songs.append(DataSong(str(other), "SAME", root=str(tmp_path / "music")))
    plan = plan_data(songs + [songs[0]], layout="library")
    assert dests(plan) == ["A/Same.mp3", "B/x.mp3", "A/SAME (2).mp3", "A/Same (3).mp3"]


# ---------------------------------------------------------------------------------------- layouts


def test_the_library_layout_keeps_the_folders_of_the_library(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "Artist/Album/01 One.mp3"), song(tmp_path, "Artist/Album/02 Two.flac"), song(tmp_path, "Single.mp3")])
    assert dests(plan) == ["Artist/Album/01 One.mp3", "Artist/Album/02 Two.flac", "Single.mp3"]
    assert plan.layout == "library" and plan.folders == 2


def test_the_library_layout_makes_every_folder_name_safe(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "AC_DC/Who Made Who?/01 You Shook Me.mp3")])
    assert dests(plan) == ["AC_DC/Who Made Who_/01 You Shook Me.mp3"]
    odd = song(tmp_path, "x/y.mp3")
    odd.path = str(tmp_path / "elsewhere.mp3")
    (tmp_path / "elsewhere.mp3").write_bytes(b"z")
    assert dests(plan_data([odd])) == ["elsewhere.mp3"]  # (a song outside the folder it was said to be in: just its name)


def test_the_flat_layout_numbers_the_songs_in_the_order_chosen(tmp_path: Path) -> None:
    songs = [song(tmp_path, "b/Second.mp3", title="Second Song"), song(tmp_path, "a/First.flac", title="First Song"), song(tmp_path, "c/untitled.ogg", title="")]
    plan = plan_data(songs, layout="flat")
    assert dests(plan) == ["01 - Second Song.mp3", "02 - First Song.flac", "03 - untitled.ogg"] and plan.folders == 0


def test_the_flat_layout_uses_three_digits_from_a_hundred_songs(tmp_path: Path) -> None:
    one = song(tmp_path, "s.mp3", title="Song")
    plan = plan_data([one] * 120, layout="flat")
    assert plan.files[0].dest == "001 - Song.mp3" and plan.files[119].dest == "120 - Song.mp3"


def test_the_flat_layout_makes_titles_safe(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "s.mp3", title="What/Why? CON")], layout="flat")
    assert dests(plan) == ["01 - What_Why_ CON.mp3"]


def test_an_unknown_layout_or_file_system_or_an_empty_list_is_refused() -> None:
    with pytest.raises(ValueError, match="layout must be one of: library, flat"):
        plan_data([DataSong("x")], layout="tree")
    with pytest.raises(ValueError, match="file systems must be some of"):
        plan_data([DataSong("x")], filesystems=["ntfs"])
    with pytest.raises(ValueError, match="Choose some songs first"):
        plan_data([])


# ---------------------------------------------------------------------------------------- what cannot go


def test_a_file_that_is_gone_is_left_out_and_said_so(tmp_path: Path) -> None:
    here, gone = song(tmp_path, "A/here.mp3"), song(tmp_path, "A/gone.mp3")
    Path(gone.path).unlink()
    plan = plan_data([here, gone])
    assert dests(plan) == ["A/here.mp3", None] and plan.files[1].problem == "The file is not there any more."
    assert plan.warnings == ["1 file will be left out: see the list."] and plan.problems == [] and len(plan.going) == 1
    assert plan_data([here, gone, gone]).warnings == ["2 files will be left out: see the list."]


def test_when_nothing_can_go_that_is_the_problem(tmp_path: Path) -> None:
    gone = song(tmp_path, "gone.mp3")
    Path(gone.path).unlink()
    plan = plan_data([gone])
    assert plan.problems == ["None of these files can be put on a disc."] and plan.to_dict()["ok"] is False and plan.disc_sectors == 0


def test_folders_nested_too_deep_cannot_go(tmp_path: Path) -> None:
    deep = song(tmp_path, "/".join(f"d{i}" for i in range(9)) + "/deep.mp3")
    ok = song(tmp_path, "/".join(f"d{i}" for i in range(8)) + "/fine.mp3")
    plan = plan_data([deep, ok])
    assert plan.files[0].problem == "The folders are nested too deep for a disc (8 levels at most)." and plan.files[1].dest is not None


def test_a_long_path_is_shortened_by_cutting_the_folder_names_and_refused_if_that_is_not_enough(tmp_path: Path) -> None:
    long = "/".join("f" * 60 for _ in range(4)) + "/song.mp3"  # 4 x 61 + 8 = 252 characters
    plan = plan_data([virtual(tmp_path, long)], stat=lambda _p: 10)
    assert plan.files[0].dest == "/".join("f" * 32 for _ in range(4)) + "/song.mp3" and len(plan.files[0].dest) <= 240
    huge = "/".join("g" * 60 for _ in range(8)) + "/" + "s" * 60 + ".mp3"  # still more than 240 after cutting
    assert plan_data([virtual(tmp_path, huge)], stat=lambda _p: 10).files[0].problem == "The name is too long for a disc."


def test_a_file_of_4_gb_or_more_needs_udf(tmp_path: Path) -> None:
    big = song(tmp_path, "big.mp3")
    sizes = {big.path: 4 * 1024**3}
    cd = plan_data([big], stat=sizes.get)
    assert cd.files[0].problem == "The file is bigger than 4 GB, which only a UDF disc can hold." and cd.problems == ["None of these files can be put on a disc."]
    assert plan_data([big], filesystems=["udf"], stat=sizes.get).files[0].dest == "big.mp3"
    sizes[big.path] = 4 * 1024**3 - 1
    assert plan_data([big], stat=sizes.get).files[0].dest == "big.mp3"


# ---------------------------------------------------------------------------------------- the disc


def test_a_disc_gets_the_file_systems_its_kind_can_use(tmp_path: Path) -> None:
    one = [song(tmp_path, "a.mp3")]
    assert plan_data(one).filesystems == ["iso9660", "joliet"]
    assert plan_data(one, media=media(type="CD-RW")).filesystems == ["iso9660", "joliet"]
    assert plan_data(one, media=media(type="DVD+R")).filesystems == ["iso9660", "joliet", "udf"]
    assert plan_data(one, media=media(type="BD-R")).filesystems == ["iso9660", "joliet", "udf"]
    assert plan_data(one, media=media(type="DVD+R"), filesystems=["udf"]).filesystems == ["udf"]


def test_what_the_files_take_counts_their_sectors_and_the_entries_for_files_and_folders(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "A/B/one.mp3", size=SECTOR * 3 + 1), song(tmp_path, "A/two.mp3", size=SECTOR), song(tmp_path, "three.mp3", size=1)])
    assert plan.total_bytes == SECTOR * 3 + 1 + SECTOR + 1 and plan.folders == 2
    assert plan.disc_sectors == (4 + 1 + 1) + 3 * 3 + 3 * 2


def test_files_that_do_not_fit_are_said_so_in_megabytes(tmp_path: Path) -> None:
    sizes = {}
    files = [song(tmp_path, f"{i}.mp3") for i in range(2)]
    for entry in files:
        sizes[entry.path] = 400 * 1024 * 1024
    plan = plan_data(files, media=media(), stat=sizes.get)
    assert plan.problems == ["The files take 800 MB and the disc has 703 MB free. Take out about 105 MB (a little is kept in reserve)."]
    assert plan.free_sectors == 359846 and plan.to_dict()["ok"] is False


def test_the_files_fit_up_to_the_reserve_and_not_a_sector_beyond(tmp_path: Path) -> None:
    one = song(tmp_path, "one.mp3")
    sectors = 100000
    sizes = {one.path: sectors * SECTOR}
    needed = plan_data([one], stat=sizes.get).disc_sectors + datadisc.RESERVE_SECTORS
    assert plan_data([one], media=media(free_sectors=needed), stat=sizes.get).problems == []
    assert len(plan_data([one], media=media(free_sectors=needed - 1), stat=sizes.get).problems) == 1


@pytest.mark.parametrize(
    "disc,said",
    [
        ({"writer": False}, "This drive cannot write discs."),
        ({"state": "none", "blank": False}, "There is no disc in the drive."),
        ({"state": "written", "blank": False}, "This CD-R is not blank, and a disc that has been written cannot be used again. Put in a blank disc."),
        ({"blank": False}, "This CD-R is not blank, and a disc that has been written cannot be used again. Put in a blank disc."),
        (
            {"state": "written", "blank": False, "type": "DVD-RW", "rewritable": True},
            "This DVD-RW already has something on it. Erase it first (Erase, on this screen), or put in a blank disc.",
        ),
        (
            {"state": "unusable", "blank": False, "type": "DVD-R"},
            "This drive cannot use this DVD-R: it may be damaged, dirty, or a kind of disc the drive does not write. Try another disc.",
        ),
        ({"state": "unusable", "blank": False, "type": ""}, "This drive cannot use this disc: it may be damaged, dirty, or a kind of disc the drive does not write. Try another disc."),
        ({"protected": True}, "This CD-R is write-protected. Use another disc."),
    ],
)
def test_a_disc_that_cannot_take_files_says_why(tmp_path: Path, disc: dict, said: str) -> None:
    plan = plan_data([song(tmp_path, "a.mp3")], media=media(**disc))
    assert plan.problems == [said] and plan.free_sectors is None


def test_an_image_file_has_no_disc_to_fit(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "a.mp3")], media=None)
    assert plan.problems == [] and plan.free_sectors is None


def test_the_plan_is_told_to_the_page_in_one_shape(tmp_path: Path) -> None:
    shown = plan_data([song(tmp_path, "A/one.mp3", title="One", artist="Band", track_id=7, size=5000)], label="My Mix", layout="library", media=media()).to_dict()
    assert shown["label"] == "My Mix" and shown["label_iso"] == "MY_MIX" and shown["layout"] == "library" and shown["filesystems"] == ["iso9660", "joliet"]
    assert shown["files"] == [{"position": 1, "track_id": 7, "title": "One", "artist": "Band", "dest": "A/one.mp3", "size": 5000, "problem": ""}]
    assert shown["counts"] == {"going": 1, "left_out": 0} and shown["bytes"] == 5000 and shown["disc_sectors"] == 3 + 3 + 3
    assert shown["free_sectors"] == 359846 and shown["problems"] == [] and shown["warnings"] == [] and shown["ok"] is True


# ---------------------------------------------------------------------------------------- the image file


def test_an_image_file_is_named_after_the_disc_and_never_replaces_one(tmp_path: Path) -> None:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    first = datadisc.iso_target(folder, "Road Trip")
    assert first == folder / "Road Trip.iso"
    first.write_bytes(b"1")
    second = datadisc.iso_target(folder, "Road Trip")
    assert second == folder / "Road Trip (2).iso"
    second.write_bytes(b"2")
    assert datadisc.iso_target(folder, "Road Trip") == folder / "Road Trip (3).iso"
    assert datadisc.iso_target(folder, 'a/b:c?"') == folder / "a_b_c__.iso"
    assert datadisc.iso_target(folder, "") == folder / "MUSIC.iso"


# ---------------------------------------------------------------------------------------- reading the disc back


def written(tmp_path: Path, plan, damage: dict[str, bytes | None] | None = None) -> Path:
    """What a disc that was burned from the plan shows: every file where it should be (changed or left out as the test says)."""
    disc = tmp_path / "disc"
    for entry in plan.going:
        target = disc.joinpath(*entry.dest.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if damage and entry.dest in damage:
            if damage[entry.dest] is not None:
                target.write_bytes(damage[entry.dest])
        else:
            target.write_bytes(Path(entry.song.path).read_bytes())
    return disc


def test_a_disc_that_matches_is_good(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "A/one.mp3", size=3000, fill=b"1"), song(tmp_path, "two.mp3", size=SECTOR * 600, fill=b"2")])
    progress: list[tuple[int, int]] = []
    verdict = datadisc.verify_files(plan, written(tmp_path, plan), on_progress=lambda d, t: progress.append((d, t)))
    assert verdict.ok and verdict.checked == 2 and verdict.words() == "All 2 files were read back from the disc and match."
    assert progress[-1] == (plan.total_bytes, plan.total_bytes) and [d for d, _ in progress] == sorted(d for d, _ in progress)


def test_a_file_that_reads_back_differently_or_not_at_all_is_found(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, f"{n}.mp3", size=5000, fill=bytes([65 + n])) for n in range(4)])
    disc = written(tmp_path, plan, damage={"1.mp3": b"X" * 5000, "2.mp3": None})
    verdict = datadisc.verify_files(plan, disc)
    assert not verdict.ok and verdict.different == ["1.mp3"] and verdict.missing == ["2.mp3"] and verdict.checked == 4
    assert verdict.words() == "The disc was written, but it cannot be trusted: 2.mp3 is not on the disc; 1.mp3 does not match."


def test_a_same_size_but_different_file_is_not_missed(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "a.mp3", size=SECTOR * 4, fill=b"A")])
    flipped = bytearray(b"A" * SECTOR * 4)
    flipped[-1] ^= 1  # one bit, at the very end
    assert datadisc.verify_files(plan, written(tmp_path, plan, {"a.mp3": bytes(flipped)})).different == ["a.mp3"]


def test_the_words_for_a_bad_disc_list_a_few_and_count_the_rest(tmp_path: Path) -> None:
    verdict = datadisc.Verdict(checked=20, missing=["a", "b", "c", "d"], different=["e", "f", "g", "h"])
    assert verdict.words() == (
        "The disc was written, but it cannot be trusted: a is not on the disc; b is not on the disc; c is not on the disc; "
        "e does not match; f does not match; g does not match (and 2 more)."
    )


def test_a_stop_while_reading_back_stops_it(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, f"{n}.mp3", size=SECTOR * 4) for n in range(3)])
    disc = written(tmp_path, plan)
    seen = []

    class Stop(Exception):
        pass

    def check() -> None:
        seen.append(1)
        if len(seen) > 4:
            raise Stop

    with pytest.raises(Stop):
        datadisc.verify_files(plan, disc, check=check)


def test_a_file_the_disc_cannot_read_counts_as_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_data([song(tmp_path, "a.mp3", size=100), song(tmp_path, "b.mp3", size=100)])
    disc = written(tmp_path, plan)
    real = datadisc._digest

    def fails_on_the_disc(path: Path, check):
        if str(path).startswith(str(disc)) and path.name == "b.mp3":
            raise OSError("I/O error")
        return real(path, check)

    monkeypatch.setattr(datadisc, "_digest", fails_on_the_disc)
    verdict = datadisc.verify_files(plan, disc)
    assert verdict.missing == ["b.mp3"] and verdict.different == [] and verdict.checked == 2


# ---------------------------------------------------------------------------------------- corners found by changing the code one way at a time


def test_the_name_of_the_disc_is_composed_and_a_forbidden_character_becomes_a_space() -> None:
    assert datadisc.clean_label("Café Mix") == "Café Mix"  # (e and an accent, written as the one letter)
    assert datadisc.clean_label("A:B/C") == "A B C"
    assert datadisc.clean_label("  A  \t B  ") == "A B"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("name..", "name"),  # (the dots at the end are all taken off, not the last one only)
        ("name. .", "name"),
        ("a.mp3.", "a.mp3"),
        ("a.mp3 ", "a.mp3"),
    ],
)
def test_dots_and_spaces_at_the_end_of_a_name_are_all_taken_off(raw: str, expected: str) -> None:
    assert safe_name(raw) == expected


def test_only_a_short_ending_is_an_extension_the_rest_is_cut_with_the_name() -> None:
    cut = safe_name("x" * 100 + ".abcdefghi")  # (nine letters after the dot: part of the name)
    assert len(cut) == datadisc.JOLIET_NAME and "." not in cut
    kept = safe_name("x" * 100 + ".abcdefgh")  # (eight: an extension, kept)
    assert len(kept) == datadisc.JOLIET_NAME and kept.endswith(".abcdefgh")


def test_a_limit_smaller_than_the_extension_still_leaves_a_letter_of_the_name() -> None:
    assert safe_name("abc.mp3", limit=4) == "a.mp3"


def test_a_name_made_unique_stays_within_the_length_a_disc_allows() -> None:
    names: set[str] = set()
    first = datadisc._unique(names, "A" * 60 + ".mp3")
    second = datadisc._unique(names, "a" * 60 + ".mp3")
    third = datadisc._unique(names, "a" * 60 + ".mp3")
    assert first == "A" * 60 + ".mp3"
    assert second.endswith(" (2).mp3") and third.endswith(" (3).mp3")
    assert len(second) <= datadisc.JOLIET_NAME and len(third) <= datadisc.JOLIET_NAME


def test_what_the_files_take_counts_only_the_files_that_go(tmp_path: Path) -> None:
    deep = song(tmp_path, "/".join(["d"] * 10) + "/deep.mp3", size=100)  # (too many folders: left out, but its size is known)
    plan = plan_data([deep, song(tmp_path, "ok.mp3", size=50)])
    assert plan.files[0].size == 100 and plan.files[0].dest is None
    assert plan.total_bytes == 50


def test_the_plan_tells_the_page_why_a_file_is_left_out_and_how_many(tmp_path: Path) -> None:
    gone = DataSong(str(tmp_path / "gone.mp3"), "Gone", root=str(tmp_path))
    shown = plan_data([song(tmp_path, "ok.mp3"), gone]).to_dict()
    assert shown["counts"] == {"going": 1, "left_out": 1}
    assert [(f["dest"], f["problem"]) for f in shown["files"]] == [("ok.mp3", ""), (None, "The file is not there any more.")]
    assert shown["warnings"] == ["1 file will be left out: see the list."]


def test_a_flat_name_uses_the_title_without_stray_spaces_or_the_file_name_when_there_is_none(tmp_path: Path) -> None:
    one = song(tmp_path, "Loud.MP3", title="Hello ")
    two = DataSong(str(tmp_path / "music" / "Loud.MP3"), "", root=str(tmp_path / "music"))
    assert dests(plan_data([one, two], layout="flat")) == ["01 - Hello.mp3", "02 - Loud.mp3"]  # (the extension in lower case)


def test_a_song_without_a_library_folder_goes_in_the_top_folder_by_its_name(tmp_path: Path) -> None:
    plan = plan_data([DataSong("sub/folder/x.mp3", "x")], stat=lambda _p: 10)
    assert dests(plan) == ["x.mp3"]


def test_a_song_that_is_its_own_library_folder_is_named_after_itself(tmp_path: Path) -> None:
    here = tmp_path / "music"
    here.mkdir()
    assert datadisc._place(DataSong(str(here), "", root=str(here)), 1, 1, "library") == ["music"]


def test_a_path_of_exactly_the_longest_length_is_not_shortened(tmp_path: Path) -> None:
    folder = "f" * 60
    name = "n" * (datadisc.JOLIET_PATH - 3 * 61 - 4) + ".mp3"
    relative = f"{folder}/{folder}/{folder}/{name}"
    assert len(relative) == datadisc.JOLIET_PATH
    assert dests(plan_data([virtual(tmp_path, relative)], stat=lambda _p: 10)) == [relative]
    longer = relative[:-5] + "nn.mp3"  # one character more: the folders are cut to 32
    shortened = dests(plan_data([virtual(tmp_path, longer)], stat=lambda _p: 10))[0]
    assert shortened is not None and shortened.startswith("f" * 32 + "/") and len(shortened) < len(longer)


def test_two_files_with_one_name_in_folders_that_differ_only_in_capitals_are_told_apart_but_not_in_different_folders(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "A/x.mp3"), song(tmp_path, "a/x.mp3"), song(tmp_path, "B/x.mp3")])
    assert dests(plan) == ["A/x.mp3", "a/x (2).mp3", "B/x.mp3"]


def test_a_folder_is_not_a_file_and_has_no_size(tmp_path: Path) -> None:
    assert datadisc._size_of(str(tmp_path)) is None
    assert datadisc._size_of(str(tmp_path / "nothing")) is None
    (tmp_path / "x").write_bytes(b"1234")
    assert datadisc._size_of(str(tmp_path / "x")) == 4


@pytest.mark.parametrize(
    "size,said",
    [(0, "0 MB"), (5 * 1024**2, "5 MB"), (1024**3 - 1, "1024 MB"), (1024**3, "1.0 GB"), (int(2.5 * 1024**3), "2.5 GB")],
)
def test_sizes_are_said_in_megabytes_until_they_reach_a_gigabyte(size: int, said: str) -> None:
    assert datadisc.megabytes(size) == said


def test_a_disc_that_says_it_is_blank_but_is_not_in_the_state_of_one_is_not_taken_for_blank() -> None:
    assert datadisc.media_problems(media(state="written", blank=True)) == [
        "This CD-R is not blank, and a disc that has been written cannot be used again. Put in a blank disc."
    ]


def test_a_verdict_is_good_only_when_nothing_is_missing_and_nothing_differs() -> None:
    assert datadisc.Verdict(checked=2).ok
    assert not datadisc.Verdict(checked=2, missing=["a"]).ok
    assert not datadisc.Verdict(checked=2, different=["b"]).ok
    assert datadisc.Verdict(checked=2, missing=["a"]).words() == "The disc was written, but it cannot be trusted: a is not on the disc."
    assert datadisc.Verdict(checked=2, different=["b"]).words() == "The disc was written, but it cannot be trusted: b does not match."


def test_a_list_of_empty_files_still_reports_a_whole_to_count_against(tmp_path: Path) -> None:
    plan = plan_data([song(tmp_path, "empty.mp3", size=0)])
    disc = tmp_path / "disc"
    (disc).mkdir()
    (disc / "empty.mp3").write_bytes(b"")
    seen: list[tuple[int, int]] = []
    verdict = datadisc.verify_files(plan, disc, on_progress=lambda done, total: seen.append((done, total)))
    assert verdict.ok and seen == [(0, 1)]


@pytest.mark.parametrize(
    "stem,expected",
    [("01 - Loud", "01 - Loud.mp3"), ("07. Quiet Song", "01 - Quiet Song.mp3"), ("3-Short", "01 - Short.mp3"), ("99 Luftballons", "01 - 99 Luftballons.mp3"), ("1984", "01 - 1984.mp3"), ("Plain", "01 - Plain.mp3")],
)
def test_a_flat_name_for_a_song_without_a_title_drops_the_number_its_file_name_begins_with(tmp_path: Path, stem: str, expected: str) -> None:
    path = tmp_path / "music" / f"{stem}.mp3"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"x")
    assert dests(plan_data([DataSong(str(path), "", root=str(path.parent))], layout="flat")) == [expected]
