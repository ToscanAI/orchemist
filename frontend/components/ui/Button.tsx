/**
 * Button — primary interactive element.
 *
 * Supports size variants (default, sm) and a loading state with spinner.
 */

import clsx from "clsx";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Show a spinner and disable the button */
  loading?: boolean;
  /** Size variant */
  size?: "default" | "sm";
}

/**
 * Accessible, styled button with loading state.
 *
 * @example
 * <Button onClick={handleLaunch} loading={launching}>
 *   Launch Run
 * </Button>
 */
export function Button({
  loading = false,
  size = "default",
  disabled,
  className,
  children,
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;

  return (
    <button
      type="button"
      disabled={isDisabled}
      aria-busy={loading}
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-md font-medium",
        "bg-accent text-white transition-colors",
        "hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2",
        "focus-visible:ring-offset-surface-canvas outline-none",
        "disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "px-3 py-1.5 text-xs" : "px-4 py-2 text-sm",
        className
      )}
      {...rest}
    >
      {loading && (
        <span
          className="h-3.5 w-3.5 animate-spin rounded-full border border-white/30 border-t-white"
          aria-hidden="true"
        />
      )}
      {children}
    </button>
  );
}
