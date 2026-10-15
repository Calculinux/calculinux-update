
from calculinux_update.opkg import status

STATUS_SAMPLE = """Package: foo\nVersion: 1.0\n\nPackage: bar\nVersion: 2.0\n\n"""


def test_load_status_entries(tmp_path):
    path = tmp_path / "status"
    path.write_text(STATUS_SAMPLE)
    entries = status.load_status_entries(path)
    assert [entry.name for entry in entries] == ["foo", "bar"]


def test_write_status_entries(tmp_path):
    path = tmp_path / "status"
    entries = [
        status.StatusEntry(name="foo", raw="Package: foo\n"),
        status.StatusEntry(name="bar", raw="Package: bar\n"),
    ]
    status.write_status_entries(path, entries)
    assert "foo" in path.read_text()


def test_filter_entries(tmp_path):
    entries = [status.StatusEntry(name="foo", raw=""), status.StatusEntry(name="bar", raw="")]
    kept = status.filter_entries(entries, ["bar"])
    assert len(kept) == 1 and kept[0].name == "bar"


def test_load_status_index(tmp_path):
    path = tmp_path / "status"
    path.write_text(STATUS_SAMPLE)
    index = status.load_status_index(path)
    assert set(index.keys()) == {"foo", "bar"}


def test_entry_is_removed():
    removed = status.StatusEntry(
        name="foo", raw="Package: foo\nStatus: deinstall ok not-installed\n"
    )
    installed = status.StatusEntry(
        name="bar", raw="Package: bar\nStatus: install ok installed\n"
    )
    no_status = status.StatusEntry(name="baz", raw="Package: baz\nVersion: 1.0\n")
    assert status.entry_is_removed(removed)
    assert not status.entry_is_removed(installed)
    assert not status.entry_is_removed(no_status)
