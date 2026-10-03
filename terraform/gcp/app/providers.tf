provider "google" {
  project = var.project_id
  region  = var.region

  # Unlike the AWS stack, the source revision is not a provider-wide label: it
  # changes on every deploy and would turn each secret, bucket and job into an
  # in-place update. Only revision-bearing resources carry it (see locals.tf).
  default_labels = {
    project    = "inframorph"
    stack      = "app"
    app_id     = var.app_id
    managed_by = "terraform"
  }
}
