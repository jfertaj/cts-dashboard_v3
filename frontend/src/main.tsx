// frontend/src/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// Listener de telemetría (ok dejarlo aquí)
window.addEventListener("cts:fill", (e: any) => {
  console.log("[FILL EVENT]", e?.detail);
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);