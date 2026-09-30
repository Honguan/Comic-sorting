"""Snapshot arithmetic shared by phase workers and live queue statistics."""
from collections.abc import MutableMapping
from decimal import Decimal


def merge_snapshots(records):
    result = {}
    unknown_cost = False
    for record in records:
        unknown_cost |= record.get('cost_incomplete', False) or ('cost' in record and record['cost'] is None)
        for key, value in record.items():
            if key == 'cost' and value is not None:
                result[key] = str(Decimal(result.get(key) or '0') + Decimal(value))
            elif type(value) in (int, float):
                result[key] = (result.get(key) or 0) + value
            elif key not in result:
                result[key] = value
    if unknown_cost and result.get('cost') is not None:
        result['cost_incomplete'] = True
    return result


class SnapshotTotals(MutableMapping):
    """Replace cumulative snapshots and update numeric totals by their delta.

    Values are copied on assignment; publish a new snapshot to change a record.
    Missing values stay missing, including unknown costs and elapsed time.
    """
    def __init__(self, group=lambda key: None):
        self.group = group
        self.records, self.groups = {}, {}
        self.group_counts = {}

    def __getitem__(self, key):
        return self.records[key]

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def adjust(self, group, record, direction):
        totals = self.groups.setdefault(group, {})
        for field, value in record.items():
            if field == 'cost' and value is not None:
                value = Decimal(value)
            elif field == 'cost_incomplete':
                value = int(bool(value))
            elif type(value) not in (int, float):
                continue
            amount, count = totals.get(field, (0, 0))
            count += direction
            if count:
                totals[field] = amount + direction * value, count
            else:
                totals.pop(field, None)

    def __setitem__(self, key, record):
        group = self.group(key)
        if key not in self.records:
            self.group_counts[group] = self.group_counts.get(group, 0) + 1
        self.adjust(group, self.records.get(key, {}), -1)
        self.records[key] = dict(record)
        self.adjust(group, record, 1)

    def __delitem__(self, key):
        group = self.group(key)
        self.adjust(group, self.records.pop(key), -1)
        self.group_counts[group] -= 1

    def clear(self):
        self.records.clear()
        self.groups.clear()
        self.group_counts.clear()

    def aggregate(self, group=None):
        result = {key: value[0] for key, value in self.groups.get(group, {}).items()}
        if result and 'cost' not in result:
            result['cost'] = None
        partial = result.pop('cost_incomplete', 0)
        if partial or ('cost' in self.groups.get(group, {}) and self.groups[group]['cost'][1] < self.group_counts[group]):
            result['cost_incomplete'] = True
        return result
