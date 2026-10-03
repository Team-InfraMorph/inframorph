locals {
  deployer_member = "serviceAccount:${var.deployer_service_account_email}"
  project_type    = "resource.type == \"cloudresourcemanager.googleapis.com/Project\""

  # Deployer roles valid for the whole project. None of them can read the
  # Cloud SQL master secret.
  #
  # Service-account roles cannot be narrowed by name: IAM evaluates a service
  # account by its numeric unique ID, so an "im-" email condition never matches
  # (measured on the first real deploy). They are project-wide instead, and the
  # only powerful identity they could reach, the default Compute Engine service
  # account, loses roles/editor below.
  deployer_project_roles = toset([
    "roles/run.admin",
    "roles/iam.serviceAccountCreator",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/logging.viewer",
    "roles/serviceusage.serviceUsageConsumer",
  ])

  # Deployer roles limited to app-owned resources named with app_resource_prefix.
  # Creation is checked on the project, everything else on the named resource,
  # so the condition fails closed for Foundation and other non-app resources.
  deployer_app_roles = {
    secrets = {
      role       = "roles/secretmanager.admin"
      expression = "${local.project_type} || resource.name.startsWith(\"projects/${data.google_project.current.number}/secrets/${var.app_resource_prefix}\")"
    }
    buckets = {
      role       = "roles/storage.admin"
      expression = "${local.project_type} || resource.name.startsWith(\"projects/_/buckets/${var.app_resource_prefix}\")"
    }
  }
}

resource "google_project_iam_member" "deployer" {
  for_each = local.deployer_project_roles

  project = var.project_id
  role    = each.value
  member  = local.deployer_member

  # Never grant "act as any service account" while the default Compute
  # identity still holds roles/editor.
  depends_on = [google_project_iam_member_remove.default_compute_editor]
}

resource "google_project_iam_member" "deployer_app_scoped" {
  for_each = local.deployer_app_roles

  project = var.project_id
  role    = each.value.role
  member  = local.deployer_member

  condition {
    title       = "inframorph-app-${each.key}"
    description = "Only app-owned resources named ${var.app_resource_prefix}*"
    expression  = each.value.expression
  }
}

# App stacks use state under apps/; the Foundation state stays owner-only.
resource "google_storage_bucket_iam_member" "deployer_app_state" {
  bucket = var.state_bucket
  role   = "roles/storage.objectAdmin"
  member = local.deployer_member

  condition {
    title       = "inframorph-app-state-only"
    description = "Objects under apps/ and bucket-level listing for the GCS backend"
    expression  = "resource.type == \"storage.googleapis.com/Bucket\" || resource.name.startsWith(\"projects/_/buckets/${var.state_bucket}/objects/apps/\")"
  }
}

# Cloud Run checks that the caller can read a job's image when the job is
# created, so the deployer needs read access to the Docker Hub proxy too.
resource "google_artifact_registry_repository_iam_member" "deployer_proxy_read" {
  location   = google_artifact_registry_repository.dockerhub.location
  repository = google_artifact_registry_repository.dockerhub.name
  role       = "roles/artifactregistry.reader"
  member     = local.deployer_member
}

# Project-wide serviceAccountUser would otherwise let the deployer run code as
# the default Compute Engine identity, which GCP grants roles/editor.
resource "google_project_iam_member_remove" "default_compute_editor" {
  count = var.remove_default_compute_editor ? 1 : 0

  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  location   = google_artifact_registry_repository.apps.location
  repository = google_artifact_registry_repository.apps.name
  role       = "roles/artifactregistry.writer"
  member     = local.deployer_member
}

# Direct VPC egress places app instances on this subnet.
resource "google_compute_subnetwork_iam_member" "deployer_run_subnet" {
  region     = google_compute_subnetwork.run.region
  subnetwork = google_compute_subnetwork.run.name
  role       = "roles/compute.networkUser"
  member     = local.deployer_member
}

# The only identity that can read the master secret. App stacks attach it to the
# DB bootstrap job; web, worker and migration only get app-scoped credentials.
resource "google_service_account" "db_bootstrap" {
  account_id   = "${var.project}-db-bootstrap"
  display_name = "InfraMorph DB bootstrap"
  description  = "Creates per-app PostgreSQL roles and databases; used only by bootstrap jobs"

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "bootstrap_master" {
  secret_id = google_secret_manager_secret.master.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.db_bootstrap.email}"
}

resource "google_service_account_iam_member" "deployer_uses_bootstrap" {
  service_account_id = google_service_account.db_bootstrap.name
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member
}
