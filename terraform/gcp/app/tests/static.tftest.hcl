mock_provider "google" {}
# random is used for real: mocks cannot open ephemeral resources.

variables {
  project_id                 = "example-project"
  region                     = "asia-northeast3"
  app_id                     = "demo-app"
  resource_prefix            = "im-demo-app-cf9ec463"
  runtime_service_account_id = "im-demo-app-cf9ec463"
  # Test-only placeholder with the same 40-character shape as a full Git SHA.
  source_revision                    = "ebad709867cf1f3523075d8038ae41bd69ecae9b"
  network_id                         = "projects/example-project/global/networks/inframorph-vpc"
  run_subnet_id                      = "projects/example-project/regions/asia-northeast3/subnetworks/inframorph-run-asia-northeast3"
  deployment_image_uri               = "asia-northeast3-docker.pkg.dev/example-project/apps/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  service_image_uri                  = "asia-northeast3-docker.pkg.dev/example-project/apps/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  app_config                         = { STORAGE_DRIVER = "gcs" }
  service_config                     = { STORAGE_DRIVER = "gcs", PORT = "9000", GCS_BUCKET = "wrong-bucket" }
  db_enabled                         = true
  service_db_enabled                 = true
  cloudsql_private_ip                = "10.61.0.3"
  cloudsql_port                      = 5432
  cloudsql_initial_database          = "app"
  cloudsql_master_username           = "inframorph"
  cloudsql_master_secret_id          = "projects/123456789012/secrets/inframorph-sql-master-password"
  db_bootstrap_service_account_email = "inframorph-db-bootstrap@example-project.iam.gserviceaccount.com"
  db_bootstrap_image                 = "asia-northeast3-docker.pkg.dev/example-project/dockerhub/library/postgres@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  app_database_name                  = "app_demo_app_cf9ec463"
  app_database_role                  = "app_demo_app_cf9ec463_user"
  migration_command                  = ["./node_modules/.bin/prisma", "db", "push", "--skip-generate"]
  storage_enabled                    = true
  service_storage_enabled            = true
  storage_bucket_name                = "im-demo-app-cf9ec463-example-project"
  load_balancer_enabled              = true
  hostname                           = "demo-app.apps.example.com"
  services = [
    {
      name    = "web"
      kind    = "http"
      cpu     = 256
      memory  = 512
      port    = 3000
      health  = "/health"
      public  = true
      command = []
    },
    {
      name    = "worker"
      kind    = "worker"
      cpu     = 256
      memory  = 512
      port    = null
      health  = null
      public  = false
      command = ["node", "src/worker.js"]
    },
  ]
}

run "first_deployment_stage" {
  command = plan

  variables {
    activate_services = false
  }

  assert {
    condition     = length(google_cloud_run_v2_service.http) == 0 && length(google_cloud_run_v2_worker_pool.worker) == 0
    error_message = "The first deployment must not start services before bootstrap and migration."
  }

  assert {
    condition     = length(google_cloud_run_v2_job.bootstrap) == 1 && length(google_cloud_run_v2_job.migration) == 1
    error_message = "Staging must create the bootstrap and migration jobs."
  }

  assert {
    condition     = google_cloud_run_v2_job.bootstrap[0].template[0].template[0].service_account == var.db_bootstrap_service_account_email
    error_message = "Only the bootstrap job may run as the identity that reads the master secret."
  }

  assert {
    condition = alltrue([for job in [google_cloud_run_v2_job.bootstrap[0], google_cloud_run_v2_job.migration[0]] :
    job.template[0].template[0].max_retries == 0])
    error_message = "One-off DB jobs must not retry silently."
  }

  assert {
    condition = (length(google_secret_manager_secret_version.database_password) == 1
    && length(google_secret_manager_secret_version.database_url) == 1)
    error_message = "Staging must create the secret versions the jobs reference; Cloud Run rejects jobs without them."
  }

  assert {
    condition     = google_cloud_run_v2_job.migration[0].template[0].template[0].containers[0].image == var.deployment_image_uri
    error_message = "The migration job must run the new artifact."
  }

  assert {
    condition     = !contains([for env in google_cloud_run_v2_job.migration[0].template[0].template[0].containers[0].env : env.name], "MASTER_PASSWORD")
    error_message = "The migration job must not receive the master credential."
  }

  assert {
    condition     = strcontains(local.db_bootstrap_script, "GRANT %I TO %I")
    error_message = "The Cloud SQL master must join the app role before transferring database ownership."
  }

  assert {
    condition     = try(google_secret_manager_secret.database_url[0].labels["source_sha"], null) == null && try(google_cloud_run_v2_job.bootstrap[0].labels["source_sha"], null) == null
    error_message = "Per-deploy labels must not turn data resources into in-place updates."
  }
}

