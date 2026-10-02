locals {
  service_map     = { for service in var.services : service.name => service }
  public_services = { for name, service in local.service_map : name => service if service.public }
  config_environment = [
    for key in sort(keys(var.app_config)) : {
      name  = key
      value = var.app_config[key]
    }
  ]
  service_config_environment = [
    for key in sort(keys(var.service_config)) : {
      name  = key
      value = var.service_config[key]
    }
  ]
  storage_environment = var.storage_enabled ? [{
    name  = "STORAGE_BUCKET"
    value = var.storage_bucket_name
  }] : []
  service_storage_environment = var.service_storage_enabled ? [{
    name  = "STORAGE_BUCKET"
    value = var.storage_bucket_name
  }] : []

  db_bootstrap_script = <<-SCRIPT
    export PGPASSWORD="$MASTER_PASSWORD"
    psql --host="$DB_HOST" --port="$DB_PORT" --username="$MASTER_USERNAME" --dbname="$MASTER_DATABASE" --set=ON_ERROR_STOP=1 --set=app_role="$APP_DB_ROLE" --set=app_password="$APP_DB_PASSWORD" --set=app_db="$APP_DB_NAME" <<'SQL'
    SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password')
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec
    SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password') \gexec
    SELECT format('CREATE DATABASE %I OWNER %I', :'app_db', :'app_role')
    WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_db') \gexec
    SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_db', :'app_role') \gexec
    SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'app_db') \gexec
    SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'app_db', :'app_role') \gexec
    SQL
    psql --host="$DB_HOST" --port="$DB_PORT" --username="$MASTER_USERNAME" --dbname="$APP_DB_NAME" --set=ON_ERROR_STOP=1 --set=app_role="$APP_DB_ROLE" <<'SQL'
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'app_role') \gexec
    SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', :'app_role') \gexec
    SELECT format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_role') \gexec
    SQL
  SCRIPT
}
