variable "project_id" {
  description = "GCP project that is allowed to own the Foundation stack"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID."
  }
}

variable "expected_project_number" {
  description = "Project number guard against applying to a similarly named project"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{6,20}$", var.expected_project_number))
    error_message = "expected_project_number must contain digits only."
  }
}

variable "region" {
  description = "GCP region for the Foundation stack"
  type        = string
  default     = "asia-northeast3"

  validation {
    condition     = var.region == "asia-northeast3"
    error_message = "InfraMorph GCP Foundation is fixed to asia-northeast3 (Seoul) for the MVP."
  }
}

variable "project" {
  description = "Resource name prefix and project label"
  type        = string
  default     = "inframorph"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,14}$", var.project))
    error_message = "project must be 2-15 lowercase letters, numbers, or hyphens."
  }
}

variable "run_subnet_cidr" {
  description = "Subnet used by Cloud Run Direct VPC egress (services, worker pools, jobs)"
  type        = string
  default     = "10.60.0.0/24"
}

variable "psa_prefix_length" {
  description = "Private services access range size reserved for Cloud SQL"
  type        = number
  default     = 20
}

variable "artifact_repository_id" {
  description = "Shared Artifact Registry Docker repository used by all MVP applications"
  type        = string
  default     = "apps"
}

variable "dockerhub_remote_repository_id" {
  description = "Artifact Registry remote repository that proxies Docker Hub (DB bootstrap image)"
  type        = string
  default     = "dockerhub"
}

variable "db_version" {
  description = "Cloud SQL PostgreSQL version"
  type        = string
  default     = "POSTGRES_16"
}

variable "db_tier" {
  description = "Machine tier for the shared MVP PostgreSQL instance"
  type        = string
  default     = "db-f1-micro"
}

variable "db_name" {
  description = "Initial database used by private app bootstrap jobs"
  type        = string
  default     = "app"
}

variable "db_username" {
  description = "Cloud SQL master user; the password is generated and stored only in Secret Manager"
  type        = string
  default     = "inframorph"
}

variable "db_master_password_version" {
  description = "Increase to rotate the master password (write-only values are not kept in state)"
  type        = number
  default     = 1
}

variable "db_backup_retention" {
  description = "Number of automated Cloud SQL backups to retain"
  type        = number
  default     = 7
}

variable "deployer_service_account_email" {
  description = "Service account created by the GCP bootstrap step and impersonated by the deployer"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\\.iam\\.gserviceaccount\\.com$", var.deployer_service_account_email))
    error_message = "deployer_service_account_email must be a service account email."
  }
}

variable "state_bucket" {
  description = "Terraform state bucket created by the GCP bootstrap step"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.state_bucket))
    error_message = "state_bucket must be a GCS bucket name."
  }
}

variable "app_resource_prefix" {
  description = "Name prefix of every app-owned service account, secret, and bucket the deployer may manage"
  type        = string
  default     = "im-"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,5}-$", var.app_resource_prefix))
    error_message = "app_resource_prefix must be a short lowercase prefix ending with a hyphen."
  }
}

variable "enable_load_balancer" {
  description = "Create the shared HTTPS load balancer for <app>.apps_domain hostnames"
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_load_balancer || var.apps_domain != null
    error_message = "apps_domain is required when enable_load_balancer is true."
  }
}

variable "apps_domain" {
  description = "Base domain below which project hostnames are created; required with the load balancer"
  type        = string
  default     = null

  validation {
    condition = var.apps_domain == null || (
      !startswith(coalesce(var.apps_domain, "x"), "*.") &&
      !endswith(coalesce(var.apps_domain, "x"), ".") &&
      can(regex("^[a-z0-9.-]+$", coalesce(var.apps_domain, "x")))
    )
    error_message = "apps_domain must be a lowercase base domain without a wildcard or trailing dot."
  }
}

variable "lb_ssl_policy_min_tls" {
  description = "Minimum TLS version for the shared HTTPS load balancer"
  type        = string
  default     = "TLS_1_2"
}
