/**
 * Resolves a Next.js route param under static export (`output: 'export'`).
 *
 * When the app is exported as static HTML, dynamic route segments like
 * `/runs/[id]` resolve to a fallback `_` placeholder at hydration time. The
 * real id is still present in `window.location.pathname`. This hook centralises
 * the placeholder→real-id resolution so RunDetailClient, TemplateDetailClient,
 * and EditTemplateClient share a single implementation (previously three
 * subtly different copies of the same window.location parser).
 *
 * Issues: #774 (helper consolidation), #761/#775 cluster.
 *
 * @param routeParam  The value of the route param from `useParams()`.
 * @param options     `segmentIndexFromEnd` selects which path segment from the
 *                    end is the id. Defaults to `0` (last segment) which
 *                    matches `/runs/:id` and `/templates/:id`. Use `1` for
 *                    `/templates/:id/edit`-style nested routes.
 * @returns           - The route param unchanged if it isn't the placeholder.
 *                    - The decoded path segment in the browser when the param
 *                      is the placeholder.
 *                    - The placeholder string itself in SSR (typeof window
 *                      === 'undefined') without throwing.
 *                    - `'_'` if the requested index is out of range.
 */
function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export function useStaticExportParam(
  routeParam: string | undefined,
  options?: { segmentIndexFromEnd?: number },
): string {
  // Non-placeholder + non-empty: URL-decode and pass through. Callers
  // previously did `decodeURIComponent(rawId)` themselves; centralising the
  // decode here keeps the convergence on a single helper (#774).
  if (routeParam && routeParam !== '_') {
    return safeDecode(routeParam);
  }
  // SSR safe: return the placeholder unchanged.
  if (typeof window === 'undefined') {
    return routeParam ?? '_';
  }
  const segments = window.location.pathname.split('/').filter(Boolean);
  const idx = options?.segmentIndexFromEnd ?? 0;
  const target = segments[segments.length - 1 - idx];
  if (target === undefined) return '_';
  return safeDecode(target);
}
