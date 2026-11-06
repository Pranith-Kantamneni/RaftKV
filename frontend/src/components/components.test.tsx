import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfirmDialog } from "./ConfirmDialog";
import { EventList } from "./EventList";
import type { ClusterEvent } from "../types";

describe("ConfirmDialog", () => {
  it("requires an explicit confirmation and reports the action", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Kill n2?"
        description="This terminates the process."
        confirmLabel="Kill process"
        onConfirm={onConfirm}
        onClose={onClose}
      >
        <div>target n2</div>
      </ConfirmDialog>,
    );

    expect(screen.getByText("Kill n2?")).toBeInTheDocument();
    expect(screen.getByText("target n2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Kill process" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("closes on cancel and Escape, but not while busy", () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <ConfirmDialog open title="Stop cluster?" onConfirm={vi.fn()} onClose={onClose} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    rerender(
      <ConfirmDialog open busy title="Stop cluster?" onConfirm={vi.fn()} onClose={onClose} />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

const event: ClusterEvent = {
  seq: 41,
  ts: 1_700_000_000_000,
  source: "node",
  node_id: "n1",
  type: "leader_elected",
  severity: "info",
  message: "n1 became leader for term 3",
  data: { term: 3, leader: "n1" },
};

describe("EventList", () => {
  it("expands an event to show its raw structured payload", () => {
    render(<EventList events={[event]} />);

    const row = screen.getByRole("button", { name: /became leader/i });
    expect(row).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(row);

    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/"term": 3/)).toBeInTheDocument();
  });

  it("renders an empty state", () => {
    render(<EventList events={[]} empty="No traffic observed" />);
    expect(screen.getByText("No traffic observed")).toBeInTheDocument();
  });
});
