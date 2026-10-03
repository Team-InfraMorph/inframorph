locals {
  required_services = toset(concat([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ], var.enable_load_balancer ? ["certificatemanager.googleapis.com"] : []))
}

resource "google_project_service" "required" {
  for_each = local.required_services

  project = var.project_id
  service = each.value

  # Other workloads in the project may depend on the same APIs.
  disable_on_destroy         = false
  disable_dependent_services = false
}
