locals {
  service_map       = { for service in var.services : service.name => service }
  public_service    = one([for service in var.services : service.name if service.public])
  http_services     = { for name, service in local.service_map : name => service if service.kind == "http" }
  worker_services   = { for name, service in local.service_map : name => service if service.kind != "http" }
  active_http       = var.activate_services ? local.http_services : {}
  active_workers    = var.activate_services ? local.worker_services : {}
  service_full_name = { for name, service in local.service_map : name => service.public ? var.app_id : "${var.resource_prefix}-${name}" }

  # Labels that change on every deploy stay on revision-bearing resources only.
  revision_labels = { source_sha = var.source_revision }

  # Names Cloud Run sets itself or the adapter owns; plan config cannot override them.
  reserved_env = [
    "PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION",
    "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION", "CLOUD_RUN_TASK_INDEX", "CLOUD_RUN_TASK_ATTEMPT", "CLOUD_RUN_TASK_COUNT",
    "DATABASE_URL", "GCS_BUCKET",
  ]
  job_environment = merge(
    { for key, value in var.app_config : key => value if !contains(local.reserved_env, key) },
    var.storage_enabled ? { GCS_BUCKET = var.storage_bucket_name } : {},
  )
  service_environment = merge(
    { for key, value in var.service_config : key => value if !contains(local.reserved_env, key) },
    var.service_storage_enabled ? { GCS_BUCKET = var.storage_bucket_name } : {},
  )

  # Cloud Run needs at least 1 vCPU for request concurrency above 1; memory in MiB.
  limits = {
    for name, service in local.service_map : name => {
      cpu    = tostring(max(1, ceil(service.cpu / 1024)))
      memory = "${max(512, service.memory)}Mi"
    }
  }

  db_bootstrap_script = <<-SCRIPT
    export PGPASSWORD="$MASTER_PASSWORD"
    psql --host="$DB_HOST" --port="$DB_PORT" --username="$MASTER_USERNAME" --dbname="$MASTER_DATABASE" --set=ON_ERROR_STOP=1 --set=app_role="$APP_DB_ROLE" --set=app_password="$APP_DB_PASSWORD" --set=app_db="$APP_DB_NAME" <<'SQL'
    SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password')
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec
    SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password') \gexec
    -- Cloud SQL's master is not a superuser; it must be a member to hand over ownership.
    SELECT format('GRANT %I TO %I', :'app_role', current_user)
    WHERE NOT pg_has_role(current_user, :'app_role', 'MEMBER') \gexec
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
