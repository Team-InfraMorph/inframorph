variable "region" {
  type = string

  validation {
    condition     = var.region == "ap-northeast-2"
    error_message = "The MVP AWS Adapter supports ap-northeast-2 only."
  }
}

variable "account_id" {
  type = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must contain 12 digits."
  }
}

variable "app_id" {
  type = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,40}$", var.app_id))
    error_message = "app_id must satisfy the shared Name contract."
  }
}

variable "resource_prefix" {
  type = string
}

variable "hostname" {
  type = string
}

variable "listener_rule_priority" {
  type = number

  validation {
    condition     = var.listener_rule_priority >= 1 && var.listener_rule_priority <= 50000
    error_message = "ALB listener priority must be between 1 and 50000."
  }
}

variable "source_revision" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.source_revision))
    error_message = "source_revision must be a lowercase full Git SHA."
  }
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "At least two private subnets are required."
  }
}

variable "alb_security_group_id" {
  type = string
}

variable "https_listener_arn" {
  type = string
}

variable "ecs_cluster_arn" {
  type = string
}

variable "deployment_image_uri" {
  description = "New artifact used only by the one-off migration task during staging"
  type        = string

  validation {
    condition     = can(regex("^[^@]+@sha256:[0-9a-f]{64}$", var.deployment_image_uri))
    error_message = "deployment_image_uri must be pinned to an ECR sha256 digest."
  }
}

variable "service_image_uri" {
  description = "Artifact used by long-running services; previous digest during staging, new digest on activation"
  type        = string

  validation {
    condition     = can(regex("^[^@]+@sha256:[0-9a-f]{64}$", var.service_image_uri))
    error_message = "service_image_uri must be pinned to an ECR sha256 digest."
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
}

variable "rds_address" {
  type = string
}

variable "rds_port" {
  type = number
}

variable "rds_initial_database" {
  type = string
}

variable "rds_security_group_id" {
  type = string
}

variable "rds_master_secret_arn" {
  type      = string
  sensitive = true
}

variable "app_database_name" {
  type = string
}

variable "app_database_role" {
  type = string
}

variable "app_secret_name" {
  type = string
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
}

variable "storage_bucket_name" {
  type = string
}

variable "activate_services" {
  type        = bool
  description = "False only for the first deployment before bootstrap and migration succeed"
}
