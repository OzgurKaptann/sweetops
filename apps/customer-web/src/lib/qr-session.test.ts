/**
 * Pure-TypeScript unit tests for the client-side QR token session.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/qr-session.test.ts
 *
 * All DOM/environment inputs (hash, storage, scrub) are injected, so no browser
 * is required. Excluded from the Next production build via tsconfig `exclude`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  parseQrTokenFromHash,
  acquireQrToken,
  clearQrToken,
  QR_TOKEN_STORAGE_KEY,
  type KeyValueStorage,
} from "./qr-session.ts";

function fakeStorage(): KeyValueStorage & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k)! : null),
    setItem: (k, v) => {
      map.set(k, v);
    },
    removeItem: (k) => {
      map.delete(k);
    },
  };
}

// ── Fragment parsing ─────────────────────────────────────────────────────────

test("parseQrTokenFromHash reads #qr=<token>", () => {
  assert.equal(parseQrTokenFromHash("#qr=abc123"), "abc123");
  assert.equal(parseQrTokenFromHash("qr=abc123"), "abc123");
});

test("parseQrTokenFromHash rejects missing / empty / wrong-key fragments", () => {
  assert.equal(parseQrTokenFromHash(""), null);
  assert.equal(parseQrTokenFromHash("#"), null);
  assert.equal(parseQrTokenFromHash("#qr="), null);
  assert.equal(parseQrTokenFromHash("#store=1&table=5"), null);
  assert.equal(parseQrTokenFromHash(undefined), null);
});

// ── Test 2 — initial page reads the token from the fragment ──────────────────

test("acquireQrToken captures the token from the URL fragment", () => {
  const storage = fakeStorage();
  let scrubbed = false;
  const token = acquireQrToken({
    hash: "#qr=tok-from-hash",
    storage,
    scrub: () => {
      scrubbed = true;
    },
  });
  assert.equal(token, "tok-from-hash");
  // Test 3 — the address bar is scrubbed.
  assert.equal(scrubbed, true);
  // Test 4 — the token is stored only in the guarded session storage.
  assert.equal(storage.map.get(QR_TOKEN_STORAGE_KEY), "tok-from-hash");
});

// ── Test 5 — same-tab refresh reuses the stored token (no fragment) ──────────

test("acquireQrToken falls back to the stored token when the fragment is gone", () => {
  const storage = fakeStorage();
  // First load with a fragment persists it.
  acquireQrToken({ hash: "#qr=persisted", storage, scrub: () => {} });
  // A refresh has no fragment (it was scrubbed) — the session token is reused.
  let scrubCalled = false;
  const token = acquireQrToken({
    hash: "",
    storage,
    scrub: () => {
      scrubCalled = true;
    },
  });
  assert.equal(token, "persisted");
  // Nothing to scrub when there was no fragment.
  assert.equal(scrubCalled, false);
});

// ── Test 6 — new tab without scan finds no token ─────────────────────────────

test("acquireQrToken returns null with no fragment and empty storage", () => {
  const storage = fakeStorage();
  const token = acquireQrToken({ hash: "", storage, scrub: () => {} });
  assert.equal(token, null);
});

test("a fresh fragment overrides a previously stored token", () => {
  const storage = fakeStorage();
  acquireQrToken({ hash: "#qr=old", storage, scrub: () => {} });
  const token = acquireQrToken({ hash: "#qr=new", storage, scrub: () => {} });
  assert.equal(token, "new");
  assert.equal(storage.map.get(QR_TOKEN_STORAGE_KEY), "new");
});

// ── Test 9 — clearing the token (definitive invalid) ─────────────────────────

test("clearQrToken removes the session token so a refresh finds nothing", () => {
  const storage = fakeStorage();
  acquireQrToken({ hash: "#qr=doomed", storage, scrub: () => {} });
  clearQrToken(storage);
  assert.equal(storage.map.has(QR_TOKEN_STORAGE_KEY), false);
  assert.equal(acquireQrToken({ hash: "", storage, scrub: () => {} }), null);
});

// ── Storage disabled → fragment still drives a single render ─────────────────

test("acquireQrToken works with storage disabled (null)", () => {
  const token = acquireQrToken({ hash: "#qr=mem", storage: null, scrub: () => {} });
  assert.equal(token, "mem");
});
