---
name: karpathy-guidelines
description: Use for non-trivial repository code changes or reviews that need disciplined scope and verification.
license: MIT
---

# Karpathy Guidelines

Apply these guidelines when writing, debugging, reviewing, or refactoring repository
code. Assume Codex is capable; use this skill for the decisions that are easy to get
wrong, not as a fixed coding itinerary.

## Scope before code

- Identify the requested outcome and the observable success criteria.
- Surface an assumption only when it changes behavior, ownership, compatibility,
  permissions, or the likely implementation.
- Ask only when missing information is material. Otherwise choose the simplest safe
  interpretation and proceed.
- Read the assigned Work Package and only the references needed for the affected
  behavior.

## Implementation

- Make the smallest change that satisfies the request.
- Match existing architecture, conventions, and public behavior.
- Avoid speculative configuration, abstractions, frameworks, or fallback paths.
- Do not refactor adjacent code unless the requested result requires it.
- Preserve unrelated user and Agent changes.
- Remove only code or imports made obsolete by the current change.
- Every changed line should trace to the request, an accepted contract, or a required
  regression.

## Verification

- Verify observable behavior, not confidence or wording.
- Start with the narrowest test that can fail for the change.
- Fix failures caused by the change and rerun affected checks without waiting for
  approval after every local step.
- Expand verification in proportion to risk:
  - focused checks for local changes;
  - integration/protocol checks for state, concurrency, persistence, or lifecycle;
  - full suites, soaks, or platform checks when required by the Work Package or
    release decision.
- Do not run unrelated expensive suites for a trivial edit, and do not skip a
  required acceptance boundary because smaller tests pass.

## Continue until done

For an implementation request, continue through implementation, inspection, and
correction until the requested outcome and its required verification are complete.
Do not stop at a skeleton, first pass, or first green test when required work remains.

Stop only when:

- the Work Package defines an explicit checkpoint;
- a contract decision or new authority is required;
- an external/destructive action requires permission;
- the required environment is unavailable;
- the user requested review before further work.

## Boundaries

This skill does not authorize:

- additional features or dependencies;
- unrelated cleanup;
- paid APIs, credentials, hardware, deployment, or external messages;
- destructive file or Git operations;
- changes outside the active task or Work Package.

Record a concise Handoff when the repository workflow requires one: outcome, changed
paths, verification, remaining limitations, and external access. Do not add process
diaries or speculative recommendations.
