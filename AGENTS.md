# AGENTS.md

# General Rules

Make the smallest possible change required to solve the task.

Do not:
- refactor unrelated code
- rewrite large sections unnecessarily
- introduce unrelated improvements
- modify unrelated formatting or file structure

Preserve existing project behavior unless changes are explicitly requested.

---

# Before Making Changes

Before coding:

1. read surrounding code carefully
2. understand existing patterns
3. reuse existing utilities and components first
4. avoid introducing new abstractions unnecessarily

If requirements are ambiguous:
- stop
- ask for clarification

Do not guess business logic or expected behavior.

---

# iOS Project Rules

Respect the existing project structure.

Do not:
- introduce new architecture patterns unnecessarily
- create new base classes without strong justification
- add protocols for single implementations
- introduce generic abstractions without clear need

Prefer localized and explicit implementations.

---

# UI Rules

Preserve existing UI behavior and layout structure.

Avoid:
- redesigning screens
- restructuring layouts unnecessarily
- modifying unrelated constraints
- changing styling without request

Keep UI edits focused on the requested behavior.

---

# Networking Rules

Reuse existing networking flows whenever possible.

Avoid:
- changing API contracts silently
- modifying shared models unnecessarily
- altering global networking behavior for local issues

Keep networking changes minimal and isolated.

---

# State and Async Rules

Avoid:
- deeply nested async flows
- duplicated state
- hidden side effects

Prefer predictable and readable state changes.

---

# Debugging Rules

Fix root causes instead of symptoms.

Before applying fixes:
- identify the exact issue
- understand why it occurs
- apply the smallest effective correction

Avoid speculative refactoring.

---

# Output Rules

Prefer:
- patch-style edits
- focused diffs
- minimal code changes

Avoid:
- broad cleanup passes
- unnecessary modernization
- “while we're here” refactors

Stability and consistency are more important than architectural perfection.