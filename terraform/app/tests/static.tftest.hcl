mock_provider "aws" {}

run "first_deployment_plan" {
  command = plan

  override_resource {
    target          = aws_secretsmanager_secret.database[0]
    override_during = plan
    values = {
      arn = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:app-database-AbCd"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.ecs_tasks_assume
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.execution_secret[0]
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.task_storage[0]
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.bootstrap_secrets[0]
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  variables {
    region                 = "ap-northeast-2"
    account_id             = "111122223333"
    app_id                 = "demo-app"
    resource_prefix        = "im-demo-app-cf9ec463"
    hostname               = "demo-app.apps.example.com"
    listener_rule_priority = 12000
    # Test-only placeholder with the same 40-character shape as a full Git SHA.
    source_revision         = "ebad709867cf1f3523075d8038ae41bd69ecae9b"
    vpc_id                  = "vpc-0123abcd"
    private_subnet_ids      = ["subnet-0123abcd", "subnet-4567abcd"]
    alb_security_group_id   = "sg-0123abcd"
    https_listener_arn      = "arn:aws:elasticloadbalancing:ap-northeast-2:111122223333:listener/app/im/1/2"
    ecs_cluster_arn         = "arn:aws:ecs:ap-northeast-2:111122223333:cluster/inframorph"
    deployment_image_uri    = "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/inframorph/apps@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    service_image_uri       = "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/inframorph/apps@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    app_config              = { STORAGE_DRIVER = "s3" }
    service_config          = { STORAGE_DRIVER = "s3", PORT = "9000", AWS_REGION = "wrong-region" }
    db_enabled              = true
    service_db_enabled      = true
    rds_address             = "db.example.ap-northeast-2.rds.amazonaws.com"
    rds_port                = 5432
    rds_initial_database    = "app"
    rds_security_group_id   = "sg-4567abcd"
    rds_master_secret_arn   = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:rds-master-AbCd"
    app_database_name       = "app_demo_app_cf9ec463"
    app_database_role       = "app_demo_app_cf9ec463_user"
    app_secret_name         = "/inframorph/apps/demo-app/database"
    migration_command       = ["npx", "prisma", "migrate", "deploy"]
    storage_enabled         = true
    service_storage_enabled = true
    storage_bucket_name     = "im-demo-app-cf9ec463-111122223333-an2"
    activate_services       = false
    services = [
      {
        name    = "web"
        kind    = "http"
        cpu     = 256
        memory  = 512
        port    = 3000
        health  = "/health"
        public  = true
        command = []
      },
      {
        name    = "worker"
        kind    = "worker"
        cpu     = 256
        memory  = 512
        port    = null
        health  = null
        public  = false
        command = ["node", "src/worker.js"]
      },
    ]
  }

  assert {
    condition     = aws_ecs_service.service["web"].desired_count == 0
    error_message = "The first deployment must not start services before bootstrap and migration."
  }

  assert {
    condition     = length(aws_lb_target_group.public) == 1
    error_message = "Only the public web service may own a target group."
  }

  assert {
    condition     = !contains(keys(aws_lb_target_group.public), "worker")
    error_message = "A worker must not own public routing resources."
  }

  assert {
    condition     = strcontains(var.service_image_uri, "@sha256:") && !strcontains(var.service_image_uri, ":latest")
    error_message = "ECS task definitions must use digest-pinned images."
  }

  assert {
    condition = alltrue([for task in aws_ecs_task_definition.service :
      length(jsondecode(task.container_definitions)[0].environment) ==
      length(distinct([for item in jsondecode(task.container_definitions)[0].environment : item.name]))
    ])
    error_message = "ECS environment names must be unique even when config repeats adapter-owned names."
  }

  assert {
    condition = [for item in jsondecode(aws_ecs_task_definition.service["web"].container_definitions)[0].environment :
    item.value if item.name == "PORT"] == ["3000"]
    error_message = "The web service must receive its declared port exactly once."
  }

  assert {
    condition = length([for item in jsondecode(aws_ecs_task_definition.service["worker"].container_definitions)[0].environment :
    item if item.name == "PORT"]) == 0
    error_message = "A worker must not inherit the web service port."
  }

  assert {
    condition = [for item in jsondecode(aws_ecs_task_definition.service["web"].container_definitions)[0].environment :
    item.value if item.name == "AWS_REGION"] == ["ap-northeast-2"]
    error_message = "Storage settings must use the operator's region without duplicate names."
  }
}
