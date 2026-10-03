locals {
  lb_count = var.enable_load_balancer ? 1 : 0
}

resource "google_compute_global_address" "lb" {
  count = local.lb_count

  name = "${var.project}-apps-lb"
}

# Wildcard certificates need DNS authorization: add the CNAME from the
# certificate_dns_authorization_records output at the DNS provider.
resource "google_certificate_manager_dns_authorization" "apps" {
  count = local.lb_count

  name   = "${var.project}-apps"
  domain = var.apps_domain

  depends_on = [google_project_service.required]
}

resource "google_certificate_manager_certificate" "apps" {
  count = local.lb_count

  name = "${var.project}-apps-wildcard"

  managed {
    domains            = ["*.${var.apps_domain}"]
    dns_authorizations = [google_certificate_manager_dns_authorization.apps[0].id]
  }
}

resource "google_certificate_manager_certificate_map" "apps" {
  count = local.lb_count

  name = "${var.project}-apps"
}

resource "google_certificate_manager_certificate_map_entry" "apps" {
  count = local.lb_count

  name         = "${var.project}-apps-wildcard"
  map          = google_certificate_manager_certificate_map.apps[0].name
  hostname     = "*.${var.apps_domain}"
  certificates = [google_certificate_manager_certificate.apps[0].id]
}

# One serverless NEG routes <app>.apps_domain to the Cloud Run service named
# <app>. Apps never add load balancer resources, so a deploy has no per-app
# routing, target registration, or health-check warm-up to wait for.
resource "google_compute_region_network_endpoint_group" "apps" {
  count = local.lb_count

  name                  = "${var.project}-apps"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    url_mask = "<service>.${var.apps_domain}"
  }
}

resource "google_compute_backend_service" "apps" {
  count = local.lb_count

  name                  = "${var.project}-apps"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"

  backend {
    group = google_compute_region_network_endpoint_group.apps[0].id
  }
}

resource "google_compute_url_map" "https" {
  count = local.lb_count

  name            = "${var.project}-apps-https"
  default_service = google_compute_backend_service.apps[0].id
}

resource "google_compute_ssl_policy" "apps" {
  count = local.lb_count

  name            = "${var.project}-apps"
  profile         = "MODERN"
  min_tls_version = var.lb_ssl_policy_min_tls
}

resource "google_compute_target_https_proxy" "apps" {
  count = local.lb_count

  name            = "${var.project}-apps-https"
  url_map         = google_compute_url_map.https[0].id
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.apps[0].id}"
  ssl_policy      = google_compute_ssl_policy.apps[0].id
}

resource "google_compute_global_forwarding_rule" "https" {
  count = local.lb_count

  name                  = "${var.project}-apps-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.lb[0].id
  port_range            = "443"
  target                = google_compute_target_https_proxy.apps[0].id
}

resource "google_compute_url_map" "http_redirect" {
  count = local.lb_count

  name = "${var.project}-apps-http"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "apps" {
  count = local.lb_count

  name    = "${var.project}-apps-http"
  url_map = google_compute_url_map.http_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count = local.lb_count

  name                  = "${var.project}-apps-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.lb[0].id
  port_range            = "80"
  target                = google_compute_target_http_proxy.apps[0].id
}
