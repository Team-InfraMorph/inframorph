# Cloud Run validates every referenced secret version when a job or service is
# created, so the versions must exist inside the staging apply, before the
# jobs. The app password is opened only for this run and written through
# write-only attributes: it reaches Secret Manager but never Terraform state.
# Terraform never receives the Foundation master value.
ephemeral "random_password" "database" {
  count = var.db_enabled ? 1 : 0

  length  = 40
  special = false
}
resource "google_secret_manager_secret" "database_url" {
  count = var.db_enabled ? 1 : 0

  secret_id = "${var.resource_prefix}-database-url"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret" "database_password" {
  count = var.db_enabled ? 1 : 0

  secret_id = "${var.resource_prefix}-database-password"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_version" "database_password" {
  count = var.db_enabled ? 1 : 0

  secret                 = google_secret_manager_secret.database_password[0].id
  secret_data_wo         = ephemeral.random_password.database[0].result
  secret_data_wo_version = var.database_password_version
}

resource "google_secret_manager_secret_version" "database_url" {
  count = var.db_enabled ? 1 : 0

  secret                 = google_secret_manager_secret.database_url[0].id
  secret_data_wo         = "postgresql://${var.app_database_role}:${ephemeral.random_password.database[0].result}@${var.cloudsql_private_ip}:${var.cloudsql_port}/${var.app_database_name}?sslmode=require"
  secret_data_wo_version = var.database_password_version
}

resource "google_storage_bucket" "uploads" {
  count = var.storage_enabled ? 1 : 0

  name                        = var.storage_bucket_name
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}
