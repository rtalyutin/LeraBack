CREATE ROLE lera_test LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'local-synthetic-only';
CREATE DATABASE booking_test OWNER lera_test;
CREATE DATABASE constructor_test OWNER lera_test;
CREATE DATABASE startup_test OWNER lera_test;
CREATE DATABASE shared_schema_test OWNER lera_test;
CREATE DATABASE multisalon_test OWNER lera_test;
CREATE DATABASE namespace_test OWNER lera_test;
CREATE DATABASE vk_connections_test OWNER lera_test;
