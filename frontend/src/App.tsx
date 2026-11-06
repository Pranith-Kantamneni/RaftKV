import { Navigate, Route, Routes } from "react-router-dom";
import { ClusterProvider } from "./hooks/useCluster";
import { Layout } from "./components/Layout";
import { OverviewPage } from "./pages/Overview";
import { ClusterPage } from "./pages/Cluster";
import { SimulatorPage } from "./pages/Simulator";
import { KVPage } from "./pages/KV";
import { TimelinePage } from "./pages/Timeline";
import { MetricsPage } from "./pages/Metrics";
import { ReportsPage } from "./pages/Reports";

export function App() {
  return (
    <ClusterProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<OverviewPage />} />
          <Route path="cluster" element={<ClusterPage />} />
          <Route path="simulator" element={<SimulatorPage />} />
          <Route path="kv" element={<KVPage />} />
          <Route path="timeline" element={<TimelinePage />} />
          <Route path="metrics" element={<MetricsPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </ClusterProvider>
  );
}
