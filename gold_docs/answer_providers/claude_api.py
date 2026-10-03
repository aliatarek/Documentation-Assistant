"""Alternative Claude RAG experiment; it does not change the local web app.

Run after setting ANTHROPIC_API_KEY in the current terminal:
    python -m gold_docs.claude_rag "Which fields identify a branch and name it?"

The current catalog is first searched by BM25 + BGE-M3. Claude receives only
the top evidence records and may return zero, one, or several source IDs from
that supplied set. It never receives the API key from a browser.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..catalog import Catalog, YamlReader


API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-haiku-4-5-20251001'

RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'answer': {'type': 'string'},
        'source_ids': {'type': 'array', 'items': {'type': 'string'}},
        'abstained': {'type': 'boolean'},
        'out_of_scope': {'type': 'boolean'},
        'answer_basis': {
            'type': 'string',
            'enum': ['yaml_documentation', 't24_general_knowledge', 'undocumented', 'out_of_scope'],
        },
        'proposed_yaml_description': {'type': 'string'},
    },
    'required': ['answer', 'source_ids', 'abstained', 'out_of_scope', 'answer_basis',
                 'proposed_yaml_description'],
    'additionalProperties': False,
}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_aliases() -> dict[str, list[str]]:
    """Read existing user-confirmed aliases without importing the web server."""
    path = Path(__file__).with_name('aliases.json')
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8')).get('aliases', {})
        return {key: items for key, items in value.items()
                if isinstance(key, str) and isinstance(items, list)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def build_catalog() -> Catalog:
    root = project_root()
    return Catalog(YamlReader({
        'l06_marts': root / 'l06_marts' / 'l06_marts',
        'l07_mart_views': root / 'l07_mart_views' / 'l07_mart_views',
    }))


def text_content(response: dict[str, Any]) -> str:
    """Extract Claude's text block from a non-streaming Messages response."""
    return ''.join(block.get('text', '') for block in response.get('content', [])
                   if isinstance(block, dict) and block.get('type') == 'text').strip()


