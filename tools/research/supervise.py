"""Supervise the existing worker; resource-blocked exit never auto-recovers."""
from cli import run_worker, DEFAULT_DB, DEFAULT_CONFIG, DEFAULT_RESOURCES
if __name__ == '__main__':
    code=run_worker(str(DEFAULT_DB),registry_path=str(DEFAULT_CONFIG),resources_dir=str(DEFAULT_RESOURCES))
    # launchd restarts unexpected errors, but leaves cleanup blockers for safe recovery.
    raise SystemExit(0 if code==3 else code)
