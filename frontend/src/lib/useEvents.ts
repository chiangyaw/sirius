import { useEffect, useRef, useState } from "react";
import type { SiriusEvent } from "./types";

// Subscribe to the backend WebSocket event stream for a session.
// onDelta receives streamed reply text (llm_delta) out-of-band — these are NOT
// pushed into events[] (so they never render as EventStream cards).
export function useEvents(
  sessionId: string,
  onDelta?: (text: string, sessionId: string) => void
) {
  const [events, setEvents] = useState<SiriusEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  // Keep onDelta in a ref so a changing callback identity never re-runs the
  // effect (which would tear down + reconnect the socket on every render).
  const onDeltaRef = useRef(onDelta);
  onDeltaRef.current = onDelta;

  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/events?session_id=${sessionId}`;
    let closed = false;

    function connect() {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, 1500);
      };
      ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === "connected") return;
        if (data.type === "llm_delta") {
          onDeltaRef.current?.(data.payload?.text ?? "", sessionId);
          return; // never enters events[] → never reaches EventStream
        }
        setEvents((prev) => [...prev, data as SiriusEvent]);
      };
    }
    connect();

    return () => {
      closed = true;
      wsRef.current?.close();
    };
  }, [sessionId]);

  const clear = () => setEvents([]);
  return { events, connected, clear };
}
