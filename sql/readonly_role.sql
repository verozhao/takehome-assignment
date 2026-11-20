-- The login the service connects with. It can read the tables and views in the
-- public schema and change nothing. Run this in the database that holds the data.
CREATE ROLE analyst LOGIN PASSWORD 'analyst';
GRANT USAGE ON SCHEMA public TO analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst;
ALTER ROLE analyst SET default_transaction_read_only = on;

-- Some databases, Pagila among them, let every role create tables in the public schema.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
