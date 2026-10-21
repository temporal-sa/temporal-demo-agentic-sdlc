import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./kit/css/temporal-theme.css";
import "./kit/css/temporal-components.css";
import "./app.css";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
