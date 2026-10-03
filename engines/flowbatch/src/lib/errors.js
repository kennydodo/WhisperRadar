/**
 * Raised when a semantic UI element cannot be located on the Flow page.
 * The message is deliberately actionable: it names the semantic key, lists the
 * candidates that were tried, and points at the calibration workflow.
 */
export class SelectorError extends Error {
  constructor(key, candidates = [], details = []) {
    const lines = [
      `Could not locate the Flow UI element "${key}".`,
      `Candidates tried (in order):`,
      ...candidates.map((selector) => `  - ${selector}`),
    ];
    if (details.length > 0) {
      lines.push('Last errors:', ...details.slice(-3).map((detail) => `  - ${detail}`));
    }
    lines.push(
      'The Flow UI may have changed. Run `npm run discover` while signed in to dump the',
      'current elements, then update the matching key in config/selectors.json.',
    );
    super(lines.join('\n'));
    this.name = 'SelectorError';
    this.key = key;
    this.candidates = candidates;
  }
}

export class GenerationError extends Error {
  constructor(message, { retryable = true } = {}) {
    super(message);
    this.name = 'GenerationError';
    this.retryable = retryable;
  }
}

export class TimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = 'TimeoutError';
  }
}

export class ConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ConfigError';
  }
}
