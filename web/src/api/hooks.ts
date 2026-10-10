import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, type S } from "./client";

/** Poll a background job until it finishes; refresh every query of the profile afterwards.
 *  A failed poll (e.g. the server restarted and forgot the job) ends the wait and is returned
 *  as `error`, so the buttons come back. */
export function useJob(profile: string) {
  const [job, setJob] = useState<S["JobOut"] | null>(null);
  const [lost, setLost] = useState<unknown>(null);
  const client = useQueryClient();
  const live = job !== null && lost === null && (job.status === "queued" || job.status === "running");
  const polled = useQuery({
    queryKey: ["job", profile, job?.id],
    queryFn: () => api.job(profile, job!.id),
    enabled: live,
    refetchInterval: 1500,
    retry: 2,
  });
  useEffect(() => {
    if (live && polled.error) setLost(polled.error);
  }, [live, polled.error]);
  useEffect(() => {
    if (!polled.data) return;
    setJob(polled.data);
    if (polled.data.status === "ok" || polled.data.status === "failed") {
      void client.invalidateQueries({
        predicate: (qq) => qq.queryKey[1] === profile || qq.queryKey[0] === "profiles",
      });
    }
  }, [polled.data, client, profile]);
  const start = (next: S["JobOut"]) => {
    setLost(null);
    setJob(next);
  };
  return { job, start, busy: live, error: lost };
}

export const todayIso = (): string => {
  const d = new Date();
  const off = d.getTimezoneOffset() * 60_000;
  return new Date(d.getTime() - off).toISOString().slice(0, 10);
};

export const daysAgoIso = (n: number): string => {
  const d = new Date(Date.now() - n * 86_400_000);
  const off = d.getTimezoneOffset() * 60_000;
  return new Date(d.getTime() - off).toISOString().slice(0, 10);
};
