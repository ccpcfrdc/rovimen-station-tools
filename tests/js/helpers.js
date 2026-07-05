import { readFileSync } from 'fs';
import { runInNewContext } from 'vm';
import { resolve } from 'path';

const ROOT = resolve(import.meta.dirname, '..', '..');

export function loadBrowserScript(relPath, extraGlobals = {}) {
  const absPath = resolve(ROOT, relPath);
  const code = readFileSync(absPath, 'utf8');

  const noop = () => {};
  const sandbox = {
    document: {
      getElementById: () => ({ innerHTML: '', style: {}, classList: { add: noop, remove: noop }, setAttribute: noop, addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], appendChild: noop, value: '', textContent: '' }),
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => ({ innerHTML: '', style: {}, classList: { add: noop, remove: noop }, setAttribute: noop, addEventListener: noop, appendChild: noop }),
      addEventListener: noop,
      activeElement: null,
    },
    location: { href: 'http://localhost/', origin: 'http://localhost', pathname: '/', search: '', hash: '' },
    self: {},
    globalThis: {},
    setInterval: noop,
    setTimeout: noop,
    clearInterval: noop,
    clearTimeout: noop,
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    requestAnimationFrame: noop,
    cancelAnimationFrame: noop,
    caches: { open: async () => ({ match: async () => null, put: async () => {}, addAll: async () => {} }) },
    console,
    URL,
    Date,
    Math,
    Number,
    String,
    Array,
    Object,
    RegExp,
    JSON,
    Map,
    Set,
    Error,
    TypeError,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
    encodeURIComponent,
    decodeURIComponent,
    URLSearchParams,
    history: { replaceState: noop, pushState: noop },
    Infinity,
    NaN,
    undefined,
    ...extraGlobals,
  };

  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self.addEventListener = noop;
  sandbox.self.skipWaiting = () => Promise.resolve();

  try {
    runInNewContext(code, sandbox, { filename: absPath });
  } catch (e) {
    // Browser init code (event listeners, DOM manipulation) may throw
    // ReferenceError/TypeError when globals are missing. The pure
    // functions we're testing are already defined by this point.
  }
  return sandbox;
}
