resource "google_artifact_registry_repository" "apps" {
  repository_id = var.artifact_repository_id
  location      = var.region
  format        = "DOCKER"
  description   = "Shared InfraMorph application images (immutable tags)"

  docker_config {
    immutable_tags = true
  }

  depends_on = [google_project_service.required]
}

# DB bootstrap jobs pull the digest-pinned PostgreSQL client image through this
# proxy instead of Docker Hub directly (rate limits, single registry for audit).
resource "google_artifact_registry_repository" "dockerhub" {
  repository_id = var.dockerhub_remote_repository_id
  location      = var.region
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  description   = "Docker Hub proxy for digest-pinned helper images"

  remote_repository_config {
    docker_repository {
      public_repository = "DOCKER_HUB"
    }
  }

  depends_on = [google_project_service.required]
}
