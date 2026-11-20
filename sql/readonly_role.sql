-- The login the service connects with. It can read every table and change nothing.
-- pg_read_all_data is built into PostgreSQL 14 and later.
CREATE ROLE analyst LOGIN PASSWORD 'analyst';
GRANT pg_read_all_data TO analyst;
ALTER ROLE analyst SET default_transaction_read_only = on;
