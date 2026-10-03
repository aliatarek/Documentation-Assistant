"""Loopback-only, dependency-light HTTP adapter for the documentation catalog."""
import argparse
import json
import os
import re
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .catalog import Catalog, YamlReader, coverage
from .answer_providers.claude_api import ClaudeRag
from .catalog_monitoring.file_watcher import CatalogWatcher




class AliasStore:
    """Persistent local aliases; aliases aid retrieval but are never documentation facts."""
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, list[str]]:
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            aliases = value.get('aliases', {})
            if not isinstance(aliases, dict):
                raise ValueError('aliases must be an object')
            return {key: [item for item in items if isinstance(item, str)] for key, items in aliases.items() if isinstance(key, str) and isinstance(items, list)}
        except FileNotFoundError:
            return {}
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'Alias file is invalid: {exc}') from exc

    def add(self, source_id: str, phrase: str, valid_ids: set[str]) -> dict[str, list[str]]:
        phrase = ' '.join(phrase.split())
        if source_id not in valid_ids:
            raise ValueError('Choose a documented column from the displayed evidence')
        if not 2 <= len(phrase) <= 160:
            raise ValueError('Alias must contain 2 to 160 characters')
        aliases = self.read()
        current = aliases.setdefault(source_id, [])
        if phrase.casefold() not in {item.casefold() for item in current}:
            current.append(phrase)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version': 1, 'aliases': aliases}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, self.path)
        return aliases


