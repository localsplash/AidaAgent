# Agent bootstrap contract v1

This is the consumer contract implemented for AidaAgent #7 and #2. It is a
breaking replacement for the inline business metadata in Agent PR #8.
The executable cross-repository example is
[`tests/fixtures/bootstrap-v1.json`](../tests/fixtures/bootstrap-v1.json).
Its credentials are deliberately fake.

## Current implementation boundary

Checked over GitHub SSH on 2026-09-11:

- AidaAgent main/dev: `bdaacf5079795b7a5917745ea6dce2dc77006507` before this change.
- OfficePulseAidaIntegration main: `48ee08a1ef99f4b6acd32c2a91ebf67ed34e4bc4`.
- OfficePulseAidaIntegration dev: `ddb064e28e21728ee7019e655e5378562eb13977`.
- Platform specification main/dev: `e28990b29b9cf22da981d06351f15f15fa29d225`.

OfficePulse dev removed the legacy NocoDB call orchestrator; its canonical
FastAGI decider intentionally returns `FALLBACK` with reason
`native-pbx-admission-not-configured`. There is no deployed implementation of
the endpoint below in that revision. This change implements the Agent, fixtures,
and HTTP contract tests; it does not implement native PBX admission or restore
the retired dispatcher. The tests use a mock authority, not a production token
store. Production replay protection must be implemented and tested in OfficePulse.

The unified platform plan assigns voice runtime ownership to OfficePulse;
AidaControl remains a reserved future extraction. The older issue/spec references
to a standalone AidaControl service and PostgreSQL are not deployment dependencies.

## Dispatch

LiveKit job metadata is a strict JSON object with exactly:

```json
{"callSessionId":"281c6b8e-6a61-45ba-9165-eb199825d12e","bootstrapToken":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
```

`callSessionId` is an application-generated, canonical lowercase UUID string.
The assigned room must be `aida-<callSessionId>`. No assumption is made about its
storage type or UUID version. `bootstrapToken` is an opaque URL-safe bearer
credential: 43–256 ASCII letters, digits, `_`, or `-`. The producer must generate
at least 256 random bits. `routeToken` has the same wire grammar and is a
separate, independently generated credential. Neither value is a JWT.

Dispatch is at most 16,384 UTF-8 bytes. Unknown fields (including `schemaVersion`,
inline prompts, models, voices, URLs, tools, or credentials), duplicate keys,
nulls, non-object JSON, and non-finite JSON numbers are rejected before connection.
There is no automatic fallback to the old metadata format.

## SIP leg and authorization request

The worker joins without auto-subscribing. It waits for exactly one SIP-kind
remote participant and the configured `AIDA_ROUTE_TOKEN_ATTRIBUTE`. OfficePulse
must pass `X-Aida-Route-Token` on the SIP leg and configure LiveKit to map that
header to this attribute (example: `sip.aidaRouteToken`). This is not a
room-metadata or participant-name convention. Missing attributes may arrive via
an update; malformed nonempty credentials and multiple SIP participants reject
the job. Waiting is bounded by the overall startup deadline.

The worker then makes **one** request:

```http
POST /v1/agent/calls/{callSessionId}/bootstrap
Authorization: Bearer <bootstrapToken>
Content-Type: application/json
Accept: application/json
```

```json
{
  "roomName": "aida-281c6b8e-6a61-45ba-9165-eb199825d12e",
  "sipParticipantIdentity": "sip-caller",
  "sipParticipantSid": "PA_sip",
  "routeToken": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"
}
```

The URL comes only from `AIDA_BOOTSTRAP_URL`, an HTTPS origin with no userinfo,
path, query, or fragment. Environment proxy credentials are not used. The client
follows no redirects, makes no automatic retries, accepts only status 200 and
uncompressed `application/json`, and caps the response at 69,632 bytes. Each
HTTP attempt has at most 10 seconds within the overall deadline. An ambiguous
network result ends admission: it does not retry a possibly consumed credential.

### Required OfficePulse authority behavior

Before returning the profile, OfficePulse must:

1. Authenticate the bootstrap credential against its stored hash, expiry,
   unused state, call ID, room, and intended dispatch. Reject ended, failed,
   disabled, or no-longer-authorized calls/tenants.
2. Verify the route credential's hash, expiry, unused state, call/room,
   OfficePulse instance, and PBX linked ID. The intended route-token lifetime
   is 120 seconds; the Agent's local deadline does not extend either expiry.
3. Independently verify the submitted identity/SID is the SIP participant in
   that room, using trusted LiveKit state. A client's observed identity alone
   is not server authorization.
4. Atomically consume both credentials and bind the authorized identity/SID
   to the call. Concurrent submissions must have at most one success. A
   failed binding validation must not partially consume credentials.
5. Return the immutable per-call profile captured at call admission. Never
   re-resolve mutable administrator settings to service this request.

Expired, unknown, reused, or mismatched credentials should receive a generic
401 `credential_rejected`; unsupported request shapes receive 400; unavailable
authority receives 503. The Agent fails closed on every non-200 response.
Responses must use `Cache-Control: no-store`. Tokens and profiles must be excluded
from server/proxy request, response, tracing, and error logs. This endpoint is
call-credential authenticated, not an anonymous or CIDR-only profile reader.

## Authorized response and profile

The response has exactly `callSessionId`, `roomName`, `sipParticipantIdentity`,
`sipParticipantSid`, and `profileSnapshot`. The first four must match the
request/dispatch. `profileSnapshot` is a strict JSON object:

