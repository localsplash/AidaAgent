# AidaAgent

Python LiveKit worker for the unified Echo/Aida office platform. OfficePulse
dispatches one screening agent to `aida-<callSessionId>`. The worker listens to
the caller's SIP audio, responds using LiveKit Inference, streams transcripts to
AidaHandset, and leaves when OfficePulse confirms a human has answered.

This worker implements call-scoped bootstrap authorization and SIP-leg validation.
It requires the [bootstrap contract](docs/BOOTSTRAP_CONTRACT.md). Current OfficePulse
dev intentionally returns local fallback pending native call admission; its new
bootstrap endpoint and a real LiveKit/PBX acceptance call remain release prerequisites.

## Ownership

- **Identity** owns users, organizations, memberships, and sign-in sessions.
- **OfficePulse** checks tenant access, resolves business settings, owns calls and
  device access, creates LiveKit rooms/tokens, and performs PBX takeover.
- **AidaAgent** receives an authorized per-call configuration. It has no database,
  Identity credentials, carrier API, room deletion, or participant-removal API.
- **AidaHandset** displays live transcripts using a tenant-authorized room token
  from OfficePulse.

The worker receives only a call ID and one-time bootstrap credential in dispatch.
OfficePulse must authorize and atomically consume bootstrap and SIP route credentials
before returning the immutable business profile. Keep dispatch
and room-admin credentials on servers. Each business call has separate state.

## Run

Requires Docker Compose, a LiveKit project with Inference access, and the
OfficePulse/PBX SIP path into that project's rooms.

```sh
cp .env.example .env
# Set LiveKit credentials, bootstrap URL/attribute, and model/voice choices in .env.
scripts/with-build-info.sh docker compose build
scripts/with-build-info.sh docker compose up -d
docker compose ps
```

This is an **outbound worker**: NPM needs no `*.localsplash.dev` hostname for it.
Ports 8081 (SDK health) and 8082 (application diagnostics) are exposed only on its
Docker network. Process health alone does not validate SIP audio or inference.

`AIDA_AGENT_NAME` defaults to `aida-prime` and must match OfficePulse's
`LIVEKIT_AGENT_NAME`. Alongside a pre-existing cloud agent, use `aida-prime-dev`
in both deployments. The example uses `aida-prime-bootstrap-dev` for the new
contract. Retire the previous worker before reusing its name:
identical names may receive jobs across either implementation. The historical
Cloud deployment ID `CA_Lbh5CTq2Rxhd` is not a runtime configuration source.

Models and voice are explicit deployment settings: `AIDA_LLM_MODEL`,
`AIDA_STT_MODEL`, `AIDA_TTS_MODEL`, and `AIDA_TTS_VOICE`. `.env.example` uses
LiveKit Inference with LiveKit credentials; no separate provider key is needed.
A self-hosted LiveKit server alone does not include hosted inference. Where
needed, LiveKit supports separate `LIVEKIT_INFERENCE_API_KEY` and
`LIVEKIT_INFERENCE_API_SECRET` credentials. This worker cannot inherit another
cloud worker's defaults. Per-call metadata cannot select models, voices, URLs,
plugins, tools, or credentials.

