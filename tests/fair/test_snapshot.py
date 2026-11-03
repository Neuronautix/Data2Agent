"""Frozen external evidence is collected once, then served only as collected."""

from __future__ import annotations

import json

import pytest

from data2agent.cli import main
from data2agent.errors import ModeError, OutputError
from data2agent.mcp import DatasetService
from data2agent.mcp.modes import MODES
from data2agent.profiles.fair.live import fetch_public_document
from data2agent.snapshot import MAX_READ_CHARS, Snapshot, SnapshotError, collect, validate_spec

DOI = "https://doi.org/10.5281/zenodo.0000000"
API = "https://api.repository.example/records/0000000"


def _spec(**overrides):
    spec = {
        "snapshot_spec_version": "1.0",
        "dataset_label": "example-published",
        "requests": [
            {"id": "doi", "url": DOI, "accept": "text/html", "purpose": "F1/3 resolution"},
            {"id": "record", "url": API, "accept": "application/json", "purpose": "F1/2 issuer"},
        ],
    }
    spec.update(overrides)
    return spec


def _fake_fetch(responses):
    calls = []

    def fetch(url, *, accept, max_body):
        calls.append(url)
        return dict(responses[url])

    fetch.calls = calls
    return fetch


REACHED = {
    "source": DOI,
    "observed_at": "2026-11-02T10:00:00Z",
    "result": "reached",
    "status_code": 200,
    "final_url": "https://repository.example/records/0000000",
    "content_type": "text/html; charset=utf-8",
    "chain": [{"url": DOI, "status_code": 302}],
    "body": b"<html>DOI 10.5281/zenodo.0000000</html>",
    "truncated": False,
}
FAILED = {
    "source": API,
    "observed_at": "2026-11-02T10:00:01Z",
    "result": "network_error",
    "error_type": "TimeoutError",
    "chain": [],
}


def _collected(tmp_path, spec=None):
    fetch = _fake_fetch({DOI: REACHED, API: FAILED})
    out = tmp_path / "snap"
    collect(spec or _spec(), out, fetch=fetch)
    return out, fetch


def test_collect_freezes_bodies_with_checksums_and_keeps_failures(tmp_path):
    out, fetch = _collected(tmp_path)
    assert fetch.calls == [DOI, API]
    data = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
    doi, record = data["records"]
    assert doi["body_path"] == "bodies/doi.html" and doi["body_bytes"] == len(REACHED["body"])
    assert (out / doi["body_path"]).read_bytes() == REACHED["body"]
    # A timeout is an observation, not an absence: it stays in the snapshot.
    assert record["result"] == "network_error" and record["body_path"] is None
    assert data["collector"]["method"] == "bounded-public-http-get-document"


@pytest.mark.parametrize(
    "change",
    [
        {"url": API + "?token=x"},  # query strings could carry data out
        {"url": "http://127.0.0.1/admin"},
        {"url": "file:///etc/passwd"},
        {"url": "https://example.org/café"},  # http.client cannot send it
        {"url": "https://exämple.org/records"},
        {"id": "../escape"},
        {"id": "doi"},  # duplicate
        {"extra": "field"},
    ],
)
def test_invalid_spec_is_refused_before_any_fetch(tmp_path, change):
    spec = _spec()
    spec["requests"][1] = {**spec["requests"][1], **change}
    fetch = _fake_fetch({})
    with pytest.raises(SnapshotError):
        collect(spec, tmp_path / "snap", fetch=fetch)
    assert fetch.calls == []
    assert not (tmp_path / "snap").exists()


def test_spec_needs_version_label_and_requests():
    for bad in (_spec(snapshot_spec_version="0.9"), _spec(dataset_label=""), _spec(requests=[])):
        with pytest.raises(SnapshotError):
            validate_spec(bad)


def test_existing_snapshot_is_never_overwritten(tmp_path):
    out, _ = _collected(tmp_path)
    with pytest.raises(SnapshotError, match="never overwritten"):
        collect(_spec(), out, fetch=_fake_fetch({DOI: REACHED, API: FAILED}))


def test_a_collection_that_stops_early_leaves_nothing_behind(tmp_path):
    def fetch(url, *, accept, max_body):
        if url == API:
            raise KeyboardInterrupt
        return dict(REACHED)

    out = tmp_path / "snap"
    with pytest.raises(KeyboardInterrupt):
        collect(_spec(), out, fetch=fetch)
    assert not out.exists()
    collect(_spec(), out, fetch=_fake_fetch({DOI: REACHED, API: FAILED}))
    assert (out / "snapshot.json").exists()


@pytest.mark.parametrize("tail", ["é", "€", "😀"])
def test_a_cut_utf8_body_serves_its_complete_characters(tmp_path, tail):
    whole = ("x" * 20 + tail).encode()
    cut = {**REACHED, "body": whole[:-1], "truncated": True}
    out = tmp_path / "snap"
    collect(_spec(), out, fetch=_fake_fetch({DOI: cut, API: FAILED}))
    page = Snapshot(out).read("doi")
    assert page["text"] == "x" * 20 and page["truncated"] is True
    # The stored bytes are the collected ones, incomplete character included.
    assert (out / "bodies/doi.html").read_bytes() == whole[:-1]


