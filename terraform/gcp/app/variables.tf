variable "project_id" {
  type = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID."
  }
}

variable "region" {
  type = string

  validation {
    condition     = var.region == "asia-northeast3"
    error_message = "The MVP GCP stack supports asia-northeast3 only."
  }
}

variable "app_id" {
  description = "Stable app identity; also the public Cloud Run service name and hostname label"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,40}[a-z0-9]$", var.app_id))
    error_message = "app_id must satisfy the shared Name contract and end with a letter or digit."
  }
}

variable "app_resource_prefix" {
  description = "Prefix the Foundation lets the deployer manage; every app-owned name must start with it"
  type        = string
  default     = "im-"
}

variable "resource_prefix" {
  description = "Per-app name prefix (im-<slug>-<hash>) for jobs, worker pools and secrets"
  type        = string

  validation {
    condition     = startswith(var.resource_prefix, var.app_resource_prefix) && can(regex("^[a-z][a-z0-9-]{2,31}$", var.resource_prefix))
    error_message = "resource_prefix must start with app_resource_prefix and be at most 32 characters."
  }
}

variable "runtime_service_account_id" {
  description = "Per-app runtime service account ID (6-30 characters)"
  type        = string

  validation {
    condition     = startswith(var.runtime_service_account_id, var.app_resource_prefix) && can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.runtime_service_account_id))
    error_message = "runtime_service_account_id must start with app_resource_prefix and be a valid service account ID."
  }
}

variable "source_revision" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.source_revision))
    error_message = "source_revision must be a lowercase full Git SHA."
  }
}

variable "network_id" {
  type = string
}

variable "run_subnet_id" {
  type = string
}

variable "deployment_image_uri" {
  description = "New artifact used only by the one-off migration job during staging"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[^@]+@sha256:[0-9a-f]{64}$", var.deployment_image_uri))
    error_message = "deployment_image_uri must be pinned to an Artifact Registry sha256 digest."
  }
}

variable "service_image_uri" {
  description = "Artifact used by long-running services; previous digest during staging, new digest on activation"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[^@]+@sha256:[0-9a-f]{64}$", var.service_image_uri))
    error_message = "service_image_uri must be pinned to an Artifact Registry sha256 digest."
  }
}

variable "services" {
  type = list(object({
    name    = string
    kind    = string
    cpu     = number
    memory  = number
    port    = optional(number)
    health  = optional(string)
    public  = bool
    command = list(string)
  }))

  validation {
    condition     = length([for service in var.services : service if service.kind == "http" && service.public]) == 1
    error_message = "Exactly one public HTTP service is required."
  }

  validation {
    condition = alltrue([
      for service in var.services :
      service.kind == "http" ? service.port != null && startswith(service.health, "/") : !service.public && service.port == null && service.health == null && length(service.command) > 0
    ])
    error_message = "HTTP and worker service fields do not satisfy the contract."
  }

  validation {
    # Cloud Run service and worker pool names are limited to 49 characters.
    condition     = alltrue([for service in var.services : service.public || length("${var.resource_prefix}-${service.name}") <= 49])
    error_message = "resource_prefix-service name must be at most 49 characters."
  }
}

variable "app_config" {
  type    = map(string)
  default = {}
}

variable "service_config" {
  description = "Configuration kept at the previous successful value during staging"
  type        = map(string)
  default     = {}
}

variable "db_enabled" {
  type = bool
}

variable "service_db_enabled" {
  description = "Previous successful DB setting during staging; current setting on activation"
  type        = bool

  validation {
    condition     = !var.service_db_enabled || var.db_enabled
    error_message = "Removing an app database requires a separate data-lifecycle approval."
  }
}

variable "cloudsql_private_ip" {
  type    = string
  default = null
}

variable "cloudsql_port" {
  type    = number
  default = 5432
}

variable "cloudsql_initial_database" {
  type    = string
  default = null
}

variable "cloudsql_master_username" {
  type    = string
  default = null
}

variable "cloudsql_master_secret_id" {
  description = "Foundation secret holding the master password; only the bootstrap job reads it"
  type        = string
  default     = null
}

variable "db_bootstrap_service_account_email" {
  type    = string
  default = null
}

variable "db_bootstrap_image" {
  description = "Digest-pinned linux/amd64 PostgreSQL client image (through the Foundation Docker Hub proxy)"
  type        = string
  default     = null

  validation {
    condition     = var.db_bootstrap_image == null || can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[^@]+@sha256:[0-9a-f]{64}$", coalesce(var.db_bootstrap_image, "x")))
    error_message = "db_bootstrap_image must be pinned to an Artifact Registry sha256 digest."
  }
}

variable "app_database_name" {
  type    = string
  default = null
}

variable "app_database_role" {
  type    = string
  default = null
}

variable "migration_command" {
  type    = list(string)
  default = []

  validation {
    condition     = !var.db_enabled || length(var.migration_command) > 0
    error_message = "A database deployment requires an explicit migration command."
  }
}

variable "storage_enabled" {
  type = bool
}

variable "service_storage_enabled" {
  description = "Previous successful storage setting during staging; current setting on activation"
  type        = bool

  validation {
    condition     = !var.service_storage_enabled || var.storage_enabled
    error_message = "Removing app storage requires a separate data-lifecycle approval."
  }
}

variable "storage_bucket_name" {
  type = string

  validation {
    condition     = startswith(var.storage_bucket_name, var.app_resource_prefix) && can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.storage_bucket_name))
    error_message = "storage_bucket_name must start with app_resource_prefix and be a valid bucket name."
  }
}

variable "activate_services" {
  type        = bool
  description = "False only for the first deployment before bootstrap and migration succeed"
}

variable "load_balancer_enabled" {
  description = "True when the Foundation HTTPS load balancer routes <app_id>.apps_domain"
  type        = bool
  default     = false
}

variable "hostname" {
  description = "<app_id>.apps_domain; required with the load balancer"
  type        = string
  default     = null

  validation {
    condition     = var.hostname == null || startswith(coalesce(var.hostname, "x"), "${var.app_id}.")
    error_message = "hostname must be <app_id>.<apps_domain> so the serverless NEG URL mask can route it."
  }
}

variable "http_min_instances" {
  description = "Warm instances per HTTP service (0 = scale to zero)"
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Upper bound per HTTP service to cap cost"
  type        = number
  default     = 2
}