class ClaudeRag:
    """Evidence-grounded, multi-column Claude answerer for evaluation only."""
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: int = 45):
        if not api_key:
            raise ValueError('Set ANTHROPIC_API_KEY; do not put an API key in source code.')
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def ask(self, question: str, evidence: list[dict]) -> dict[str, Any]:
        allowed = {item['source_id'] for item in evidence}
        prompt = (
            'You are a bank data-documentation assistant for a Temenos T24-oriented '
            'Egyptian bank. The bank context may help interpret banking terminology, but '
            'it must never be treated as proof of this bank\'s local implementation. '
            'YAML descriptions in EVIDENCE are the primary documented business source. '
            'OUT_OF_SCOPE is only for a request unrelated to bank data models, tables, '
            'columns, or documentation. A question that names or describes a retrieved field '
            'is always in scope, even when its YAML description is blank or it is marked '
            'sql_only. For YAML-described fields, set answer_basis to yaml_documentation. '
            'For an SQL-only or description-blank field, the catalog is undocumented: cite its '
            'source_id and set abstained to true. When the user asks what such a field means, '
            'automatically attempt a cautious, short general T24 interpretation if one is '
            'genuinely useful; the user does not need to ask for T24 explicitly. In that case set answer_basis '
            'to t24_general_knowledge and explicitly state that it is general T24 knowledge, '
            'not verified against this bank\'s documentation. Do not call it T24 documentation, '
            'do not invent product-specific facts, and do not claim the field is documented. '
            'If no useful general interpretation is justified, set answer_basis to undocumented '
            'and state that the local description is missing. For a supported request, return '
            'every evidence field needed to answer it; this may be one or several fields. '
            'Treat source_ids as precise citations, not as a list of loosely related candidates: '
            'every factual claim in the answer must be supported by one of those cited fields. '
            'Do not say a field is a customer key, identifier, date, status, or anything else '
            'unless the cited evidence explicitly establishes that meaning. Do not cite a field '
            'just because it is from the same model. If a needed field was not supplied in '
            'EVIDENCE, say that it was not retrieved rather than guessing. Never invent a '
            'source ID. Keep answer to at most two short sentences. When answer_basis is '
            't24_general_knowledge, also return proposed_yaml_description: one factual YAML '
            'description sentence of 8 to 35 words, with no disclaimer, no mention of T24, '
            'no reference to documentation, and no related-field discussion. For every other '
            'answer_basis, return proposed_yaml_description as an empty string. Return the '
            'required JSON.\n\n'
            f'QUESTION:\n{question}\n\nEVIDENCE:\n{json.dumps(evidence, ensure_ascii=False)}'
        )
        body = {
            'model': self.model,
            'max_tokens': 500,
            'temperature': 0,
            'system': 'Return structured output matching the supplied JSON schema.',
            'messages': [{'role': 'user', 'content': prompt}],
            'output_config': {'format': {'type': 'json_schema', 'schema': RESPONSE_SCHEMA}},
        }
        request = Request(API_URL, data=json.dumps(body).encode('utf-8'), method='POST', headers={
            'content-type': 'application/json',
            'x-api-key': self.api_key,
            'anthropic-version': '2023-06-01',
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Claude API returned HTTP {exc.code}: {detail}') from exc
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'Claude API request failed: {exc}') from exc

        try:
            result = json.loads(text_content(payload))
        except json.JSONDecodeError as exc:
            raise RuntimeError('Claude did not return valid structured output') from exc
        source_ids = [source_id for source_id in result.get('source_ids', []) if source_id in allowed]
        out_of_scope = bool(result.get('out_of_scope'))
        answer = result.get('answer')
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError('Claude returned an empty answer')
        proposed_description = result.get('proposed_yaml_description', '')
        if not isinstance(proposed_description, str):
            proposed_description = ''
        basis = result.get('answer_basis')
        valid_bases = {'yaml_documentation', 't24_general_knowledge', 'undocumented', 'out_of_scope'}
        if basis not in valid_bases:
            raise RuntimeError('Claude returned an invalid answer basis')
        if out_of_scope:
            basis = 'out_of_scope'

        undocumented_ids = {
            item['source_id'] for item in evidence
            if not item.get('description') or item.get('documentation_status') == 'sql_only'
        }
        cited_undocumented = set(source_ids) & undocumented_ids
        # A local YAML description is the only thing that can make an answer
        # locally documented. Do not let a model's label override that fact.
        # This also prevents a fluent general-T24 answer from being displayed
        # as though it came from this bank's catalog.
        if cited_undocumented and basis == 'yaml_documentation':
            basis = 't24_general_knowledge'
        if basis == 't24_general_knowledge' and not (set(source_ids) & undocumented_ids):
            # General knowledge is only allowed to supplement a field that our catalog
            # explicitly identifies as undocumented.
            basis = 'undocumented'
        if basis == 't24_general_knowledge':
            status_by_id = {item['source_id']: item.get('documentation_status') for item in evidence}
            local_fields = [source_id for source_id in source_ids if source_id in undocumented_ids]
            catalog_status = '; '.join(
                (f'{source_id} exists in SQL but is absent from local YAML'
                 if status_by_id.get(source_id) == 'sql_only'
                 else f'{source_id} is listed in local YAML but its description is blank')
                for source_id in local_fields
            )
            answer = re.sub(
                r'\b(?:according to|as stated in|documented in)\s+(?:the )?'
                r'(?:bank|local catalog|catalog|bank\'s)\s+documentation[:,]?\s*',
                '', answer, flags=re.IGNORECASE)
            answer = (
                f'Catalog status: {catalog_status}. '
                f'General T24 knowledge (unverified for this bank): {answer.strip()}'
            )
            proposed_description = ' '.join(proposed_description.split())
            if not 8 <= len(proposed_description.split()) <= 35:
                proposed_description = ''
        else:
            proposed_description = ''

        return {
            'answer': answer.strip(),
            'source_ids': [] if out_of_scope else source_ids,
            'abstained': (False if out_of_scope else basis in {'t24_general_knowledge', 'undocumented'}
                          or bool(result.get('abstained')) or not source_ids),
            'out_of_scope': out_of_scope,
            'answer_basis': basis,
            'proposed_yaml_description': proposed_description,
            'evidence': evidence,
            'model': self.model,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question')
    parser.add_argument('--layer', choices=('l06_marts', 'l07_mart_views'), default='l06_marts')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--candidates', type=int, default=6,
                        help='Top hybrid-retrieval records sent to Claude (default: 6).')
    args = parser.parse_args()
    if args.candidates < 1:
        parser.error('--candidates must be at least 1')

    catalog = build_catalog()
    evidence = catalog.evidence(args.question, args.layer, limit=args.candidates,
                                aliases=load_aliases())
    if not evidence:
        print(json.dumps({'answer': 'No catalog evidence was retrieved.', 'source_ids': [],
                          'abstained': True, 'out_of_scope': False, 'evidence': []}, indent=2))
        return
    client = ClaudeRag(os.environ.get('ANTHROPIC_API_KEY', ''), args.model)
    print(json.dumps(client.ask(args.question, evidence), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
