"""Synchronous private derived-buffer ownership and late-work fences (RM37/RM42)."""

import threading

from relaymind.assets.embeddings import EmbeddingError


def _erase(value):
    if isinstance(value, bytearray):
        value[:] = b"\x00" * len(value)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if type(item) in (int, float):
                value[i] = 0
        value.clear()
    elif hasattr(value, "fill"):
        value.fill(0)
    else:
        raise TypeError("unsupported_private_buffer")


class PrivateLease:
    def __init__(self, owner, scope):
        self.owner, self.scope = owner, dict(scope)
        self.fenced = False
        self._buffers = []
        self._cancel = []

    def check(self):
        if self.fenced:
            raise EmbeddingError("private_state_changed")

    def register(self, buffer):
        with self.owner._lock:
            if self.fenced:
                _erase(buffer)
                raise EmbeddingError("private_state_changed")
            self._buffers.append(buffer)
        return buffer

    def register_cancel(self, cancel):
        with self.owner._lock:
            self.check()
            self._cancel.append(cancel)

    def _invalidate(self):
        with self.owner._lock:
            self.fenced = True
            for cancel in self._cancel:
                cancel()
            for buffer in self._buffers:
                _erase(buffer)
            self._buffers.clear()
            self._cancel.clear()

    def close(self):
        self._invalidate()
        with self.owner._lock:
            self.owner._leases.discard(self)


class PrivateAdaptationBuffers:
    def __init__(self, feedback=None):
        self._lock = threading.RLock()
        self._leases = set()
        self._closed = False
        self._remove = None if feedback is None else feedback.register_invalidator(self.invalidate)

    @property
    def active(self):
        with self._lock:
            return len(self._leases)

    def acquire(self, scope):
        with self._lock:
            if self._closed:
                raise EmbeddingError("private_state_changed")
            lease = PrivateLease(self, scope)
            self._leases.add(lease)
            return lease

    def invalidate(self, scope):
        with self._lock:
            for lease in tuple(self._leases):
                if lease.scope == dict(scope):
                    lease._invalidate()

    def close(self):
        with self._lock:
            self._closed = True
            for lease in tuple(self._leases):
                lease.close()
            remove, self._remove = self._remove, None
        # Store mutations take store -> private-owner. Never acquire the
        # store lock while holding this owner: an in-flight reset/delete
        # must still be able to finish synchronous invalidation.
        if remove is not None:
            remove()
