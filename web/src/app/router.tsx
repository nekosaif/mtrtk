import { createBrowserRouter } from "react-router";
import { Shell } from "./Shell";
import { PageHeader } from "./PageHeader";
import Dashboard from "@/pages/Dashboard";

function Stub({ title }: { title: string }) {
  return (
    <>
      <PageHeader title={title} />
      <p className="text-ink-2">This page arrives in a later task.</p>
    </>
  );
}

// To add a page: import its component and replace the Stub for its path here;
// the rail entry lives in Rail.tsx (NAV), in the same order.
export const routes = [
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "satellites", element: <Stub title="Satellites" /> },
      { path: "receiver", element: <Stub title="Receiver" /> },
      { path: "corrections", element: <Stub title="Corrections" /> },
      { path: "site", element: <Stub title="Site" /> },
      { path: "logs", element: <Stub title="Logs" /> },
      { path: "history", element: <Stub title="History" /> },
      { path: "events", element: <Stub title="Events" /> },
      { path: "settings", element: <Stub title="Settings" /> },
    ],
  },
  { path: "/login", element: <Stub title="Sign in" /> },
];

export const router = createBrowserRouter(routes);
