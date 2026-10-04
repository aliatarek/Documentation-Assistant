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
- BAAI/bge-m3 is required for semantic retrieval.

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Download BGE-M3 once before starting the application:

```powershell
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"
```

BGE-M3 is required for the intended hybrid keyword and semantic retrieval pipeline. It downloads once and is reused from the local cache.

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

The launcher is simply an alternative way to start the same app. Choose either this launcher or the manual Python command; do not run both:

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

## Approving documentation in the UI

1. Start the app against a local clone of the documentation repository.
2. Ask about an undocumented field.
3. Review or edit the proposed column name and description.
4. Select **Save approved description to YAML**.
5. The backend validates the field and writes the approved description directly to its mapped YAML file.
6. The catalog refreshes so future searches use the new documentation.

No separate command is needed to edit YAML. The UI saves the approved description automatically. Use the team’s normal Git commit, push, and pull-request process only when the saved change is ready to be shared.

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

## Team API-key storage

For a team deployment, store ANTHROPIC_API_KEY in the company-approved secret manager or in the protected environment of the server service account. The deployment process sets the environment variable when starting Python. The browser never receives the key, and the key is never committed to Git.
