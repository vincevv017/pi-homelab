CREATE OR REPLACE PROCEDURE FIX_SQL_LOCAL(BROKEN_SQL STRING, ERROR_MESSAGE STRING)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = 3.11
  HANDLER = 'fix_sql'
  EXTERNAL_ACCESS_INTEGRATIONS = (pi_sql_fixer_integration)
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  SECRETS = ('pi_token' = pi_sql_fixer_token)
AS
$$
import _snowflake
import requests

PI_ENDPOINT = "https://<PI2_FUNNEL_FQDN>:8443/fix-sql"

def fix_sql(session, broken_sql, error_message):
    token = _snowflake.get_generic_secret_string('pi_token')
    resp = requests.post(
        PI_ENDPOINT,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"sql": broken_sql,
              "error": error_message or "",
              "dialect": "snowflake"},
        timeout=160,
    )
    resp.raise_for_status()
    return resp.json()
$$;