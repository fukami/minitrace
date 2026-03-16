-- read-ratio-distribution.sql
-- Analyze read-before-write patterns across frameworks.
-- Read ratio = proportion of tool calls that are READ operations.
-- Higher read ratio suggests more cautious behavior (reading before acting).
-- Usage: duckdb -f queries/read-ratio-distribution.sql

SELECT
  environment->>'agent_framework' AS framework,
  id,
  CAST(metrics->>'tool_call_count' AS INT) AS tools,
  CAST(metrics->>'read_count' AS INT) AS reads,
  CAST(metrics->>'modify_count' AS INT) AS modifies,
  CAST(metrics->>'create_count' AS INT) AS creates,
  CAST(metrics->>'execute_count' AS INT) AS executes,
  ROUND(CAST(metrics->>'read_ratio' AS DOUBLE), 2) AS read_ratio
FROM read_json('examples/**/*.minitrace.json',
  columns={id: 'VARCHAR', environment: 'JSON', metrics: 'JSON'},
  ignore_errors=true)
WHERE CAST(metrics->>'tool_call_count' AS INT) > 0
ORDER BY read_ratio DESC;
