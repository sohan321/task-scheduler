resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.project_name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

# ElastiCache AUTH tokens must be 16-128 printable ASCII chars, excluding
# '@', '"', and '/' - override_special avoids those three explicitly rather
# than disabling special characters altogether.
resource "random_password" "redis_auth_token" {
  length           = 32
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?"
}

# A single-node replication group (not aws_elasticache_cluster) because only
# the replication-group resource supports transit_encryption_enabled/
# auth_token - the security groups alone aren't enough to keep queue data
# off the wire in plaintext.
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${var.project_name}-redis"
  description          = "Redis for the task scheduler job queue"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_clusters   = 1
  port                 = 6379
  parameter_group_name = "default.redis7"

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = random_password.redis_auth_token.result

  tags = { Name = "${var.project_name}-redis" }
}

# Whole connection string as one secret, mirroring rds.tf's database_url
# secret - simplest for a single ECS "secrets" entry. Terraform state holds
# both the token and this string in plaintext either way - see the backend
# note in versions.tf.
resource "aws_secretsmanager_secret" "redis_url" {
  name = "${var.project_name}/redis-url"
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = "rediss://:${random_password.redis_auth_token.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:${aws_elasticache_replication_group.main.port}/0"
}
