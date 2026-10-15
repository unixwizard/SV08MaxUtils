import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from klippy.extras import plr_checkpoint as writer


class Completion:
    def __init__(self):
        self.event = threading.Event()
        self.result = None

    def wait(self):
        assert self.event.wait(3), "worker did not finish"
        return self.result

    def complete(self, result):
        self.result = result
        self.event.set()


def make_writer(tmp_path):
    reactor = SimpleNamespace(
        completion=Completion, async_complete=lambda c, r: c.complete(r)
    )
    printer = SimpleNamespace(
        get_reactor=lambda: reactor,
        register_event_handler=Mock(),
        is_shutdown=lambda: False,
    )
    obj = writer.CheckpointWriter(printer, str(tmp_path / "record.json"))
    obj.start()
    return obj


def test_write_fsync_close_precede_acknowledgement(tmp_path, monkeypatch):
    obj = make_writer(tmp_path)
    calls = []
    fsync = writer.os.fsync
    monkeypatch.setattr(
        writer.os, "fsync", lambda fd: (calls.append("fsync"), fsync(fd))
    )
    obj.commit({"cursor": 123})
    calls.append("ack")
    assert json.loads((tmp_path / "record.json").read_text()) == {"cursor": 123}
    assert calls == ["fsync", "ack"]
    obj.stop()


def test_storage_failure_is_latched_and_not_reported_as_success(tmp_path):
    obj = make_writer(tmp_path)
    obj._write_record = Mock(side_effect=OSError("disk full"))
    with pytest.raises(writer.CheckpointError, match="disk full"):
        obj.commit({"cursor": 2})
    assert "disk full" in obj.failure
    with pytest.raises(writer.CheckpointError):
        obj.commit({"cursor": 3})
    assert obj._write_record.call_count == 1
    obj.stop()


def test_worker_io_does_not_block_caller_thread_other_work(tmp_path):
    obj = make_writer(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = obj._write_record

    def slow(content):
        entered.set()
        assert release.wait(3)
        original(content)

    obj._write_record = slow
    errors = []

    def caller():
        try:
            obj.commit({"cursor": 3})
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=caller)
    thread.start()
    assert entered.wait(3)
    assert obj.busy
    with pytest.raises(writer.CheckpointError, match="Concurrent"):
        obj.commit({"cursor": 4})
    release.set()
    thread.join(3)
    assert not thread.is_alive() and not errors
    obj.stop()


def test_shutdown_never_acknowledges_success(tmp_path):
    obj = make_writer(tmp_path)
    obj.printer.is_shutdown = lambda: True
    with pytest.raises(writer.CheckpointError, match="shut down"):
        obj.commit({"cursor": 1})
    obj.stop()


def test_no_new_write_after_stop(tmp_path):
    obj = make_writer(tmp_path)
    obj.stop()
    with pytest.raises(writer.CheckpointError, match="unavailable"):
        obj.commit({"cursor": 1})
