resource "google_sql_database_instance" "main" {
  name             = "${var.project}-postgres"
  database_version = var.db_version
  region           = var.region

  deletion_protection = true

  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.db_tier
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    deletion_protection_enabled = true

    ip_configuration {
      # Private IP only. Apps connect through Direct VPC egress with TLS required.
      ipv4_enabled    = false
      private_network = google_compute_network.main.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled = true

      backup_retention_settings {
        retained_backups = var.db_backup_retention
      }
    }

    user_labels = {
      project = var.project
      stack   = "foundation"
    }
  }

  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "initial" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
}

# Opened only during plan/apply. With write-only attributes below, the value is
# sent to Cloud SQL and Secret Manager but never stored in Terraform state.
ephemeral "random_password" "master" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "master" {
  secret_id = "${var.project}-sql-master-password"

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

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "master" {
  secret                 = google_secret_manager_secret.master.id
  secret_data_wo         = ephemeral.random_password.master.result
  secret_data_wo_version = var.db_master_password_version
}

resource "google_sql_user" "master" {
  name                = var.db_username
  instance            = google_sql_database_instance.main.name
  password_wo         = ephemeral.random_password.master.result
  password_wo_version = var.db_master_password_version
}
