# Handoff — n8n pilot conversation context

## Outcome

`get_reception_context` now returns the current synthetic reception state needed
by n8n across turns: contact profile, retained recent messages, upcoming
confirmed appointments and the newest valid pending action.

## Safety boundaries

- Conversation and proposal reads are organization-, conversation- and
  contact-bound.
- Expired or already-redacted message content is excluded at query time even if
  the asynchronous retention cleanup has not run.
- Historical confirmed appointments cannot displace upcoming appointments from
  the ten-item context window.
- No organization identifiers or records belonging to another contact are
  returned.

## Review corrections

Independent review found and the implementation corrected three issues before
commit: missing message-expiry filtering, historical appointment ordering and
missing contact filters on booking, cancellation and reschedule proposals. The
context test now creates expired content, eleven historical appointments, one
upcoming appointment and inconsistent cross-contact proposals for all three
action types.

## Verification

- Focused reception and bootstrap suites: `9 passed`, 2 warnings.
- Full PostgreSQL suite before review corrections: `466 passed`, 21 warnings.
- Full PostgreSQL suite after review corrections: `466 passed`, 21 warnings,
  0 failures in 631.20 seconds.
