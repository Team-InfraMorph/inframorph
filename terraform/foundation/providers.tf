provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.project
      stack      = "foundation"
      managed_by = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {
  lifecycle {
    postcondition {
      condition     = self.account_id == var.expected_account_id
      error_message = "The authenticated AWS account does not match expected_account_id."
    }
  }
}
