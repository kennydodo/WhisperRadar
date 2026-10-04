import fs from 'node:fs';
import path from 'node:path';

import { readJson, writeJson } from '../lib/json.js';
import { nowIso } from '../lib/time.js';

export const STATUS = {
  pending: 'pending',
  running: 'running',
  done: 'done',
  failed: 'failed',
  skipped: 'skipped',
};

/**
 * Per-job progress that survives restarts, so `--resume` can skip finished work.
 */
export class RunState {
  constructor(filePath, { jobName, jobPath, items, projectUrl = null }) {
    this.filePath = filePath;
    this.data = {
      jobName,
      jobPath,
      // Recorded so a switch of Flow project can be detected: progress belongs to
      // a project, not just to a job file.
      projectUrl: projectUrl ?? null,
      createdAt: nowIso(),
      updatedAt: nowIso(),
      items: {},
    };
    /** Set by `open` when the job points at a different Flow project. */
    this.projectChanged = null;
    for (const item of items) {
      this.data.items[item.id] = {
        status: STATUS.pending,
        attempts: 0,
        files: [],
        error: null,
        updatedAt: nowIso(),
      };
    }
  }

  static open(filePath, meta) {
    const state = new RunState(filePath, meta);
    const existing = readJson(filePath, { required: false });
    if (!existing || existing.jobName !== meta.jobName) return state;

    // A different Flow project means the recorded progress is for images that
    // live somewhere else, so it must not be treated as already done.
    if (meta.projectUrl && existing.projectUrl && existing.projectUrl !== meta.projectUrl) {
      state.projectChanged = { from: existing.projectUrl, to: meta.projectUrl };
      return state;
    }

    state.data.createdAt = existing.createdAt ?? state.data.createdAt;
    for (const [id, entry] of Object.entries(existing.items ?? {})) {
      if (id in state.data.items) {
        state.data.items[id] = { ...state.data.items[id], ...entry };
      }
    }
    return state;
  }

  /**
   * An item left `running` can only mean the previous run was killed, since
   * nothing else writes that status. Put it back to pending so the summary is
   * honest and it is clearly still to do.
   */
  clearStaleRunning() {
    const stale = [];
    for (const [id, entry] of Object.entries(this.data.items)) {
      if (entry.status === STATUS.running) {
        stale.push(id);
        Object.assign(entry, { status: STATUS.pending, error: null, updatedAt: nowIso() });
      }
    }
    return stale;
  }

  /**
   * An item marked `done` whose recorded output is gone is not done. The adoption
   * step downstream reads the disk, so leaving it done reports the image as
   * generated while nothing is there ("could not be generated"), and refreshing a
   * production's images folder silently loses the work. Reopen it instead.
   * Returns the ids that were put back to pending.
   */
  clearMissingFiles() {
    const reopened = [];
    for (const [id, entry] of Object.entries(this.data.items)) {
      if (entry.status !== STATUS.done) continue;
      const files = Array.isArray(entry.files) ? entry.files : [];
      if (files.length > 0 && files.every((file) => fs.existsSync(file))) continue;
      reopened.push(id);
      Object.assign(entry, {
        status: STATUS.pending,
        attempts: 0,
        files: [],
        error: null,
        updatedAt: nowIso(),
      });
    }
    return reopened;
  }

  get(id) {
    return this.data.items[id];
  }

  isDone(id) {
    return this.data.items[id]?.status === STATUS.done;
  }

  update(id, patch) {
    const entry = this.data.items[id];
    if (!entry) return;
    Object.assign(entry, patch, { updatedAt: nowIso() });
    this.data.updatedAt = nowIso();
  }

  save() {
    writeJson(this.filePath, this.data);
  }

  counts() {
    const counts = { pending: 0, running: 0, done: 0, failed: 0, skipped: 0 };
    for (const entry of Object.values(this.data.items)) {
      counts[entry.status] = (counts[entry.status] ?? 0) + 1;
    }
    return counts;
  }

  reset(ids) {
    for (const id of ids) {
      this.update(id, { status: STATUS.pending, attempts: 0, files: [], error: null });
    }
  }

  static pathFor(stateDir, jobName) {
    return path.join(stateDir, `${jobName}.json`);
  }

  static exists(stateDir, jobName) {
    return fs.existsSync(RunState.pathFor(stateDir, jobName));
  }
}
