---
name: rigorous-code-review
description: Review pull requests and diffs with a risk-ranked rubric covering intent, correctness, performance, maintainability, validation, and goal completeness. Use for thorough code review; do not use for implementation-only work or superficial formatting checks.
license: MIT
---

# Rigorous Code Review

Review the change as an engineer responsible for its production consequences.
Follow documented repository conventions first; use this rubric where the
repository is silent. Focus on defects and material risks, not personal taste.

## Establish the contract

Before judging the diff:

- Read the PR or task description and identify the promised behavior.
- Inspect the complete diff, affected call sites, relevant tests, and nearby
  conventions. Do not review isolated lines without their execution context.
- Explain the technical mechanism only as deeply as needed to evaluate the
  change.
- Decide whether the change solves a real problem with proportionate
  complexity or duplicates an existing capability.
- Distinguish requirements, repository conventions, and reviewer preferences.

## Rank findings

Use the narrowest applicable label:

- `[P0-BLOCKER]`: incorrect behavior, safety or security failure, data loss,
  race condition, resource leak, broken compatibility, or another issue that
  must be fixed before merge.
- `[P1-PERF]`: material hot-path latency, throughput, memory, allocation,
  synchronization, or scalability regression.
- `[P2-MAINTAIN]`: unnecessary complexity, poor boundaries, misleading names,
  duplication, type-contract erosion, or architecture that makes future
  changes substantially riskier.
- `[P3-STYLE]`: meaningful readability or consistency problem not already
  covered above. Do not report mechanical formatting handled by configured
  tools.
- `[P4-PROCESS]`: missing tests, migration steps, reproducible verification,
  documentation, observability, or deterministic test controls.

Every finding must be self-contained:

1. Cite the smallest useful file and line range.
2. State the observable failure or maintenance cost.
3. Explain the conditions that trigger it and why current behavior is wrong.
4. Recommend a concrete, proportionate fix.
5. Identify a test or verification step when one would prove the correction.

Do not inflate severity. Separate blocking defects from optional improvements.

## Correctness and operational safety

Check the behaviors that can invalidate the change:

- Public boundaries validate untrusted input; internal code states and relies
  on clear invariants.
- Shapes, dimensions, units, dtypes, indexes, ordering, ownership, lifetimes,
  and partial-failure behavior are correct.
- Branches cover the meaningful state space without leaving values
  uninitialized or silently selecting a fallback.
- Exceptions protect a narrow operation, catch expected failures, and preserve
  useful error information. Broad catches and silent fallback paths require a
  concrete operational reason.
- Resources use deterministic cleanup. Long-lived caches and buffers have a
  bound or eviction policy.
- Concurrent code documents its guarantees, minimizes lock scope, and avoids
  blocking I/O or expensive work while holding a lock.
- Compatibility, migrations, rollback, retries, idempotency, and cancellation
  are considered where the system requires them.

Fail fast for violated programmer-controlled invariants. Use appropriate
exceptions for invalid user or environment input. Do not add defensive
branches for impossible states merely to avoid a visible failure.

## Performance

Review performance in context; do not speculate without identifying a hot path
or scaling boundary.

- Avoid unnecessary synchronization, blocking I/O, device-to-host transfers,
  repeated serialization, N+1 operations, and avoidable allocations.
- Keep bulk data work vectorized or batched when the platform benefits from it.
- Remove interpreter or framework overhead from tight loops when it is
  measurable and the lower-overhead design remains clear.
- Require profiling or benchmark evidence for complexity that exists only to
  optimize performance.
- Never trade correctness for an unmeasured micro-optimization.

## Simplicity and maintainability

Prefer the smallest design that clearly expresses the current requirement.

- Do not introduce a helper, wrapper, base class, registry, configuration
  layer, or plugin system for hypothetical reuse. A single-use helper is
  justified only when it makes dense logic easier to understand or isolates a
  hazardous operation.
- Keep related logic together. Avoid both oversized modules and forests of tiny
  files or functions.
- Use domain-specific names that communicate meaning, units, and role. Avoid
  vague names and abbreviations unless they are established domain language.
- Use real types. Do not use `Any`, dynamic attributes, casts, ignored checks,
  or immediately discarded parameters merely to silence tooling.
- Keep a constant near its owner. Share it only when multiple consumers truly
  depend on one concept; do not duplicate a higher-level default downstream.
- Match the repository's import, typing, logging, configuration, and entry-point
  conventions. Avoid wildcard imports, path manipulation, circular
  dependencies, hidden module side effects, and multiple competing mechanisms.
- Prefer explicit access when the type contract is known. Reflection and
  fallback lookup need a genuine polymorphic or compatibility requirement.

Do not enforce arbitrary line-count or reuse-count thresholds. Use cohesion,
clarity, change risk, and actual reuse as the decision criteria.

## Comments and documentation

- Comments explain a non-obvious reason, constraint, invariant, workaround, or
  empirical choice. Delete comments that merely narrate the code.
- Keep comments concise and self-contained. Preserve exact identifiers and
  domain terminology.
- Document public contracts and surprising behavior; avoid boilerplate
  docstrings that obscure the important detail.
- Do not leak review process markers, temporary debugging notes, or irrelevant
  provenance into production source.
- Match the repository's language policy and documentation style.

## Tests and verification

- Test observable contracts, failure boundaries, and regressions rather than
  private implementation details.
- Cover realistic edge cases, not branches added solely for speculative
  defensive behavior.
- Make tests deterministic where randomness, time, concurrency, or ordering
  affects the result.
- Require important behavior to run in CI when practical.
- Provide exact verification commands and report what was actually run. Never
  imply that an unrun check passed.

## Goal completeness

Compare the implementation with the stated goal:

- Does every promised behavior exist and work together?
- Are important edge cases, call sites, platforms, migrations, and operational
  states omitted?
- Does the implementation rely on unstated assumptions?
- Is it under-engineered for a real requirement or over-engineered for the
  stated scope?
- Did unrelated behavior change without justification?

## Authorship-neutral quality review

Do not guess whether code was generated by AI or accuse an author based on
style. Review the observable problems themselves:

- generic boilerplate that ignores repository conventions;
- verbose comments that explain syntax but miss domain constraints;
- broad exception handling and fallback defaults that hide failures;
- unnecessary null checks, dynamic access, wrappers, or abstractions;
- inconsistent naming or a textbook solution that ignores system realities.

Report these under correctness or maintainability with concrete evidence. The
source of the code is irrelevant to whether it is safe to merge.

## Response format

Return:

1. **Outcome** — `block`, `request changes`, or `approve`, with one sentence
   explaining the decision.
2. **Findings** — ordered by P0 through P4, then by impact. Include only
   actionable findings.
3. **Technical summary** — what changed, how it works, and whether the
   complexity is justified.
4. **Goal completeness** — what is complete, missing, or based on an
   assumption.
5. **Verification and residual risk** — checks observed or run, plus anything
   that could not be verified.

If there are no findings, say so explicitly and explain which evidence supports
approval. Never substitute a bare "LGTM" for a review.
