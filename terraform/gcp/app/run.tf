# Deploy-time notes (see feat/a-aws-deploy-speed for the measured AWS side):
# - Cloud Run moves traffic as soon as the new revision passes its startup
#   probe and never waits for old instances to drain, so there is no
#   deregistration delay or SIGTERM stop timeout on the deploy path.
# - The startup probe hits the health path every second (the AWS ALB check
#   needed >= 10s) and startup CPU boost shortens Node start-up.
# - Direct VPC egress is attached only when the app uses the database.
resource "google_cloud_run_v2_service" "http" {
  for_each = local.active_http

  name                 = local.service_full_name[each.key]
  location             = var.region
  deletion_protection  = false
  labels               = local.revision_labels
  ingress              = !each.value.public ? "INGRESS_TRAFFIC_INTERNAL_ONLY" : var.load_balancer_enabled ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"
  default_uri_disabled = each.value.public && var.load_balancer_enabled
  # Public without an allUsers IAM binding: one resource fewer per deploy and
  # no IAM propagation wait before the first request.
  invoker_iam_disabled = each.value.public

  template {
    service_account = google_service_account.runtime.email
    labels          = local.revision_labels

    scaling {
      min_instance_count = var.http_min_instances
      max_instance_count = var.max_instances
    }

    dynamic "vpc_access" {
      for_each = var.service_db_enabled ? [1] : []
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.run_subnet_id
        }
      }
    }

    containers {
      name  = each.key
      image = var.service_image_uri
      args  = length(each.value.command) > 0 ? each.value.command : null

      ports {
        container_port = each.value.port
      }

      dynamic "env" {
        for_each = local.service_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.service_db_enabled ? [1] : []
        content {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url[0].secret_id
              version = "latest"
            }
          }
        }
      }

      resources {
        limits            = local.limits[each.key]
        cpu_idle          = true
        startup_cpu_boost = true
      }

      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 1
        timeout_seconds       = 1
        failure_threshold     = 120

        http_get {
          path = each.value.health
          port = each.value.port
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.runtime_database_url,
    google_secret_manager_secret_version.database_url,
    google_storage_bucket_iam_member.runtime_uploads,
  ]
}

# Workers have no port to probe, so they run as worker pools instead of
# request-driven services.
resource "google_cloud_run_v2_worker_pool" "worker" {
  for_each = local.active_workers

  name                = local.service_full_name[each.key]
  location            = var.region
  deletion_protection = false
  labels              = local.revision_labels
  # Worker pools may still be a beta surface; BETA is also accepted once GA.
  launch_stage = "BETA"

  scaling {
    manual_instance_count = 1
  }

  template {
    service_account = google_service_account.runtime.email
    labels          = local.revision_labels

    dynamic "vpc_access" {
      for_each = var.service_db_enabled ? [1] : []
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.run_subnet_id
        }
      }
    }

    containers {
      name  = each.key
      image = var.service_image_uri
      args  = each.value.command

      dynamic "env" {
        for_each = local.service_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.service_db_enabled ? [1] : []
        content {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url[0].secret_id
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = local.limits[each.key]
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.runtime_database_url,
    google_secret_manager_secret_version.database_url,
    google_storage_bucket_iam_member.runtime_uploads,
  ]
}
