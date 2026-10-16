"""Pinned archive fetch with disk scratch and fully simulated network/sleep."""

import hashlib
import io
import json
import ssl
import tarfile
import tempfile
from email.message import Message
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import fetch_upstream as source
import pytest

LOCK = json.loads(Path(source.__file__).with_name("hermes.lock.json").read_text())
ROOT = "hermes-agent-" + LOCK["commit"]
NOW = 1_700_000_000


class Response(io.BytesIO):
    def __init__(self, body, fault=None):
        super().__init__(body)
        self.fault = fault

    def info(self):
        return Message()

    def read(self, size=-1):
        if self.fault and self.tell():
            raise self.fault
        return super().read(size)


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                content = b"reviewed fixture source\n"
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            else:
                if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    member.linkname = "../../outside"
                archive.addfile(member)
    return output.getvalue()


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(LOCK["archive_url"], code, "fixture HTTP error", headers, io.BytesIO(b"fixture error"))


@pytest.fixture
def download(tmp_path, monkeypatch):
    original_temporary = tempfile.TemporaryDirectory
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    lock_path = tmp_path / "hermes.lock.json"
    state = SimpleNamespace(destination=tmp_path / "extracted", events=[], calls=[], waits=[],
                            opened=[], scratch=scratch, temporary_paths=[], now=NOW)

    def use_archive(entries=None):
        state.body = archive_bytes(entries or [(ROOT, tarfile.DIRTYPE),
                                               (ROOT + "/package", tarfile.DIRTYPE),
                                               (ROOT + "/package/main.py", tarfile.REGTYPE)])
        lock_path.write_text(json.dumps({**LOCK, "sha256": hashlib.sha256(state.body).hexdigest()}))
        state.events = [Response(state.body)]

    def temporary(**kwargs):
        assert kwargs == {"dir": "/opt"}
        directory = original_temporary(dir=scratch)
        state.temporary_paths.append(Path(directory.name))
        return directory

    def urlopen(url, data=None, *, timeout=None):
        assert url == LOCK["archive_url"] and data is None
        state.calls.append((url, timeout))
        assert state.events, "unexpected extra download"
        event = state.events.pop(0)
        if hasattr(event, "close"):
            state.opened.append(event)
        if isinstance(event, BaseException):
            raise event
        return event

    monkeypatch.setattr(source, "__file__", str(tmp_path / "fetch_upstream.py"))
    monkeypatch.setattr(source, "tempfile", SimpleNamespace(TemporaryDirectory=temporary))
    monkeypatch.setattr(source, "time", SimpleNamespace(time=lambda: state.now, sleep=state.waits.append),
                        raising=False)
    monkeypatch.setattr(source.urllib.request, "urlopen", urlopen)
    state.use_archive = use_archive
    state.fetch = lambda: source.fetch(state.destination)
    use_archive()
    yield state
    try:
        assert not list(scratch.iterdir())
        assert all(not path.exists() for path in state.temporary_paths)
    finally:
        for response in state.opened:
            response.close()


def assert_verified(download, calls):
    assert download.calls == [(LOCK["archive_url"], 60)] * calls
    assert (download.destination / "package/main.py").read_bytes() == b"reviewed fixture source\n"
    assert (download.destination / ".agentic-commit").read_text() == LOCK["commit"] + "\n"
    assert all(response.closed for response in download.opened)


@pytest.mark.parametrize("code", [429, 502, 503, 504])
def test_transient_http_retries_exact_pinned_archive_and_then_extracts(download, code):
    download.events.insert(0, http_error(code))
    download.fetch()
    assert_verified(download, 2)
    assert download.waits == [5]


def test_final_allowed_attempt_can_succeed_after_two_transient_failures(download):
    download.events[:0] = [http_error(429), http_error(502)]
    download.fetch()
    assert_verified(download, 3)
    assert download.waits == [5, 10]


@pytest.mark.parametrize("code", [429, 502, 503, 504])
def test_three_attempts_exhaust_without_extracting_and_close_errors(download, code):
    download.events = [http_error(code) for _ in range(3)]
    with pytest.raises(HTTPError) as error:
        download.fetch()
    assert error.value.code == code
    assert len(download.calls) == 3 and download.waits == [5, 10]
    assert all(response.closed for response in download.opened)
    assert not download.destination.exists()


@pytest.mark.parametrize("header,wait", [("0", 0), ("7", 7), ("60", 60)])
def test_retry_after_seconds_are_honored(download, header, wait):
    download.events.insert(0, http_error(429, header))
    download.fetch()
    assert_verified(download, 2)
    assert download.waits == [wait]


@pytest.mark.parametrize("offset,wait", [(11, 11), (60, 60), (-10, 0)])
def test_retry_after_http_date_uses_current_utc_time(download, offset, wait):
    download.events.insert(0, http_error(503, formatdate(NOW + offset, usegmt=True)))
    download.fetch()
    assert_verified(download, 2)
    assert download.waits == [wait]


