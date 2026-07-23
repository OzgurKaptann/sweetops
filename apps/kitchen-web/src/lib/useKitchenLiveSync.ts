"use client";

/**
 * React binding for the kitchen live-sync controller.
 *
 * All reliability rules live in `liveSync.ts`; this file only owns the React
 * lifecycle — one controller per mount, torn down on unmount — plus the two
 * browser signals a tablet gives us that the controller cannot see for itself:
 * the screen waking up, and the network coming back.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchKitchenOrders, fetchKitchenTiming } from "@/lib/api";
import { UnauthorizedError } from "@/lib/auth";
import {
  KitchenLiveState,
  KitchenLiveSync,
  browserSocketFactory,
  initialLiveState,
} from "@/lib/liveSync";

export interface KitchenLive extends KitchenLiveState {
  /** Manual "Yenile" — refetches orders and timing; never throws. */
  refresh: () => void;
}

export function useKitchenLiveSync(
  wsUrl: string,
  onUnauthorized: () => void,
): KitchenLive {
  const [state, setState] = useState<KitchenLiveState>(initialLiveState);
  const syncRef = useRef<KitchenLiveSync | null>(null);

  // Kept in a ref so a new auth callback identity never restarts the socket.
  const unauthorizedRef = useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;

  useEffect(() => {
    const controller = new KitchenLiveSync({
      wsUrl,
      fetchOrders: fetchKitchenOrders,
      fetchTiming: fetchKitchenTiming,
      createSocket: browserSocketFactory,
      onState: setState,
      onUnauthorized: () => unauthorizedRef.current(),
      isUnauthorized: (err) => err instanceof UnauthorizedError,
      onError: (err) => console.error("Kitchen live sync:", err),
    });
    syncRef.current = controller;
    controller.start();

    // A kitchen tablet spends its day asleep between rushes. Waking up (or
    // regaining Wi-Fi) is exactly the moment the board is most likely stale.
    const resume = () => controller.resume();
    const onVisibility = () => {
      if (document.visibilityState === "visible") controller.resume();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", resume);
    window.addEventListener("focus", resume);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", resume);
      window.removeEventListener("focus", resume);
      controller.stop();
      syncRef.current = null;
    };
  }, [wsUrl]);

  const refresh = useCallback(() => {
    void syncRef.current?.refresh();
  }, []);

  return { ...state, refresh };
}
