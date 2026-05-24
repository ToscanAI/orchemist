/**
 * Brand wordmark — gradient text matching the SVG mockup header.
 * Used in the left rail and in the install banner on Skills Pack Mode.
 */
export function Logo({ subtitle = 'HARNESS · LEVEL 5' }: { subtitle?: string }) {
  return (
    <div className="flex flex-col">
      <span
        className="text-[18px] font-extrabold tracking-tight"
        style={{
          background: 'linear-gradient(90deg, #7C5CFC 0%, #2DD4BF 100%)',
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
          backgroundClip: 'text',
        }}
      >
        ORCHEMIST
      </span>
      <span className="text-[10px] font-medium tracking-[0.16em] text-harness-muted">
        {subtitle}
      </span>
    </div>
  );
}