class ApprovedDescriptionWriter:
    """Apply a reviewer-approved description to an existing blank YAML field.

    This deliberately supports only a field already declared in YAML. SQL-only
    additions need a richer YAML editor and should be reviewed separately.
    """
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    @staticmethod
    def description_lines(indent: int, description: str) -> list[str]:
        """Keep approved descriptions in the existing one-line YAML shape."""
        try:
            import yaml
            if yaml.safe_load(description) == description and not description.startswith(('#', '-', '?', ':', '!', '&', '*', '@', '`')):
                return [f"{' ' * indent}description: {description}"]
        except Exception:
            pass
        raise ValueError('Description cannot be written safely as an unquoted one-line YAML value. Edit it to avoid YAML syntax such as a colon followed by a space or a leading special character.')

    def apply(self, source_id: str, description: str, approved_name: str | None = None) -> dict:
        description = ' '.join(description.split())
        if not 4 <= len(description) <= 1_000:
            raise ValueError('Approved description must contain 4 to 1,000 characters.')
        model_id, separator, column_name = source_id.rpartition('.')
        if not separator:
            raise ValueError('Choose a column from the displayed evidence.')
        model = next((item for item in self.catalog.snapshot()['models'] if item['id'] == model_id), None)
        if not model:
            raise ValueError('The selected model is not in the active catalog.')
        column = next((item for item in model['columns'] if item['name'] == column_name), None)
        if not column:
            raise ValueError('The selected column is not in the active catalog.')
        is_sql_only = column.get('documentation_status') == 'sql_only'
        approved_name = (approved_name or column_name).strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', approved_name):
            raise ValueError('Column name must be a valid dbt-style identifier.')
        if not is_sql_only and approved_name != column_name:
            raise ValueError('Only SQL-only review proposals may change the column name.')
        if column.get('description'):
            raise ValueError('This column already has a YAML description and cannot be overwritten here.')
        existing_yaml_names = {item['name'] for item in model['columns']
                               if item.get('documentation_status') != 'sql_only'}
        if is_sql_only and approved_name in existing_yaml_names:
            raise ValueError('That YAML column name already exists in this model.')

        layer, relative = model['source'].split('/', 1)
        root = self.catalog.reader.roots[layer].resolve()
        path = (root / relative).resolve()
        if root not in path.parents:
            raise ValueError('The selected source file is not an approved catalog path.')
        if not model['has_yaml']:
            if path.suffix != '.sql' or not path.is_file():
                raise ValueError('The SQL source for this undocumented model is unavailable.')
            yaml_path = path.with_suffix('.yml')
            alternate_yaml_path = path.with_suffix('.yaml')
            if yaml_path.exists() or alternate_yaml_path.exists():
                raise ValueError('A YAML file already exists for this model; reload the catalog before approving a description.')
            created_lines = [
                'models:',
                f'  - name: {model["name"]}',
                '    columns:',
                f'      - name: {approved_name}',
                *self.description_lines(8, description),
            ]
            temporary = yaml_path.with_suffix('.yml.tmp')
            temporary.write_bytes(('\n'.join(created_lines) + '\n').encode('utf-8'))
            os.replace(temporary, yaml_path)
            return {'source_id': source_id, 'approved_source_id': f'{model_id}.{approved_name}',
                    'source_file': f'{layer}/{yaml_path.relative_to(root).as_posix()}',
                    'description': description, 'created_model_yaml': True}
        if path.suffix not in {'.yml', '.yaml'} or not path.is_file():
            raise ValueError('The selected YAML source file is not an approved catalog path.')
        raw = path.read_bytes().decode('utf-8-sig')
        newline = '\r\n' if '\r\n' in raw else '\n'
        lines = raw.splitlines()
        model_match = re.compile(rf"^(\s*)-\s+name:\s*['\"]?{re.escape(model['name'])}['\"]?\s*(?:#.*)?$")
        column_match = re.compile(rf"^(\s*)-\s+name:\s*['\"]?{re.escape(column_name)}['\"]?\s*(?:#.*)?$")
        model_index = next((index for index, line in enumerate(lines) if model_match.match(line)), None)
        if model_index is None:
            raise ValueError('Could not locate the selected model in its YAML file.')
        model_indent = len(model_match.match(lines[model_index]).group(1))
        if is_sql_only:
            columns_indent = model_indent + 2
            columns_match = re.compile(rf"^\s{{{columns_indent}}}columns:\s*(?:#.*)?$")
            columns_index = None
            for index in range(model_index + 1, len(lines)):
                candidate = model_match.match(lines[index])
                if candidate and len(candidate.group(1)) <= model_indent:
                    break
                if columns_match.match(lines[index]):
                    columns_index = index
                    break
            if columns_index is None:
                raise ValueError('Could not locate the model columns list in its YAML file.')
            insertion_index = len(lines)
            for index in range(columns_index + 1, len(lines)):
                stripped = lines[index].strip()
                indent = len(lines[index]) - len(lines[index].lstrip())
                if stripped and indent <= columns_indent:
                    insertion_index = index
                    break
            column_indent = columns_indent + 2
            description_lines = self.description_lines(column_indent + 2, description)
            lines[insertion_index:insertion_index] = [f"{' ' * column_indent}- name: {approved_name}",
                                                       *description_lines, '']
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_bytes((newline.join(lines) + newline).encode('utf-8'))
            os.replace(temporary, path)
            return {'source_id': source_id, 'approved_source_id': f'{model_id}.{approved_name}',
                    'source_file': model['source'], 'description': description}

        column_index = None
        column_indent = None
        for index in range(model_index + 1, len(lines)):
            candidate = model_match.match(lines[index])
            if candidate and len(candidate.group(1)) <= model_indent:
                break
            candidate = column_match.match(lines[index])
            if candidate and len(candidate.group(1)) > model_indent:
                column_index, column_indent = index, len(candidate.group(1))
                break
        if column_index is None:
            raise ValueError('Could not locate the selected blank column in its YAML file.')

        block_end = len(lines)
        next_column = re.compile(rf"^\s{{{column_indent}}}-\s+name:")
        for index in range(column_index + 1, len(lines)):
            if next_column.match(lines[index]):
                block_end = index
                break
        description_lines = self.description_lines(column_indent + 2, description)
        existing = re.compile(rf"^\s{{{column_indent + 2}}}description:\s*")
        for index in range(column_index + 1, block_end):
            if existing.match(lines[index]):
                lines[index:index + 1] = description_lines
                break
        else:
            lines[column_index + 1:column_index + 1] = description_lines

        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_bytes((newline.join(lines) + newline).encode('utf-8'))
        os.replace(temporary, path)
        return {'source_id': source_id, 'approved_source_id': source_id,
                'source_file': model['source'], 'description': description}




class VersionStore:
    """Append-only local record of successfully activated catalog versions."""
    def __init__(self, path: Path):
        self.path = path

    def record(self, snapshot: dict, reason: str) -> None:
        try:
            history = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            history = []
        if history and history[-1]['version'] == snapshot['version'] and history[-1]['loaded_at'] == snapshot['loaded_at']:
            return
        history.append({'version': snapshot['version'], 'loaded_at': snapshot['loaded_at'], 'reason': reason})
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(history, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, self.path)

    def read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return []


