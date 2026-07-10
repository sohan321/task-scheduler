output "alb_dns_name" {
  description = "Public URL for the API (http://<this>/jobs, http://<this>/metrics)"
  value       = aws_lb.main.dns_name
}

output "ecr_api_repository_url" {
  description = "Push API images here, then set -var api_image=<this>:<tag> and re-apply"
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_worker_repository_url" {
  description = "Push worker images here, then set -var worker_image=<this>:<tag> and re-apply"
  value       = aws_ecr_repository.worker.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "rds_endpoint" {
  description = "RDS endpoint (host:port), for reference - the app reads the full DATABASE_URL from Secrets Manager"
  value       = aws_db_instance.main.endpoint
}

output "redis_endpoint" {
  description = "ElastiCache primary endpoint (host:port), for reference - the app reads the full REDIS_URL (including the AUTH token) from Secrets Manager"
  value       = "${aws_elasticache_replication_group.main.primary_endpoint_address}:${aws_elasticache_replication_group.main.port}"
}

output "database_url_secret_arn" {
  description = "Secrets Manager ARN holding the full Postgres connection string"
  value       = aws_secretsmanager_secret.database_url.arn
}

output "redis_url_secret_arn" {
  description = "Secrets Manager ARN holding the full Redis connection string (with AUTH token)"
  value       = aws_secretsmanager_secret.redis_url.arn
}
