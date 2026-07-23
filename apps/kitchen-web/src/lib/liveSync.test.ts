/**
 * The kitchen board must never silently miss an order.
 *
 * Every test here drives the controller with a fake clock and a fake socket, so
 * the reliability rules are checked deterministically and instantly — no real
 * timers, no network, no DOM.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/liveSync.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type {
  KitchenLiveState,
  Scheduler,
  TimerHandle,
} from "./liveSync.ts";
import {
  KitchenLiveSync,
  LIVE_SAFETY_POLL_MS,
  POLL_INTERVAL_MS,
  RECONNECT_BACKOFF_MS,
  STALE_AFTER_MS,
  TERMINAL_STATUSES,
  dedupeOrders,
  deriveLinkState,
  emptySnapshot,
  indexTimingByOrderId,
  initialLiveState,
  isDegradedLink,
  shouldResyncForFrame,
} from "./liveSync.ts";

// ── Test doubles ────────────────────────────────────────────────────────────

interface FakeTimer {
  id: number;
  dueAt: number;
  every: number | null;
  fn: () => void;
}

/** A hand-rolled clock: time only moves when a test says so. */
class FakeClock implements Scheduler {
  time = 0;
  private seq = 0;
  private timers: FakeTimer[] = [];

  now(): number {
    return this.time;
  }

  setTimeout(fn: () => void, ms: number): TimerHandle {
    const id = ++this.seq;
    this.timers.push({ id, dueAt: this.time + ms, every: null, fn });
    return id;
  }

  setInterval(fn: () => void, ms: number): TimerHandle {
    const id = ++this.seq;
    this.timers.push({ id, dueAt: this.time + ms, every: ms, fn });
    return id;
  }

  clearTimeout(handle: TimerHandle): void {
    this.timers = this.timers.filter((t) => t.id !== handle);
  }

  clearInterval(handle: TimerHandle): void {
    this.clearTimeout(handle);
  }

  get pending(): number {
    return this.timers.length;
  }

  /** Advance time, firing every timer that comes due, in order. */
  advance(ms: number): void {
    const target = this.time + ms;
    for (;;) {
      const due = this.timers
        .filter((t) => t.dueAt <= target)
        .sort((a, b) => a.dueAt - b.dueAt)[0];
      if (!due) break;
      this.time = due.dueAt;
      if (due.every === null) {
        this.timers = this.timers.filter((t) => t.id !== due.id);
      } else {
        due.dueAt = this.time + due.every;
      }
      due.fn();
    }
    this.time = target;
  }
}

interface FakeSocket {
  url: string;
  closed: boolean;
  open(): void;
  send(frame: unknown): void;
  drop(): void;
  fail(): void;
}

/** Records every socket the controller creates, so leaks are visible. */
function socketRecorder() {
  const sockets: FakeSocket[] = [];
  const create = (url: string, handlers: {
    onOpen(): void;
    onMessage(raw: string): void;
    onClose(): void;
    onError(err: unknown): void;
  }) => {
    const fake: FakeSocket = {
      url,
      closed: false,
      open: () => handlers.onOpen(),
      send: (frame) => handlers.onMessage(JSON.stringify(frame)),
      // A remote drop leaves the socket dead too — modelling it as still alive
      // would hide exactly the leak these tests look for.
      drop: () => {
        fake.closed = true;
        handlers.onClose();
      },
      fail: () => handlers.onError(new Error("socket error")),
    };
    sockets.push(fake);
    return {
      close: () => {
        fake.closed = true;
      },
    };
  };
  return { sockets, create, get live() {
    return sockets.filter((s) => !s.closed);
  } };
}

const ORDER = (id: number, status = "NEW") => ({
  id,
  store_id: 1,
  table_id: 3,
  status,
  created_at: "2026-07-23T09:00:00Z",
  items: [],
});

const TIMING = (orderIds: number[], delayed = 0) => ({
  orders: orderIds.map((id) => ({
    order_id: id,
    store_id: 1,
    table_id: 3,
    status: "NEW",
    created_at: "2026-07-23T09:00:00Z",
    prep_started_at: null,
    ready_at: null,
    delivered_at: null,
    cancelled_at: null,
    queued_seconds: null,
    prep_seconds: null,
    time_to_ready_seconds: null,
    queued_seconds_active: 30,
    prep_seconds_active: null,
    active_seconds: 30,
    is_delayed: false,
    delay_state: "ok" as const,
    delay_reason: null,
  })),
  summary: {
    active_orders: orderIds.length,
    waiting_orders: orderIds.length,
    in_prep_orders: 0,
    ready_orders: 0,
    delayed_orders: delayed,
  },
});

