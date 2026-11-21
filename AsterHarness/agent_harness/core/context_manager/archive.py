"""Append-only transcript archive, independent of the model's effective view.

IDs are Thread-local sequence numbers, never model/wire IDs or list positions in
the effective view. Legacy rollout items acquire the same IDs on every replay.
The rollout remains the durable journal, including events not consumed yet.
"""

from copy import deepcopy

from ..client_common import Item


def history_id(sequence: int) -> str:
    return f"h{sequence:012d}"


class HistoryArchive:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def __len__(self) -> int:
        return len(self._items)

    def append(self, item: Item) -> str:
        identifier = history_id(len(self) + 1)
        self._items[identifier] = deepcopy(item)
        return identifier

    def ids(self) -> list[str]:
        return list(self._items)

    def item(self, identifier: str) -> Item:
        try:
            return deepcopy(self._items[identifier])
        except KeyError:
            raise ValueError(f"Unknown history ID: {identifier}") from None
