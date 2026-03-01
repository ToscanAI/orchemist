/**
 * Render tests for the Button component (components/ui/Button.tsx).
 *
 * Verifies:
 *   - Default rendering (label, accessible role)
 *   - Loading state (spinner, aria-busy, disabled)
 *   - Disabled state
 *   - Click handler is called when not disabled
 *   - Click handler is NOT called when disabled or loading
 */

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { Button } from "@/components/ui/Button";

describe("Button", () => {
  it("renders children as button text", () => {
    render(<Button>Launch</Button>);
    expect(screen.getByRole("button", { name: "Launch" })).toBeInTheDocument();
  });

  it("is enabled by default", () => {
    render(<Button>Click me</Button>);
    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("calls onClick when clicked", () => {
    const handler = jest.fn();
    render(<Button onClick={handler}>Click</Button>);
    fireEvent.click(screen.getByRole("button"));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("is disabled when disabled prop is set", () => {
    render(<Button disabled>Disabled</Button>);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("does not call onClick when disabled", () => {
    const handler = jest.fn();
    render(<Button disabled onClick={handler}>Disabled</Button>);
    fireEvent.click(screen.getByRole("button"));
    expect(handler).not.toHaveBeenCalled();
  });

  it("shows loading spinner and sets aria-busy when loading=true", () => {
    render(<Button loading>Loading</Button>);
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    // spinner is a sibling of the label text (aria-hidden span)
    const spinner = button.querySelector("[aria-hidden='true']");
    expect(spinner).toBeInTheDocument();
  });

  it("does not call onClick when loading", () => {
    const handler = jest.fn();
    render(<Button loading onClick={handler}>Loading</Button>);
    fireEvent.click(screen.getByRole("button"));
    expect(handler).not.toHaveBeenCalled();
  });

  it("applies sm size classes when size='sm'", () => {
    const { rerender } = render(<Button size="sm">Small</Button>);
    const button = screen.getByRole("button");
    // sm variant uses text-xs class
    expect(button.className).toContain("text-xs");
  });

  it("applies default size classes when no size prop given", () => {
    render(<Button>Default</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toContain("text-sm");
  });

  it("merges custom className with base styles", () => {
    render(<Button className="custom-class">Styled</Button>);
    expect(screen.getByRole("button").className).toContain("custom-class");
  });
});
