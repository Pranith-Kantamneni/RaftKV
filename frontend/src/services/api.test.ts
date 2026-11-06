import { describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../services/api";

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  } as Response;
}

describe("api client", () => {
  it("returns a typed cluster snapshot", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ leader_id: "n1", term: 4 }));
    vi.stubGlobal("fetch", fetchMock);

    const snapshot = await api.getCluster();

    expect(snapshot.leader_id).toBe("n1");
    expect(fetchMock).toHaveBeenCalledWith("/api/cluster", expect.objectContaining({ headers: expect.any(Headers) }));
  });

  it("serializes writes as JSON", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ committed: true, index: 8, term: 2, leader: "n1", result: null }));
    vi.stubGlobal("fetch", fetchMock);

    await api.kvSet("alpha", "beta");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/kv");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ op: "set", key: "alpha", value: "beta" }));
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
  });

  it("URL-encodes keys for reads and deletes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ exists: false }));
    vi.stubGlobal("fetch", fetchMock);

    await api.kvGet("user/1001");
    await api.kvDelete("space key?&");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/kv/user%2F1001");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/kv/space%20key%3F%26");
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("DELETE");
  });

  it("passes a directed scope for latency injection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    await api.setLatency(250, "n1", "n2");

    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
    expect(body).toEqual({ latency_ms: 250, source: "n1", target: "n2" });
  });

  it("surfaces server error details as ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "no leader available" }, 503)),
    );

    await expect(api.kvSet("k", "v")).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      message: "no leader available",
    });
  });

  it("turns transport failures into status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connection refused")));

    const failure = api.getCluster();
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 0, message: "connection refused" });
  });

  it("falls back to plain-text error bodies", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: async () => "internal boom",
      } as Response),
    );

    await expect(api.metrics()).rejects.toMatchObject({ status: 500, message: "internal boom" });
  });
});
