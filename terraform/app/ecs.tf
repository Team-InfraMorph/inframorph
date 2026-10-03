resource "aws_cloudwatch_log_group" "service" {
  for_each = local.service_map

  name              = "/inframorph/apps/${var.app_id}/${each.key}"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "data" {
  count = var.db_enabled ? 1 : 0

  name              = "/inframorph/apps/${var.app_id}/data-tasks"
  retention_in_days = 7
}

resource "aws_ecs_task_definition" "service" {
  for_each = local.service_map

  family                   = "${var.resource_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(each.value.cpu)
  memory                   = tostring(each.value.memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  skip_destroy             = true

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    merge(
      {
        name      = each.key
        image     = var.service_image_uri
        essential = true
        # ECS collapses duplicate names. Build one map so refreshing state does
        # not schedule another task replacement; PORT belongs to HTTP services.
        environment = [for name, value in merge(
          { for item in local.service_config_environment : item.name => item.value if item.name != "PORT" },
          { for item in local.service_storage_environment : item.name => item.value },
          each.value.port == null ? {} : { PORT = tostring(each.value.port) },
        ) : { name = name, value = value }]
        secrets = var.service_db_enabled ? [{
          name      = "DATABASE_URL"
          valueFrom = "${aws_secretsmanager_secret.database[0].arn}:url::"
        }] : []
        portMappings = each.value.kind == "http" ? [{
          containerPort = each.value.port
          hostPort      = each.value.port
          protocol      = "tcp"
          name          = "${each.key}-http"
        }] : []
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            awslogs-group         = aws_cloudwatch_log_group.service[each.key].name
            awslogs-region        = var.region
            awslogs-stream-prefix = "ecs"
          }
        }
      },
      length(each.value.command) > 0 ? { command = each.value.command } : {},
    )
  ])
}

resource "aws_ecs_service" "service" {
  for_each = local.service_map

  name            = "${var.resource_prefix}-${each.key}"
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.service[each.key].arn
  desired_count   = var.activate_services ? 1 : 0
  launch_type     = "FARGATE"

  platform_version                   = "LATEST"
  enable_ecs_managed_tags            = true
  propagate_tags                     = "SERVICE"
  health_check_grace_period_seconds  = each.value.public ? 60 : null
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service[each.key].id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.value.public ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.public[each.key].arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  depends_on = [
    aws_lb_listener_rule.public,
    aws_vpc_security_group_egress_rule.alb_to_service,
    aws_vpc_security_group_ingress_rule.alb_to_service,
  ]
}

resource "aws_ecs_task_definition" "bootstrap" {
  count = var.db_enabled ? 1 : 0

  family                   = "${var.resource_prefix}-db-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.bootstrap_execution[0].arn
  task_role_arn            = aws_iam_role.task.arn
  skip_destroy             = true

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name = "db-bootstrap"
    # linux/amd64 manifest for PostgreSQL 16.15-alpine3.24, resolved from
    # public ECR on 2026-10-02. Bootstrap tasks follow the same digest-only
    # rule as application and migration tasks.
    image      = "public.ecr.aws/docker/library/postgres@sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551"
    essential  = true
    entryPoint = ["/bin/sh"]
    command    = ["-ceu", local.db_bootstrap_script]
    environment = [
      { name = "DB_HOST", value = var.rds_address },
      { name = "DB_PORT", value = tostring(var.rds_port) },
      { name = "MASTER_DATABASE", value = var.rds_initial_database },
      { name = "APP_DB_NAME", value = var.app_database_name },
      { name = "APP_DB_ROLE", value = var.app_database_role },
    ]
    secrets = [
      { name = "MASTER_USERNAME", valueFrom = "${var.rds_master_secret_arn}:username::" },
      { name = "MASTER_PASSWORD", valueFrom = "${var.rds_master_secret_arn}:password::" },
      { name = "APP_DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.database[0].arn}:password::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.data[0].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "bootstrap"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "migration" {
  count = var.db_enabled ? 1 : 0

  family                   = "${var.resource_prefix}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  skip_destroy             = true

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "migration"
    image       = var.deployment_image_uri
    essential   = true
    command     = var.migration_command
    environment = concat(local.config_environment, local.storage_environment)
    secrets = [{
      name      = "DATABASE_URL"
      valueFrom = "${aws_secretsmanager_secret.database[0].arn}:url::"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.data[0].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migration"
      }
    }
  }])
}
