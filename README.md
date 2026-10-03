# Documentation Assistant

Documentation Assistant is a local web application for exploring dbt model documentation and asking grounded questions about Gold and Reporting columns. It combines SQL output-column discovery, dbt YAML documentation, BM25 keyword retrieval, local BGE-M3 semantic retrieval, and Claude answers grounded in a small evidence set.

## What it does

- Reads dbt YAML models and column descriptions.
- Detects SQL output columns absent from YAML and labels them undocumented.
- Searches exact identifiers, aliases, typos, and semantically similar wording.
- Answers with Claude Haiku or Claude Sonnet from source-labelled evidence.
- Shows related evidence, documentation browsing, conversation history, and documentation priorities.
- Reloads the in-memory catalog when YAML or SQL changes.
- Lets a reviewer approve a description and write it to the mapped YAML file in a local Git checkout.

## Architecture

```text
dbt YAML + SQL
  -> Catalog snapshot
  -> model / column retrieval records
  -> BM25 + BGE-M3 rankings
  -> reciprocal-rank fusion
  -> compact evidence set
  -> Claude answer + backend verification
  -> browser UI / reviewer workflow
```

YAML is the local business-documentation source. SQL establishes only that a column exists. General T24 guidance is explicitly labelled unverified; it is never represented as local bank documentation.

## Requirements

- Python 3.10 or newer.
- An Anthropic API key with access to Claude Haiku and/or Sonnet.
- A local clone of the dbt repository that the application should inspect.
- Optional: local `BAAI/bge-m3` SentenceTransformers files. Without BGE-M3, BM25 keyword search remains available.

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Set the API key only in the terminal that starts the server:

```powershell
$env:ANTHROPIC_API_KEY = "your-key"
```

Never place API keys in source code, JSON, browser JavaScript, or Git.

## Run this copy

```powershell
cd D:\CSB\MCP
$env:ANTHROPIC_API_KEY = "your-key"
python -m gold_docs.server --open-browser
```

The server opens `http://127.0.0.1:8765`. Stop it with `Ctrl+C`.

The PowerShell launcher can prompt for the key without displaying it:

```powershell
.\start_documentation_assistant.ps1 -UseClaude
```

## Run against a team repository

The application edits only the files on the machine where the server runs. Clone the chosen GitHub repository locally, then point the app at that clone:

```powershell
git clone https://github.com/ORG/DBT-REPOSITORY.git D:\work\dbt-repository
cd D:\CSB\MCP
$env:ANTHROPIC_API_KEY = "your-key"
python -m gold_docs.server --source-root D:\work\dbt-repository --open-browser
```

`--source-root` expects this folder layout:

```text
l06_marts/l06_marts/
l07_mart_views/l07_mart_views/
```

For another layout, pass the input folders directly:

```powershell
python -m gold_docs.server `
  --gold-root D:\work\dbt-repository\path\to\gold `
  --reporting-root D:\work\dbt-repository\path\to\reporting `
  --open-browser
```

## Reviewer-approved YAML edits and GitHub

1. Run the app against a local clone of the repository.
2. Ask about an explicitly named undocumented field.
3. Review/edit the proposed description in the UI.
4. Select **Save approved description to YAML**.
5. The backend validates the catalog field and writes only its mapped YAML file in that local clone.
6. Use the team’s normal Git branch, commit, push, and pull-request process.

```powershell
cd D:\work\dbt-repository
git status
git switch -c docs/describe-column
git add path\to\model.yml
git commit -m "docs: describe column"
git push -u origin docs/describe-column
```

The app deliberately does not auto-commit, push, or open pull requests: those actions need reviewer authority and the team’s approval process.

## Retrieval pipeline

1. `catalog.py` parses YAML and overlays SQL-only columns in memory.
2. `retrieval_entries.py` flattens every model and column into a searchable record.
3. `lexical_index.py` tokenizes text and ranks keyword matches with BM25.
4. `embedder.py` turns text into local BGE-M3 vectors; `vector_index.py` ranks vectors by cosine similarity.
5. `hybrid_search.py` combines both rank lists using Reciprocal Rank Fusion.
6. `claude_rag.py` receives only the selected evidence and returns structured output.
7. Backend checks citations against supplied evidence and enforces YAML/SQL-only documentation status.

When a user explicitly names `model.column`, that exact field is authoritative. Related fields may appear as context but cannot establish that the requested field is documented.

## Security and storage

- The app binds only to `127.0.0.1`; it is a single-user prototype.
- Conversation history and documentation priorities are browser-local storage; history can be downloaded as JSON.
- Vector cache, aliases, catalog versions, citation rank data, API-key files, and the optional local-Qwen implementation are ignored by Git.
- Before shared production use, add organisational authentication, auditing, concurrency controls, access control, and security review.

## Verify

```powershell
python -m unittest discover -s tests -v
python -m py_compile gold_docs\server.py gold_docs\catalog.py
```

## Current project layout

```text
gold_docs/
  server.py              HTTP API, reviewer write endpoint, watcher
  catalog.py             YAML/SQL catalog and evidence construction
  sql_output_columns.py  Static SQL output-column extraction
  retrieval_entries.py   Search-record construction
  lexical_index.py       BM25 keyword retrieval
  embedder.py            BGE-M3 embedding adapter
  vector_index.py        Persistent NumPy vector index
  reindexer.py           Incremental index updater
  change_tracking.py     Per-record embedding hash comparison
  hybrid_search.py       RRF coordinator
  claude_rag.py          Claude adapter
  index.html             Browser UI
```
