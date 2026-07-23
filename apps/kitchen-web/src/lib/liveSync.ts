/**
 * Kitchen live-sync controller.
 *
 * The kitchen board is the one screen where a silent failure costs a guest their
 * order: the socket drops, three tickets are placed, the socket comes back, and
 * the board still shows the pre-drop list under a green "Canlı" badge.
 *
 * This module owns every rule that prevents that. It is deliberately free of
 * React and of the DOM so those rules can be tested with a fake clock and a fake
 * socket instead of a browser:
 *
 *   - every live event (initial_state, order_created, order_status_updated)
 *     funnels into ONE coalesced refetch of orders + timing, so the board is a
 *     whole server-rendered truth rather than a patched-up guess;
 *   - a socket open — first connect or reconnect — always resyncs;
 *   - a single interval polls as a fallback whenever the socket is not live, and
 *     at a low cadence even when it is, so a lying socket still self-heals;
 *   - the connection state is derived from when data last *arrived*, not from
 *     what the socket claims, so "Canlı" cannot be shown over a stale board;
 *   - nothing is emitted after stop(), and there is never a second socket or a
 *     second interval.
 */
import type {
  ActiveTimingResponse,
  KitchenOrder,
  OrderTiming,
} from "@sweetops/types";

// ── Timing constants ────────────────────────────────────────────────────────
// The fallback cadence. Fast enough that a cook never waits on a ticket, slow
// enough that a shop full of tablets does not hammer the API.
export const POLL_INTERVAL_MS = 12_000;

// Even a socket that reports "open" gets re-checked this often. This is what
// makes the board self-heal when the socket is alive but silent.
export const LIVE_SAFETY_POLL_MS = 30_000;

// Data older than this is not allowed to be presented as live, whatever the
// socket says. Must stay comfortably above LIVE_SAFETY_POLL_MS + POLL_INTERVAL_MS
// (30s + 12s = 42s worst case) so a healthy idle board never flaps to "stale".
export const STALE_AFTER_MS = 60_000;

// Reconnect backoff, capped — a kitchen tablet on flaky café Wi-Fi should retry
// promptly, but must not spin.
export const RECONNECT_BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 15_000];

// Terminal statuses never belong on the board. The server's kitchen list already
// returns only NEW/IN_PREP, so a full refetch drops these on its own; the set is
// kept here so the rule is stated once and can be asserted.
export const TERMINAL_STATUSES = ["READY", "DELIVERED", "CANCELLED"];

// ── Connection state ────────────────────────────────────────────────────────

/**
 * What the badge is allowed to claim.
 *
 *   connecting   — no data on screen yet
 *   live         — socket open AND data arrived recently
 *   reconnecting — socket dropped, retrying, fallback has not delivered yet
 *   polling      — socket down, but the HTTP fallback is keeping the board fresh
 *   stale        — socket claims open, data is old or the last refresh failed
 *   offline      — no fresh data at all; the board may be missing tickets
 */
export type KitchenLinkState =
  | "connecting"
  | "live"
  | "reconnecting"
  | "polling"
  | "stale"
  | "offline";

/** The raw facts. Everything the UI shows is derived from these. */
export interface KitchenSyncSnapshot {
  socket: "connecting" | "open" | "closed";
  /** A socket has been open at least once in this controller's life. */
  hasConnected: boolean;
  /** Reconnect attempts since the last successful open. */
  reconnectAttempts: number;
  /** Successful fallback refreshes since the socket last went down. */
  fallbackSyncs: number;
  /** Epoch ms of the last refresh where BOTH orders and timing succeeded. */
  lastSyncedAt: number | null;
  /** The most recent refresh attempt failed. */
  lastSyncFailed: boolean;
  /** A refresh is in flight. */
  syncing: boolean;
}

/** The snapshot plus the data and the derived badge state, as the UI sees it. */
export interface KitchenLiveState extends KitchenSyncSnapshot {
  link: KitchenLinkState;
  orders: KitchenOrder[];
  timing: ActiveTimingResponse | null;
  timingById: Record<number, OrderTiming>;
  /** True once the first refresh has settled, successfully or not. */
  loaded: boolean;
}

/**
 * The fallback is "active" once we know the socket is not simply still opening
 * for the first time — either it has been up, or an attempt has already failed.
 * Before that, HTTP fetches are the normal boot path, not a fallback.
 */
function fallbackActive(snap: KitchenSyncSnapshot): boolean {
  return snap.hasConnected || snap.reconnectAttempts > 0;
}