class CitationRankStore:
    """Persistent measurements used to conservatively trim LLM evidence."""
    def __init__(self, path: Path, minimum_samples: int = 20, minimum_limit: int = 4):
        self.path, self.minimum_samples, self.minimum_limit = path, minimum_samples, minimum_limit

    def read(self) -> list[int]:
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            return [rank for rank in value.get('ranks', []) if isinstance(rank, int) and rank > 0]
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return []

    def record(self, evidence: list[dict], source_ids: list[str]) -> None:
        positions = {item['source_id']: index for index, item in enumerate(evidence, 1)}
        ranks = self.read()
        ranks.extend(positions[identifier] for identifier in source_ids if identifier in positions)
        ranks = ranks[-1_000:]
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'ranks': ranks}, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, self.path)

    def summary(self) -> dict:
        ranks = sorted(self.read())
        if not ranks:
            return {'samples': 0, 'candidate_limit': None, 'percentile_95_rank': None}
        p95 = ranks[min(len(ranks) - 1, max(0, (95 * len(ranks) + 99) // 100 - 1))]
        limit = max(self.minimum_limit, p95) if len(ranks) >= self.minimum_samples else None
        return {'samples': len(ranks), 'candidate_limit': limit, 'percentile_95_rank': p95}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--gold-root', type=Path,
                        help='Optional folder containing the Gold SQL/YAML files. Overrides --source-root.')
    parser.add_argument('--reporting-root', type=Path,
                        help='Optional folder containing the Reporting SQL/YAML files. Overrides --source-root.')
    parser.add_argument('--anthropic-timeout', type=int, default=45)
    parser.add_argument('--watch-interval', type=float, default=1.0,
                        help='Seconds between documentation checks; 0 disables automatic reload.')
    parser.add_argument('--open-browser', action='store_true',
                        help='Open the local application in the default browser after startup.')
    args = parser.parse_args()
    roots = {
        'l06_marts': args.gold_root or args.source_root / 'l06_marts' / 'l06_marts',
        'l07_mart_views': args.reporting_root or args.source_root / 'l07_mart_views' / 'l07_mart_views',
    }
    catalog = Catalog(YamlReader(roots))
    aliases = AliasStore(Path(__file__).with_name('aliases.json'))
    approved_descriptions = ApprovedDescriptionWriter(catalog)
    versions = VersionStore(Path(__file__).with_name('catalog-versions.json'))
    citation_ranks = CitationRankStore(Path(__file__).with_name('citation-ranks.json'))
    versions.record(catalog.snapshot(), 'server started')
    watcher = CatalogWatcher(catalog, catalog.reader.roots, args.watch_interval)
    page = Path(__file__).with_name('index.html').read_bytes()
    allowed = {f'127.0.0.1:{args.port}', f'localhost:{args.port}'}

    class Handler(BaseHTTPRequestHandler):
        def send(self, value, status=200, mime='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def permitted(self):
            origin = self.headers.get('Origin')
            return self.headers.get('Host') in allowed and (not origin or origin in {f'http://{h}' for h in allowed})

        def do_GET(self):
            if not self.permitted():
                return self.send({'error': 'Local requests only'}, 403)
            parsed = urlparse(self.path)
            if parsed.path == '/':
                return self.send(page, mime='text/html; charset=utf-8')
            if parsed.path == '/api/catalog':
                snapshot = catalog.snapshot()
                return self.send({**snapshot, 'coverage': {layer: coverage([m for m in snapshot['models'] if m['layer'] == layer])
                                                          for layer in ('l06_marts', 'l07_mart_views')},
                                  'watcher': watcher.status(),
                                  # Availability only: the secret itself never
                                  # leaves this Python process.
                                  'providers': {'claude': bool(os.environ.get('ANTHROPIC_API_KEY'))}})
            if parsed.path == '/api/search':
                params = parse_qs(parsed.query)
                return self.send({'results': catalog.search(params.get('q', [''])[0], params.get('layer', [''])[0], aliases.read())})
            if parsed.path == '/api/ranking':
                params = parse_qs(parsed.query)
                question, layer = params.get('q', [''])[0], params.get('layer', [''])[0]
                catalog.search(question, layer, aliases.read())
                return self.send(catalog.ranking())
            if parsed.path == '/api/aliases':
                return self.send({'aliases': aliases.read()})
            if parsed.path == '/api/versions':
                return self.send({'versions': versions.read()})
            return self.send({'error': 'Not found'}, 404)

        def do_POST(self):
            if not self.permitted() or self.headers.get('X-Requested-With') != 'gold-docs':
                return self.send({'error': 'Local application requests only'}, 403)
            if self.path == '/api/ask':
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8_000:
                        raise ValueError('Question request must be between 1 and 8,000 bytes')
                    body = json.loads(self.rfile.read(length))
                    question, layer = body.get('question', '').strip(), body.get('layer', '')
                    provider = body.get('provider', 'claude-haiku')
                    if (not question or layer not in {'l06_marts', 'l07_mart_views'} or
                            provider not in {'claude-haiku', 'claude-sonnet'}):
                        raise ValueError('A question, valid layer, and Claude model are required')
                    # Claude receives a compact candidate set and may select
                    # one or multiple evidence fields.
                    limit = 6
                    evidence = catalog.evidence(question, layer, limit=limit, aliases=aliases.read())
                    if not evidence:
                        return self.send({'answer': 'I could not find supporting documentation for that question.', 'source_ids': [], 'abstained': True, 'evidence': []})
                    # Related fields may stay in the evidence list for a
                    # reviewer to inspect, but they cannot document an exact
                    # field the user explicitly named.  Resolve that status
                    # before either LLM has an opportunity to substitute a
                    # better-described sibling field.
                    exact_undocumented = next(
                        (item for item in evidence
                         if item.get('exact_match') and not item.get('description')),
                        None,
                    )
                    if exact_undocumented:
                        source_id = exact_undocumented['source_id']
                        status = ('exists in SQL but is absent from local YAML'
                                  if exact_undocumented.get('documentation_status') == 'sql_only'
                                  else 'is listed in local YAML but its description is blank')
                        answer = {
                            'answer': (f'`{source_id}` was explicitly requested, but it {status}. '
                                       'Its business meaning is undocumented.'),
                            'source_ids': [source_id], 'abstained': True,
                            'out_of_scope': False, 'answer_basis': 'undocumented',
                            'response_mode': 'exact field selected (documentation status enforced)',
                        }
                    else:
                        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
                        if not api_key:
                            raise ValueError('Claude API is not configured. Set ANTHROPIC_API_KEY in the server terminal, then restart the server.')
                        model = ('claude-haiku-4-5-20251001' if provider == 'claude-haiku'
                                 else 'claude-sonnet-5')
                        answer = ClaudeRag(api_key, model, args.anthropic_timeout).ask(question, evidence)
                        answer['response_mode'] = f'Claude API ({model}) + hybrid retrieval ({len(evidence)} candidates)'
                    if not answer['abstained'] and not answer['out_of_scope']:
                        citation_ranks.record(evidence, answer['source_ids'])
                    answer['retrieval_stats'] = citation_ranks.summary()
                    stats = answer['retrieval_stats']
                    print('Citation-rank stats: '
                          f"samples={stats['samples']}, p95={stats['percentile_95_rank']}, "
                          f"candidate_limit={stats['candidate_limit']}", flush=True)
                    # A model citation is evidence of retrieval, not proof of correctness.
                    # Only a user selecting the column may create an alias.
                    # Retrieval candidates are not useful evidence for a question
                    # the model has classified as unrelated to documentation.
                    return self.send({**answer, 'evidence': [] if answer['out_of_scope'] else evidence})
                except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    return self.send({'error': str(exc)}, 400)
            if self.path == '/api/aliases':
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1_000:
                        raise ValueError('Alias request must be between 1 and 1,000 bytes')
                    body = json.loads(self.rfile.read(length))
                    stored = aliases.add(body.get('source_id', ''), body.get('phrase', ''), catalog.source_ids())
                    return self.send({'aliases': stored})
                except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    return self.send({'error': str(exc)}, 400)
            if self.path == '/api/approve-description':
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 2_000:
                        raise ValueError('Description approval request must be between 1 and 2,000 bytes.')
                    body = json.loads(self.rfile.read(length))
                    saved = approved_descriptions.apply(body.get('source_id', ''), body.get('description', ''),
                                                        body.get('column_name'))
                    snapshot = catalog.refresh()
                    watcher._fingerprint = watcher.fingerprint()
                    versions.record(snapshot, 'reviewer approved description')
                    return self.send({**saved, 'version': snapshot['version']})
                except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    return self.send({'error': str(exc)}, 400)
            if self.path != '/api/reload':
                return self.send({'error': 'Not found'}, 404)
            try:
                snapshot = catalog.refresh()
                versions.record(snapshot, 'manual reload')
                self.send({'version': snapshot['version']})
            except Exception as exc:
                self.send({'error': f'Reload failed; previous catalog retained. {exc}'}, 400)

        def log_message(self, fmt, *values):
            pass  # Do not persist question text or metadata in access logs.

    class WatchingHTTPServer(HTTPServer):
        def service_actions(self):
            # serve_forever invokes this in its own single server thread.
            if watcher.check():
                versions.record(catalog.snapshot(), 'automatic file change')

    server = WatchingHTTPServer(('127.0.0.1', args.port), Handler)
    url = f'http://127.0.0.1:{args.port}'
    print(f'Gold documentation: {url}', flush=True)
    print('Claude API: ' + ('enabled' if os.environ.get('ANTHROPIC_API_KEY') else
                            'disabled (ANTHROPIC_API_KEY is not set for this server process)'),
          flush=True)
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
