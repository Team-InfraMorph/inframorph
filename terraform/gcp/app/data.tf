# The Adapter writes both secret versions after the metadata exists.
# Terraform never receives the app password or the Foundation master value.
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
