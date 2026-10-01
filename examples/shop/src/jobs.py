"""The job queue between the API and the worker."""

JOB_MESSAGE_FIELDS = ("order_id", "kind", "attempt")


def enqueue(order_id, kind):
    """Add a fulfilment job (journal verb: enqueue)."""
    return {"order_id": order_id, "kind": kind, "attempt": 1}


def deliver(job):
    """Hand a job to a worker (event kind: job.delivered)."""
    return job
