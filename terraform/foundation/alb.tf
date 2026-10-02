resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  drop_invalid_header_fields = true
  enable_deletion_protection = false

  tags = { Name = "${var.project}-alb" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = var.enable_https_listener ? "redirect" : "fixed-response"

    dynamic "redirect" {
      for_each = var.enable_https_listener ? [1] : []

      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }

    dynamic "fixed_response" {
      for_each = var.enable_https_listener ? [] : [1]

      content {
        content_type = "text/plain"
        message_body = "InfraMorph: HTTPS certificate setup pending"
        status_code  = "404"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.enable_https_listener ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = aws_acm_certificate.apps.arn
  ssl_policy        = var.alb_ssl_policy

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "InfraMorph: no app deployed"
      status_code  = "404"
    }
  }

  lifecycle {
    precondition {
      condition     = aws_acm_certificate.apps.status == "ISSUED"
      error_message = "The ACM certificate is not ISSUED. Add the DNS validation CNAME and retry after ACM reports ISSUED."
    }
  }
}
