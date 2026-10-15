"""Background worker: claims Tasks from the graph and resumes paused runs.

Agents never call each other. Triage creates an 'investigate' Task, a human resolution creates a
'record' Task; whichever worker claims a task (atomically, in one Cypher query) runs the agent for it.
Several workers can run at once.

    python -m blastradius.agents.worker            # loop forever
    python -m blastradius.agents.worker --once     # process what is waiting, then exit
"""
import argparse
import logging
import socket
import threading
import time
import uuid

from blastradius import config
from blastradius.agents import incident_agents, runtime, store

log = logging.getLogger("blastradius.worker")


def process_once(tenant: str, worker_id: str) -> int:
    """Triage new alerts, run open tasks and due resumes. Returns how many things were done."""
    done = 0
    if incident_agents.triage(tenant):
        done += 1
    while task := store.claim_task(tenant, ["investigate", "record"], worker_id):
        done += 1
        try:
            if task["type"] == "investigate":
                out = incident_agents.investigate(tenant, task["incident"], task["id"])
            else:
                out = incident_agents.write_memory(tenant, task["incident"], task["payload"], task["id"])
            store.finish_task(tenant, task["id"], "done" if out["status"] == "done" else out["status"])
        except Exception:
            log.exception("task %s failed", task["id"])
            store.finish_task(tenant, task["id"], "failed")
    for run in store.resumable_runs(tenant):
        done += 1
        spec = incident_agents.AGENTS[run["agent"]]
        out = runtime.execute(tenant, spec, run["id"])
        if out["status"] in ("done", "failed"):
            _finish_task_of_run(tenant, run["id"], out["status"])
    return done


def _finish_task_of_run(tenant, run_id, status):
    from blastradius import db
    db.graph(tenant).query("MATCH (r:AgentRun {id: $id})-[:WORKED_ON]->(t:Task) "
                           "SET t.status = $status, t.finished_at = timestamp() / 1000",
                           {"id": run_id, "status": status})


def run_forever(tenants: list[str], interval: float = 2.0, stop: threading.Event | None = None) -> None:
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:4]}"
    log.info("worker %s watching %s", worker_id, tenants)
    while not (stop and stop.is_set()):
        for t in tenants:
            try:
                process_once(t, worker_id)
            except Exception:
                log.exception("worker loop error for tenant %s", t)
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", action="append")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    tenants = args.tenant or [config.DEFAULT_TENANT]
    if args.once:
        for t in tenants:
            print(t, "processed", process_once(t, "cli"))
    else:
        run_forever(tenants)


if __name__ == "__main__":
    main()
