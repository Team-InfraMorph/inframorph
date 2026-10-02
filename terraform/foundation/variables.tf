variable "expected_account_id" {
  description = "AWS account that is allowed to own the Foundation stack"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region for the Foundation stack"
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.region == "ap-northeast-2"
    error_message = "InfraMorph Foundation is fixed to ap-northeast-2 for the MVP."
  }
}

variable "project" {
  description = "Resource name prefix and project tag"
  type        = string
  default     = "inframorph"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project))
    error_message = "project must contain only lowercase letters, numbers, and hyphens."
  }
}

variable "azs" {
  description = "Availability zones used by the public and private subnets"
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]

  validation {
    condition     = length(var.azs) == 2 && length(distinct(var.azs)) == 2
    error_message = "Exactly two distinct availability zones are required."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the shared VPC"
  type        = string
  default     = "10.40.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDRs in the same order as azs"
  type        = list(string)
  default     = ["10.40.1.0/24", "10.40.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "Private workload subnet CIDRs in the same order as azs"
  type        = list(string)
  default     = ["10.40.11.0/24", "10.40.12.0/24"]
}

variable "apps_domain" {
  description = "Base domain below which project hostnames are created"
  type        = string

  validation {
    condition = (
      !startswith(var.apps_domain, "*.") &&
      !endswith(var.apps_domain, ".") &&
      can(regex("^[a-z0-9.-]+$", var.apps_domain))
    )
    error_message = "apps_domain must be a lowercase base domain without a wildcard or trailing dot."
  }
}

variable "enable_https_listener" {
  description = "Create the HTTPS listener after the ACM certificate reaches ISSUED"
  type        = bool
}

variable "alb_ssl_policy" {
  description = "TLS security policy for the ALB HTTPS listener"
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

variable "ecr_repository_name" {
  description = "Shared ECR repository used by all MVP applications"
  type        = string
  default     = "inframorph/apps"
}

variable "db_instance_class" {
  description = "Instance class for the shared MVP PostgreSQL database"
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = "PostgreSQL major version"
  type        = string
  default     = "16"
}

variable "db_name" {
  description = "Initial database used by private app bootstrap tasks"
  type        = string
  default     = "app"
}

variable "db_username" {
  description = "RDS master username; the password is managed by RDS"
  type        = string
  default     = "inframorph"
}
