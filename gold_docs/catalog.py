"""Documentation-only catalog. Readers are independent of HTTP and UI code."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import yaml


class CatalogError(ValueError):
    pass


class Reader(Protocol):
    def read(self) -> dict: ...


class YamlReader:
    def __init__(self, roots: dict[str, Path]):
        self.roots = roots

    def read(self) -> dict:
        models = []
        digest = hashlib.sha256()
        for layer, root in self.roots.items():
            if not root.is_dir():
                raise CatalogError(f"Source folder is missing: {root}")
            seen = set()
            sql = {p.stem: p for p in root.rglob('*.sql') if '__MACOSX' not in p.parts}
            files = sorted(p for p in root.rglob('*') if p.suffix in {'.yml', '.yaml'} and '__MACOSX' not in p.parts)
            for path in files:
                source = f"{layer}/{path.relative_to(root).as_posix()}"
                raw = path.read_bytes()
                digest.update(source.encode() + raw)
                try:
                    doc = yaml.safe_load(raw) or {}
                    if not isinstance(doc, dict) or not isinstance(doc.get('models', []), list):
                        raise CatalogError('Expected a mapping with a models list')
                    for model in doc.get('models', []):
                        name = model['name']
                        if not isinstance(name, str) or not name or name in seen:
                            raise CatalogError(f'Duplicate or invalid model name: {name}')
                        seen.add(name)
                        columns = model.get('columns') or []
                        if not isinstance(columns, list):
                            raise CatalogError('Columns must be a list')
                        entries, names = [], set()
                        for column in columns:
                            cname = column['name']
                            if not isinstance(cname, str) or not cname or cname in names:
                                raise CatalogError(f'Duplicate or invalid column: {cname}')
                            names.add(cname)
                            entries.append({
                                'name': cname,
                                'description': self.description(column.get('description')),
                                'data_type': column.get('data_type'),
                                'tests': column.get('data_tests', column.get('tests', [])) or [],
                            })
                        models.append({'id': f'{layer}/{name}', 'layer': layer, 'name': name,
                                       'description': self.description(model.get('description')),
                                       'source': source, 'has_yaml': True, 'columns': entries,
                                       'tests': model.get('data_tests', model.get('tests', [])) or []})
                except (yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise CatalogError(f'{source}: {exc}') from exc
            for name, path in sorted(sql.items()):
                digest.update(f'{layer}/{path.relative_to(root).as_posix()}'.encode())
                if name not in seen:
                    models.append({'id': f'{layer}/{name}', 'layer': layer, 'name': name,
                                   'description': None, 'source': f'{layer}/{path.relative_to(root).as_posix()}',
                                   'has_yaml': False, 'columns': [], 'tests': []})
        return {'version': digest.hexdigest()[:12], 'models': sorted(models, key=lambda m: m['id'])}

    @staticmethod
    def description(value):
        if value is None or value == '':
            return None
        if not isinstance(value, str):
            raise CatalogError('Description must be text')
        return value if value.strip() else None


class Catalog:
    def __init__(self, reader: Reader):
        self.reader = reader
        self._snapshot = {}
        self._retriever = None
        self._retrieval_entries = {}
        self._retrieval_alias_fingerprint = None
        self._last_ranking = {}
        self._source_modified_at = {}
        self.refresh()

    def refresh(self):
        # Assign only after parsing AND JSON validation succeed: preserve last good catalog.
        candidate = self.reader.read()
        # Keep YAML parsing untouched, then overlay only output columns that a
        # best-effort static SQL audit can prove exist. This makes a missing
        # YAML entry visible as an undocumented field without inventing a
        # description. SQL parse failures are retained as diagnostics instead
        # of invalidating the documentation catalog.
        from .sql_output_columns import outputs
        sql_outputs, sql_unresolved = outputs(self.reader.roots)
        by_model = {(model['layer'], model['name']): model for model in candidate['models']}
        for key, (output_columns, sql_source) in sql_outputs.items():
            model = by_model.get(key)
            if model is None:
                continue
            model['sql_source'] = sql_source
            documented_names = {column['name'] for column in model['columns']}
            for name in output_columns:
                if name not in documented_names:
                    model['columns'].append({
                        'name': name, 'description': None, 'data_type': None,
                        'tests': [], 'documentation_status': 'sql_only', 'source': sql_source,
                    })
        candidate['sql_unresolved_models'] = sql_unresolved
        # SQL output changes must also create a new catalog version, even
        # though YamlReader intentionally hashes only its own input parsing.
        overlay = json.dumps({model['id']: [column['name'] for column in model['columns']]
                              for model in candidate['models']}, sort_keys=True).encode('utf-8')
        candidate['version'] = hashlib.sha256(candidate['version'].encode('utf-8') + overlay).hexdigest()[:12]
        json.dumps(candidate)
        loaded_at = datetime.now(timezone.utc).isoformat()
        for model in candidate['models']:
            layer, relative = model['source'].split('/', 1)
            path = self.reader.roots[layer] / relative
            self._source_modified_at[model['source']] = datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc).isoformat()
            if model.get('sql_source'):
                sql_layer, sql_relative = model['sql_source'].split('/', 1)
                sql_path = self.reader.roots[sql_layer] / sql_relative
                self._source_modified_at[model['sql_source']] = datetime.fromtimestamp(
                    sql_path.stat().st_mtime, timezone.utc).isoformat()
        candidate['loaded_at'] = loaded_at
        self._snapshot = candidate
        # The vector index is synchronized lazily on first search. This keeps YAML
        # parsing independent from optional embedding-runtime startup.
        self._retrieval_alias_fingerprint = None
        return candidate

    def snapshot(self):
        return self._snapshot

    def search(self, question: str, layer: str = '', aliases: dict[str, list[str]] | None = None) -> list[dict]:
        """Hybrid BM25 + semantic retrieval fused by reciprocal rank fusion."""
        aliases = aliases or {}
        self._ensure_retrieval(aliases)
        source_ids, ranking = self._retriever.search(question, aliases, k=len(self._retrieval_entries))
        self._last_ranking = ranking
        results = []
        for rank, identifier in enumerate(source_ids, 1):
            entry = self._retrieval_entries[identifier]
            if layer and not identifier.startswith(f'{layer}/'):
                continue
            results.append({'model_id': entry['model_id'], 'model': entry['model'], 'column': entry['column'],
                            'description': entry['description'], 'source': entry['source'],
                            'documentation_status': entry.get('documentation_status', 'yaml'), 'score': -rank})
        return results

    def ranking(self) -> dict:
        """Diagnostics for the most recent search; useful for the local ranking UI."""
        return self._last_ranking

    def _ensure_retrieval(self, aliases: dict[str, list[str]]) -> None:
        alias_fingerprint = json.dumps(aliases, sort_keys=True, ensure_ascii=False)
        if self._retriever is None:
            from .documentation_retrieval.bge_m3_embeddings import Embedder
            from .documentation_retrieval.hybrid_rank_fusion import HybridSearch
            from .documentation_retrieval.bm25_lexical import LexicalIndex
            from .documentation_retrieval.incremental_indexer import Reindexer
            from .documentation_retrieval.numpy_vector_store import VectorIndex
            vector = VectorIndex(Path(__file__).with_name('catalog-vectors.npz'))
            self._reindexer = Reindexer(LexicalIndex(), vector, Embedder())
            self._retriever = HybridSearch(self._reindexer.lexical, vector, self._reindexer.embedder)
        if alias_fingerprint != self._retrieval_alias_fingerprint:
            current, _ = self._reindexer.reindex(self._snapshot, aliases)
            self._retrieval_entries = {entry['source_id']: entry for entry in current}
            self._retrieval_alias_fingerprint = alias_fingerprint

    def evidence(self, question: str, layer: str = '', limit: int | None = None,
                 max_evidence_chars: int = 4_500,
                 aliases: dict[str, list[str]] | None = None) -> list[dict]:
        """Return highest-ranked evidence that fits the LLM prompt budget."""
        # An explicit ``model.column`` (or "column in model") request is not
        # a semantic-search problem.  The named record is authoritative even
        # when a related field has a more fluent description.  This prevents
        # e.g. ``limit_reference_description`` from being used as proof of the
        # distinct ``dim_loan.limit_reference`` field.
        explicit = self._explicit_field_evidence(question, layer, aliases)
        entries = []
        used_chars = 2  # JSON list brackets
        models = {model['id']: model for model in self._snapshot['models']}
        for result in self.search(question, layer, aliases):
            model = models[result['model_id']]
            column = next((item for item in model['columns'] if item['name'] == result['column']), None)
            source_id = f"{result['model_id']}{'.' + result['column'] if result['column'] else ''}"
            entry = {'source_id': source_id, 'source_file': result['source'],
                     'source_modified_at': self._source_modified_at[result['source']],
                     'catalog_loaded_at': self._snapshot['loaded_at'],
                     'model_description': model['description'], 'column': result['column'],
                     'description': result['description'], 'declared_type': column['data_type'] if column else None,
                     'documentation_status': result['documentation_status'],
                     'aliases': (aliases or {}).get(source_id, [])}
            entry_chars = len(json.dumps(entry, ensure_ascii=False)) + 1
            if entries and used_chars + entry_chars > max_evidence_chars:
                break
            entries.append(entry)
            used_chars += entry_chars
            if limit is not None and len(entries) >= limit:
                break
        if not explicit:
            return entries
        explicit_ids = {entry['source_id'] for entry in explicit}
        # Keep related high-ranking records visible as context in the UI.  The
        # server uses ``exact_match`` to ensure they cannot establish the
        # meaning of the explicitly requested field.
        return explicit + [entry for entry in entries if entry['source_id'] not in explicit_ids]

    @staticmethod
    def _identifier_words(value: str) -> str:
        """Make dbt identifiers and normal user wording comparable."""
        return ' '.join(re.findall(r'[a-z0-9]+', value.casefold().replace('_', ' ')))

    @classmethod
    def _mentions_identifier(cls, question_words: str, identifier: str) -> bool:
        value = cls._identifier_words(identifier)
        return bool(value) and f' {value} ' in f' {question_words} '

    def _explicit_field_evidence(self, question: str, layer: str,
                                 aliases: dict[str, list[str]] | None) -> list[dict]:
        """Return exact fields named together with a model in the question.

        We deliberately require both a model and a column.  An unqualified
        name such as ``limit_reference`` can legitimately occur in several
        models; normal hybrid retrieval remains responsible for that ambiguous
        request.  A qualified request, however, must never be redirected to a
        sibling field merely because the sibling is better documented.
        """
        question_words = self._identifier_words(question)
        aliases = aliases or {}
        selected = []
        for model in self._snapshot['models']:
            if layer and model['layer'] != layer:
                continue
            if not self._mentions_identifier(question_words, model['name']):
                continue
            for column in model['columns']:
                if not self._mentions_identifier(question_words, column['name']):
                    continue
                source_id = f"{model['id']}.{column['name']}"
                selected.append({
                    'source_id': source_id,
                    'exact_match': True,
                    'source_file': column.get('source', model['source']),
                    'source_modified_at': self._source_modified_at[
                        column.get('source', model['source'])],
                    'catalog_loaded_at': self._snapshot['loaded_at'],
                    'model_description': model['description'],
                    'column': column['name'],
                    'description': column['description'],
                    'declared_type': column['data_type'],
                    'documentation_status': column.get('documentation_status', 'yaml'),
                    'aliases': aliases.get(source_id, []),
                })
        return selected

    def source_ids(self) -> set[str]:
        """Identifiers that aliases are permitted to target."""
        return {f"{model['id']}.{column['name']}" for model in self._snapshot['models'] for column in model['columns']}


def coverage(models: list[dict]) -> dict:
    columns = [c for m in models for c in m['columns']]
    yaml_columns = [column for column in columns if column.get('documentation_status') != 'sql_only']
    described = sum(bool(c['description']) for c in yaml_columns)
    sql_only = [column for column in columns if column.get('documentation_status') == 'sql_only']
    return {'models': len(models), 'columns_listed': len(yaml_columns), 'columns_described': described,
            'columns_undocumented': len(yaml_columns) - described,
            'sql_columns_missing_from_yaml': len(sql_only),
            'description_percent': round(100 * described / len(yaml_columns), 1) if yaml_columns else None,
            'columns_typed': sum(bool(c['data_type']) for c in yaml_columns),
            'models_without_yaml': sum(not m['has_yaml'] for m in models)}