interface Harness {
  clock: FakeClock;
  controller: KitchenLiveSync;
  sockets: FakeSocket[];
  liveSockets: () => FakeSocket[];
  states: KitchenLiveState[];
  last: () => KitchenLiveState;
  counts: { orders: number; timing: number };
  /** Flush pending promise jobs so awaited fetches settle. */
  settle: () => Promise<void>;
}

function harness(opts: {
  // Deliberately argument-free: a test declares what the server *currently*
  // returns and flips it, rather than depending on how many times the
  // controller happened to fetch.
  orders?: () => unknown[];
  timing?: () => unknown;
  failOrders?: boolean;
  isUnauthorized?: (err: unknown) => boolean;
  onUnauthorized?: () => void;
} = {}): Harness {
  const clock = new FakeClock();
  const rec = socketRecorder();
  const states: KitchenLiveState[] = [];
  const counts = { orders: 0, timing: 0 };

  const controller = new KitchenLiveSync({
    wsUrl: "ws://test/ws/kitchen",
    fetchOrders: async () => {
      counts.orders += 1;
      if (opts.failOrders) throw new Error("network down");
      return (opts.orders?.() ?? [ORDER(1)]) as never;
    },
    fetchTiming: async () => {
      counts.timing += 1;
      return (opts.timing?.() ?? TIMING([1])) as never;
    },
    createSocket: rec.create,
    onState: (s) => states.push(s),
    isUnauthorized: opts.isUnauthorized,
    onUnauthorized: opts.onUnauthorized,
    onError: () => {},
    scheduler: clock,
  });

  return {
    clock,
    controller,
    sockets: rec.sockets,
    liveSockets: () => rec.live,
    states,
    last: () => states[states.length - 1],
    counts,
    settle: async () => {
      // Four turns clears Promise.all + the coalescing trailer.
      for (let i = 0; i < 6; i++) await Promise.resolve();
    },
  };
}

// ── Pure helpers ────────────────────────────────────────────────────────────

test("a repeated order id collapses to one card", () => {
  const list = dedupeOrders([ORDER(1), ORDER(2), ORDER(1, "IN_PREP")] as never);
  assert.deepEqual(list.map((o) => o.id), [1, 2], "a duplicate produced a card");
  // The later row wins, and keeps the position of the first — the board must not
  // reshuffle under a cook's hand.
  assert.equal(list[0].status, "IN_PREP");
});

test("a non-array orders payload degrades to an empty board, not a crash", () => {
  assert.deepEqual(dedupeOrders(null as never), []);
  assert.deepEqual(dedupeOrders(undefined as never), []);
});

test("timing rows index by order id, and a missing payload is empty", () => {
  const byId = indexTimingByOrderId(TIMING([7, 9]) as never);
  assert.equal(byId[7].order_id, 7);
  assert.equal(byId[9].order_id, 9);
  assert.deepEqual(indexTimingByOrderId(null), {});
});

test("only kitchen events that change the board trigger a resync", () => {
  assert.equal(shouldResyncForFrame(JSON.stringify({ event: "initial_state" })), true);
  assert.equal(shouldResyncForFrame(JSON.stringify({ event: "order_created" })), true);
  assert.equal(
    shouldResyncForFrame(JSON.stringify({ event: "order_status_updated" })),
    true,
  );
  assert.equal(shouldResyncForFrame(JSON.stringify({ event: "pong" })), false);
});

test("a malformed frame is ignored rather than thrown", () => {
  assert.equal(shouldResyncForFrame("not json {{"), false);
  assert.equal(shouldResyncForFrame(""), false);
  assert.equal(shouldResyncForFrame("null"), false);
});

// ── Honest connection state ─────────────────────────────────────────────────

test("the badge is never 'live' unless the socket is open AND data is fresh", () => {
  const base = emptySnapshot();
  const fresh = { ...base, socket: "open" as const, lastSyncedAt: 1_000 };

  assert.equal(deriveLinkState(fresh, 1_000), "live");
  // Socket open, data older than the staleness threshold → not live.
  assert.equal(deriveLinkState(fresh, 1_000 + STALE_AFTER_MS), "stale");
  // Socket open but the last refresh failed → not live.
  assert.equal(
    deriveLinkState({ ...fresh, lastSyncFailed: true }, 1_000),
    "stale",
  );
  // Socket closed can never be live, however fresh the data is.
  for (const socket of ["closed", "connecting"] as const) {
    assert.notEqual(deriveLinkState({ ...fresh, socket }, 1_000), "live");
  }
});

