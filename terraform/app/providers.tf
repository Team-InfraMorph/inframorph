provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      project    = "inframorph"
      stack      = "app"
      app_id     = var.app_id
      managed_by = "terraform"
      source_sha = var.source_revision
    }
  }
}
