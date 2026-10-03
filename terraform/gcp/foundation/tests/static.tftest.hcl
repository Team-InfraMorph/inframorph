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
    !contains(["roles/secretmanager.admin", "roles/storage.admin", "roles/iam.serviceAccountUser", "roles/iam.serviceAccountAdmin", "roles/editor", "roles/owner"], binding.role)])
    error_message = "Project-wide deployer roles must not reach secrets, buckets or other identities."
  }

  assert {
    condition = alltrue([for binding in google_project_iam_member.deployer_app_scoped :
    strcontains(binding.condition[0].expression, var.app_resource_prefix)])
    error_message = "Secret, bucket and service-account roles must be limited to app-owned names."
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
