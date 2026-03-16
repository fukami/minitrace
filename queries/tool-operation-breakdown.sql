-- tool-operation-breakdown.sql
-- Count tool calls by framework and operation type (READ, MODIFY, NEW, EXECUTE, DELEGATE, OTHER).
-- Usage: duckdb -f queries/tool-operation-breakdown.sql

SELECT
  environment->>'agent_framework' AS framework,
  REPLACE(CAST(json_extract(tc, '$.operation_type') AS VARCHAR), '"', '') AS operation,
  COUNT(*) AS count
FROM read_json('examples/**/*.minitrace.json',
  columns={id: 'VARCHAR', environment: 'JSON', tool_calls: 'JSON[]'},
  ignore_errors=true),
UNNEST(tool_calls) AS t(tc)
GROUP BY framework, operation
ORDER BY framework, count DESC;
