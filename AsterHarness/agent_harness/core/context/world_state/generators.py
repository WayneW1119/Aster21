"""Project current event sources without exposing command arguments or timer contents."""

import json

from . import ContextSection, WorldStateUpdate


class GeneratorsState(ContextSection):
    ID = "generators"
    marker = "_generator_inventory"

    def __init__(self, manager):
        self.manager = manager

    def render_diff(self, previous, items):
        current = self.manager.inventory()
        visible = next((item[self.marker] for item in reversed(items) if self.marker in item), [])
        additions = ()
        if visible != current:
            additions = ({"type": "message", "role": "developer",
                          "text": "Current event generators:\n" + json.dumps(current, ensure_ascii=False),
                          self.marker: current},)
        snapshot = {"registrations": current}
        if additions or previous != snapshot and (current or previous):
            return WorldStateUpdate(self.ID, snapshot, additions)

    def project_items(self, items):
        return [{k: v for k, v in item.items() if k != self.marker} for item in items]
