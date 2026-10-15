# API Contract Gate

Flag breaking changes to an HTTP route contract. Look for:

- a changed or removed route path or HTTP method;
- request or response fields that are renamed, removed, retyped, or made nullable;
- tightened request validation (a previously accepted payload now rejected);
- changed status codes or error shapes that callers branch on.

Pay special attention to `server/src/modules/*/routes.ts` and the vendored
`@devdigest/shared` contracts, which must be mirrored by hand into both
`server/src/vendor/shared` and `client/src/vendor/shared` — a change to only
one side is itself a contract break.

Severity guidance: a breaking change with no compatible migration path (an alias,
a deprecation window, a versioned route, or an updated caller in the same PR) is
a **WARNING**. Use **CRITICAL** only when you can name a caller present in this
repo that the change demonstrably breaks. Additive, backwards-compatible changes
are fine and should not be reported.