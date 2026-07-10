resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb"
  description = "Internet-facing ALB in front of the API service"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-alb-sg" }
}

# Attach this to whatever scrapes Prometheus metrics (e.g. a self-hosted
# Prometheus task, or an ECS Exec/bastion host used for ad hoc curl checks).
# Empty by default - nothing is a member until you explicitly add one, which
# is the point: it scopes the metrics-scrape grants below to a named,
# intentional set of scrapers instead of the whole VPC CIDR.
resource "aws_security_group" "monitoring" {
  name        = "${var.project_name}-monitoring"
  description = "Members of this SG may scrape API/worker /metrics endpoints"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-monitoring-sg" }
}

resource "aws_security_group" "api" {
  name        = "${var.project_name}-api"
  description = "API Fargate tasks - reachable from the ALB, plus /metrics from the monitoring SG"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "app traffic from the ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    # The ALB listener blocks /metrics from public traffic (see ecs.tf) - an
    # internal Prometheus scrapes it directly against the task instead.
    description     = "internal /metrics scrape from the monitoring SG"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.monitoring.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-api-sg" }
}

resource "aws_security_group" "worker" {
  name        = "${var.project_name}-worker"
  description = "Worker Fargate tasks - no public ingress, metrics scraped only by the monitoring SG"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "prometheus scrape of /metrics from the monitoring SG"
    from_port       = 9100
    to_port         = 9100
    protocol        = "tcp"
    security_groups = [aws_security_group.monitoring.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-worker-sg" }
}

resource "aws_security_group" "rds" {
  name        = "${var.project_name}-rds"
  description = "Postgres - reachable only from the API and worker tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from API tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id]
  }

  ingress {
    description     = "Postgres from worker tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.worker.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-rds-sg" }
}

resource "aws_security_group" "redis" {
  name        = "${var.project_name}-redis"
  description = "Redis - reachable only from the API and worker tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis from API tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id]
  }

  ingress {
    description     = "Redis from worker tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.worker.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-redis-sg" }
}
