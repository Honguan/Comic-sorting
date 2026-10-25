"""Snapshot arithmetic shared by phase workers and live queue statistics."""
from collections.abc import MutableMapping
from collections import Counter
from decimal import Decimal


def used_models(record):
    models = record.get('models', [])
    if isinstance(models, str):
        models = [models]
    if not isinstance(models, (list, tuple)):
        models = []
    return {model for model in [*models, record.get('model')] if isinstance(model, str) and model}


def model_usage(record):
    """Attribute legacy usage only when exactly one model is known."""
    if 'by_model' in record:
        return record['by_model']
    if not record:
        return {}
    models = used_models(record)
    model = next(iter(models)) if len(models) == 1 else ''
    fields = ('requests', 'total_tokens', 'prompt', 'completion', 'cache_hit', 'cache_write',
              'cost', 'cost_incomplete', 'missing_usage_requests', 'unpriced_requests')
    value = {key: record[key] for key in fields if key in record}
    if value.get('cost') is not None:
        value['cost'] = str(value['cost'])
    return {model: value}


def merge_snapshots(records):
    result = {}
    unknown_cost = False
    for record in records:
        models = used_models(record)
        if models:
            result['models'] = sorted(set(result.get('models', [])) | models)
        unknown_cost |= record.get('cost_incomplete', False) or ('cost' in record and record['cost'] is None)
        for key, value in record.items():
            if key == 'models':
                continue
            if key == 'by_model':
                models = result.setdefault(key, {})
                for model, usage in value.items():
                    models[model] = merge_snapshots((models.get(model, {}), usage))
            elif key == 'model_rates':
                result.setdefault(key, {}).update(value)
            elif key == 'cost' and value is not None:
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
        self.model_counts = {}
        self.model_totals = {}

    def __getitem__(self, key):
        return self.records[key]

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def adjust(self, group, record, direction):
        models = self.model_counts.setdefault(group, Counter())
        for model in used_models(record):
            models[model] += direction
            if not models[model]:
                del models[model]
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
        previous = self.records.get(key, {})
        old_models, new_models = previous.get('by_model', {}), record.get('by_model', {})
        if old_models or new_models:
            totals = self.model_totals.setdefault(group, SnapshotTotals(lambda item: item[1]))
            for model in old_models.keys() - new_models.keys():
                del totals[key, model]
            for model, value in new_models.items():
                totals[key, model] = value
        if key not in self.records:
            self.group_counts[group] = self.group_counts.get(group, 0) + 1
        self.adjust(group, self.records.get(key, {}), -1)
        self.records[key] = dict(record)
        if 'models' in record:
            self.records[key]['models'] = sorted(used_models(record))
        if 'by_model' in record:
            self.records[key]['by_model'] = {model: dict(value) for model, value in new_models.items()}
        self.adjust(group, record, 1)

    def __delitem__(self, key):
        group = self.group(key)
        record = self.records.pop(key)
        for model in record.get('by_model', {}):
            del self.model_totals[group][key, model]
        self.adjust(group, record, -1)
        self.group_counts[group] -= 1

    def clear(self):
        self.records.clear()
        self.groups.clear()
        self.group_counts.clear()
        self.model_counts.clear()
        self.model_totals.clear()

    def aggregate(self, group=None):
        result = {key: value[0] for key, value in self.groups.get(group, {}).items()}
        if self.model_counts.get(group):
            result['models'] = sorted(self.model_counts[group])
        if group in self.model_totals:
            totals = self.model_totals[group]
            models = {model: totals.aggregate(model) for model, count in totals.group_counts.items() if count}
            if models:
                result['by_model'] = models
        if result and 'cost' not in result:
            result['cost'] = None
        partial = result.pop('cost_incomplete', 0)
        if partial or ('cost' in self.groups.get(group, {}) and self.groups[group]['cost'][1] < self.group_counts[group]):
            result['cost_incomplete'] = True
        return result
