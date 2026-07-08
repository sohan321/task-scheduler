Project: Distributed Task Scheduler / Job Queue Service
Build a job queue/task scheduler service, similar in spirit to Celery/Sidekiq. Start with Phase 1 and confirm it works before moving to later phases.
Stack: FastAPI (Python) for the API, PostgreSQL for durable job state, Redis for queueing/dispatch, Docker Compose for local dev.
Phase 1 — Core loop:

POST /jobs (accepts a payload, creates a job row, status="pending")
GET /jobs/{id} (returns job status/result)
Postgres table: jobs(id, payload, status, attempts, created_at, updated_at)
A worker script that polls for pending jobs, simulates work (sleep + random failure), updates status to success/failed
docker-compose.yml running API + Postgres

Phase 2 — Real queueing: replace polling with Redis (BLPOP), support multiple concurrent workers with no double-processing.
Phase 3 — Reliability: exponential backoff retries with jitter, visibility timeout/lease so crashed workers don't lose jobs, dead-letter table after N failed attempts.
Phase 4 — Scheduling & priority: delayed jobs (run_at), priority queues via Redis sorted sets.
Phase 5 — Observability & AWS deploy: structured logging, a /metrics endpoint, deploy to AWS (ECS Fargate + RDS + ElastiCache).
Phase 6 (stretch) — Webhooks on job completion with retry.
Start by scaffolding Phase 1, explain your file structure choices, and pause for my confirmation before Phase 2.