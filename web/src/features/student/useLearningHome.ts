import {useEffect, useRef, useState} from "react";
import {studentRequest, StudentApiError} from "./client";
import {parseLearningHome, StudentContractError, type LearningHome} from "./model";

/** Keep the task feed in memory and scoped to the current authenticated session. */
export function useLearningHome({session, studentKey, courseKeys, enabled, onDenied}: {
  session: string; studentKey: string; courseKeys: string[]; enabled: boolean; onDenied: () => void;
}) {
  const [snapshot, setSnapshot] = useState<{owner:string; data:LearningHome} | null>(null);
  const [error, setError] = useState(""), [loading, setLoading] = useState(false), [generation, setGeneration] = useState(0);
  const deny = useRef(onDenied); deny.current = onDenied;
  const scope = JSON.stringify([...courseKeys].sort());
  useEffect(() => {
    setSnapshot(null); setError("");
    if (!enabled || !session || !studentKey || !courseKeys.length) { setLoading(false); return; }
    let disposed = false, pending = false;
    const controller = new AbortController();
    async function refresh() {
      if (disposed || pending || document.visibilityState !== "visible") return;
      pending = true; setLoading(true);
      try {
        const data = parseLearningHome(await studentRequest("/student/api/home", undefined, controller.signal), studentKey, JSON.parse(scope));
        if (!disposed) { setSnapshot({owner:session, data}); setError(""); }
      } catch (cause) {
        if (!disposed && !controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : "해야 할 실습을 불러오지 못했습니다. 다시 확인해 주세요.");
          if (cause instanceof StudentContractError || cause instanceof StudentApiError && [401,403].includes(cause.status)) {
            setSnapshot(null); deny.current();
          }
        }
      } finally { pending = false; if (!disposed) setLoading(false); }
    }
    void refresh();
    const timer = setInterval(() => void refresh(), 30000);
    return () => { disposed = true; controller.abort(); clearInterval(timer); };
  }, [session, studentKey, scope, enabled, generation]);
  return {data: enabled && snapshot?.owner === session ? snapshot.data : null, loading: enabled && loading, error: enabled ? error : "", refresh:() => setGeneration(value => value + 1)};
}
