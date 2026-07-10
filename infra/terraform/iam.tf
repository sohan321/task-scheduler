data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS itself uses to pull images and write logs on the
# task's behalf, before the application code ever runs.
resource "aws_iam_role" "ecs_execution" {
  name               = "${var.project_name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_connection_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.redis_url.arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_execution_read_secrets" {
  name   = "${var.project_name}-read-connection-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.read_connection_secrets.json
}

# Task role: what the application code itself would assume for AWS API
# calls. Nothing in this app calls AWS APIs today, but ECS task definitions
# require a task role to be set, so this is an empty placeholder rather
# than reusing the execution role (which has broader ECR/logs permissions
# the app itself has no business holding).
resource "aws_iam_role" "ecs_task" {
  name               = "${var.project_name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}