Local Python development (Python 3.11–3.14):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
aida-agent --help
# Export .env values through your process manager/shell before starting.
aida-agent start
```

The runtime runs as UID 10001. Silero VAD is packaged in its pinned wheel and
needs no startup download. Caller barge-in is enabled. Recording and room text
input are disabled. Application logs omit prompts, transcripts, metadata, and
provider exception payloads. Keep SDK debug logging disabled for real calls;
inference providers necessarily process the supplied audio and text.

## Dispatch and authorized profile

Job dispatch metadata contains exactly `callSessionId` and `bootstrapToken`.
Inline business context and unknown fields are rejected before connection.
The worker joins the matching `aida-<callSessionId>` room without subscriptions,
waits for the inbound SIP participant and configured route-token attribute,
and authorizes both credentials with OfficePulse. Only the returned immutable
`profileSnapshot` can supply business context.

See [Bootstrap contract v1](docs/BOOTSTRAP_CONTRACT.md) for the exact endpoint,
request/response and readiness schemas, limits, fail-closed behavior, upstream
implementation requirements, and coordinated rollout. The shared
[contract fixture](tests/fixtures/bootstrap-v1.json) is exercised by offline HTTP tests.

Set `AIDA_BOOTSTRAP_URL` to the authority's HTTPS origin and
`AIDA_ROUTE_TOKEN_ATTRIBUTE` to the trunk's mapping for `X-Aida-Route-Token`.
`AIDA_BOOTSTRAP_TIMEOUT_SECONDS` bounds all startup work (default 30, range 1–60).
No audio turns or greeting begin until authorization, profile validation,
muted session startup and reliable `aida.event.agent_ready` publication succeed.
After bootstrap, the worker explicitly subscribes only to the bound SIP leg's
microphone audio and waits for the subscription before publishing readiness.
Missing audio shares the startup deadline; loss of subscribed audio ends the job.
Room text input and SDK remote session hosting are disabled. SIP replacement,
timeout, credential rejection, or readiness failure terminates the agent job.
OfficePulse owns the corresponding PBX fallback watchdog.

INFO lifecycle diagnostics include the call session ID, startup stages, final STT
event counts, assistant item counts, and whether a provider error is recoverable.
They never include speech text, profile content, credentials, or exception messages.

Provider and voice selections remain deployment settings. Profile strings use
literal JSON escaping under fixed English screening instructions; no template
evaluation or secret interpolation occurs. `transferStatement` is reserved
for a future confirmed pre-bridge announcement; the worker does not initiate transfers.

## Transcript contract

Reliable LiveKit data packets on **`transcript`** contain:

```json
{
  "type": "transcript",
  "callId": "281c6b8e-6a61-45ba-9165-eb199825d12e",
  "eventId": "57088b14-64b5-462f-8df4-785db2624668",
  "streamId": "9ee82a35-b7a1-45e3-85c5-d05f13e24e43",
  "sequence": 1,
  "segmentId": "e990a858-5e22-4f78-af2d-4ba83fc12909",
  "text": "How late are you open?",
  "isFinal": true,
  "timestamp": "2026-09-06T12:00:00.000Z",
  "speaker": "caller"
}
```

Each job connection gets a UUID `streamId`; sequence starts at 1 and increases
across speakers. Caller STT partials and their final share a `segmentId`; the
next utterance gets a new segment. Assistant text comes from committed
`conversation_item_added` messages with the SDK message ID as segment suffix.
User history events are not republished.

One bounded sender preserves order. Delivery failure or queue saturation ends
the agent job without logging content. Reliable data is live delivery, not
durable history: handset reconnects cannot request replay here. Long segments
are UTF-8-truncated to 6,000 bytes; control characters are removed to fit both
packet and handset limits. Native SDK speech lifecycle is used internally;
no separate lifecycle data topic is published in this MVP.

## Takeover contract

OfficePulse sends **`aida.control`** using server `RoomService.SendData`:

```json
{
  "type": "control",
  "callId": "281c6b8e-6a61-45ba-9165-eb199825d12e",
  "commandId": "call:bridged:1",
  "action": "human_answered",
  "deadlineMs": 1788696000000
}
```

Only packets with SDK `participant is None` are accepted. A participant named
`officepulse-integration` is still rejected. Only `human_answered` and
`transfer_failed` are valid; call ID must match. Command IDs use a bounded
256-entry deduplication cache. Deadlines are absolute Unix milliseconds.

`human_answered` immediately mutes input/output, interrupts queued speech, stops
new turns, and disconnects **only the agent participant**. It waits no more than
the remaining deadline, capped at 10 seconds, before requesting job shutdown.
An expired success command still silences immediately. `transfer_failed` keeps
screening active and speaks the configured failure statement once; expired
commands do not speak. Failure cannot restart an agent after human takeover.
OfficePulse must publish `human_answered` only after the human bridge succeeds.

## Validation

```sh
docker build --target test -t aida-agent:test .
docker build --target runtime -t aida-agent:runtime .
```

Dependency installation uses the network. Ruff, fake-provider tests, installed
SDK/Silero checks, and CLI help validation run in a build step with
`--network=none`. No credentials or paid model calls are used. CI builds both
targets. Direct runtime/test dependencies are pinned in `pyproject.toml`;
pip resolves transitive dependencies when building the image.

Live acceptance still needs one inbound SIP call, caller/agent text on Android,
caller barge-in, answered takeover with continued human/SIP audio, and failed
takeover returning to screening. Repeat across two businesses and verify
OfficePulse token authorization and SUPER ADMIN visibility.

## Upstream references

- [LiveKit Agents 1.8.0](https://pypi.org/project/livekit-agents/1.8.0/)
- [Sessions and room options](https://docs.livekit.io/agents/logic/sessions/)
- [Transcript and conversation events](https://docs.livekit.io/reference/agents/events/)
- [Data packets](https://docs.livekit.io/transport/data/packets/)
- [Inference TTS and voice configuration](https://docs.livekit.io/agents/models/tts/cartesia/)
- [Unified platform plan](https://github.com/localsplash/AidaInfrastructureSetupInstructions/blob/dev/docs/PLATFORM_MASTER_PLAN.md)
- [Current OfficePulse runtime API](https://github.com/localsplash/OfficePulseAidaIntegration/blob/dev/docs/PLATFORM_API.md)
- [Agent bootstrap contract](docs/BOOTSTRAP_CONTRACT.md)

## Status-only preview

Run `aida-agent preview` while voice configuration is being supplied. This command
uses Python's standard HTTP server on port 8081, imports no LiveKit SDK, and never
registers a worker or calls a provider. `/healthz` returns 200; `/readyz` always
returns 503 because this mode cannot handle calls. The JSON status lists missing
setting names only. Supplying every setting does not activate voice: explicitly
restart with `aida-agent start` after configuring and validating the providers.
Use `preview --host 127.0.0.1 --port 8081` for a local-only status listener.

The preview status service is internal and does not require an NPM hostname.
Production `start` and `dev` additionally expose the diagnostics described below.

## Source version and Pacific timezone

Worker diagnostics and preview `/healthz` and `/readyz` include `version` (`YYYY.M.D.H.M`), full Git
`revision`, `sourceUpdatedAt` (Pacific ISO 8601 offset), `timeZone`
(`America/Los_Angeles`), and `dirty`, alongside the existing preview fields.
Preview readiness still returns 503 and never claims voice is enabled.

The production worker's port 8081 `/` is owned by LiveKit and retains its existing
SDK health semantics. Use `aida-agent version` (or `--version`) to inspect the
same installed artifact identity in worker mode, for example:

```sh
docker compose exec aida-agent aida-agent version
```

This command, like preview, does not import/register the voice SDK or call providers.
The diagnostic listener is internal to the Docker network. Python package builds (`pip install .` or
wheels) stamp HEAD's committer timestamp, not build time. Versions are always Pacific
(PST/PDT); runtime local timezone defaults to Pacific and respects an explicit `TZ`.
Docker includes timezone data. Existing UTC transcript/protocol timestamps preserve
their storage semantics. Source-only development reports `unbuilt` with null metadata.

The commit clock belongs to the machine creating the commit (including GitHub for
web-created commits). Rebuilding the same commit keeps its version; dirty source
adds `-dirty`. Full `revision` distinguishes commits in the same minute and the
repeated autumn DST hour. Package SemVer stays separate from the source version.

Containers and source archives must supply `BUILD_REVISION` (full SHA),
`SOURCE_DATE_EPOCH` (committer epoch), and `BUILD_DIRTY` (`true` or `false`). Missing
or malformed identity fails the build. The wrapper reads these from the checkout:

```sh
scripts/with-build-info.sh sh -c 'docker build --target runtime \
  --build-arg BUILD_REVISION --build-arg SOURCE_DATE_EPOCH --build-arg BUILD_DIRTY \
  -t aida-agent:local .'
