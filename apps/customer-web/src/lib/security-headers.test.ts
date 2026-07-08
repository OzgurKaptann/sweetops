/**
 * Test 11 — the customer web app sends `Referrer-Policy: no-referrer`.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/security-headers.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  REFERRER_POLICY,
  securityHeaders,
  nextHeaders,
} from "./security-headers.ts";

test("referrer policy is no-referrer", () => {
  assert.equal(REFERRER_POLICY, "no-referrer");
  const headers = securityHeaders();
  const ref = headers.find((h) => h.key === "Referrer-Policy");
  assert.ok(ref, "Referrer-Policy header must be present");
  assert.equal(ref!.value, "no-referrer");
});

test("nextHeaders applies the policy to every route", async () => {
  const rules = await nextHeaders();
  assert.equal(rules[0].source, "/:path*");
  const ref = rules[0].headers.find((h) => h.key === "Referrer-Policy");
  assert.equal(ref?.value, "no-referrer");
});
