resource "aws_lb_target_group" "public" {
  for_each = local.public_services

  name        = substr("${var.resource_prefix}-${each.key}", 0, 32)
  port        = each.value.port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  # Demo apps serve short requests only; 30s of draining only kept the replaced
  # task alive longer.
  deregistration_delay = 5

  # With 2 x 15s a new target needed at least 30s to turn healthy; at 5s it is
  # healthy within seconds of registration. The timeout must stay below the interval.
  health_check {
    enabled             = true
    path                = each.value.health
    port                = "traffic-port"
    protocol            = "HTTP"
    matcher             = "200-299"
    interval            = 5
    timeout             = 4
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "public" {
  for_each = local.public_services

  listener_arn = var.https_listener_arn
  priority     = var.listener_rule_priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.public[each.key].arn
  }

  condition {
    host_header {
      values = [var.hostname]
    }
  }

  tags = {
    Name = "${var.resource_prefix}-${each.key}-https"
  }
}
