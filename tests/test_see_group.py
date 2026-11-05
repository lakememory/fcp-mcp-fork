"""The see tool group: advertised, dispatching, read-only, and it returns pictures."""

import asyncio
import base64

import pytest

import server
import tools
from fcpxml import blackness, journal, render
from tests.cuts import BASE_SPINE, MASKED_SPINE, WORSE_SPINE, xml


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(render, "cache_dir", lambda: cache)
    return cache


@pytest.fixture
def cuts(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    good, bad = project / "base.fcpxml", project / "worse.fcpxml"
    good.write_text(xml(BASE_SPINE, "30s"))
    bad.write_text(xml(WORSE_SPINE, "36s"))
    return good, bad


def _call(action, args=None):
    return asyncio.run(server.call_tool("see", {"action": action, "args": args or {}}))


def test_the_group_is_advertised_with_its_actions():
    assert "see" in tools.EXTRA_GROUPS and "see" in server.TOOL_GROUPS
    assert tools.EXTRA_GROUPS["see"]["actions"] == ["shadow", "contact_sheet"]


def test_every_action_asks_for_a_filepath_rather_than_failing():
    for action in tools.EXTRA_GROUPS["see"]["actions"]:
        result = _call(action)
        assert result and "filepath" in result[0].text


def test_shadow_returns_the_view_with_checks_and_the_files_hash(cuts, _private_cache):
    good, bad = cuts
    text = _call("shadow", {"filepath": str(bad), "base": str(good)})[0].text
    assert f"SHADOW  worse.fcpxml  sha256 {journal.file_hash(str(bad))[:12]}" in text
    assert "base    base.fcpxml" in text
    assert "FAIL      black" in text and "FAIL      vo-uncovered" in text
    assert "<< over black" in text
    # The sources in the fixture do not exist, and the tool checks.
    assert "FAIL      media" in text
    assert "check(s) FAIL. Each one is fixed, or named in the handoff" in text
    saved = list((_private_cache / "shadow").glob("worse.*.shadow.txt"))
    assert len(saved) == 1 and saved[0].read_text().startswith("SHADOW  worse.fcpxml")


def test_shadow_takes_a_scope_and_a_window(cuts):
    good, bad = cuts
    text = _call("shadow", {"filepath": str(bad), "base": str(good),
                            "scope": "0:08-0:18", "start": "0:20", "end": 30})[0].text
    assert "scope   0:08.0 to 0:18.0" in text
    assert "FAIL      scope" in text
    assert "THE CUT  0:20.0 to 0:30.0" in text


def test_a_time_that_cannot_be_read_is_answered_not_raised(cuts):
    _good, bad = cuts
    assert "not a time" in _call("shadow", {"filepath": str(bad), "start": "soon"})[0].text
    assert "not a time" in _call("contact_sheet", {"filepath": str(bad), "end": "later"})[0].text


def test_a_base_that_cannot_be_read_is_named(cuts, tmp_path):
    _good, bad = cuts
    text = _call("shadow", {"filepath": str(bad), "base": str(tmp_path / "nope.fcpxml")})[0].text
    assert "nope.fcpxml" in text


def test_nothing_is_written_to_the_project(cuts):
    good, bad = cuts
    before = {p.name: journal.file_hash(str(p)) for p in bad.parent.iterdir()}
    _call("shadow", {"filepath": str(bad), "base": str(good)})
    _call("contact_sheet", {"filepath": str(bad), "inline": False})
    after = {p.name: journal.file_hash(str(p)) for p in bad.parent.iterdir()}
    assert after == before


def test_see_actions_are_not_gated_and_not_recorded_as_writes(cuts):
    _good, bad = cuts
    assert not {"shadow", "contact_sheet"} & set(server.GATED_ACTIONS)
    text = _call("shadow", {"filepath": str(bad)})[0].text
    assert "sha256 of what was written" not in text.lower()


@pytest.fixture
def mask(tmp_path, monkeypatch):
    """A cut with a black movie over its back two thirds; the movie is a real file."""
    movie = tmp_path / "black.mov"
    movie.write_bytes(b"stands in for a black movie")
    looked = []
    monkeypatch.setattr(
        blackness, "peak", lambda source, at: looked.append(source) or 0
    )
    path = tmp_path / "masked.fcpxml"
    path.write_text(xml(MASKED_SPINE, "30s", black=movie.as_uri()))
    return path, looked


def test_shadow_looks_at_the_files_and_keeps_what_it_saw(mask, _private_cache):
    path, looked = mask
    text = _call("shadow", {"filepath": str(path)})[0].text
    assert "FAIL      black         0:10.0 to 0:30.0  20.0 s with no picture: black clip" in text
    assert "A clip whose file is black where it plays counts as black." in text
    assert len(looked) == 3 and (_private_cache / "black.json").is_file()
    _call("shadow", {"filepath": str(path)})
    assert len(looked) == 3  # the second call read the cache, not the file


PIL = pytest.importorskip("PIL")


def test_contact_sheet_names_a_black_movie_as_no_picture(mask):
    path, _looked = mask
    text = _call("contact_sheet", {"filepath": str(path), "start": "0:08", "end": "0:14",
                                   "inline": False})[0].text
    assert " 0:10.0  NO PICTURE: black clip: BLACKOUT from 0:10" in text


def test_contact_sheet_returns_the_index_and_the_picture(cuts, _private_cache):
    _good, bad = cuts
    result = _call("contact_sheet", {"filepath": str(bad), "start": "0:08", "end": "0:20"})
    text = result[0].text
    assert text.startswith("Sheet 1 of 1: 0:08.0 to 0:18.0")
    assert ' 0:10.0  NO PICTURE: gap "BLACK: a shot still to come"' in text
    assert " 0:12.0  NO PICTURE" in text and "| N02 second line" in text
    # The fixture's media is not on disk: said, not hidden.
    assert "No frame could be read for: Shot A, Shot B" in text

    images = [c for c in result[1:] if c.type == "image"]
    assert len(images) == 1
    mime = getattr(images[0], "mimeType", None) or getattr(images[0], "mime_type", None)
    assert mime == "image/jpeg"
    raw = base64.b64decode(images[0].data)
    assert raw[:3] == b"\xff\xd8\xff"
    on_disk = list((_private_cache / "sheets").glob("worse.*.jpg"))
    assert len(on_disk) == 1 and on_disk[0].read_bytes() == raw


def test_inline_false_returns_paths_only(cuts):
    _good, bad = cuts
    result = _call("contact_sheet", {"filepath": str(bad), "inline": False})
    assert len(result) == 1 and ".jpg" in result[0].text


def test_a_long_range_stops_at_max_sheets_and_says_how_to_continue(cuts):
    _good, bad = cuts
    result = _call("contact_sheet", {"filepath": str(bad), "step": 1, "max_sheets": 1})
    assert len([c for c in result if c.type == "image"]) == 1
    assert "1 more sheet(s) in this range. Continue with start=0:20.0" in result[0].text


def test_a_movie_that_is_not_there_is_refused(cuts, tmp_path):
    _good, bad = cuts
    text = _call("contact_sheet", {"filepath": str(bad), "movie": str(tmp_path / "x.mov")})[0].text
    assert text.startswith("movie not found")
