import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { SignIn } from "./components/SignIn";
import { Audit } from "./modules/Audit";
import { Evaluation } from "./modules/Evaluation";
import { Gate } from "./modules/Gate";
import { Ingestion } from "./modules/Ingestion";
import { Lineage } from "./modules/Lineage";
import { Marts } from "./modules/Marts";
import { Overview } from "./modules/Overview";
import { Quality } from "./modules/Quality";
import { Runs } from "./modules/Runs";

export function App() {
  const { token, expired } = useAuth();

  // Every route is behind the token, including the ones a viewer could reach --
  // there is no useful unauthenticated view of a platform whose every endpoint
  // needs a role. `/meta` is the exception and it is fetched by the sign-in screen.
  if (!token || expired) return <SignIn />;

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="runs" element={<Runs />} />
        <Route path="quality" element={<Quality />} />
        <Route path="evaluation" element={<Evaluation />} />
        <Route path="gate" element={<Gate />} />
        <Route path="marts" element={<Marts />} />
        <Route path="ingestion" element={<Ingestion />} />
        <Route path="lineage" element={<Lineage />} />
        <Route path="audit" element={<Audit />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
