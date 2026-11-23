"""Build the offline crafting catalog from MC_tree and a target Java data pack."""
import argparse
from collections import Counter
import json
from pathlib import Path


def normalize(recipe):
    """Extract the material counts for plain shaped and shapeless crafting."""
    result = recipe.get('result')
    if not isinstance(result, dict) or result.keys() - {'id', 'count'}:
        return None
    if recipe['type'] == 'minecraft:crafting_shaped':
        pattern = recipe['pattern']
        slots = [recipe['key'][symbol] for row in pattern for symbol in row if symbol != ' ']
        table = len(pattern) > 2 or max(map(len, pattern)) > 2
    elif recipe['type'] == 'minecraft:crafting_shapeless':
        slots = recipe['ingredients']
        table = len(slots) > 4
    else:
        return None
    materials = Counter()
    for slot in slots:
        if slot == '#minecraft:planks':
            materials['planks'] += 1
        elif isinstance(slot, str) and slot.startswith('minecraft:'):
            materials[slot.removeprefix('minecraft:')] += 1
        else:
            return None
    return [dict(sorted(materials.items())), result.get('count', 1), table]


def build(source, target):
    catalog = json.loads(source.read_text(encoding='utf-8'))
    candidates = {}
    outputs = Counter()
    for file in sorted(target.glob('*.json')):
        recipe = json.loads(file.read_text(encoding='utf-8'))
        result = recipe.get('result')
        if not recipe['type'].startswith('minecraft:crafting_') or not isinstance(result, dict):
            continue
        item = result['id'].removeprefix('minecraft:')
        outputs[item] += 1
        other = catalog.get(file.stem)
        normalized = normalize(recipe)
        if normalized and other and recipe['result']['id'] == other.get('result', {}).get('id') and normalize(other) == normalized:
            candidates[item] = normalized
    return {k: v for k, v in sorted(candidates.items()) if outputs[k] == 1}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'target', 'output'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    rows = build(args.source, args.target)
    if not rows:
        parser.error('No matching recipes in the provided inputs')
    args.output.write_text(json.dumps(rows, sort_keys=True, indent=1) + '\n', encoding='utf-8')
    print(f'{len(rows)} compatible crafting outputs')
