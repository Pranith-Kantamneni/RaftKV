import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatDuration,
  formatMs,
  formatNumber,
  pct,
  relativeTime,
  severityRank,
} from "./format";

describe("format helpers", () => {
  it("formats numbers and missing values", () => {
    expect(formatNumber(1234.567, 1)).toMatch(/1[,.]?234[.,]6/);
    expect(formatNumber(null)).toBe("—");
  });

  it("formats milliseconds and durations without pretending precision", () => {
    expect(formatMs(0.4)).toBe("<1 ms");
    expect(formatMs(480)).toBe("480 ms");
    expect(formatMs(1500)).toBe("1.50 s");
    expect(formatDuration(0.4)).toBe("400 ms");
    expect(formatDuration(75)).toBe("1m 15s");
  });

  it("formats bytes with binary units", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
  });

  it("ranks severities for minimum-severity filtering", () => {
    expect(severityRank("debug")).toBe(0);
    expect(severityRank("warning")).toBeGreaterThan(severityRank("info"));
    expect(severityRank("unknown")).toBe(1);
  });

  it("computes percentages and relative times safely", () => {
    expect(pct(2, 4)).toBe("50%");
    expect(pct(2, 0)).toBe("0%");
    expect(relativeTime(1_000, 1_000)).toBe("now");
    expect(relativeTime(1_000, 61_000)).toBe("1m ago");
  });
});