/**
 * Derive the badge state from the facts.
 *
 * The ordering matters: freshness is checked before the socket is believed, so
 * there is no path to "live" that does not require recently arrived data.
 */
export function deriveLinkState(
  snap: KitchenSyncSnapshot,
  now: number,
): KitchenLinkState {
  if (snap.lastSyncedAt === null) {
    return snap.lastSyncFailed ? "offline" : "connecting";
  }
  const fresh = now - snap.lastSyncedAt < STALE_AFTER_MS;

  if (snap.socket === "open") {
    return fresh && !snap.lastSyncFailed ? "live" : "stale";
  }
  if (!fresh || snap.lastSyncFailed) return "offline";
  if (!fallbackActive(snap)) return "connecting";
  return snap.fallbackSyncs > 0 ? "polling" : "reconnecting";
}

/** States in which the board may be missing or mis-stating tickets. */
export function isDegradedLink(link: KitchenLinkState): boolean {
  return link !== "live" && link !== "connecting";
}

// ── Injected collaborators ──────────────────────────────────────────────────

export type TimerHandle = unknown;

/** Clock + timers, injected so tests never wait on real time. */
export interface Scheduler {
  setTimeout(fn: () => void, ms: number): TimerHandle;
  clearTimeout(handle: TimerHandle): void;
  setInterval(fn: () => void, ms: number): TimerHandle;
  clearInterval(handle: TimerHandle): void;
  now(): number;
}

export const systemScheduler: Scheduler = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
  now: () => Date.now(),
};

/**
 * Handlers are bound when the socket is created rather than assigned afterwards,
 * so a socket can never be live for a moment with no listener attached.
 */
export interface LiveSocketHandlers {
  onOpen(): void;
  onMessage(raw: string): void;
  onClose(): void;
  onError(err: unknown): void;
}

/** The only thing the controller needs from a socket. */
export interface LiveSocket {
  close(): void;
}

export type SocketFactory = (
  url: string,
  handlers: LiveSocketHandlers,
) => LiveSocket;

/** Browser adapter — the one place the real WebSocket API is touched. */
export const browserSocketFactory: SocketFactory = (url, handlers) => {
  const ws = new WebSocket(url);
  ws.onopen = () => handlers.onOpen();
  ws.onmessage = (event) =>
    handlers.onMessage(
      typeof event.data === "string" ? event.data : String(event.data),
    );
  ws.onclose = () => handlers.onClose();
  ws.onerror = (event) => handlers.onError(event);
  return {
    close: () => {
      // Drop the handlers first: a close() we asked for must not be reported
      // back as an unexpected drop.
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.close();
    },
  };
};

export interface KitchenLiveSyncOptions {
  wsUrl: string;
  fetchOrders: () => Promise<KitchenOrder[]>;
  fetchTiming: () => Promise<ActiveTimingResponse>;
  createSocket: SocketFactory;
  onState: (state: KitchenLiveState) => void;
  /** Session expired — the auth gate takes the screen from here. */
  onUnauthorized?: () => void;
  isUnauthorized?: (err: unknown) => boolean;
  onError?: (err: unknown) => void;
  scheduler?: Scheduler;
}

// ── Pure helpers ────────────────────────────────────────────────────────────

/**
 * Collapse repeated order ids, keeping the last one seen.
 *
 * A whole-list refetch already makes duplicates impossible from the client side;
 * this guarantees it stays true even if the API ever repeats a row.
 */
export function dedupeOrders(orders: KitchenOrder[]): KitchenOrder[] {
  if (!Array.isArray(orders)) return [];
  const byId = new Map<number, KitchenOrder>();
  for (const order of orders) {
    if (order && typeof order.id === "number") byId.set(order.id, order);
  }
  return Array.from(byId.values());
}

/** Timing rows keyed by order id, for per-card lookup. */
export function indexTimingByOrderId(
  timing: ActiveTimingResponse | null,
): Record<number, OrderTiming> {
  const byId: Record<number, OrderTiming> = {};
  if (!timing || !Array.isArray(timing.orders)) return byId;
  for (const row of timing.orders) byId[row.order_id] = row;
  return byId;
}

/** Live kitchen events that mean "the board may no longer be correct". */
export const RESYNC_EVENTS = [
  "initial_state",
  "order_created",
  "order_status_updated",
];

/**
 * Decide whether a raw socket frame should trigger a resync.
 *
 * Malformed frames are ignored rather than thrown: a bad byte on the wire must
 * never take the kitchen board down.
 */
export function shouldResyncForFrame(raw: string): boolean {
  try {
    const payload = JSON.parse(raw);
    return (
      typeof payload === "object" &&
      payload !== null &&
      RESYNC_EVENTS.includes(payload.event)
    );
  } catch {
    return false;
  }
}

