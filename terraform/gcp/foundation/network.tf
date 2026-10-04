resource "google_compute_network" "main" {
  name                    = "${var.project}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.required]
}

# Cloud Run reaches private Cloud SQL through Direct VPC egress on this subnet.
# Internet egress stays on Cloud Run's own path (PRIVATE_RANGES_ONLY), so no NAT is needed.
resource "google_compute_subnetwork" "run" {
  name                     = "${var.project}-run-${var.region}"
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.run_subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_global_address" "private_services" {
  name          = "${var.project}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.psa_prefix_length
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]

  # Removing the peering while Cloud SQL still exists fails; keep it on destroy.
  deletion_policy = "ABANDON"
}
