import fs from 'node:fs';
import path from 'node:path';

/**
 * Timeline of ONE video clip's wait, for finding out why a clip "timed out".
 *
 * Every poll, network sighting, status word, download attempt and tile poke is
 * appended (JSON lines) to the trace file as it happens, so a hung or killed
 * run still leaves it behind. `diagnose()` turns the events into one verdict
 * that says which of the possible causes it was.
 */
export class VideoTrace {
  constructor({ file = null, now = Date.now } = {}) {
    this.file = file;
    this.now = now;
    this.startedAt = now();
    this.events = [];
    this.statuses = new Map();
    if (file) {
      try {
        fs.mkdirSync(path.dirname(file), { recursive: true });
      } catch {
        /* best effort */
      }
    }
  }

  note(kind, data = {}) {
    const event = { t: Math.round((this.now() - this.startedAt) / 100) / 10, kind, ...data };
    this.events.push(event);
    if (this.file) {
      try {
        fs.appendFileSync(this.file, `${JSON.stringify(event)}\n`);
      } catch {
        /* best effort */
      }
    }
    return event;
  }

  /** Remember a (field, value) status pair from Flow's JSON; note only changes. */
  status(field, value) {
    const key = String(field);
    if (this.statuses.get(key) === value) return;
    this.statuses.set(key, value);
    this.note('status', { field: key, value });
  }

  diagnose() {
    return diagnose(this.events);
  }

  /** Write the verdict next to the trace; returns the text. */
  writeSummary(outcome) {
    const d = this.diagnose();
    const text = [`outcome: ${outcome}`, `verdict: ${d.verdict}`, ...d.facts.map((f) => `- ${f}`)].join('\n');
    this.note('verdict', { outcome, verdict: d.verdict });
    if (this.file) {
      try {
        fs.writeFileSync(this.file.replace(/\.jsonl$/, '') + '-summary.txt', `${text}\n`);
      } catch {
        /* best effort */
      }
    }
    return text;
  }
}

/** Pure: events -> { verdict, facts[] }. */
export function diagnose(events) {
  const polls = events.filter((e) => e.kind === 'poll');
  const sightings = events.filter((e) => e.kind === 'net');
  const fetches = events.filter((e) => e.kind === 'fetch');
  const alerts = events.filter((e) => e.kind === 'alert');
  const statuses = events.filter((e) => e.kind === 'status');
  const facts = [];
  const lastT = events.length ? events[events.length - 1].t : 0;
  facts.push(`${polls.length} poll(s) over ${lastT}s`);

  if (statuses.length) {
    facts.push(
      'status words seen: ' + statuses.map((s) => `${s.field}=${s.value}@${s.t}s`).slice(-8).join(', '),
    );
  }
  const tileTexts = [...new Set(polls.map((p) => p.tile).filter(Boolean))];
  if (tileTexts.length) facts.push('newest tile text: ' + tileTexts.slice(-4).map((t) => JSON.stringify(t)).join(' -> '));

  if (fetches.some((f) => f.ok)) {
    return { verdict: 'SAVED: a clip was downloaded', facts };
  }
  if (alerts.length) {
    const a = alerts[alerts.length - 1];
    facts.push(`alert at ${a.t}s: ${String(a.text).slice(0, 200)}`);
    return { verdict: 'FLOW REFUSED / FAILED the generation (an on-page alert appeared)', facts };
  }
  if (fetches.length) {
    const codes = [...new Set(fetches.map((f) => f.status ?? f.error ?? 'no response'))];
    facts.push(`download attempts: ${fetches.length}, results: ${codes.join(', ')}`);
    const first = fetches[0];
    facts.push(`first URL seen at ${first.t}s (id ${first.id})`);
    const expired = fetches.some((f) => f.status === 403 || f.status === 401 || f.status === 410);
    return {
      verdict: expired
        ? 'CLIP EXISTED BUT THE DOWNLOAD WAS REFUSED (403/401/410) - the signed URL expired or the session lost access'
        : 'CLIP URL WAS SEEN BUT NEVER DOWNLOADED - the file was not ready or the request failed',
      facts,
    };
  }
  const maxCandidates = Math.max(0, ...polls.map((p) => p.candidates ?? 0));
  const maxFresh = Math.max(0, ...polls.map((p) => p.fresh ?? 0));
  const excluded = Math.max(0, ...polls.map((p) => (p.knownOrSaved ?? 0)));
  facts.push(`network sightings of a video: ${sightings.length}; max candidates on page: ${maxCandidates}; max fresh: ${maxFresh}`);
  if (excluded > 0 && maxFresh === 0 && maxCandidates > 0) {
    facts.push(`${excluded} candidate(s) were ignored as already known/saved`);
    return {
      verdict: 'ONLY KNOWN/SAVED CLIPS WERE VISIBLE - the new clip was filtered out as a duplicate (or never appeared)',
      facts,
    };
  }
  if (sightings.length === 0 && maxCandidates === 0) {
    const grew = polls.some((p) => (p.videos ?? 0) > 0);
    return {
      verdict: grew
        ? 'A <video> element existed but its source never became a usable URL'
        : 'FLOW NEVER EXPOSED A VIDEO - no response, no <video> source, no URL in any status JSON (still generating, or the page never showed the result)',
      facts,
    };
  }
  return { verdict: 'INCONCLUSIVE - see the trace file', facts };
}
