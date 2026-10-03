"""Best-effort static SQL output-column extraction for catalog gap visibility.

This does not compile dbt or inspect a warehouse. SQL that cannot be resolved
is reported as unresolved and does not prevent the YAML catalog from loading.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import sqlglot
from jinja2 import Environment, StrictUndefined
from sqlglot import exp


def _variable(name: str, default=None):
    if default is not None:
        return default
    return 0 if 'numeric' in name else '2000-01-01'


def render(source: str, incremental: bool = False) -> str:
    """Render harmless placeholders sufficient for static projection parsing."""
    return Environment(undefined=StrictUndefined).from_string(source).render(
        config=lambda **kwargs: '', ref=lambda name: name, this='__this__',
        var=_variable, is_incremental=lambda: incremental,
        dbt_utils=SimpleNamespace(generate_surrogate_key=lambda *args, **kwargs: "'__hash__'"),
        build_scd2=lambda *args, **kwargs: 'SELECT * FROM __unresolved_macro__',
        get_valid_dim_rows=lambda relation, *args, **kwargs: f'SELECT * FROM {relation}',
        current_timestamp_ts6=lambda: 'CURRENT_TIMESTAMP',
        business_run_date=lambda: "DATE '2000-01-01'", hash=lambda *args, **kwargs: "'__hash__'",
        parse_t24_date=lambda *args, **kwargs: 'NULL', run_started_at=datetime.datetime(2000, 1, 1),
        modules=SimpleNamespace(datetime=datetime),
        dbt_date=SimpleNamespace(get_base_dates=lambda **kwargs: 'SELECT * FROM __unresolved_dates__',
                                 get_fiscal_periods=lambda *args, **kwargs: 'SELECT * FROM __unresolved_dates__'),
    )


def columns(query, ctes=None, stack=()):
    """Return explicit output names and resolve only local CTE stars."""
    ctes = dict(ctes or {})
    if isinstance(query, exp.Subquery):
        return columns(query.this, ctes, stack)
    block = query.args.get('with_')
    if block:
        ctes.update({cte.alias_or_name: cte.this for cte in block.expressions})
    if isinstance(query, exp.SetOperation):
        left, right = columns(query.this, ctes, stack), columns(query.expression, ctes, stack)
        if len(left) != len(right):
            raise ValueError('Set-operation column counts differ')
        return left
    if not isinstance(query, exp.Select):
        raise ValueError(f'Unsupported output expression: {type(query).__name__}')
    relations = {}
    from_clause = query.args.get('from_')
    if from_clause:
        relations[from_clause.this.alias_or_name] = from_clause.this
    for join in query.args.get('joins', []):
        relations[join.this.alias_or_name] = join.this

    def expand(relation):
        if isinstance(relation, exp.Subquery):
            return columns(relation.this, ctes, stack)
        name = relation.name
        if name not in ctes or name in stack:
            raise ValueError(f'Unresolved star from {name}')
        return columns(ctes[name], ctes, (*stack, name))

    result = []
    for item in query.expressions:
        if isinstance(item, exp.Star):
            if any(item.args.values()) or len(relations) != 1:
                raise ValueError('Ambiguous/modified star')
            result.extend(expand(next(iter(relations.values()))))
        elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            result.extend(expand(relations[item.table]))
        elif isinstance(item, (exp.Alias, exp.Column)) and item.alias_or_name:
            result.append(item.alias_or_name)
        else:
            raise ValueError(f'Unnamed output: {item.sql()}')
    if len(result) != len(set(result)):
        raise ValueError('Duplicate output names')
    return result


def outputs(roots: dict[str, Path]) -> tuple[dict[tuple[str, str], tuple[list[str], str]], dict[str, str]]:
    """Return resolved model columns and parse errors keyed by layer/model."""
    resolved, unresolved = {}, {}
    for layer, root in roots.items():
        for path in sorted(root.rglob('*.sql')):
            if '__MACOSX' in path.parts:
                continue
            key = (layer, path.stem)
            relative = f'{layer}/{path.relative_to(root).as_posix()}'
            try:
                source = path.read_text(encoding='utf-8-sig')
                full = columns(sqlglot.parse_one(render(source), read='trino'))
                incremental = columns(sqlglot.parse_one(render(source, True), read='trino'))
                if full != incremental:
                    raise ValueError('Full-refresh and incremental output lists differ')
                resolved[key] = (full, relative)
            except Exception as exc:
                unresolved[f'{layer}/{path.stem}'] = str(exc)
    return resolved, unresolved
