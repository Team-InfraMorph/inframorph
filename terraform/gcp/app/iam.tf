resource "google_service_account" "runtime" {
  account_id   = var.runtime_service_account_id
  display_name = "InfraMorph ${var.app_id} runtime"
  description  = "Runtime identity limited to ${var.app_id} data resources"
}

resource "google_secret_manager_secret_iam_member" "runtime_database_url" {
  count = var.db_enabled ? 1 : 0

  secret_id = google_secret_manager_secret.database_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

# The bootstrap identity sets this app's role password; it never sees other apps' secrets.
resource "google_secret_manager_secret_iam_member" "bootstrap_database_password" {
  count = var.db_enabled ? 1 : 0

  secret_id = google_secret_manager_secret.database_password[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.db_bootstrap_service_account_email}"
}

resource "google_storage_bucket_iam_member" "runtime_uploads" {
  count = var.storage_enabled ? 1 : 0

  bucket = google_storage_bucket.uploads[0].name
  role   = "roles/storage.objectUser"
  member = google_service_account.runtime.member
}
