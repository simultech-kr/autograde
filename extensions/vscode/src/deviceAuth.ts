import {
  ApiError,
  isRetryableApiError,
  RequestCancelledError,
  retryDelayMs,
} from "./api";
import type { TokenResponse } from "./types";

const MAX_DEVICE_POLL_INTERVAL_MS = 30_000;

export interface DisposableLike {
  dispose(): void;
}

export interface CancellationTokenLike {
  readonly isCancellationRequested: boolean;
  onCancellationRequested(listener: () => void): DisposableLike;
}

export interface DeviceTokenPollOptions {
  readonly deviceCode: string;
  readonly expiresInSeconds: number;
  readonly initialIntervalSeconds: number;
  readonly cancellationToken: CancellationTokenLike;
  readonly exchange: (deviceCode: string, signal: AbortSignal) => Promise<TokenResponse>;
  readonly onProgress?: (remainingSeconds: number) => void;
  readonly now?: () => number;
  readonly sleep?: (milliseconds: number, token: CancellationTokenLike) => Promise<void>;
}

export class OperationCancelledError extends Error {
  public constructor() {
    super("operation cancelled");
    this.name = "OperationCancelledError";
  }
}

export async function pollForDeviceToken(options: DeviceTokenPollOptions): Promise<TokenResponse> {
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? cancellableDelay;
  const expiresAt = now() + options.expiresInSeconds * 1000;
  let normalIntervalMs = boundedInterval(options.initialIntervalSeconds * 1000);
  let nextDelayMs = normalIntervalMs;
  let transientRetryIndex = 0;

  while (now() < expiresAt) {
    throwIfCancelled(options.cancellationToken);
    const remainingMs = Math.max(0, expiresAt - now());
    options.onProgress?.(Math.ceil(remainingMs / 1000));
    await sleep(Math.min(nextDelayMs, remainingMs), options.cancellationToken);
    if (now() >= expiresAt) {
      break;
    }

    const controller = new AbortController();
    const subscription = options.cancellationToken.onCancellationRequested(() => controller.abort());
    if (options.cancellationToken.isCancellationRequested) {
      controller.abort();
    }
    try {
      const tokens = await options.exchange(options.deviceCode, controller.signal);
      throwIfCancelled(options.cancellationToken);
      return tokens;
    } catch (error) {
      if (
        options.cancellationToken.isCancellationRequested ||
        error instanceof RequestCancelledError
      ) {
        throw new OperationCancelledError();
      }
      if (!(error instanceof ApiError)) {
        throw error;
      }
      switch (error.code) {
        case "authorization_pending":
          transientRetryIndex = 0;
          nextDelayMs = normalIntervalMs;
          continue;
        case "slow_down":
          transientRetryIndex = 0;
          normalIntervalMs = boundedInterval(normalIntervalMs + 5_000);
          nextDelayMs = normalIntervalMs;
          continue;
        case "access_denied":
          throw new Error("웹사이트에서 Autograde 연결이 거부되었습니다.");
        case "expired_token":
          throw new Error("연결 코드가 만료되었습니다. 다시 로그인하세요.");
        default:
          if (isRetryableApiError(error)) {
            nextDelayMs = Math.max(
              normalIntervalMs,
              boundedInterval(retryDelayMs(error, transientRetryIndex)),
            );
            transientRetryIndex += 1;
            continue;
          }
          throw error;
      }
    } finally {
      subscription.dispose();
    }
  }
  throwIfCancelled(options.cancellationToken);
  throw new Error("연결 코드가 만료되었습니다. 다시 로그인하세요.");
}

function boundedInterval(milliseconds: number): number {
  if (!Number.isFinite(milliseconds)) {
    return MAX_DEVICE_POLL_INTERVAL_MS;
  }
  return Math.min(MAX_DEVICE_POLL_INTERVAL_MS, Math.max(1_000, Math.ceil(milliseconds)));
}

function throwIfCancelled(token: CancellationTokenLike): void {
  if (token.isCancellationRequested) {
    throw new OperationCancelledError();
  }
}

export function cancellableDelay(
  milliseconds: number,
  token: CancellationTokenLike,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (token.isCancellationRequested) {
      reject(new OperationCancelledError());
      return;
    }
    const timeout = setTimeout(() => {
      subscription.dispose();
      resolve();
    }, milliseconds);
    const subscription = token.onCancellationRequested(() => {
      clearTimeout(timeout);
      subscription.dispose();
      reject(new OperationCancelledError());
    });
  });
}
