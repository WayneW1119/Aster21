"""A transient line index for replaying a locked journal without loading it all.

The index is never persisted. Slices share it and decode one row at a time, so
recovery hashes and backwards lookups retain their existing sequence semantics.
"""
from collections.abc import Sequence
import json


class JournalRows(Sequence):
    def __init__(self, stream):
        self._stream = stream
        self._offsets = []  # TextIO seek cookies can exceed 64 bits (legacy CR newlines).
        stream.seek(0)
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith('\n'):
                raise ValueError('Rollout is incomplete; automatic crash recovery is not implemented')
            self._offsets.append(offset)
        self._indices = range(len(self._offsets))

    def __len__(self):
        return len(self._indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            view = object.__new__(type(self))
            view._stream = self._stream
            view._offsets = self._offsets
            view._indices = self._indices[index]
            return view
        self._stream.seek(self._offsets[self._indices[index]])
        return json.loads(self._stream.readline())