scripts/with-build-info.sh docker compose up -d --build
```

External orchestrators building this Dockerfile must forward the same build args.
Installed packages need neither Git nor runtime version environment variables.


## Worker HTTP diagnostics

Production `start` and `dev` serve an application-owned HTTP listener on
`AIDA_STATUS_HOST=0.0.0.0`, `AIDA_STATUS_PORT=8082`. Compose exposes it on the private
Docker network without publishing a host port. The SDK keeps its existing 8081
listener and health semantics. The application port must differ from the SDK port.

- `GET /healthz` or `/status`: 200 while the worker event loop and HTTP listener
  are responding, including during startup or LiveKit reconnection.
- `GET /readyz`: 200 only after WebSocket registration, while that socket is open,
  the worker is not draining/stopping, and the SDK's loopback health check returns
  200. Otherwise 503. The SDK probe has a 500 ms timeout and ignores proxy env vars.

Every response has `Cache-Control: no-store`, the source version fields, `mode`,
Pacific `startedAt`, `uptimeSeconds`, `draining`, and aggregate diagnostics:

```json
{
  "connection": {"state": "connected", "connected": true, "connections": 2},
  "sessions": {
    "started": 12,
    "active": 2,
    "processed": 10,
    "completed": 9,
    "failed": 1,
    "scope": "since_process_start"
  }
}
```

Connection state is `starting`, `connecting`, `connected`, `reconnecting`, `failed`,
`stopping`, or `stopped`. `connections` counts successful registered socket sessions,
including reconnections. A dropped socket immediately makes readiness false; a
previous successful registration alone never implies current connectivity.

Session counts refer to **dispatched LiveKit jobs**, observed in the parent worker
across its job subprocesses. `processed = completed + failed`; processed excludes
currently active jobs. These are SDK outcomes: an admission rejection or normal
hangup can end a job successfully, so completed does not mean a successful business
call. Counters reset on process restart and are per replica, not database totals.
No call IDs, room names, credentials, provider errors, or transcripts are returned.

```sh
# Query the running worker from its container without exposing another public port:
scripts/with-build-info.sh docker compose exec aida-agent python -c \
  'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8082/healthz").read().decode())'
```

`monitored_server.py` isolates three internal hooks in pinned `livekit-agents==1.8.0`
to observe registered WebSocket lifetime and parent-process job status; the SDK has
no public disconnect/job-status events. A version guard requires review when the SDK
is upgraded. Offline tests exercise the actual SDK against a loopback WebSocket
server, readiness during reconnect/drain, job completion/failure deduplication, and
listener cleanup. The SDK's dispatch, retries, job execution and shutdown handlers
still perform their original work.