export function emptySnapshot(): KitchenSyncSnapshot {
  return {
    socket: "closed",
    hasConnected: false,
    reconnectAttempts: 0,
    fallbackSyncs: 0,
    lastSyncedAt: null,
    lastSyncFailed: false,
    syncing: false,
  };
}

/** The state a mounting component starts from, before anything has happened. */
export function initialLiveState(): KitchenLiveState {
  const snap = emptySnapshot();
  return {
    ...snap,
    link: "connecting",
    orders: [],
    timing: null,
    timingById: {},
    loaded: false,
  };
}

// ── Controller ──────────────────────────────────────────────────────────────

export class KitchenLiveSync {
  private readonly opts: KitchenLiveSyncOptions;
  private readonly clock: Scheduler;

  private snap: KitchenSyncSnapshot = emptySnapshot();
  private orders: KitchenOrder[] = [];
  private timing: ActiveTimingResponse | null = null;
  private timingById: Record<number, OrderTiming> = {};
  private loaded = false;

  private started = false;
  private stopped = false;

  private socket: LiveSocket | null = null;
  /** Bumped per socket; late callbacks from a replaced socket are ignored. */
  private socketEpoch = 0;
  private reconnectTimer: TimerHandle = null;
  private pollTimer: TimerHandle = null;

  private inFlight: Promise<void> | null = null;
  private resyncQueued = false;

  constructor(opts: KitchenLiveSyncOptions) {
    this.opts = opts;
    this.clock = opts.scheduler ?? systemScheduler;
  }

  /** Load once over HTTP, open the socket, and arm the fallback poll. */
  start(): void {
    if (this.started || this.stopped) return;
    this.started = true;
    void this.sync();
    this.openSocket();
    this.startPolling();
  }

  /**
   * Tear everything down. After this the controller is inert: no timer fires, no
   * socket callback lands, and no state is emitted — so a late fetch resolving
   * after unmount cannot touch React state.
   */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.closeSocket();
    this.clearReconnectTimer();
    if (this.pollTimer !== null) {
      this.clock.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  /** The "Yenile" button. Never throws; failure shows up in the badge. */
  refresh(): Promise<void> {
    return this.sync();
  }

  /**
   * The tablet woke up, or the network came back. Everything on screen is
   * suspect: resync immediately and stop waiting out any reconnect backoff.
   */
  resume(): void {
    if (this.stopped || !this.started) return;
    void this.sync();
    if (this.snap.socket !== "open") {
      this.clearReconnectTimer();
      this.snap.reconnectAttempts = 0;
      this.openSocket();
    }
  }

  /** Current state — exposed for tests and for imperative reads. */
  state(): KitchenLiveState {
    const snap = { ...this.snap };
    return {
      ...snap,
      link: deriveLinkState(snap, this.clock.now()),
      orders: this.orders,
      timing: this.timing,
      timingById: this.timingById,
      loaded: this.loaded,
    };
  }

  // ── Refresh ───────────────────────────────────────────────────────────────

  /**
   * Refetch orders and timing together, always as a pair — a board whose tickets
   * and delay badges come from different moments is its own kind of lie.
   *
   * Concurrent calls coalesce: a burst of live events produces one refetch plus
   * at most one trailing refetch, so a busy service cannot stampede the API.
   */
  private sync(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.inFlight) {
      this.resyncQueued = true;
      return this.inFlight;
    }
    const run = this.runSync().then(() => {
      this.inFlight = null;
      if (this.resyncQueued && !this.stopped) {
        this.resyncQueued = false;
        void this.sync();
      }
    });
    this.inFlight = run;
    return run;
  }

  private async runSync(): Promise<void> {
    this.snap.syncing = true;
    this.emit();

    let unauthorized = false;
    try {
      const [orders, timing] = await Promise.all([
        this.opts.fetchOrders(),
        this.opts.fetchTiming(),
      ]);
      if (this.stopped) return;

      // Whole-list replacement, not a patch: terminal orders disappear because
      // the server stopped listing them, and no card can survive its own status.
      this.orders = dedupeOrders(orders);
      this.timing = timing ?? null;
      this.timingById = indexTimingByOrderId(this.timing);
      this.snap.lastSyncedAt = this.clock.now();
      this.snap.lastSyncFailed = false;
      if (this.snap.socket !== "open" && fallbackActive(this.snap)) {
        this.snap.fallbackSyncs += 1;
      }
    } catch (err) {
      if (this.stopped) return;
      unauthorized = this.opts.isUnauthorized?.(err) === true;
      // Either way the board is no longer trustworthy; the badge must say so.
      this.snap.lastSyncFailed = true;
      if (!unauthorized) this.opts.onError?.(err);
    } finally {
      if (!this.stopped) {
        this.snap.syncing = false;
        this.loaded = true;
        this.emit();
      }
    }

    if (unauthorized && !this.stopped) this.opts.onUnauthorized?.();
  }

