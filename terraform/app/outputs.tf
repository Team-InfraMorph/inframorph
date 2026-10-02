output "service_names" {
  value = { for name, service in aws_ecs_service.service : name => service.name }
}

output "task_definition_arns" {
  value = { for name, task in aws_ecs_task_definition.service : name => task.arn }
}

output "public_target_group_arn" {
  value = one(values(aws_lb_target_group.public)).arn
}

output "external_url" {
  value = "https://${var.hostname}"
}

output "app_secret_arn" {
  value = var.db_enabled ? aws_secretsmanager_secret.database[0].arn : null
}

output "data_task_security_group_id" {
  value = var.db_enabled ? aws_security_group.data_task[0].id : null
}

output "bootstrap_task_definition_arn" {
  value = var.db_enabled ? aws_ecs_task_definition.bootstrap[0].arn : null
}

output "migration_task_definition_arn" {
  value = var.db_enabled ? aws_ecs_task_definition.migration[0].arn : null
}

output "storage_bucket_name" {
  value = var.storage_enabled ? aws_s3_bucket.uploads[0].bucket : null
}
