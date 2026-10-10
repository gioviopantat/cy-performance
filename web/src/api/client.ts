// Typed client for /v1. Types come from docs/api/openapi.json (npm run gen:api).
import type { components } from "./schema";

export type S = components["schemas"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** One request for `profile` (sent as X-CYP-Profile; "" = the server's default profile). */
async function call<T>(profile: string, method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (profile) headers["X-CYP-Profile"] = profile;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/v1${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const j = (await res.json()) as { message?: string; detail?: unknown };
      message = j.message ?? (typeof j.detail === "string" ? j.detail : message);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

const q = (params: Record<string, string | number | boolean | undefined>): string => {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined);
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)]))}` : "";
};

/** Every call except `profiles` takes the profile first, the same value as in its query key. */
export const api = {
  profiles: () => call<S["ProfileOut"][]>("", "GET", "/profiles"),
  meta: (p: string) => call<S["Meta"]>(p, "GET", "/meta"),
  fitness: (p: string, start: string) => call<S["FitnessSeries"]>(p, "GET", `/fitness${q({ start })}`),
  readiness: (p: string, day: string) => call<S["ReadinessOut"]>(p, "GET", `/readiness/${day}`),
  plan: (p: string, days = 14, start?: string) =>
    call<S["PlannedDayOut"][]>(p, "GET", `/plan${q({ start, days })}`),
  calendar: (p: string, start: string, end: string) =>
    call<S["CalendarOut"]>(p, "GET", `/calendar${q({ start, end })}`),
  season: (p: string) => call<S["SeasonOut"]>(p, "GET", "/season"),
  activities: (p: string, offset: number, limit: number) =>
    call<S["ActivityPage"]>(p, "GET", `/activities${q({ rides_only: true, offset, limit })}`),
  activity: (p: string, id: number) => call<S["ActivityDetail"]>(p, "GET", `/activities/${id}`),
  rideLog: (p: string, id: number) => call<S["RideLogOut"]>(p, "GET", `/activities/${id}/ride-log`),
  feedback: (p: string, id: number) => call<S["RideFeedbackOut"]>(p, "GET", `/activities/${id}/feedback`),
  saveFeedback: (p: string, id: number, rpe: number | null, feel: number | null) =>
    call<S["RideFeedbackOut"]>(p, "POST", `/activities/${id}/feedback`, { rpe, feel }),
  saveRideLog: (p: string, id: number, text: string) =>
    call<S["RideLogOut"]>(p, "POST", `/activities/${id}/ride-log`, { text }),
  stravaDescription: (p: string, id: number, text: string, confirm: boolean) =>
    call<S["StravaDescriptionOut"]>(p, "POST", `/activities/${id}/strava-description`, { text, confirm }),
  runs: (p: string) => call<S["AutopilotRunOut"][]>(p, "GET", "/autopilot/runs"),
  runAutopilot: (p: string, write: boolean) =>
    call<S["JobOut"]>(p, "POST", "/autopilot", { write, confirm: write }),
  sync: (p: string) => call<S["JobOut"]>(p, "POST", "/jobs/sync"),
  job: (p: string, id: string) => call<S["JobOut"]>(p, "GET", `/jobs/${id}`),
};
