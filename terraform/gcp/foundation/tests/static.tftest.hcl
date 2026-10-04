mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }

  mock_resource "google_service_account" {
    override_during = plan
    defaults = {
      name  = "projects/example-project/serviceAccounts/inframorph-db-bootstrap@example-project.iam.gserviceaccount.com"
      email = "inframorph-db-bootstrap@example-project.iam.gserviceaccount.com"
    }
  }
}

# random is used for real: it is local-only and mocks cannot open ephemeral resources.

variables {
  project_id                     = "example-project"
  expected_project_number        = "123456789012"
  deployer_service_account_email = "inframorph-deployer@example-project.iam.gserviceaccount.com"
  state_bucket                   = "example-tfstate-bucket"
  # Explicit so an operator's local terraform.tfvars cannot change the cases.
  enable_load_balancer          = false
  apps_domain                   = null
  remove_default_compute_editor = true
}

run "private_foundation_without_load_balancer" {
  command = plan

  assert {
    condition = (!google_sql_database_instance.main.settings[0].ip_configuration[0].ipv4_enabled
    && google_sql_database_instance.main.settings[0].ip_configuration[0].ssl_mode == "ENCRYPTED_ONLY")
    error_message = "Cloud SQL must be private-IP only with TLS required."
  }

  assert {
    condition     = google_sql_database_instance.main.deletion_protection && google_sql_database_instance.main.settings[0].deletion_protection_enabled
    error_message = "The shared database must be protected from Terraform and API deletion."
  }

  assert {
    condition     = google_artifact_registry_repository.apps.docker_config[0].immutable_tags
    error_message = "App image tags must be immutable."
  }

  assert {
    condition     = google_artifact_registry_repository.apps.location == var.region && google_artifact_registry_repository.dockerhub.location == var.region
    error_message = "Images must be pulled from the deployment region."
  }

  assert {
    condition     = length(google_compute_region_network_endpoint_group.apps) == 0 && length(google_compute_global_forwarding_rule.https) == 0
    error_message = "The load balancer is opt-in."
  }

  assert {
    condition = alltrue([for binding in google_project_iam_member.deployer :
    !contains(["roles/secretmanager.admin", "roles/storage.admin", "roles/editor", "roles/owner"], binding.role)])
    error_message = "Project-wide deployer roles must not reach secrets, buckets or broad project access."
  }

  assert {
    condition = alltrue([for binding in google_project_iam_member.deployer_app_scoped :
    strcontains(binding.condition[0].expression, var.app_resource_prefix)])
    error_message = "Secret and bucket roles must be limited to app-owned names."
  }

  assert {
    # Service-account roles are project-wide (IAM matches SAs by unique ID, not
    # name), so the default Compute identity must lose roles/editor.
    condition = (contains(keys(google_project_iam_member.deployer), "roles/iam.serviceAccountUser")
      && google_project_iam_member_remove.default_compute_editor[0].role == "roles/editor"
    && google_project_iam_member_remove.default_compute_editor[0].member == "serviceAccount:123456789012-compute@developer.gserviceaccount.com")
    error_message = "Acting as any service account is only safe once the default Compute identity has no editor role."
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.deployer_proxy_read.role == "roles/artifactregistry.reader"
    error_message = "Creating a DB bootstrap job needs read access to the Docker Hub proxy."
  }

  assert {
    condition     = strcontains(google_storage_bucket_iam_member.deployer_app_state.condition[0].expression, "/objects/apps/")
    error_message = "The deployer may write app state only, never the Foundation state."
  }

  assert {
    condition     = google_secret_manager_secret_iam_member.bootstrap_master.member == "serviceAccount:${google_service_account.db_bootstrap.email}"
    error_message = "Only the DB bootstrap identity may read the master secret."
  }
}

run "shared_load_balancer" {
  command = plan

  variables {
    enable_load_balancer = true
    apps_domain          = "apps.example.com"
  }

  assert {
    condition     = google_compute_region_network_endpoint_group.apps[0].cloud_run[0].url_mask == "<service>.apps.example.com"
    error_message = "One serverless NEG must route <app>.apps_domain to the Cloud Run service named <app>."
  }

  assert {
    condition     = tolist(google_certificate_manager_certificate.apps[0].managed[0].domains) == tolist(["*.apps.example.com"])
    error_message = "The shared certificate must cover every app hostname."
  }

  assert {
    condition     = google_compute_url_map.http_redirect[0].default_url_redirect[0].https_redirect
    error_message = "HTTP must redirect to HTTPS."
  }

  assert {
    condition     = contains(local.required_services, "certificatemanager.googleapis.com")
    error_message = "The load balancer needs the Certificate Manager API."
  }
}

run "load_balancer_requires_domain" {
  command = plan

  variables {
    enable_load_balancer = true
    apps_domain          = null
  }

  expect_failures = [var.enable_load_balancer]
}

run "rejects_other_region" {
  command = plan

  variables {
    region = "us-central1"
  }

  expect_failures = [var.region]
}