test("a dropped socket reads as reconnecting, then as fallback polling", () => {
  const dropped = {
    ...emptySnapshot(),
    socket: "closed" as const,
    hasConnected: true,
    lastSyncedAt: 1_000,
  };
  assert.equal(deriveLinkState(dropped, 1_000), "reconnecting");
  assert.equal(
    deriveLinkState({ ...dropped, fallbackSyncs: 1 }, 1_000),
    "polling",
  );
  // Once the data ages out, neither label is honest any more.
  assert.equal(
    deriveLinkState({ ...dropped, fallbackSyncs: 3 }, 1_000 + STALE_AFTER_MS),
    "offline",
  );
});

test("no data yet reads as connecting, or offline once a fetch has failed", () => {
  assert.equal(deriveLinkState(emptySnapshot(), 0), "connecting");
  assert.equal(
    deriveLinkState({ ...emptySnapshot(), lastSyncFailed: true }, 0),
    "offline",
  );
  assert.equal(initialLiveState().link, "connecting");
});

test("every state except live and connecting is flagged as degraded", () => {
  assert.equal(isDegradedLink("live"), false);
  assert.equal(isDegradedLink("connecting"), false);
  for (const s of ["reconnecting", "polling", "stale", "offline"] as const) {
    assert.equal(isDegradedLink(s), true, `${s} should be degraded`);
  }
});

// ── initial_state ───────────────────────────────────────────────────────────

test("initial_state refreshes both orders and timing", async () => {
  const h = harness();
  h.controller.start();
  await h.settle();

  const before = { ...h.counts };
  h.sockets[0].send({ event: "initial_state", data: { store_id: 1, orders: [] } });
  await h.settle();

  assert.equal(h.counts.orders, before.orders + 1, "orders were not refetched");
  assert.equal(h.counts.timing, before.timing + 1, "timing was not refetched");
});

test("initial_state updates the last-synced stamp and clears a failed state", async () => {
  let fail = true;
  const clock = new FakeClock();
  const rec = socketRecorder();
  const states: KitchenLiveState[] = [];
  const controller = new KitchenLiveSync({
    wsUrl: "ws://test/ws/kitchen",
    fetchOrders: async () => {
      if (fail) throw new Error("down");
      return [ORDER(1)] as never;
    },
    fetchTiming: async () => TIMING([1]) as never,
    createSocket: rec.create,
    onState: (s) => states.push(s),
    onError: () => {},
    scheduler: clock,
  });

  controller.start();
  for (let i = 0; i < 6; i++) await Promise.resolve();
  assert.equal(controller.state().lastSyncFailed, true);
  assert.equal(controller.state().lastSyncedAt, null);

  fail = false;
  clock.time = 5_000;
  rec.sockets[0].open();
  for (let i = 0; i < 6; i++) await Promise.resolve();

  assert.equal(controller.state().lastSyncFailed, false, "error state not cleared");
  assert.equal(controller.state().lastSyncedAt, 5_000);
  assert.equal(controller.state().link, "live");
  controller.stop();
});

// ── Reconnect ───────────────────────────────────────────────────────────────

test("a reconnect refetches orders and timing without a human touching it", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  const before = { ...h.counts };
  h.clock.time = 10_000;
  h.sockets[0].drop();
  assert.equal(h.controller.state().link, "reconnecting");

  // Backoff elapses, a new socket is created and opens.
  h.clock.advance(RECONNECT_BACKOFF_MS[0]);
  assert.equal(h.sockets.length, 2, "no reconnect socket was created");
  h.sockets[1].open();
  await h.settle();

  assert.ok(h.counts.orders > before.orders, "reconnect did not refetch orders");
  assert.ok(h.counts.timing > before.timing, "reconnect did not refetch timing");
  assert.equal(h.controller.state().link, "live");
  assert.equal(h.controller.state().lastSyncedAt, h.clock.now());
  h.controller.stop();
});

test("reconnecting never leaves a second socket alive", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  for (let i = 0; i < 4; i++) {
    h.sockets[h.sockets.length - 1].drop();
    h.clock.advance(RECONNECT_BACKOFF_MS[RECONNECT_BACKOFF_MS.length - 1]);
    await h.settle();
  }

  assert.ok(h.sockets.length >= 5, "reconnect did not keep retrying");
  assert.equal(h.liveSockets().length, 1, "more than one socket is alive");
  h.controller.stop();
  assert.equal(h.liveSockets().length, 0, "stop() left a socket open");
});

