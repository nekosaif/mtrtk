import { createBrowserRouter } from "react-router";
import { Shell } from "./Shell";
import Dashboard from "@/pages/Dashboard";
import Satellites from "@/pages/Satellites";
import Receiver from "@/pages/Receiver";
import Corrections from "@/pages/Corrections";
import Site from "@/pages/Site";
import Logs from "@/pages/Logs";
import History from "@/pages/History";
import Events from "@/pages/Events";
import Settings from "@/pages/Settings";
import Login from "@/pages/Login";

// To add a page: import its component and give it a path here; the rail entry lives in
// Rail.tsx (NAV), in the same order. /login sits outside the Shell on purpose — it has no
// navigation, no tape and no live socket to show.
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
      { path: "events", element: <Events /> },
      { path: "settings", element: <Settings /> },
    ],
  },
  { path: "/login", element: <Login /> },
];

export const router = createBrowserRouter(routes);
