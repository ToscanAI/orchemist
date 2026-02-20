# Factual Accuracy Rubric

You are evaluating the factual accuracy of a LinkedIn article.

## Scoring Scale (0.0 to 1.0)

- **1.0 — Excellent:** Every factual claim is either:
  (a) Attributed to a specific, named source with a URL, OR
  (b) Clearly framed as the author's personal experience ("I found...", "In my experience...")
  No claims appear fabricated or misleading.

- **0.8 — Good:** Most claims are well-sourced. 1-2 claims lack specific attribution
  but are common knowledge or easily verifiable. No outright fabrication.

- **0.6 — Acceptable:** Several claims lack sources. Some statistics are presented
  without attribution. However, no claims appear to be fabricated — they're
  plausible even if unsourced.

- **0.4 — Poor:** Multiple unsourced claims. At least one claim that appears
  to be fabricated or significantly embellished (a statistic that doesn't match
  its cited source, a quote that seems invented, a person who doesn't exist).

- **0.2 — Very Poor:** Significant fabrication. Made-up statistics, fictional
  experts cited by name, URLs that don't exist, or claims that directly
  contradict their cited sources.

- **0.0 — Unacceptable:** Primarily fabricated content. The article presents
  fiction as fact.

## Specific Checks

1. **Person names:** Are all named individuals real people? Can you verify they
   exist and are associated with the claims made?
2. **Statistics:** Are all numbers attributed? Do they match their cited sources?
3. **Product/tool names:** Are they real? Are version numbers accurate?
4. **Quotes:** Are quotes attributed? Do they seem authentic?
5. **URLs (if visible):** Do cited sources appear to be real websites?

## Output Format

Score: [0.0-1.0]
Reasoning: [2-3 sentences explaining the score]
Issues found: [list each factual concern, or "None"]
