import { useEffect, useState } from "react";
import { getHealth } from "../api/client";

/**
 * Minimal server-health indicator. The Flow-bridge / extension / token details
 * are intentionally hidden — this build generates server-side via the image API,
 * so the only status that matters to the user is whether the agent is up.
 */
export function StatusBar() {
  const [agentOk, setAgentOk] = useState(false);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const h = await getHealth();
        if (alive) setAgentOk(h.ok);
      } catch {
        if (alive) setAgentOk(false);
      }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="statusbar">
      <span style={{ color: agentOk ? "#6ee7b7" : "#ef4444" }}>{agentOk ? "● agent" : "○ agent"}</span>
    </div>
  );
}
