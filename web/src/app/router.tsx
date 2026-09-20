import { createBrowserRouter } from "react-router";
import { Shell } from "./Shell";
import { PageHeader } from "./PageHeader";
import Dashboard from "@/pages/Dashboard";
import Satellites from "@/pages/Satellites";
import Receiver from "@/pages/Receiver";
import Corrections from "@/pages/Corrections";
import Site from "@/pages/Site";
import Logs from "@/pages/Logs";
import History from "@/pages/History";

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
      { path: "satellites", element: <Satellites /> },
      { path: "receiver", element: <Receiver /> },
      { path: "corrections", element: <Corrections /> },
      { path: "site", element: <Site /> },
      { path: "logs", element: <Logs /> },
      { path: "history", element: <History /> },
      { path: "events", element: <Stub title="Events" /> },
      { path: "settings", element: <Stub title="Settings" /> },
    ],
  },
  { path: "/login", element: <Stub title="Sign in" /> },
];

export const router = createBrowserRouter(routes);
