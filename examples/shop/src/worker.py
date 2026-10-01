"""The fulfilment worker: runs jobs, writes results, reports a heartbeat."""


def run_job(job):
    """Run one job and write the result (journal verb: write)."""
    return {"job": job, "done": True}


def heartbeat():
    """Report that the worker is alive (event kind: heartbeat)."""
    return {"alive": True}
