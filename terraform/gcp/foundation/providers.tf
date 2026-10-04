provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = {
    project    = var.project
    stack      = "foundation"
    managed_by = "terraform"
  }
}

data "google_project" "current" {
  project_id = var.project_id

  lifecycle {
    postcondition {
      condition     = self.number == var.expected_project_number
      error_message = "The configured project does not match expected_project_number."
    }
  }
}
