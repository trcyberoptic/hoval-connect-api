# GMH224 adoption implementation and verification

Design: [selected fork improvements](../specs/2026-09-20-gmh224-adoptions.md).
Work branch: `feature/gmh224-improvements`.
Status: implemented and locally verified; detailed results are in the linked design review.

1. Compare the fork's committed code to v1.0.10 and the earlier adoption. Record accepted and excluded changes against a fixed fork SHA.
2. Implement independent API/coordinator, entity, and lifecycle/diagnostics changes in separate owned files. Keep shared control and options interfaces explicit.
3. Add runtime regressions for retry counts, plant/circuit isolation, cancelled lock waiters, fan debounce ordering, label round-trips, DTO normalization, option corruption and unload outcomes.
4. Review error-body redaction independently and include malformed/opaque credential cases. Modernize the standalone readers while retaining their telemetry scope.
5. Integrate all changes, run the complete test and coverage suite, unscoped Ruff check/format, Bash syntax validation and a final diff review. Update user-facing behavior documentation.
6. Report local results and the remaining live-validation boundary. Publishing a new release is separate from this implementation.