| Field | Requirement |
| --- | --- |
| `schemaVersion` | Required integer `1`; never implicit |
| `callSessionId` | Required; same UUID as the dispatch and room |
| `tenantId` | Positive canonical decimal string or safe integer |
| `businessName` | Required nonblank string, up to 256 characters |
| `prompt` | Required nonblank string, up to 12,000 characters |
| `locale` | Required `en-US` for this English POC |
| `didE164` | Required E.164 string, at most 15 digits after `+` |
| `tone` | Optional string, up to 256 characters |
| `objective` | Optional string, up to 2,048 characters |
| `openingStatement` | Optional string, up to 2,048 characters |
| `transferStatement` | Optional string, up to 2,048 characters |
| `failedTransferStatement` | Optional string, up to 2,048 characters |

Profile JSON is capped at 65,536 bytes on validation. Nulls, NUL-containing
strings, duplicate keys, unsupported versions and extra fields are rejected.
The full example response is in the shared fixture.

The worker freezes the validated profile for the job and renders it as one
JSON-escaped business-context block under fixed screening instructions. Template
syntax and braces remain literal; no profile text is evaluated as a template,
code, environment lookup, credential reference, or provider configuration.
Escaping prevents structural/template injection; it is not a claim that arbitrary
business instructions cannot influence a language model.

Missing optional profile strings remain empty. Without an opening statement the
worker generates a brief English greeting. Transfer language remains reserved;
the worker never initiates or claims an unconfirmed transfer. Confirmed transfer
failure uses the supplied failure statement if present. Model/STT/TTS/voice
choices always come from explicit deployment settings; another cloud agent's
defaults are not inherited by this repository-owned worker.

## Session readiness and failure

Startup order is:

`validate dispatch → join → observe SIP → authorize both credentials → validate
profile → bind muted session → publish ready → enable audio/greet`

The session is restricted to the authorized SIP identity, with SID and token
continuity checked throughout startup and while active. SIP replacement,
disconnect, additional SIP participants, room disconnect, or token changes
terminate admission/session. No room deletion or other-participant removal is
performed by the Agent.

Normal input/output remain disabled until session startup and successful ready
publication. Room text/video input, native text output, recording, and the SDK's
remote session-host control surface are disabled.

Reliable LiveKit data on topic `aida.event.agent_ready` contains exactly:

```json
{
  "type": "aida.event.agent_ready",
  "schemaVersion": 1,
  "callSessionId": "281c6b8e-6a61-45ba-9165-eb199825d12e",
  "agentIdentity": "agent-1",
  "agentParticipantSid": "PA_agent"
}
```

There is one publication attempt per authorized job. Failed or ambiguous
publication terminates the job without greeting. Reconnect does not replay
credentials or readiness. OfficePulse must verify the agent's identity/SID via
trusted LiveKit state; room data alone is not authentication. AidaControl need
not join the room to discover a SID.

`AIDA_BOOTSTRAP_TIMEOUT_SECONDS` defaults to 30 and accepts 1–60 seconds. It
covers join, SIP/attribute wait, authorization, session start, and ready
publication; cleanup is separately bounded. Human takeover interrupts startup
and normal turns. `transfer_failed` during startup cannot enable audio or
trigger an early announcement.

**Routing order must avoid a circular wait:** OfficePulse dispatches the worker
and routes the SIP leg before waiting for conversation readiness. Agent join
and agent_ready are different states. OfficePulse needs a server-side startup
watchdog that sends the caller to its PBX-owned local fallback when admission
fails, the Agent leaves, or the deadline expires. Disconnecting the Agent alone
does not implement that telephony fallback.

## Rollout and acceptance

1. Implement native OfficePulse admission, the durable atomic credential store,
   the endpoint above, header mapping, participant verification, and fallback
   watchdog. Test expiry/replay/concurrency and tenant/room/SIP mismatch there.
2. Run the shared fixture against both implementations. Stage the pair with a
   distinct dispatch name (`aida-prime-bootstrap-dev` is the example default).
   The current inline-metadata producer is incompatible with this consumer.
3. Validate real PBX/LiveKit calls: authorized ready once, English prompt and
   greeting, missing/wrong/reused tokens falling back locally, and concurrent
   businesses remaining isolated. Verify transcript delivery, barge-in, human
   takeover during startup/screening, and failed takeover.
4. Enable producer and worker together after development acceptance. This Agent
   has never been live; no rollback preparation is required. Fix any failed
   checks in place and keep producer/worker contracts aligned under one agent name.

## Issue #7 audit

The Agent has no database driver, SQL access, NocoDB client, Identity credential,
or PBX administration API. It reads only deployment configuration, dispatch
credentials, observed LiveKit state, and the authorized HTTP profile response.
`canonical_uuid()` validates Python UUID string representation without requiring
a PostgreSQL type or database-generated value. No Agent database migration is
needed. The old "verify only" issue assumption was invalidated by PR #8's inline
metadata; this implementation changes behavior explicitly.

References: [issue #7](https://github.com/localsplash/AidaAgent/issues/7),
[issue #2](https://github.com/localsplash/AidaAgent/issues/2),
[unified platform plan](https://github.com/localsplash/AidaInfrastructureSetupInstructions/blob/dev/docs/PLATFORM_MASTER_PLAN.md),
[current OfficePulse API](https://github.com/localsplash/OfficePulseAidaIntegration/blob/dev/docs/PLATFORM_API.md).
