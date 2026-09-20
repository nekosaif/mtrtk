import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import { initTheme } from "./components/ThemeToggle";
import "./index.css";

// Before the first paint: a light browser must not flash the dark palette (nor the reverse).
initTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
