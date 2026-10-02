resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.project}-db" }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-postgres"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name                     = var.db_name
  username                    = var.db_username
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false

  # Temporary MVP setting for the current AWS account plan. Restore the
  # operational seven-day retention target after the plan is upgraded.
  backup_retention_period      = 1
  delete_automated_backups     = false
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.project}-postgres-final"
  deletion_protection          = true
  copy_tags_to_snapshot        = true
  apply_immediately            = true
  auto_minor_version_upgrade   = true
  performance_insights_enabled = false

  tags = { Name = "${var.project}-postgres" }
}
