variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used as a prefix for resource names/tags"
  type        = string
  default     = "task-scheduler"
}

variable "environment" {
  description = "Environment tag (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "AZs to spread subnets across (2 minimum for the ALB)"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets (ALB, NAT gateway), one per AZ"
  type        = list(string)
  default     = ["10.0.0.0/24", "10.0.1.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets (ECS tasks, RDS, ElastiCache), one per AZ"
  type        = list(string)
  default     = ["10.0.10.0/24", "10.0.11.0/24"]
}

variable "db_name" {
  description = "Postgres database name"
  type        = string
  default     = "scheduler"
}

variable "db_username" {
  description = "Postgres master username"
  type        = string
  default     = "scheduler"
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "RDS allocated storage in GB"
  type        = number
  default     = 20
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t4g.micro"
}

variable "api_image" {
  description = "Full ECR image URI (including tag) for the API service. Leave blank on first apply before any image has been pushed; the ECR repo URL is available as an output to build the value."
  type        = string
  default     = ""
}

variable "worker_image" {
  description = "Full ECR image URI (including tag) for the worker service. Leave blank on first apply before any image has been pushed."
  type        = string
  default     = ""
}

variable "api_desired_count" {
  description = "Number of API tasks to run. NOTE: the app's Prometheus counters are in-process, per-task state with no shared registry; scraping /metrics through the ALB (which round-robins across tasks) makes rate()/increase() queries meaningless once this is >1. Scrape each task directly (e.g. via ECS service discovery) instead of through the ALB if you scale this up."
  type        = number
  default     = 1
}

variable "worker_desired_count" {
  description = "Number of worker tasks to run"
  type        = number
  default     = 1
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task (256 = 0.25 vCPU)"
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory (MB) for the API task"
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = "Fargate CPU units for the worker task"
  type        = number
  default     = 256
}

variable "worker_memory" {
  description = "Fargate memory (MB) for the worker task"
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for ECS task logs"
  type        = number
  default     = 14
}
