terraform {
  backend "s3" {
    key          = "foundation/terraform.tfstate"
    region       = "ap-northeast-2"
    use_lockfile = true
    encrypt      = true
  }
}
