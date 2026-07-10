# AWS deployment (ECS Fargate + RDS + ElastiCache)

Terraform in `terraform/` provisions:

- A VPC with 2 public subnets (ALB, single NAT gateway) and 2 private subnets (ECS tasks, RDS, ElastiCache)
- An Application Load Balancer in front of the API service
- ECR repositories for the `api` and `worker` images
- An RDS Postgres instance (single-AZ, `db.t4g.micro` by default) with credentials in Secrets Manager
- An ElastiCache Redis replication group (single node, `cache.t4g.micro` by default) with AUTH token + in-transit/at-rest encryption; the full `rediss://` URL lives in Secrets Manager alongside `DATABASE_URL`
- An ECS cluster running the `api` service (behind the ALB) and `worker` service (no public ingress), both Fargate

None of this has been applied. Everything below assumes you have an AWS account and `aws configure` has been run with valid credentials.

## Cost warning

Left running, this is roughly (`us-east-1`, on-demand pricing, ballpark):

- NAT gateway: ~$32/mo + data processing
- RDS `db.t4g.micro`: ~$12-15/mo
- ElastiCache `cache.t4g.micro`: ~$12/mo
- ALB: ~$16/mo + data
- Fargate tasks (1 API + 1 worker, 0.25 vCPU/512MB each): a few dollars/mo

Call it **$70-90/month** if left up. Run `terraform destroy` (see below) when you're done testing.

## First deploy

1. **Bootstrap the infrastructure and ECR repos** (image variables are left blank on this first pass, so the ECS services will come up with a placeholder `:latest` tag that doesn't exist yet - that's expected):

   ```
   cd infra/terraform
   terraform init
   terraform apply
   ```

2. **Build and push images** using the ECR repo URLs from the outputs:

   ```
   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

   docker build -t <ecr_api_repository_url>:latest ../../api
   docker push <ecr_api_repository_url>:latest

   docker build -t <ecr_worker_repository_url>:latest ../../worker
   docker push <ecr_worker_repository_url>:latest
   ```

3. **Roll the ECS services onto the pushed images.** Either re-run `terraform apply` (harmless no-op on the image reference itself since it still resolves to `:latest`, but forces you to confirm the plan) or force a fresh deployment directly:

   ```
   aws ecs update-service --cluster task-scheduler-cluster --service task-scheduler-api --force-new-deployment
   aws ecs update-service --cluster task-scheduler-cluster --service task-scheduler-worker --force-new-deployment
   ```

4. **Verify:**

   ```
   terraform output alb_dns_name
   curl http://<alb_dns_name>/healthz
   curl -X POST http://<alb_dns_name>/jobs -H "Content-Type: application/json" -d '{"payload": {"task": "hello"}}'
   ```

   `/metrics` is intentionally blocked on the public ALB listener (it discloses job counts and queue depths) - see "Observability" below for how to reach it. Worker logs/metrics also aren't reachable from outside the VPC by design (see `security_groups.tf`) - check them via CloudWatch Logs (`/ecs/task-scheduler-worker`) or `aws ecs execute-command` into a running task.

## Subsequent deploys

Build, tag with something other than `:latest` (e.g. a git SHA), push, then either set `api_image`/`worker_image` in `terraform.tfvars` and `terraform apply`, or `force-new-deployment` as above if you kept using `:latest`.

## Observability

- Structured JSON logs go to CloudWatch Logs (`/ecs/task-scheduler-api`, `/ecs/task-scheduler-worker`) - queryable with CloudWatch Logs Insights since every line is a JSON object.
- `GET /metrics` on the API (port 8000) and worker (port 9100) are Prometheus-format scrape targets, but neither is reachable from the public ALB or the open internet - only from members of the `monitoring` security group (`security_groups.tf`). To scrape them: run your Prometheus (or an ad hoc `curl`) from a task/instance placed in that security group, targeting each task's private IP directly (e.g. via ECS service discovery / Cloud Map - not provisioned here).
- If you scale `api_desired_count` above 1, scrape each API task individually rather than through the ALB - the app's Prometheus counters are in-process per-task state, so scraping via the ALB's round-robin makes `rate()`/`increase()` queries meaningless (see the comment on `api_desired_count` in `variables.tf`).
- There's no Prometheus/Grafana stack provisioned here - use CloudWatch Container Insights / Amazon Managed Service for Prometheus if you want that provisioned too (not included, to keep this footprint minimal).

## Tearing down

```
cd infra/terraform
terraform destroy
```

This deletes everything created here, including the RDS instance (`skip_final_snapshot = true`, so no final snapshot is kept - don't run this against a deployment holding data you care about).
