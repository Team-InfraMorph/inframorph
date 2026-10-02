resource "aws_secretsmanager_secret" "database" {
  count = var.db_enabled ? 1 : 0

  name                    = var.app_secret_name
  description             = "Application-scoped PostgreSQL credential for ${var.app_id}"
  recovery_window_in_days = 7

  lifecycle {
    prevent_destroy = true
  }
}

# The Adapter writes the secret version only after the metadata resource exists.
# Terraform never receives either the app password or the Foundation master value.

resource "aws_s3_bucket" "uploads" {
  count = var.storage_enabled ? 1 : 0

  bucket = var.storage_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  count = var.storage_enabled ? 1 : 0

  bucket                  = aws_s3_bucket.uploads[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  count = var.storage_enabled ? 1 : 0

  bucket = aws_s3_bucket.uploads[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "uploads" {
  count = var.storage_enabled ? 1 : 0

  bucket = aws_s3_bucket.uploads[0].id
  versioning_configuration {
    status = "Enabled"
  }
}
