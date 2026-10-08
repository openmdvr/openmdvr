import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Notification } from "./api";
import { useAuth } from "./auth";

// Real-time in-app mailbox -- same design as useLivePositions.ts: never rely on
// EventSource's native retry (the one-time ticket breaks it), low-frequency REST
// reconciliation in parallel with the push.
//
// Unlike useLivePositions (a regular hook mounted/unmounted per page, with a
// module-level singleton so it can be closed from outside on logout), this is a
// CONTEXT/Provider. If the rail bell and the /notifications page each called a
// regular hook, each would have its OWN SSE connection and unread state, and
// marking a notification read on the page would not reach the rail badge until
// the next reconciliation cycle. A single Provider mounted once around the whole
// app gives one real connection and one state: every consumer (rail, "More" on
// mobile, the page) sees the same thing instantly. It also removes the need for
// the "close from outside" singleton: the effect depends directly on
// `token`/`isDriver` from useAuth(), so logging out cleans up the connection
// through the effect's cleanup.

const RECONNECT_BACKOFF_INITIAL_MS = 1_000;
const RECONNECT_BACKOFF_MAX_MS = 30_000;
const RECONCILIATION_POLL_MS = 90_000;
const MAX_ITEMS = 30;

export type NotificationConnectionState = "connecting" | "open" | "reconnecting";

interface NotificationsContextValue {
  notifications: Notification[];
  unreadCount: number;
  connectionState: NotificationConnectionState;
  markRead: (id: string) => Promise<void>;
}

const NotificationsContext = createContext<NotificationsContextValue>({
  notifications: [],
  unreadCount: 0,
  connectionState: "connecting",
  markRead: async () => {},
});

function mergeById(list: Notification[], incoming: Notification[]): Notification[] {
  const byId = new Map(list.map((n) => [n.id, n]));
  for (const n of incoming) byId.set(n.id, n);
  return Array.from(byId.values())
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, MAX_ITEMS);
}

export function NotificationsProvider({ children }: { children: ReactNode }) {
  // isDriver: a driver gets 403 on all of /notifications/* (require_non_driver,
  // see api/app/routers/notifications.py) -- never try to connect, avoiding a
  // useless retry loop against an endpoint that will always reject it.
  const { token, isDriver } = useAuth();
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [connectionState, setConnectionState] = useState<NotificationConnectionState>("connecting");
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!token || isDriver) {
      setNotifications([]);
      setUnreadCount(0);
      return;
    }

    let stopped = false;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
    let backoff = RECONNECT_BACKOFF_INITIAL_MS;

    async function reconcile() {
      try {
        const page = await api.listNotifications({ limit: MAX_ITEMS });
        if (stopped) return;
        setNotifications((prev) => mergeById(prev, page.items));
        setUnreadCount(page.unread_count);
      } catch {
        // Silent -- background safety net, not the main data source (same as
        // useUnacknowledgedAlarms).
      }
    }

    function scheduleReconnect() {
      if (stopped) return;
      setConnectionState("reconnecting");
      reconnectTimeout = setTimeout(() => {
        backoff = Math.min(backoff * 2, RECONNECT_BACKOFF_MAX_MS);
        openStream();
      }, backoff);
    }

    async function openStream() {
      if (stopped) return;
      try {
        await reconcile();
        if (stopped) return;

        const { ticket } = await api.createNotificationStreamTicket();
        if (stopped) return;

        const es = new EventSource(api.notificationStreamUrl(ticket));
        esRef.current = es;
        es.onopen = () => {
          backoff = RECONNECT_BACKOFF_INITIAL_MS;
          setConnectionState("open");
        };
        es.onmessage = (event) => {
          try {
            const notif: Notification = JSON.parse(event.data);
            setNotifications((prev) => mergeById(prev, [notif]));
            // A new notification always arrives unread -- the push only happens
            // on creation (insert_alarm(), never when marked read), so adding 1
            // is always correct here; periodic reconciliation fixes any real
            // drift.
            if (!notif.read_at) setUnreadCount((n) => n + 1);
          } catch {
            // A single malformed event must not bring down the whole stream --
            // drop it and keep listening.
          }
        };
        es.onerror = () => {
          es.close();
          if (esRef.current === es) esRef.current = null;
          scheduleReconnect();
        };
      } catch {
        scheduleReconnect();
      }
    }

    openStream();
    const reconciliationInterval = setInterval(reconcile, RECONCILIATION_POLL_MS);

    return () => {
      stopped = true;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      esRef.current?.close();
      esRef.current = null;
      clearInterval(reconciliationInterval);
    };
  }, [token, isDriver]);

  async function markRead(id: string) {
    // Optimistic: the bell feels instant; reverted on failure.
    let prevNotifications: Notification[] = [];
    let prevUnread = 0;
    setNotifications((prev) => {
      prevNotifications = prev;
      return prev.map((n) => (n.id === id ? { ...n, read_at: n.read_at ?? new Date().toISOString() } : n));
    });
    setUnreadCount((n) => {
      prevUnread = n;
      const wasUnread = !prevNotifications.find((n2) => n2.id === id)?.read_at;
      return wasUnread ? Math.max(0, n - 1) : n;
    });
    try {
      const updated = await api.markNotificationRead(id);
      setNotifications((prev) => prev.map((n) => (n.id === id ? updated : n)));
    } catch {
      setNotifications(prevNotifications);
      setUnreadCount(prevUnread);
    }
  }

  return (
    <NotificationsContext.Provider value={{ notifications, unreadCount, connectionState, markRead }}>
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotifications() {
  return useContext(NotificationsContext);
}
