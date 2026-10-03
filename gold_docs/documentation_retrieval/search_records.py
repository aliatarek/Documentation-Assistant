"""Shared entry construction for lexical and semantic retrieval."""
from __future__ import annotations

import hashlib
from typing import Iterable


def source_id(model: dict, column: dict | None) -> str:
    return f"{model['id']}{'.' + column['name'] if column else ''}"


def entries(snapshot: dict, aliases: dict[str, list[str]] | None = None,
            layer: str = '') -> list[dict]:
    aliases = aliases or {}
    result = []
    for model in snapshot['models']:
        if layer and model['layer'] != layer:
            continue
        for column in [None, *model['columns']]:
            identifier = source_id(model, column)
            name = column['name'] if column else model['name']
            description = column['description'] if column else model['description']
            data_type = column['data_type'] if column else None
            parts = [name, model['name'], description or '', data_type or '', *aliases.get(identifier, [])]
            result.append({'source_id': identifier, 'model_id': model['id'], 'model': model['name'],
                           'column': name if column else None, 'description': description,
                           'source': column.get('source', model['source']) if column else model['source'],
                           'documentation_status': column.get('documentation_status', 'yaml') if column else 'yaml',
                           'data_type': data_type,
                           'tests': column['tests'] if column else model['tests'],
                           'aliases': aliases.get(identifier, []),
                           'text': '\n'.join(str(part) for part in parts if part)})
    return result


def content_hash(entry: dict) -> str:
    content = '\x1f'.join([entry['source_id'], entry['text']]).encode('utf-8')
    return hashlib.sha256(content).hexdigest()[:12]


def tokenize(text: str) -> list[str]:
    import re
    # Identifiers in dbt are commonly snake_case, while people naturally type
    # words with spaces. Treat deposit_pk, deposit-pk, and "deposit pk" alike.
    normalized = re.sub(r'[_-]+', ' ', text.casefold())
    return re.findall(r"\w+", normalized)