test("a callback from a replaced socket cannot move the state", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();
  h.sockets[0].drop();
  h.clock.advance(RECONNECT_BACKOFF_MS[0]);
  h.sockets[1].open();
  await h.settle();

  const before = { ...h.counts };
  // The old socket wakes up late and shouts.
  h.sockets[0].send({ event: "order_created" });
  h.sockets[0].drop();
  await h.settle();

  assert.deepEqual(h.counts, before, "a stale socket triggered a refetch");
  assert.equal(h.controller.state().socket, "open", "a stale close changed state");
  h.controller.stop();
});

// ── Fallback polling ────────────────────────────────────────────────────────

test("a disconnected board polls orders and timing on the fallback interval", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();
  h.sockets[0].drop();

  const before = { ...h.counts };
  h.clock.advance(POLL_INTERVAL_MS);
  await h.settle();
  assert.equal(h.counts.orders, before.orders + 1);
  assert.equal(h.counts.timing, before.timing + 1);
  assert.equal(h.controller.state().link, "polling", "fallback is not reported");

  h.clock.advance(POLL_INTERVAL_MS);
  await h.settle();
  assert.equal(h.counts.orders, before.orders + 2, "fallback polling stopped");
  h.controller.stop();
});

test("a healthy live socket is not polled on every tick", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  const before = { ...h.counts };
  h.clock.advance(POLL_INTERVAL_MS * 2); // still inside the safety window
  await h.settle();
  assert.deepEqual(h.counts, before, "a healthy socket was polled anyway");
  assert.equal(h.controller.state().link, "live");

  // …but a socket that goes quiet still gets a low-frequency safety check.
  h.clock.advance(LIVE_SAFETY_POLL_MS);
  await h.settle();
  assert.ok(h.counts.orders > before.orders, "the safety poll never fired");
  h.controller.stop();
});

test("a live board never ages into 'stale' between safety polls", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  for (let i = 0; i < 20; i++) {
    h.clock.advance(POLL_INTERVAL_MS);
    await h.settle();
    assert.equal(
      h.controller.state().link,
      "live",
      `idle live board flapped at tick ${i}`,
    );
  }
  h.controller.stop();
});

test("stop() clears the poll interval and the reconnect timer", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();
  h.sockets[0].drop(); // arms a reconnect timer as well as the interval
  assert.ok(h.clock.pending > 0);

  h.controller.stop();
  assert.equal(h.clock.pending, 0, "a timer survived unmount");

  const before = { ...h.counts };
  h.clock.advance(POLL_INTERVAL_MS * 10);
  await h.settle();
  assert.deepEqual(h.counts, before, "polling continued after unmount");
});

test("start() is idempotent — remounting logic cannot double the timers", async () => {
  const h = harness();
  h.controller.start();
  h.controller.start();
  await h.settle();
  assert.equal(h.sockets.length, 1, "a second socket was created");
  assert.equal(h.clock.pending, 1, "a second poll interval was created");
  h.controller.stop();
});

test("no state is emitted after stop, even by a fetch already in flight", async () => {
  let release: (() => void) | null = null;
  const clock = new FakeClock();
  const rec = socketRecorder();
  const states: KitchenLiveState[] = [];
  const controller = new KitchenLiveSync({
    wsUrl: "ws://test/ws/kitchen",
    fetchOrders: () =>
      new Promise((resolve) => {
        release = () => resolve([ORDER(1)] as never);
      }),
    fetchTiming: async () => TIMING([1]) as never,
    createSocket: rec.create,
    onState: (s) => states.push(s),
    onError: () => {},
    scheduler: clock,
  });

  controller.start();
  await Promise.resolve();
  controller.stop();
  const emitted = states.length;

  release?.();
  for (let i = 0; i < 6; i++) await Promise.resolve();

  assert.equal(states.length, emitted, "state was updated after unmount");
});

// ── Live events ─────────────────────────────────────────────────────────────

test("order_created refreshes both the board and the timing summary", async () => {
  let serverOrders = [ORDER(1)];
  let serverTiming = TIMING([1]);
  const h = harness({ orders: () => serverOrders, timing: () => serverTiming });
  h.controller.start();
  h.sockets[0].open();
  await h.settle();
  assert.deepEqual(h.controller.state().orders.map((o) => o.id), [1]);

  // A guest orders while the board is up.
  serverOrders = [ORDER(1), ORDER(2)];
  serverTiming = TIMING([1, 2], 1);
  h.sockets[0].send({ event: "order_created", data: { order_id: 2 } });
  await h.settle();

  const state = h.controller.state();
  assert.deepEqual(state.orders.map((o) => o.id), [1, 2]);
  assert.equal(state.timing?.summary.active_orders, 2, "tempo strip froze");
  assert.equal(state.timing?.summary.delayed_orders, 1);
  assert.equal(state.timingById[2].order_id, 2, "per-card timing froze");
  h.controller.stop();
});