def test_an_uncut_body_ending_mid_character_is_not_text(tmp_path):
    broken = {**REACHED, "body": "xé".encode()[:-1], "truncated": False}
    out = tmp_path / "snap"
    collect(_spec(), out, fetch=_fake_fetch({DOI: broken, API: FAILED}))
    assert Snapshot(out).read("doi")["text"] is None


def test_reading_refuses_a_body_that_drifted(tmp_path):
    out, _ = _collected(tmp_path)
    snapshot = Snapshot(out)
    assert "zenodo.0000000" in snapshot.read("doi")["text"]
    (out / "bodies/doi.html").write_bytes(b"<html>edited</html>")
    with pytest.raises(SnapshotError, match="no longer matches"):
        snapshot.read("doi")
    with pytest.raises(SnapshotError, match="no longer matches"):
        Snapshot(out)


def test_read_pages_text_and_bounds_requests(tmp_path):
    out, _ = _collected(tmp_path)
    snapshot = Snapshot(out)
    page = snapshot.read("doi", offset=5, limit=3)
    assert page["text"] == REACHED["body"].decode()[5:8]
    assert page["total_chars"] == len(REACHED["body"].decode())
    assert snapshot.read("record")["text"] is None
    with pytest.raises(KeyError):
        snapshot.read("missing")
    for offset, limit in ((-1, 10), (0, 0), (0, MAX_READ_CHARS + 1)):
        with pytest.raises(ValueError):
            snapshot.read("doi", offset=offset, limit=limit)


def test_snapshot_tools_appear_only_when_a_snapshot_is_attached(ingested, tmp_path):
    out, _ = _collected(tmp_path)
    for mode in ("raw", "structured", "fair-rules"):
        plain = DatasetService(ingested.output_dir, mode=mode)
        assert plain.available_tools() == list(MODES[mode].tools)
        with pytest.raises(ModeError):
            plain.list_snapshot_records()
        attached = DatasetService(ingested.output_dir, mode=mode, evidence_snapshot=out)
        assert attached.available_tools()[-2:] == ["list_snapshot_records", "read_snapshot_record"]
        listing = attached.list_snapshot_records()
        assert [r["id"] for r in listing["records"]] == ["doi", "record"]
        assert "body_path" not in listing["records"][0]


def test_snapshot_bound_to_another_dataset_is_refused(ingested, tmp_path):
    out, _ = _collected(tmp_path, _spec(dataset_id="sha256:" + "0" * 64))
    with pytest.raises(OutputError, match="bound to dataset"):
        DatasetService(ingested.output_dir, evidence_snapshot=out)
    service = DatasetService(ingested.output_dir)
    bound, _ = _collected(tmp_path / "bound", _spec(dataset_id=service.dataset_id))
    assert DatasetService(ingested.output_dir, evidence_snapshot=bound).snapshot is not None


def test_fetch_keeps_body_and_flags_truncation(monkeypatch):
    def request(url, *, accept, max_body):
        return {
            "url": url,
            "status_code": 200,
            "location": None,
            "content_type": "application/json",
            "body": b"x" * (max_body + 1),
        }

    monkeypatch.setattr("data2agent.profiles.fair.live._request", request)
    result = fetch_public_document(API, accept="application/json", max_body=10)
    assert result["result"] == "reached" and result["truncated"] is True
    assert result["body"] == b"x" * 10


def test_fetch_reports_a_redirect_to_a_private_address_as_an_error(monkeypatch):
    def request(url, *, accept, max_body):
        return {
            "url": url,
            "status_code": 302,
            "location": "http://localhost/secret",
            "content_type": None,
            "body": b"",
        }

    monkeypatch.setattr("data2agent.profiles.fair.live._request", request)
    result = fetch_public_document(DOI, accept="text/html", max_body=10)
    assert result["result"] == "network_error" and result["error_type"] == "PublicURLRequired"
    assert "body" not in result


@pytest.mark.parametrize("location", ["https://example.org/café", "https://exämple.org/"])
def test_fetch_reports_a_non_ascii_redirect_as_an_error(monkeypatch, location):
    def request(url, *, accept, max_body):
        return {
            "url": url,
            "status_code": 302,
            "location": location,
            "content_type": None,
            "body": b"",
        }

    monkeypatch.setattr("data2agent.profiles.fair.live._request", request)
    result = fetch_public_document(DOI, accept="text/html", max_body=10)
    assert result["result"] == "network_error" and result["error_type"] == "PublicURLRequired"
    assert [hop["url"] for hop in result["chain"]] == [DOI]


def test_fetch_records_an_encoding_failure_instead_of_raising(monkeypatch):
    def request(url, *, accept, max_body):
        raise UnicodeEncodeError("ascii", "é", 0, 1, "ordinal not in range(128)")

    monkeypatch.setattr("data2agent.profiles.fair.live._request", request)
    result = fetch_public_document(DOI, accept="text/html", max_body=10)
    assert result["result"] == "network_error" and result["error_type"] == "UnicodeEncodeError"


def test_cli_snapshot_rejects_an_unreadable_spec(tmp_path, capsys):
    assert main(["snapshot", str(tmp_path / "absent.json"), "-o", str(tmp_path / "s")]) == 2
    assert "cannot read spec" in capsys.readouterr().err
