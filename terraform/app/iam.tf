data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.resource_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  description        = "ECS execution role limited to ${var.app_id} application secrets"
}

resource "aws_iam_role_policy_attachment" "execution_baseline" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secret" {
  count = var.db_enabled ? 1 : 0

  statement {
    sid       = "ReadOnlyThisAppDatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.database[0].arn]
  }
}

resource "aws_iam_role_policy" "execution_secret" {
  count = var.db_enabled ? 1 : 0

  name   = "read-app-database-secret"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secret[0].json
}

resource "aws_iam_role" "task" {
  name               = "${var.resource_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  description        = "Runtime role limited to ${var.app_id} data resources"
}

data "aws_iam_policy_document" "task_storage" {
  count = var.storage_enabled ? 1 : 0

  statement {
    sid       = "ListOnlyThisAppBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.uploads[0].arn]
  }

  statement {
    sid       = "ObjectsOnlyInThisAppBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.uploads[0].arn}/*"]
  }
}

resource "aws_iam_role_policy" "task_storage" {
  count = var.storage_enabled ? 1 : 0

  name   = "app-storage"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_storage[0].json
}

resource "aws_iam_role" "bootstrap_execution" {
  count = var.db_enabled ? 1 : 0

  name               = "${var.resource_prefix}-bootstrap-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  description        = "Execution role used only by the private DB bootstrap task"
}

resource "aws_iam_role_policy_attachment" "bootstrap_baseline" {
  count = var.db_enabled ? 1 : 0

  role       = aws_iam_role.bootstrap_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "bootstrap_secrets" {
  count = var.db_enabled ? 1 : 0

  statement {
    sid     = "BootstrapOnlyDatabaseSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      var.rds_master_secret_arn,
      aws_secretsmanager_secret.database[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "bootstrap_secrets" {
  count = var.db_enabled ? 1 : 0

  name   = "bootstrap-database-secrets"
  role   = aws_iam_role.bootstrap_execution[0].id
  policy = data.aws_iam_policy_document.bootstrap_secrets[0].json
}
