import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../api";
import {
  type CancellationTokenLike,
  type DisposableLike,
  OperationCancelledError,
  pollForDeviceToken,
} from "../deviceAuth";

class TestCancellationToken implements CancellationTokenLike {
  private cancelled = false;
  private readonly listeners = new Set<() => void>();

  public get isCancellationRequested(): boolean {
    return this.cancelled;
  }

  public onCancellationRequested(listener: () => void): DisposableLike {
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  }

  public cancel(): void {
    this.cancelled = true;
    for (const listener of [...this.listeners]) {
      listener();
    }
  }
}

test("device polling honors Retry-After and remains bounded", async () => {
  const token = new TestCancellationToken();
  const delays: number[] = [];
  let calls = 0;
  const result = await pollForDeviceToken({
    deviceCode: "device-code",
    expiresInSeconds: 120,
    initialIntervalSeconds: 1,
    cancellationToken: token,
    sleep: async (milliseconds) => { delays.push(milliseconds); },
    exchange: async () => {
      calls += 1;
      if (calls === 1) {
        throw new ApiError("rate limited", 429, "rate_limited", 120_000);
      }
      return { access_token: "access", refresh_token: "refresh", expires_in: 60 };
    },
  });

  assert.equal(result.access_token, "access");
  assert.equal(calls, 2);
  assert.deepEqual(delays, [1_000, 30_000]);
});

test("device polling retries a transient 503", async () => {
  const token = new TestCancellationToken();
  let calls = 0;
  const result = await pollForDeviceToken({
    deviceCode: "device-code",
    expiresInSeconds: 30,
    initialIntervalSeconds: 1,
    cancellationToken: token,
    sleep: async () => {},
    exchange: async () => {
      calls += 1;
      if (calls === 1) {
        throw new ApiError("unavailable", 503, "temporarily_unavailable");
      }
      return { access_token: "access", refresh_token: "refresh", expires_in: 60 };
    },
  });

  assert.equal(result.refresh_token, "refresh");
  assert.equal(calls, 2);
});

test("cancellation during token exchange aborts and discards returned tokens", async () => {
  const token = new TestCancellationToken();
  let exchangeSignalWasAborted = false;

  await assert.rejects(
    pollForDeviceToken({
      deviceCode: "device-code",
      expiresInSeconds: 30,
      initialIntervalSeconds: 1,
      cancellationToken: token,
      sleep: async () => {},
      exchange: async (_deviceCode, signal) => {
        token.cancel();
        exchangeSignalWasAborted = signal.aborted;
        return { access_token: "access", refresh_token: "refresh", expires_in: 60 };
      },
    }),
    OperationCancelledError,
  );

  assert.equal(exchangeSignalWasAborted, true);
});