test("order_status_updated refreshes timing and drops a terminal card", async () => {
  // The server's kitchen list only returns NEW/IN_PREP, so a READY order simply
  // stops being listed — the refetch is what removes the card.
  let serverOrders = [ORDER(1), ORDER(2)];
  let serverTiming = TIMING([1, 2]);
  const h = harness({ orders: () => serverOrders, timing: () => serverTiming });
  h.controller.start();
  h.sockets[0].open();
  await h.settle();
  assert.equal(h.controller.state().orders.length, 2);

  // Order 1 is marked READY, so the server stops listing it.
  serverOrders = [ORDER(2)];
  serverTiming = TIMING([2]);
  h.sockets[0].send({
    event: "order_status_updated",
    data: { order_id: 1, from_status: "IN_PREP", to_status: "READY" },
  });
  await h.settle();

  const state = h.controller.state();
  assert.deepEqual(state.orders.map((o) => o.id), [2], "terminal card survived");
  assert.equal(state.timing?.summary.active_orders, 1, "timing did not refresh");
  assert.equal(state.timingById[1], undefined, "stale timing row survived");
  h.controller.stop();
});

test("terminal statuses are the ones the board must not keep showing", () => {
  assert.deepEqual(TERMINAL_STATUSES, ["READY", "DELIVERED", "CANCELLED"]);
});

test("a burst of events coalesces instead of stampeding the API", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  const before = { ...h.counts };
  for (let i = 0; i < 10; i++) {
    h.sockets[0].send({ event: "order_created", data: { order_id: i } });
  }
  await h.settle();

  const fetches = h.counts.orders - before.orders;
  assert.ok(fetches >= 1, "the burst was ignored entirely");
  assert.ok(fetches <= 2, `burst caused ${fetches} refetches, expected <= 2`);
  h.controller.stop();
});

test("a malformed frame neither crashes nor refetches", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  const before = { ...h.counts };
  h.sockets[0].send("this is not an event object");
  h.sockets[0].fail();
  await h.settle();

  assert.deepEqual(h.counts, before);
  assert.equal(h.controller.state().link, "live", "a bad frame broke the board");
  h.controller.stop();
});

// ── Manual refresh ──────────────────────────────────────────────────────────

test("manual refresh calls both APIs and re-stamps the board", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();

  const before = { ...h.counts };
  h.clock.time = 20_000;
  await h.controller.refresh();
  await h.settle();

  assert.equal(h.counts.orders, before.orders + 1);
  assert.equal(h.counts.timing, before.timing + 1);
  assert.equal(h.controller.state().lastSyncedAt, 20_000);
  h.controller.stop();
});

test("a failing manual refresh reports the failure instead of throwing", async () => {
  const h = harness({ failOrders: true });
  h.controller.start();
  await h.settle();

  await assert.doesNotReject(() => h.controller.refresh());
  await h.settle();

  const state = h.controller.state();
  assert.equal(state.lastSyncFailed, true);
  assert.equal(state.link, "offline", "a failed board did not say so");
  assert.equal(state.loaded, true, "the skeleton would spin forever");
  h.controller.stop();
});

test("an expired session is reported once and does not fake a fresh sync", async () => {
  let reported = 0;
  // The harness's fetch throws; isUnauthorized classifies it as a 401.
  const h = harness({
    failOrders: true,
    isUnauthorized: () => true,
    onUnauthorized: () => {
      reported += 1;
    },
  });

  h.controller.start();
  await h.settle();

  assert.equal(reported, 1, "session expiry was not reported exactly once");
  assert.equal(h.controller.state().lastSyncedAt, null, "faked a fresh sync");
  h.controller.stop();
});

// ── Wake from sleep ─────────────────────────────────────────────────────────

test("resume() resyncs immediately instead of waiting out the backoff", async () => {
  const h = harness();
  h.controller.start();
  h.sockets[0].open();
  await h.settle();
  h.sockets[0].drop();

  const before = { ...h.counts };
  const socketsBefore = h.sockets.length;

  // The tablet wakes up mid-backoff.
  h.controller.resume();
  await h.settle();

  assert.ok(h.counts.orders > before.orders, "wake did not refetch");
  assert.equal(h.sockets.length, socketsBefore + 1, "wake did not reconnect");
  assert.equal(h.liveSockets().length, 1, "wake leaked a socket");
  h.controller.stop();
});

test("resume() on a stopped controller does nothing", async () => {
  const h = harness();
  h.controller.start();
  await h.settle();
  h.controller.stop();

  const before = { ...h.counts };
  h.controller.resume();
  await h.settle();
  assert.deepEqual(h.counts, before);
});
