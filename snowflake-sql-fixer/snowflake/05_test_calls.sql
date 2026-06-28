CALL FIX_SQL_LOCAL(
  'SELECT custmer_id, SUM(amount) FORM orders GROUP BY custmer_id',
  'SQL compilation error: syntax error line 1 at position 38 unexpected ''FORM'''
);
CALL FIX_SQL_LOCAL(
  'SELECT * FORM customers WHERE created_at > current_date - 7',
  'syntax error ... unexpected FORM'
);
SELECT $1:fixed_sql::STRING AS fixed_sql
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
CALL FIX_SQL_LOCAL(
  'SELECT custmer_id, SUM(amount) FORM orders GROUP BY custmer_id',
  'SQL compilation error: syntax error line 1 at position 38 unexpected ''FORM'''
);
CALL FIX_SQL_LOCAL(
  'SELECT GETDATE() AS ts, ISNULL(email, ''none'') AS email FROM customers',
  'SQL compilation error: Unknown function GETDATE'
);
CALL FIX_SQL_LOCAL(
  'SELECT GETDATE() AS ts, ISNULL(email, ''none'') AS email FROM customers',
  'SQL compilation error: Unknown function GETDATE'
);
CALL FIX_SQL_LOCAL(
  'SELECT region, channel, SUM(revenue) AS rev FROM sales GROUP BY region',
  'SQL compilation error: ''CHANNEL'' is not a valid group by expression'
);
CALL FIX_SQL_LOCAL(
  'SELECT order_id, amount, ROW_NUMBER() AS rn FROM orders',
  'SQL compilation error: Window function [ROW_NUMBER] requires an OVER clause'
);