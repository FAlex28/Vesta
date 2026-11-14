import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import "leaflet/dist/leaflet.css";
import App from "./App";
import "./styles.css";

// Three screens in one web app, routed by path (no router dependency needed):
//   /            dashboard (read-only)
//   /companion   phone page used during a session
//   /contact     trusted-contact test view
const CompanionPage = lazy(() => import("./companion/CompanionPage"));
const ContactPage = lazy(() => import("./contact/ContactPage"));

const path = window.location.pathname.replace(/\/+$/, "") || "/";

function Root() {
  if (path === "/companion") {
    document.title = "Vesta — Telefon";
    return (
      <Suspense fallback={<p className="route-loading">Se încarcă…</p>}>
        <CompanionPage />
      </Suspense>
    );
  }
  if (path === "/contact") {
    document.title = "Vesta — Contact de urgență";
    return (
      <Suspense fallback={<p className="route-loading">Se încarcă…</p>}>
        <ContactPage />
      </Suspense>
    );
  }
  return <App />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
