resource "aws_security_group" "alb" {
  name        = "${var.project}-alb"
  description = "Public HTTP and HTTPS access to the shared ALB"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from the internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count = var.enable_https_listener ? 1 : 0

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# App stacks own exact-port ALB egress rules because only they know the target
# security group and service port.

resource "aws_security_group" "rds" {
  name        = "${var.project}-rds"
  description = "Ingress is granted by app-owned security group rules"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-rds" }
}

# App stacks own standalone TCP 5432 ingress rules from their task security
# groups. Foundation never grants broad access to the shared database.
