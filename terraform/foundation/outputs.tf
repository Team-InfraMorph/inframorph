output "region" {
  value = var.region
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "alb_security_group_id" {
  description = "Source security group for app-owned task security groups"
  value       = aws_security_group.alb.id
}

output "alb_arn" {
  value = aws_lb.main.arn
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "http_listener_arn" {
  value = aws_lb_listener.http.arn
}

output "https_listener_arn" {
  description = "Listener used by app-owned host routing rules; null until HTTPS is enabled"
  value       = try(aws_lb_listener.https[0].arn, null)
}

output "nat_public_ips" {
  description = "NAT egress IP by availability zone"
  value       = { for index, az in var.azs : az => aws_eip.nat[index].public_ip }
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_cluster_arn" {
  value = aws_ecs_cluster.main.arn
}

output "ecs_execution_role_arn" {
  description = "Baseline role for apps that do not inject external secrets"
  value       = aws_iam_role.ecs_execution.arn
}

output "ecr_repository_name" {
  value = aws_ecr_repository.apps.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.apps.repository_url
}

output "acm_certificate_arn" {
  value = aws_acm_certificate.apps.arn
}

output "acm_certificate_status" {
  value = aws_acm_certificate.apps.status
}

output "acm_dns_validation_records" {
  description = "CNAME records to add at the DNS provider with proxying disabled"
  value = [
    for option in aws_acm_certificate.apps.domain_validation_options : {
      name  = option.resource_record_name
      type  = option.resource_record_type
      value = option.resource_record_value
    }
  ]
}

output "apps_wildcard_domain" {
  value = "*.${var.apps_domain}"
}

output "rds_address" {
  value = aws_db_instance.main.address
}

output "rds_port" {
  value = aws_db_instance.main.port
}

output "rds_db_name" {
  value = aws_db_instance.main.db_name
}

output "rds_security_group_id" {
  description = "Security group that app-owned ingress rules target"
  value       = aws_security_group.rds.id
}

output "rds_master_secret_arn" {
  description = "RDS-managed master credential secret ARN; secret value is not output"
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}
