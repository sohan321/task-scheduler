resource "random_password" "db_password" {
  length  = 24
  special = false # simplifies embedding in a connection-string secret; still 24 chars of a 62-char alphabet
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-db"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.project_name}-db-subnet-group" }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  max_allocated_storage = var.db_allocated_storage_gb * 2 # allow autoscaling up to 2x before a manual bump is needed

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_password.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  # Cost/durability tradeoffs for a demo deployment - bump these for prod:
  multi_az                = false
  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false

  tags = { Name = "${var.project_name}-db" }
}

# Whole connection string as one secret (simplest for a single ECS "secrets"
# entry) rather than separate host/user/password keys pieced together at
# container start. Terraform state holds this in plaintext either way - see
# the backend note in versions.tf.
resource "aws_secretsmanager_secret" "database_url" {
  name = "${var.project_name}/database-url"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql://${var.db_username}:${random_password.db_password.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}"
}
