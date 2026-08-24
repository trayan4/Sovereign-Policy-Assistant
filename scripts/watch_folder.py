#!/usr/bin/env python3
"""Watches policy_library/ for new or changed PDFs and automatically
re-runs ingestion - the "folder watcher that pretends to be SharePoint and
triggers re-index" from the project plan. A human drops a new or updated
policy PDF into the folder; nobody has to remember to run ingest.py by hand.

Design choices, deliberately kept simple for this corpus's scale (40
documents):

- Triggers a FULL re-ingestion (scripts/ingest.py processes every PDF every
  run already), not an incremental update of just the changed file. At this
  scale a full rebuild is fast and avoids an entire class of partial-state
  bugs an incremental pipeline would need to guard against - status
  computation and relationship detection both need the whole document set
  compared together anyway (see status_rules.py and relationships.py), so
  "incremental" would still end up re-touching most of the pipeline.

- Debounces: a single `cp` of a file, or an editor saving one, commonly
  fires several filesystem events in quick succession (create, then one or
  more modify/close events) and can briefly present a partially-written
  file. This waits for a quiet period (no new events) before triggering,
  rather than reacting to the very first event."""

import subprocess
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

ROOT = Path(__file__).parent.parent
POLICY_DIR = ROOT / "policy_library"
DEBOUNCE_SECONDS = 5


class PolicyFolderHandler(FileSystemEventHandler):
    def __init__(self):
        self.pending = False
        self.last_event_time = 0.0

    def _note_event(self, path: str):
        if not path.lower().endswith(".pdf"):
            return
        print(f"[watcher] Detected change: {Path(path).name}")
        self.pending = True
        self.last_event_time = time.monotonic()

    def on_created(self, event):
        if not event.is_directory:
            self._note_event(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._note_event(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._note_event(event.dest_path)


def run_ingestion():
    print("[watcher] Quiet period elapsed - re-indexing the policy library...")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ingest.py")],
        cwd=str(ROOT),
    )
    if result.returncode == 0:
        print("[watcher] Re-index complete.")
    else:
        print(f"[watcher] Re-index FAILED (exit code {result.returncode}). "
              f"Policy library may be in a stale state until this is fixed.")


def main():
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[watcher] Watching {POLICY_DIR} for new or changed PDFs...")

    handler = PolicyFolderHandler()
    observer = Observer()
    observer.schedule(handler, str(POLICY_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
            if handler.pending and (time.monotonic() - handler.last_event_time) >= DEBOUNCE_SECONDS:
                handler.pending = False
                run_ingestion()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
