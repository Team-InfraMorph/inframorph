resource "aws_security_group" "service" {
  for_each = local.service_map

  name                   = "${var.resource_prefix}-${each.key}"
  description            = "InfraMorph ${var.app_id} ${each.key} task security group"
  vpc_id                 = var.vpc_id
  revoke_rules_on_delete = true
}

resource "aws_vpc_security_group_ingress_rule" "alb_to_service" {
  for_each = local.public_services

  security_group_id            = aws_security_group.service[each.key].id
  referenced_security_group_id = var.alb_security_group_id
  description                  = "Shared ALB to ${var.app_id}/${each.key} exact service and health port"
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "alb_to_service" {
  for_each = local.public_services

  security_group_id            = var.alb_security_group_id
  referenced_security_group_id = aws_security_group.service[each.key].id
  description                  = "Shared ALB egress to ${var.app_id}/${each.key} exact port"
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "service_https" {
  for_each = local.service_map

  security_group_id = aws_security_group.service[each.key].id
  description       = "HTTPS egress through the private subnet NAT or S3 endpoint"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "service_dns_udp" {
  for_each = local.service_map

  security_group_id = aws_security_group.service[each.key].id
  description       = "DNS UDP egress"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "service_dns_tcp" {
  for_each = local.service_map

  security_group_id = aws_security_group.service[each.key].id
  description       = "DNS TCP egress"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "service_to_rds" {
  for_each = var.db_enabled ? local.service_map : {}

  security_group_id            = aws_security_group.service[each.key].id
  referenced_security_group_id = var.rds_security_group_id
  description                  = "${var.app_id}/${each.key} to shared RDS PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_service" {
  for_each = var.db_enabled ? local.service_map : {}

  security_group_id            = var.rds_security_group_id
  referenced_security_group_id = aws_security_group.service[each.key].id
  description                  = "Shared RDS from ${var.app_id}/${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_security_group" "data_task" {
  count = var.db_enabled ? 1 : 0

  name                   = "${var.resource_prefix}-data-task"
  description            = "Private DB bootstrap and migration tasks for ${var.app_id}"
  vpc_id                 = var.vpc_id
  revoke_rules_on_delete = true
}

resource "aws_vpc_security_group_egress_rule" "data_https" {
  count = var.db_enabled ? 1 : 0

  security_group_id = aws_security_group.data_task[0].id
  description       = "HTTPS egress for image pulls, logs, and secrets"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "data_dns_udp" {
  count = var.db_enabled ? 1 : 0

  security_group_id = aws_security_group.data_task[0].id
  description       = "DNS UDP egress"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "data_dns_tcp" {
  count = var.db_enabled ? 1 : 0

  security_group_id = aws_security_group.data_task[0].id
  description       = "DNS TCP egress"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "data_to_rds" {
  count = var.db_enabled ? 1 : 0

  security_group_id            = aws_security_group.data_task[0].id
  referenced_security_group_id = var.rds_security_group_id
  description                  = "Private bootstrap and migration tasks to shared RDS"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_data" {
  count = var.db_enabled ? 1 : 0

  security_group_id            = var.rds_security_group_id
  referenced_security_group_id = aws_security_group.data_task[0].id
  description                  = "Shared RDS from ${var.app_id} bootstrap and migration tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
