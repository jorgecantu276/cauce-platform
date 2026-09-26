import type { AuthAdapter } from "../auth/auth-adapter";

/** A response from the server (`status` >= 400) or a failure to get one:
 * `status` 0 means no HTTP response arrived (`network_error`, or
 * `request_timeout` when the bounded timeout below expired). */
export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super(code);
  }
}

/** The caller (a context change or an unmount) aborted the request on
 * purpose. Deliberately NOT an ApiError: it is not a business or transport
 * failure, so it must never be rendered as one. Callers treat it as "this
 * result is no longer wanted" and simply stop. */
export class RequestCancelledError extends Error {
  constructor() {
    super("request_cancelled");
    this.name = "RequestCancelledError";
  }
}

export function isRequestCancelled(error: unknown): error is RequestCancelledError {
  return error instanceof RequestCancelledError;
}

/** Pilot default for every staff/platform request: long enough for a cold
 * Lambda plus a paginated DynamoDB read, short enough that a hung
 * connection cannot leave a screen in a loading or "Validando…" state for
 * more than this. It is a pilot setting, not a contract with the server --
 * the API Gateway/Lambda ceiling is lower still (see services/payments-api/template.yaml).
 * A caller can pass `timeoutMs` for one request, e.g. a long apply slice. */
export const DEFAULT_REQUEST_TIMEOUT_MS = 20_000;

export type RequestOptions = {
  /** An external signal (e.g. a component unmounting, or the selected
   * business changing). Aborting it rejects with RequestCancelledError. */
  signal?: AbortSignal;
  timeoutMs?: number;
};

export type HttpConfig = {
  baseUrl: string;
  auth: Pick<AuthAdapter, "getAccessToken">;
};

/** Resolves with `promise`, or rejects as soon as `signal` aborts -- for
 * work that cannot itself be aborted (obtaining an access token). */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new DOMException("aborted", "AbortError"));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new DOMException("aborted", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => { signal.removeEventListener("abort", onAbort); resolve(value); },
      (error) => { signal.removeEventListener("abort", onAbort); reject(error); },
    );
  });
}

/** The one HTTP transport shared by HttpStaffApi and HttpPlatformApi.
 *
 * - Every request is bounded by a timeout and carries an AbortController.
 * - A caller-supplied `signal` is preserved: aborting it aborts the fetch.
 * - Outcomes stay distinguishable: HTTP response -> ApiError(status, code);
 *   no response -> ApiError(0, "network_error"); timeout ->
 *   ApiError(0, "request_timeout"); intentional abort ->
 *   RequestCancelledError.
 * - The timeout also covers obtaining the access token and reading the
 *   response body, so a stuck step cannot outlive it.
 *
 * It never retries and never generates an Idempotency-Key: a retry after a
 * timeout is the caller's decision, and it must reuse the key it already
 * holds for that operation (lib/idempotency.ts) -- this layer only sends
 * the header it is given, unchanged. */
export async function httpRequest<T>(config: HttpConfig, path: string, options: RequestInit & RequestOptions = {}): Promise<T> {
  const { signal: external, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...init } = options;
  if (external?.aborted) throw new RequestCancelledError();

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onExternalAbort = () => controller.abort();
  external?.addEventListener("abort", onExternalAbort, { once: true });

  const classify = (error: unknown): Error => {
    if (error instanceof ApiError) return error;
    if (timedOut) return new ApiError(0, "request_timeout");
    if (external?.aborted) return new RequestCancelledError();
    return new ApiError(0, "network_error");
  };

  try {
    let token: string | null;
    try {
      token = await untilAborted(config.auth.getAccessToken(), controller.signal);
    } catch (error) {
      // An auth adapter's own failure is not a network failure of this
      // request; only an abort is translated here.
      if (controller.signal.aborted) throw classify(error);
      throw error;
    }
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);

    let response: Response;
    try {
      // API authentication is carried by Authorization, not cookies. Leave
      // cross-origin credentials off so ordinary API CORS responses are usable
      // by browsers.
      response = await fetch(`${config.baseUrl}${path}`, { ...init, headers, signal: controller.signal });
    } catch (error) {
      throw classify(error);
    }
    // A body read that fails because the signal aborted must surface as a
    // timeout/cancellation -- never be swallowed into an empty `{}` that
    // would make a half-received 200 look like a successful empty answer.
    const data = await response.json().catch((error: unknown) => {
      if (controller.signal.aborted) throw classify(error);
      return {};
    }) as { error?: string };
    if (!response.ok) throw new ApiError(response.status, data.error ?? "request_failed");
    return data as T;
  } finally {
    clearTimeout(timer);
    external?.removeEventListener("abort", onExternalAbort);
  }
}
