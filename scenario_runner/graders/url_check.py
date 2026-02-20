"""URL Check grader — extracts URLs from article text, checks HTTP reachability.

Uses stdlib only (urllib.request). No third-party dependencies.
HEAD requests with 5-second timeout; redirects are followed automatically.
"""

import re
import urllib.request
import urllib.error
from typing import List, Tuple

from ..models import GradeResult

# Captures http/https URLs; stops at common punctuation that ends a URL in prose
_URL_RE = re.compile(r"https?://[^\s\)\]\>\"\'<,;]+")
_TIMEOUT = 5  # seconds per HEAD request

_USER_AGENT = "ScenarioRunner/1.0 (url-check grader)"


def _extract_urls(text: str) -> List[str]:
    """Return all unique HTTP/HTTPS URLs found in *text*."""
    found = _URL_RE.findall(text)
    # Deduplicate while preserving order
    seen: set = set()
    unique: List[str] = []
    for url in found:
        # Strip trailing punctuation that may have been captured
        url = url.rstrip(".,;:!?)")
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _check_url(url: str) -> Tuple[bool, str]:
    """Return (reachable, detail_string) for a single URL.

    Uses HEAD first; falls back to GET on 405 Method Not Allowed.
    Redirects are followed via the default urllib opener.
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url,
                method=method,
                headers={"User-Agent": _USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                status = resp.status
                if status < 400:
                    return True, f"✓ {url} ({status})"
                return False, f"✗ {url} ({status})"
        except urllib.error.HTTPError as exc:
            if exc.code == 405 and method == "HEAD":
                continue  # retry with GET
            return False, f"✗ {url} (HTTP {exc.code})"
        except Exception as exc:
            return False, f"✗ {url} ({type(exc).__name__}: {str(exc)[:80]})"
    return False, f"✗ {url} (HEAD and GET both failed)"


class URLCheckGrader:
    """Checks whether URLs embedded in an article are HTTP-reachable.

    score = reachable_urls / total_urls

    If no URLs are found, score is 1.0 (vacuous truth — nothing to fail).
    """

    def grade(self, article_text: str) -> GradeResult:
        """Extract and verify all URLs in *article_text*.

        Returns GradeResult where score = reachable / total.
        ``passed`` is True when score >= 0.9 (matches typical url_check
        threshold); the runner overrides this with the per-criterion threshold.
        """
        urls = _extract_urls(article_text)

        if not urls:
            return GradeResult(
                passed=True,
                score=1.0,
                details="No URLs found in article",
                grader_type="url_check",
            )

        details_lines: List[str] = [f"Checked {len(urls)} URL(s):"]
        reachable_count = 0

        for url in urls:
            ok, detail = _check_url(url)
            if ok:
                reachable_count += 1
            details_lines.append(f"  {detail}")

        score = reachable_count / len(urls)
        details_lines.append(f"Reachable: {reachable_count}/{len(urls)} ({score:.0%})")

        return GradeResult(
            passed=score >= 0.9,
            score=score,
            details="\n".join(details_lines),
            grader_type="url_check",
        )
