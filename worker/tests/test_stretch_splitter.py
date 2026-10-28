import hashlib

from src.jobs.stretch_splitter import split_document


def _words(n, tag="w"):
    return " ".join(f"{tag}{i}" for i in range(n))


def _line(ts, n=20, tag="w"):
    return f"[{ts}] {_words(n, tag)}"


def test_empty_and_short_docs_return_empty():
    assert split_document("d", "m", "") == []
    assert split_document("d", "m", "[10:00:00] hi there") == []


def test_gap_over_two_minutes_starts_new_stretch():
    content = "\n".join([
        _line("10:00:00"), _line("10:00:30"),
        _line("10:03:00"), _line("10:03:20"),  # 150s gap
    ])
    out = split_document("d", "m", content)
    assert [(s["start"], s["end"]) for s in out] == [
        ("10:00:00", "10:00:30"), ("10:03:00", "10:03:20")]


def test_gap_of_exactly_two_minutes_does_not_split():
    content = "\n".join([_line("10:00:00"), _line("10:02:00")])
    assert len(split_document("d", "m", content)) == 1


def test_word_cap_splits_long_run():
    # 10 lines x 20 words = 200 words, 10s apart -> cap at 120 words
    content = "\n".join(_line(f"10:00:{i*10:02d}", 20, f"l{i}_") for i in range(6))
    content += "\n" + "\n".join(_line(f"10:01:{i*10:02d}", 20, f"m{i}_") for i in range(4))
    out = split_document("d", "m", content)
    assert len(out) == 2
    # first stretch holds exactly 6 lines (120 words); a 7th would exceed the cap
    assert out[0]["end"] == "10:00:50"
    assert out[1]["start"] == "10:01:00"


def test_time_cap_splits_long_run():
    # lines every 100s (no >2min gap), 20 words each: spans exceed 4 min
    times = ["10:00:00", "10:01:40", "10:03:20", "10:05:00", "10:06:40"]
    out = split_document("d", "m", "\n".join(_line(t) for t in times))
    assert [(s["start"], s["end"]) for s in out] == [
        ("10:00:00", "10:03:20"), ("10:05:00", "10:06:40")]


def test_short_stretch_dropped_and_ids_over_kept():
    content = "\n".join([
        _line("10:00:00", 20),
        "[10:05:00] too short",       # gap -> own stretch, dropped
        _line("10:10:00", 20),
    ])
    out = split_document("doc", "mic", content)
    assert [s["stretch_id"] for s in out] == ["doc:0", "doc:1"]
    assert [s["start"] for s in out] == ["10:00:00", "10:10:00"]


def test_fifteen_word_boundary():
    assert len(split_document("d", "m", _line("10:00:00", 15))) == 1
    assert split_document("d", "m", _line("10:00:00", 14)) == []


def test_fields_text_and_hash():
    l1, l2 = _line("10:00:00", 10, "a"), _line("10:00:20", 10, "b")
    out = split_document("doc", "mic1", l1 + "\n" + l2)
    assert len(out) == 1
    s = out[0]
    assert s["doc_id"] == "doc" and s["mic_id"] == "mic1"
    assert (s["start"], s["end"]) == ("10:00:00", "10:00:20")
    assert s["text"] == l1 + "\n" + l2
    assert s["content_hash"] == hashlib.sha256(s["text"].encode()).hexdigest()


def test_non_timestamp_lines_ignored():
    content = "header junk\n" + _line("10:00:00", 20) + "\n\n"
    out = split_document("d", "m", content)
    assert len(out) == 1 and out[0]["text"] == _line("10:00:00", 20)
