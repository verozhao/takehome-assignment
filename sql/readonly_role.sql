-- The login the service connects with. It can read the tables and views in the
-- public schema and change nothing. Run this in the database that holds the data.
-- It is safe to run again after an update.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analyst') THEN
        CREATE ROLE analyst LOGIN PASSWORD 'analyst';
    END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst;
ALTER ROLE analyst SET default_transaction_read_only = on;

-- Some databases, Pagila among them, let every role create tables in the public schema.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- A role may cancel or end its own sessions, and every caller shares this login, so one
-- question could end another caller's query. This takes the two functions away from every
-- role that is not a superuser. A role that needs them can be granted EXECUTE again.
REVOKE EXECUTE ON FUNCTION pg_cancel_backend(integer), pg_terminate_backend(integer, bigint)
    FROM PUBLIC;

-- Specific to Pagila: keep the staff password hashes out of reach, by granting the
-- other columns of that table one by one. The service only tells the model about
-- columns this role may read.
REVOKE SELECT ON staff FROM analyst;
GRANT SELECT (staff_id, first_name, last_name, address_id, email, store_id, active, username,
              last_update, picture)
    ON staff TO analyst;
