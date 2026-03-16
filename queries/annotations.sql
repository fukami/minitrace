-- annotations.sql
-- Unnest and query session annotations.
-- Usage: duckdb -f queries/annotations.sql

SELECT
  id AS session_id,
  environment->>'agent_framework' AS framework,
  CAST(json_extract(ann, '$.annotator') AS VARCHAR) AS annotator,
  CAST(json_extract(ann, '$.content.category') AS VARCHAR) AS category,
  CAST(json_extract(ann, '$.content.title') AS VARCHAR) AS title,
  CAST(json_extract(ann, '$.scope.type') AS VARCHAR) AS scope_type
FROM read_json('examples/**/*.minitrace.json',
  columns={id: 'VARCHAR', environment: 'JSON', annotations: 'JSON[]'},
  ignore_errors=true),
UNNEST(annotations) AS a(ann)
ORDER BY session_id;
