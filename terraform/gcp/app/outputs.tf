output "service_names" {
  value = merge(
    { for name, service in google_cloud_run_v2_service.http : name => service.name },
    { for name, pool in google_cloud_run_v2_worker_pool.worker : name => pool.name },
  )
}

output "public_service_name" {
  value = local.service_full_name[local.public_service]
}

output "service_urls" {
  value = { for name, service in google_cloud_run_v2_service.http : name => service.uri }
}

output "external_url" {
  value = var.load_balancer_enabled ? "https://${var.hostname}" : try(google_cloud_run_v2_service.http[local.public_service].uri, null)
}

output "latest_revisions" {
  description = "Revision per service; the Adapter records these for rollback"
  value = merge(
    { for name, service in google_cloud_run_v2_service.http : name => service.latest_created_revision },
    { for name, pool in google_cloud_run_v2_worker_pool.worker : name => pool.latest_created_revision },
  )
}

output "runtime_service_account_email" {
  value = google_service_account.runtime.email
}

output "database_url_secret_id" {
  value = var.db_enabled ? google_secret_manager_secret.database_url[0].id : null
}

output "database_password_secret_id" {
  value = var.db_enabled ? google_secret_manager_secret.database_password[0].id : null
}

output "bootstrap_job_name" {
  value = var.db_enabled ? google_cloud_run_v2_job.bootstrap[0].name : null
}

output "migration_job_name" {
  value = var.db_enabled ? google_cloud_run_v2_job.migration[0].name : null
}

output "storage_bucket_name" {
  value = var.storage_enabled ? google_storage_bucket.uploads[0].name : null
}