run "activation" {
  command = plan

  variables {
    activate_services = true
  }

  assert {
    condition     = google_cloud_run_v2_service.http["web"].name == var.app_id
    error_message = "The public service must be named after app_id so the Foundation URL mask routes it."
  }

  assert {
    condition = (google_cloud_run_v2_service.http["web"].ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    && google_cloud_run_v2_service.http["web"].default_uri_disabled)
    error_message = "Behind the load balancer the public service must not be reachable on run.app."
  }

  assert {
    condition     = google_cloud_run_v2_service.http["web"].invoker_iam_disabled
    error_message = "The public service must be invokable without an allUsers IAM binding."
  }

  assert {
    condition     = !contains(keys(google_cloud_run_v2_service.http), "worker") && length(google_cloud_run_v2_worker_pool.worker) == 1
    error_message = "A worker must run as a worker pool, never as a public service."
  }

  assert {
    condition = (google_cloud_run_v2_service.http["web"].template[0].containers[0].startup_probe[0].period_seconds == 1
      && google_cloud_run_v2_service.http["web"].template[0].containers[0].startup_probe[0].http_get[0].path == "/health"
    && google_cloud_run_v2_service.http["web"].template[0].containers[0].resources[0].startup_cpu_boost)
    error_message = "The new revision must be probed every second on its health path with startup CPU boost."
  }

  assert {
    condition     = !contains([for env in google_cloud_run_v2_service.http["web"].template[0].containers[0].env : env.name], "PORT")
    error_message = "PORT is reserved by Cloud Run and must not come from plan config."
  }

  assert {
    condition = [for env in google_cloud_run_v2_service.http["web"].template[0].containers[0].env :
    env.value if env.name == "GCS_BUCKET"] == [var.storage_bucket_name]
    error_message = "Storage settings must use the operator's bucket without duplicate names."
  }

  assert {
    condition = alltrue([for service in google_cloud_run_v2_service.http :
    strcontains(service.template[0].containers[0].image, "@sha256:")])
    error_message = "Cloud Run services must use digest-pinned images."
  }

  assert {
    condition     = google_cloud_run_v2_service.http["web"].template[0].vpc_access[0].egress == "PRIVATE_RANGES_ONLY"
    error_message = "A database app reaches Cloud SQL through Direct VPC egress for private ranges only."
  }
}

run "web_only_without_data" {
  command = plan

  variables {
    activate_services       = true
    db_enabled              = false
    service_db_enabled      = false
    storage_enabled         = false
    service_storage_enabled = false
    load_balancer_enabled   = false
    hostname                = null
    migration_command       = []
    services = [
      {
        name    = "web"
        kind    = "http"
        cpu     = 256
        memory  = 512
        port    = 3000
        health  = "/health"
        public  = true
        command = []
      },
    ]
  }

  assert {
    condition     = length(google_cloud_run_v2_service.http["web"].template[0].vpc_access) == 0
    error_message = "Apps without a database must not pay the VPC attachment cost."
  }

  assert {
    condition     = length(google_cloud_run_v2_job.bootstrap) == 0 && length(google_secret_manager_secret.database_url) == 0
    error_message = "Apps without a database get no DB jobs or secrets."
  }

  assert {
    condition     = google_cloud_run_v2_service.http["web"].ingress == "INGRESS_TRAFFIC_ALL" && !google_cloud_run_v2_service.http["web"].default_uri_disabled
    error_message = "Without the load balancer the public service is served on its run.app URL."
  }
}

run "rejects_mutable_image" {
  command = plan

  variables {
    activate_services = true
    service_image_uri = "asia-northeast3-docker.pkg.dev/example-project/apps/app:latest"
  }

  expect_failures = [var.service_image_uri]
}

run "rejects_database_removal" {
  command = plan

  variables {
    activate_services  = true
    db_enabled         = false
    service_db_enabled = true
  }

  expect_failures = [var.service_db_enabled]
}
