# AidaAgent

Python LiveKit worker for the unified Echo/Aida office platform. OfficePulse
dispatches one screening agent to `aida-<callSessionId>`. The worker listens to
the caller's SIP audio, responds using LiveKit Inference, streams transcripts to
AidaHandset, and leaves when OfficePulse confirms a human has answered.

This repository previously contained only a README. This is the first runnable
MVP; real audio, carrier bridging, and Android end-to-end behavior still require
a LiveKit/PBX acceptance call.

## Ownership

- **Identity** owns users, organizations, memberships, and sign-in sessions.
- **OfficePulse** checks tenant access, resolves business settings, owns calls and
  device access, creates LiveKit rooms/tokens, and performs PBX takeover.
- **AidaAgent** receives an authorized per-call configuration. It has no database,
  Identity credentials, carrier API, room deletion, or participant-removal API.
- **AidaHandset** displays live transcripts using a tenant-authorized room token
  from OfficePulse.

The worker trusts dispatch metadata from an operator-controlled LiveKit project.
Tenant/room validation does not replace OfficePulse authorization. Keep dispatch
and room-admin credentials on servers. Each business call has separate state.

## Run

Requires Docker Compose, a LiveKit project with Inference access, and the
OfficePulse/PBX SIP path into that project's rooms.

```sh
cp .env.example .env
# Set LiveKit credentials and review model/voice choices in .env.
docker compose build
docker compose up -d
docker compose ps
```

This is an **outbound worker**: NPM needs no `*.localsplash.dev` hostname for it.
Port 8081 is exposed only on its Docker network for SDK process health. Healthy
process status does not validate SIP audio, dispatch, or inference.

`AIDA_AGENT_NAME` defaults to `aida-prime` and must match OfficePulse's
`LIVEKIT_AGENT_NAME`. Alongside a pre-existing cloud agent, use `aida-prime-dev`
in both deployments. Retire the previous worker before reusing its name:
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

## Dispatch contract v1

OfficePulse sends JSON as **job dispatch metadata**, not room/participant
metadata. Missing `schemaVersion` means v1; explicit `schemaVersion: 1` is also
accepted. Other versions and unknown fields are rejected before room connection
or provider initialization.

```json
{
  "schemaVersion": 1,
  "callSessionId": "281c6b8e-6a61-45ba-9165-eb199825d12e",
  "tenantId": "42",
  "businessName": "Example Office",
  "prompt": "Ask why the caller is calling and offer to take a message.",
  "tone": "friendly",
  "objective": "Understand what the caller needs",
  "openingStatement": "Thank you for calling Example Office. How can I help?",
  "transferStatement": "I will check whether someone is available.",
  "failedTransferStatement": "No one is available. May I take a message?",
  "locale": "en-US",
  "didE164": "+15551234567"
}
```

Required: `callSessionId`, `tenantId`, `businessName`, `prompt`, `locale`,
`didE164`. Remaining fields are optional strings, except the integer version.
UUIDs use canonical lowercase hyphenated form. Tenant IDs are positive canonical
decimal strings or integers within JavaScript's safe range. JSON is limited to
16 KiB; prompt text to 12,000 characters; business name/tone to 256; other optional
text to 2,048. Duplicate keys, nulls, booleans as numbers, wrong rooms, and
noncanonical IDs are rejected.

`transferStatement` is reserved for a future pre-bridge announcement. The
current contract sends only bridge success/failure. This worker does not initiate
transfers or claim they happened. On bridge success it stops immediately so the
human can speak. There is no bootstrap-token endpoint or second settings store.

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
- [Platform specification](https://github.com/localsplash/AidaInfrastructureSetupInstructions/blob/main/docs/AIDA_VOICE_PLATFORM_TECHNICAL_SPECIFICATION.md)
