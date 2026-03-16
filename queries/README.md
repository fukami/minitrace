# DuckDB Queries for minitrace

Query minitrace JSON files directly with [DuckDB](https://duckdb.org/) -- no loading step, no database setup.

## Usage

```bash
# Run a query file
duckdb -f queries/framework-summary.sql

# One-liner against your own data
duckdb -c "
SELECT id, environment->>'model' as model
FROM read_json('examples/**/*.minitrace.json',
  columns={id: 'VARCHAR', environment: 'JSON'},
  ignore_errors=true)
"
```

## Adjusting the data path

All query files use `'examples/**/*.minitrace.json'` as the default path.
Change this to match your archive location:

```sql
-- Local adapter output
'./output/active/*/*.minitrace.json'

-- Recursive glob
'./data/**/*.minitrace.json'

-- Specific framework run
'./scenario-runs/2026-03-15/minitrace/active/*/*.minitrace.json'
```

## Available Queries

| File | Purpose |
|------|---------|
| `framework-summary.sql` | Aggregate stats per framework |
| `session-list.sql` | List all sessions with key fields |
| `tool-operation-breakdown.sql` | Tool call counts by framework and operation type |
| `read-ratio-distribution.sql` | Read ratio analysis (read-before-write patterns) |
| `timing-analysis.sql` | Duration, time-to-first-action, idle ratio |
| `annotations.sql` | Unnest and query annotations |

## Requirements

DuckDB 1.0+ (earlier versions may lack `read_json` features).

```bash
# Install
brew install duckdb    # macOS
# or: pip install duckdb-cli
```