@pytest.mark.parametrize("header", ["", "later", "-1", "1.5", "NaN", "Infinity",
    "61", pytest.param("9" * 5000, id="unbounded-integer"), formatdate(NOW + 61, usegmt=True),
    "Fri, 01 Jan 9999 00:00:00 GMT",
    "Tue, 14 Nov 2023 22:13:31"])
def test_invalid_or_excessive_retry_after_fails_closed_without_early_retry(download, header):
    download.events.insert(0, http_error(429, header))
    with pytest.raises(ValueError, match="Retry-After"):
        download.fetch()
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed and not download.destination.exists()


@pytest.mark.parametrize("code", [400, 401, 403, 404, 408, 410, 500, 501])
def test_other_http_errors_are_permanent_even_with_retry_after(download, code):
    download.events.insert(0, http_error(code, "5"))
    with pytest.raises(HTTPError) as error:
        download.fetch()
    assert error.value.code == code
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed and not download.destination.exists()


@pytest.mark.parametrize("error", [URLError("unavailable"), URLError(ssl.SSLCertVerificationError("untrusted")),
                                   TimeoutError("request timeout")])
def test_url_tls_and_timeout_errors_are_not_retried(download, error):
    download.events.insert(0, error)
    with pytest.raises(type(error)):
        download.fetch()
    assert len(download.calls) == 1 and download.waits == []
    assert not download.destination.exists()


def test_partial_download_is_discarded_before_successful_retry(download):
    first = Response(b"partial data longer than the final archive" * 100, http_error(503))
    download.events.insert(0, first)
    download.fetch()
    assert_verified(download, 2)
    assert first.closed and first.fault.closed and download.waits == [5]


@pytest.mark.parametrize("error", [URLError("stream failed"), TimeoutError("read timeout"), OSError("disk full")])
def test_partial_download_permanent_failure_closes_response_and_cleans_disk(download, error):
    download.events = [Response(b"incomplete archive", error)]
    with pytest.raises(type(error)):
        download.fetch()
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed and not download.destination.exists()


def test_partial_archive_write_failure_does_not_retry_and_cleans_disk(download, monkeypatch):
    original_open = Path.open
    written = []

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def write(self, value):
            written.append(self.stream.write(value[:8]))
            self.stream.flush()
            raise OSError("fixture disk full")

    def open_path(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return PartialWriter(stream) if path.name == "upstream.tar.gz" and mode == "wb" else stream

    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(OSError, match="fixture disk full"):
        download.fetch()
    assert written == [8]
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed and not download.destination.exists()
    assert not list(download.scratch.iterdir())


def test_wrong_digest_is_not_retried_or_extracted(download, monkeypatch):
    download.events = [Response(download.body + b"changed")]
    monkeypatch.setattr(source, "tarfile", SimpleNamespace(open=lambda *_a, **_k: pytest.fail("unverified tar opened")))
    with pytest.raises(ValueError, match="digest mismatch"):
        download.fetch()
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed and not download.destination.exists()


@pytest.mark.parametrize("name,kind,message", [
    ("unexpected/file", tarfile.REGTYPE, "Unexpected archive root"),
    (ROOT + "/../escaped", tarfile.REGTYPE, "Unsafe source path"),
    ("/" + ROOT + "/absolute", tarfile.REGTYPE, "Unexpected archive root"),
    (ROOT + "/pipe", tarfile.FIFOTYPE, "Special source file refused"),
])
def test_original_archive_path_and_type_guards_do_not_retry(download, name, kind, message):
    download.use_archive([(name, kind)])
    with pytest.raises(ValueError, match=message):
        download.fetch()
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed
    assert not (download.destination / ".agentic-commit").exists()
    assert not (download.destination.parent / "escaped").exists()


def test_regular_files_extract_but_symlinks_and_hardlinks_stay_excluded(download):
    download.use_archive([(ROOT, tarfile.DIRTYPE), (ROOT + "/package", tarfile.DIRTYPE),
                          (ROOT + "/package/main.py", tarfile.REGTYPE),
                          (ROOT + "/symlink", tarfile.SYMTYPE), (ROOT + "/hardlink", tarfile.LNKTYPE)])
    download.fetch()
    assert_verified(download, 1)
    assert sorted(path.name for path in download.destination.iterdir()) == [".agentic-commit", "package"]


def test_invalid_tar_with_valid_digest_is_not_retried(download):
    body = b"not a tar archive"
    Path(source.__file__).with_name("hermes.lock.json").write_text(json.dumps({**LOCK,
        "sha256": hashlib.sha256(body).hexdigest()}))
    download.events = [Response(body)]
    with pytest.raises(tarfile.ReadError):
        download.fetch()
    assert len(download.calls) == 1 and download.waits == []
    assert download.opened[0].closed
