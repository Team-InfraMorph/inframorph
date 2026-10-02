resource "aws_acm_certificate" "apps" {
  domain_name       = "*.${var.apps_domain}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${var.project}-apps-wildcard" }
}