  // ── Socket ────────────────────────────────────────────────────────────────

  private openSocket(): void {
    if (this.stopped) return;
    this.closeSocket(); // there is never more than one socket
    const epoch = ++this.socketEpoch;
    this.snap.socket = "connecting";
    this.emit();

    try {
      this.socket = this.opts.createSocket(this.opts.wsUrl, {
        onOpen: () => this.handleOpen(epoch),
        onMessage: (raw) => this.handleMessage(epoch, raw),
        onClose: () => this.handleClose(epoch),
        onError: (err) => this.handleError(epoch, err),
      });
    } catch (err) {
      // A constructor throw (bad URL, blocked scheme) must not kill the board;
      // the HTTP fallback carries it.
      this.socket = null;
      this.opts.onError?.(err);
      this.handleClose(epoch);
    }
  }

  private closeSocket(): void {
    if (!this.socket) return;
    const socket = this.socket;
    this.socket = null;
    this.socketEpoch += 1; // orphan any callback still in flight
    try {
      socket.close();
    } catch (err) {
      this.opts.onError?.(err);
    }
  }

  /** Ignore anything from a socket we have already replaced or torn down. */
  private isCurrent(epoch: number): boolean {
    return !this.stopped && epoch === this.socketEpoch;
  }

  private handleOpen(epoch: number): void {
    if (!this.isCurrent(epoch)) return;
    this.snap.socket = "open";
    this.snap.hasConnected = true;
    this.snap.reconnectAttempts = 0;
    this.snap.fallbackSyncs = 0;
    this.emit();
    // Every open resyncs — this is what closes the "reconnected but missing
    // three tickets" gap, independently of whether initial_state arrives.
    void this.sync();
  }

  private handleMessage(epoch: number, raw: string): void {
    if (!this.isCurrent(epoch)) return;
    // initial_state, order_created and order_status_updated all mean the same
    // thing to the board: what you are showing may be wrong. Refetch rather than
    // patch — the socket payloads are event notifications, not a board snapshot.
    if (shouldResyncForFrame(raw)) void this.sync();
  }

  private handleClose(epoch: number): void {
    if (!this.isCurrent(epoch)) return;
    this.socket = null;
    this.snap.socket = "closed";
    this.snap.fallbackSyncs = 0;
    this.emit();
    this.scheduleReconnect();
  }

  private handleError(epoch: number, err: unknown): void {
    if (!this.isCurrent(epoch)) return;
    // A WebSocket error is always followed by a close, which drives the
    // reconnect. Recording it here would double-schedule the retry.
    this.opts.onError?.(err);
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return;
    const index = Math.min(
      this.snap.reconnectAttempts,
      RECONNECT_BACKOFF_MS.length - 1,
    );
    const delay = RECONNECT_BACKOFF_MS[index];
    this.snap.reconnectAttempts += 1;
    this.reconnectTimer = this.clock.setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === null) return;
    this.clock.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  // ── Fallback polling ──────────────────────────────────────────────────────

  private startPolling(): void {
    if (this.stopped || this.pollTimer !== null) return; // exactly one interval
    this.pollTimer = this.clock.setInterval(
      () => this.tick(),
      POLL_INTERVAL_MS,
    );
  }

  private tick(): void {
    if (this.stopped) return;
    if (this.shouldPollNow()) {
      void this.sync();
      return;
    }
    // Nothing to fetch, but re-emit so the "last updated" line keeps counting
    // and staleness is re-derived against the current clock.
    this.emit();
  }

  /**
   * Poll hard while the link is not live; poll rarely while it is.
   *
   * A healthy socket costs one extra request every 30 seconds — cheap insurance
   * against a socket that is open but no longer delivering.
   */
  private shouldPollNow(): boolean {
    if (this.snap.lastSyncedAt === null) return true;
    if (this.snap.lastSyncFailed) return true;
    if (this.snap.socket !== "open") return true;
    return this.clock.now() - this.snap.lastSyncedAt >= LIVE_SAFETY_POLL_MS;
  }

  private emit(): void {
    if (this.stopped) return;
    this.opts.onState(this.state());
  }
}
