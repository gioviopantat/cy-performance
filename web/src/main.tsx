import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./styles.css";

// Apply the remembered skin before the first paint, so a pixel fan never sees a flash of the default.
try {
  const skin = localStorage.getItem("cyp.skin");
  if (skin) document.documentElement.dataset.skin = skin;
} catch {
  /* private mode */
}

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: true, retry: 1 } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
