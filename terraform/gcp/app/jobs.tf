# The Adapter runs these with `gcloud run jobs execute --wait` between the
# staging and activation applies. Unlike Fargate run-task, a Cloud Run job
# execution completes when the container exits; there is no ENI detach wait.
# max_retries = 0 keeps a failed migration from silently re-running.

resource "google_cloud_run_v2_job" "bootstrap" {
  count = var.db_enabled ? 1 : 0

  name                = "${var.resource_prefix}-db-bootstrap"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = var.db_bootstrap_service_account_email
      max_retries     = 0
      timeout         = "300s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.run_subnet_id
        }
      }

      containers {
        name    = "db-bootstrap"
        image   = var.db_bootstrap_image
        command = ["/bin/sh"]
        args    = ["-ceu", local.db_bootstrap_script]

        env {
          name  = "DB_HOST"
          value = var.cloudsql_private_ip
        }
        env {
          name  = "DB_PORT"
          value = tostring(var.cloudsql_port)
        }
        env {
          name  = "PGSSLMODE"
          value = "require"
        }
        env {
          name  = "MASTER_DATABASE"
          value = var.cloudsql_initial_database
        }
        env {
          name  = "MASTER_USERNAME"
          value = var.cloudsql_master_username
        }
        env {
          name  = "APP_DB_NAME"
          value = var.app_database_name
        }
        env {
          name  = "APP_DB_ROLE"
          value = var.app_database_role
        }
        env {
          name = "MASTER_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = var.cloudsql_master_secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "APP_DB_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_password[0].secret_id
              version = "latest"
            }
          }
        }

        resources {
          limits = { cpu = "1", memory = "512Mi" }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.bootstrap_database_password,
    google_secret_manager_secret_version.database_password,
  ]

  lifecycle {
    precondition {
      condition = alltrue([
        for value in [var.cloudsql_private_ip, var.cloudsql_initial_database, var.cloudsql_master_username, var.cloudsql_master_secret_id, var.db_bootstrap_service_account_email, var.db_bootstrap_image, var.app_database_name, var.app_database_role] :
        value != null
      ])
      error_message = "A database deployment requires every Foundation Cloud SQL input and the app database names."
    }
  }
}

resource "google_cloud_run_v2_job" "migration" {
  count = var.db_enabled ? 1 : 0

  name                = "${var.resource_prefix}-migration"
  location            = var.region
  deletion_protection = false
  labels              = local.revision_labels

  template {
    task_count = 1
    labels     = local.revision_labels

    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "600s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.run_subnet_id
        }
      }

      containers {
        name  = "migration"
        image = var.deployment_image_uri
        args  = var.migration_command

        dynamic "env" {
          for_each = local.job_environment
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url[0].secret_id
              version = "latest"
            }
          }
        }

        resources {
          limits = { cpu = "1", memory = "512Mi" }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.runtime_database_url,
    google_secret_manager_secret_version.database_url,
  ]
}
