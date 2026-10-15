"""Durable I/O worker. Production uses AtomicBatchWriter in plr_batch.

One request may be outstanding. The reactor yields while file I/O releases the
GIL. Success is reported only after persistence, never on queue submission.
"""

import json
import logging
import os
import queue
import threading

try:
    import fcntl
except ImportError:  # permits host-side tests; production is Linux
    fcntl = None


class CheckpointError(Exception):
    pass


class CheckpointWriter:
    def __init__(self, printer, path="/home/sovol/sovol_plr_height"):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.path = path
        self.requests = queue.Queue(maxsize=1)
        self.thread = None
        self.closed = False
        self.busy = False
        self.failure = None
        printer.register_event_handler("klippy:ready", self.start)
        printer.register_event_handler("klippy:shutdown", self.stop)
        printer.register_event_handler("klippy:disconnect", self.stop)

    def start(self):
        if self.thread is not None or self.closed:
            return
        self.thread = threading.Thread(
            target=self._worker, name="plr-checkpoint-writer", daemon=True
        )
        self.thread.start()

    def stop(self):
        # No join or Event.wait on the reactor. An in-progress fsync is allowed
        # to finish; pending/new requests cannot dispatch motion after shutdown.
        self.closed = True
        try:
            self.requests.put_nowait(None)
        except queue.Full:
            pass

    def commit(self, content):
        if self.closed or self.thread is None or not self.thread.is_alive():
            raise CheckpointError("PLR checkpoint writer is unavailable")
        if self.failure is not None:
            raise CheckpointError("PLR checkpoint writer failed: " + self.failure)
        if self.busy:
            raise CheckpointError("Concurrent PLR checkpoint request rejected")
        completion = self.reactor.completion()
        self.busy = True
        try:
            # Copy the payload before yielding; G-code state stays reactor-owned.
            self.requests.put_nowait((dict(content), completion))
            result = completion.wait()  # cooperative greenlet wait, NOT a join
            if result is not None:
                raise CheckpointError("PLR checkpoint was not committed: " + result)
            if self.closed or self.printer.is_shutdown():
                raise CheckpointError(
                    "Printer shut down during PLR checkpoint; move not submitted"
                )
        finally:
            self.busy = False

    def _write_record(self, content):
        # Exactly the original persistence operation, now on the I/O worker.
        # Format/trigger/order are deliberately not changed in this narrow fix.
        with open(self.path, "w") as stream:
            json.dump(content, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def _worker(self):
        lock_fd = None
        try:
            while True:
                request = self.requests.get()
                if request is None:
                    return
                content, completion = request
                result = None
                locked = False
                try:
                    if self.closed:
                        raise CheckpointError("Writer stopped before this write began")
                    if lock_fd is None:
                        lock_fd = os.open(
                            self.path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600
                        )
                    # Prevent a still-finishing old instance from overwriting a
                    # new instance's record across an in-process Klipper restart.
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX)
                        locked = True
                    if self.closed:
                        raise CheckpointError(
                            "Writer stopped while waiting for its file lock"
                        )
                    self._write_record(content)
                except BaseException as exc:
                    result = "%s: %s" % (type(exc).__name__, exc)
                    self.failure = result
                    logging.exception("PLR checkpoint persistence failed")
                finally:
                    if locked:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except OSError as exc:
                            result = "PLR checkpoint lock release failed: %s" % (exc,)
                            self.failure = result
                    # Success is signaled ONLY after write, flush, fsync, and close.
                    self.reactor.async_complete(completion, result)
                if self.closed or self.failure is not None:
                    return
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
