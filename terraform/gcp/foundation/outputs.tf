output "project_id" {
  value = var.project_id
}

output "project_number" {
  value = data.google_project.current.number
}

output "region" {
  value = var.region
}

output "network_id" {
  value = google_compute_network.main.id
}

output "run_subnet_id" {
  description = "Direct VPC egress subnet for app services, worker pools and jobs"
  value       = google_compute_subnetwork.run.id
}

output "artifact_registry_repository" {
  value = google_artifact_registry_repository.apps.name
}

output "artifact_registry_url" {
  description = "Docker host/path prefix used for immutable app image tags"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.apps.repository_id}"
}

output "dockerhub_proxy_url" {
  description = "Prefix for digest-pinned Docker Hub images, e.g. <prefix>/library/postgres@sha256:..."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.dockerhub.repository_id}"
}

output "cloudsql_instance_name" {
  value = google_sql_database_instance.main.name
}

output "cloudsql_connection_name" {
  value = google_sql_database_instance.main.connection_name
}

output "cloudsql_private_ip" {
  value = google_sql_database_instance.main.private_ip_address
}

output "cloudsql_port" {
  value = 5432
}

output "cloudsql_db_name" {
  value = google_sql_database.initial.name
}

output "cloudsql_master_username" {
  value = google_sql_user.master.name
}

output "cloudsql_master_secret_id" {
  description = "Secret Manager secret holding the master password; the value is never output"
  value       = google_secret_manager_secret.master.id
}

output "db_bootstrap_service_account_email" {
  value = google_service_account.db_bootstrap.email
}

output "deployer_service_account_email" {
  value = var.deployer_service_account_email
}

output "app_resource_prefix" {
  description = "App-owned service accounts, secrets and buckets must start with this prefix"
  value       = var.app_resource_prefix
}

output "load_balancer_enabled" {
  value = var.enable_load_balancer
}

output "apps_wildcard_domain" {
  value = var.enable_load_balancer ? "*.${var.apps_domain}" : null
}

output "lb_ip_address" {
  description = "Point the *.apps_domain A record here"
  value       = try(google_compute_global_address.lb[0].address, null)
}

output "certificate_dns_authorization_records" {
  description = "CNAME records to add at the DNS provider with proxying disabled"
  value = [
    for record in try(google_certificate_manager_dns_authorization.apps[0].dns_resource_record, []) : {
      name = record.name
      type = record.type
      data = record.data
    }
  ]
}
