import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useLive } from "@/lib/live";
import { bindLiveToQueries } from "@/lib/queries";
import { router } from "./router";

export const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: 1, refetchOnWindowFocus: false } },
});

/**
 * Opens the one WebSocket for this tab and keeps it open for the life of the app. A `?token=`
 * in the URL is the manual bearer-token fallback from docs/api.md; it is passed through only
 * when present, and never `?topics=`.
 */
function useLiveConnection() {
  const connect = useLive((s) => s.connect);
  const disconnect = useLive((s) => s.disconnect);
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token");
    connect(token);
    return () => disconnect();
  }, [connect, disconnect]);
  useEffect(() => bindLiveToQueries(queryClient), []);
  // Transient bus events that deserve a toast rather than a slice of their own.
  useEffect(
    () =>
      useLive.subscribe((s, prev) => {
        if (s.receiverReset && s.receiverReset !== prev.receiverReset) {
          toast(`Receiver ${s.receiverReset.kind} reset sent`, { description: "The USB device drops off the bus and comes back; expect a reconnect." });
        }
        const failure = s.daemonFailures[0];
        if (failure && failure !== prev.daemonFailures[0]) {
          toast.warning(`${failure.name} failed and is being restarted`, { description: failure.error });
        }
      }),
    [],
  );
}

export default function App() {
  useLiveConnection();
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
      <Toaster position="bottom-right" />
    </QueryClientProvider>
  );
}
