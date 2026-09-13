# ADR-004: Ingestion Pipeline and Scaling

## Status

Accepted

## Context

Sentry sends issue-level webhooks to our backend. The backend is horizontally scaled behind a load balancer. Before batch processing (ADR-002) can occur, incoming webhooks must be validated, filtered through the ADR-001 decision tree, and staged for batching.

### Constraints

- **Postgres** for persistent state (issues, tenants, outcomes).
- **Redis** for queuing and coordination.
- **Horizontally scaled backend**, any instance can receive any webhook.
- **Open source first**, simple default deployment. Must also scale to thousands of tenants in a cloud offering without architectural changes.

### Volume assumptions

We subscribe to Sentry **issue** webhooks, not individual error events. Sentry already groups events into issues, so volume is low, even thousands of tenants produce at most a few thousand issue webhooks per minute. This is well within Redis's capacity.

### Initial backfill

When a tenant onboards, we make a one-time API call to Sentry (`GET /api/0/projects/{org}/{project}/issues/`) to sync existing issues. After that, the webhook subscription handles all new activity.

## Decision

### Two-stage pipeline

```
Stage 1: Ingestion (hot path)
  webhook → validate signature → LPUSH to Redis queue → return 200

Stage 2: Filtering (consumer)
  BRPOP from Redis queue → ADR-001 check against Postgres → enqueue to pending pool for batch processing
```

### Stage 1: Webhook ingestion

The webhook endpoint does the absolute minimum:

1. Validate the Sentry webhook signature.
2. Extract tenant ID, issue ID, and event metadata.
3. `LPUSH` the payload to a single Redis list (`queue:incoming`).
4. Return `200 OK`.

No business logic. No database calls. The handler's only job is to acknowledge Sentry and not lose events.

**Why queue-first:**

- **Reliability**, If Postgres is slow or briefly unavailable, webhooks are buffered in Redis instead of dropped.
- **Separation of concerns**, Ingestion is decoupled from decision logic. The webhook handler has no reason to change when ADR-001 rules evolve.
- **Backpressure**, During incidents, Sentry may fire many issue webhooks across tenants simultaneously. The queue absorbs the burst; consumers process at their own pace.

### Stage 2: Filtering consumer

A pool of consumer workers runs `BRPOP` on the Redis queue and processes events:

1. Deserialize the payload (tenant ID, issue ID, metadata).
2. Look up the issue in Postgres: `SELECT status FROM issues WHERE tenant_id = $1 AND sentry_issue_id = $2`.
3. Apply ADR-001 decision tree:
   - **SKIP**, Log and discard. No further action.
   - **PROCESS**, Push to the pending pool for batch processing (ADR-002).
4. For new issues (no row exists), insert into Postgres and push to the pending pool.

Consumers are stateless, any consumer handles any tenant. Scale horizontally by adding consumers.

### Queue abstraction

The pipeline depends on a queue interface, not a specific implementation:

- **`enqueue(event)`**, Push an event to the queue.
- **`dequeue() -> event`**, Block until an event is available, then return it.

**Default implementation:** Redis list (`LPUSH` / `BRPOP`). Single list, simple, handles the expected volume.

**Future option:** If durable replay or higher throughput is needed at cloud scale, swap Redis list for Kafka. The change is mechanical, `LPUSH` becomes a Kafka producer, `BRPOP` becomes a Kafka consumer group. Nothing above or below the queue interface changes.

### Scaling characteristics

| Deployment | Backend instances | Consumers | Queue |
|---|---|---|---|
| Open source (self-hosted) | 1 | 1 | Redis list |
| Cloud (hundreds of tenants) | N (behind LB) | M (worker pool) | Redis list |
| Cloud (thousands of tenants) | N | M | Redis list or Kafka |

The architecture is the same at every tier. Scaling is additive (more instances, more consumers), not structural.

## Consequences

- **Sentry gets a fast 200**, No database calls in the hot path. Webhook handler is trivially fast.
- **No lost events**, Redis buffers webhooks if downstream processing is slow.
- **ADR-001 logic is isolated**, Decision tree runs in the consumer, not the webhook handler. Changes to filtering rules don't touch the ingestion layer.
- **Horizontally scalable without coordination**, Consumers are stateless and compete on the same queue. No leader election or partitioning needed.
- **Queue implementation is swappable**, Redis list today, Kafka later if needed. Same interface, same consumers.
- **Open questions for follow-up ADRs:** Error handling and retry strategy for consumer failures. Pending pool structure and how the batch dispatcher (ADR-002) reads from it.
