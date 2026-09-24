# Optimus task: deploy AidaAgent on the development server

> Note: this brief predates the context migration (AidaAgent #12). Its verified
> facts are from 2026-09-11; the bootstrap contract is now v2 with dispatch scope
> `{pbxInstanceId, context}` (see [BOOTSTRAP_CONTRACT.md](BOOTSTRAP_CONTRACT.md)).
>
> Superseded 2026-09-18: the `/opt/platform-local/agent` project and its
> `aida-agent-local` container were removed. The dev host now runs this
> repository's Compose project from `/opt/aida/AidaAgent` (container
> `aidaagent-aida-agent`, with `compose.proxy.yaml` for `npm_network`); see the
> README "Run" section.

Deploy the GitHub `dev` revision of `localsplash/AidaAgent` on
`dockerappvm01.localsplash.dev`. Generate the required environment configuration
using approved secret/configuration sources. Preserve the canonical single Agent
service and distinguish a status-only deployment from an enabled voice worker.
This is an initial development deployment; AidaAgent has never been live. No
rollback plan, configuration backup, or retention of a previous image is required.

## Verified starting point (2026-09-11)

- Source: `/opt/aida/AidaAgent`; Python package `aida_agent`.
- Canonical Compose project: `/opt/platform-local/agent/compose.yaml`.
- Project/service/container: `platform-agent-local` / `agent` / `aida-agent-local`.
- External Docker network: `platform-preview`; alias `aida-agent-local`.
- Existing image reference: `aida-agent:local`; current image ID at inspection:
  `sha256:45a5d4561de2f8419082f377712848e45d711bf8b3b6e17f6beaea69e1e53086`.
- Command: `[preview]`; runtime user: `aida` (image UID 10001).
- No host ports or persistent volume. Internal process-health port: 8081.
- The canonical Compose file currently has **no `env_file` entry**. Creating a
  `.env` alone will not inject variables into this container.

Reinspect these facts before making changes; another task may have updated them.
Do not print environment values, run unrestricted `docker inspect`, or render
an interpolated `docker compose config` into logs.

## Runtime explanation

The Dockerfile builds from Python 3.12 slim, copies `src`, and installs the Python
package with `pip install .`. The runtime entrypoint is `aida-agent`, which calls
`aida_agent.cli:main`. There is no source bind mount or hot reload. A source merge
changes GitHub only; rebuilding and recreating the container installs the change.

`preview` runs a standard-library HTTP status server. It does not import the
LiveKit SDK, register for jobs, or call model providers. `/healthz` is 200 and
`/readyz` is intentionally 503, even if every environment setting is present.

`start` runs the LiveKit worker, registering outbound under `LIVEKIT_AGENT_NAME`.
LiveKit assigns a job; the worker validates dispatch, joins its room, observes
the SIP leg, authorizes bootstrap/route credentials over HTTPS, starts the muted
voice session, publishes readiness, then enables conversation. STT/LLM/TTS use
LiveKit Inference; Silero VAD runs in the worker. OfficePulse owns PBX routing and
fallback. The worker neither reads a database/NocoDB nor controls other call legs.

## Deployment steps

1. Inspect Git status and remote `dev`. Preserve any unrelated local work. Fetch
   via `git@github.com:localsplash/AidaAgent.git`, and build an exact fetched `dev`
   commit from a clean checkout/worktree. Record the full SHA.
2. Inspect the current Compose configuration and existing setting names without
   copying secret values into task output. Update the development deployment in place.
3. Build and validate from that checkout:

   ```sh
   docker build --target test -t aida-agent:dev-test-<short-sha> .
   docker build --target runtime -t aida-agent:dev-<short-sha> .
   ```

   Substitute the actual SHA in commands. Dependency installation needs network
   access; the Dockerfile runs tests and CLI checks with `--network=none`.
   Bootstrap implementation `51d425a` passed 184 tests, lint, and runtime build.
   Run the tests for the revision actually being deployed.

4. Generate `/opt/platform-local/agent/.env`, outside Git, with restrictive host
   permissions (`0600`, readable by the deployment operator). Use real values
   from approved sources; never invent credentials or copy production secrets
   merely because a development value is missing. The required settings are:

   ```dotenv
   LIVEKIT_URL=wss://officepulse-localsplash-dev-wx1v0ch5.livekit.cloud
   LIVEKIT_API_KEY=<development-key>
   LIVEKIT_API_SECRET=<development-secret>
   LIVEKIT_AGENT_NAME=aida-prime-bootstrap-dev
   AIDA_STT_MODEL=deepgram/nova-3-general
   AIDA_LLM_MODEL=google/gemma-4-31b-it
   AIDA_TTS_MODEL=deepgram/aura-2
   AIDA_TTS_VOICE=asteria
   OFFICEPULSE_API_BASE_URL=https://<OfficePulse-bootstrap-origin>
   AIDA_ROUTE_TOKEN_ATTRIBUTE=sip.aidaRouteToken
   AIDA_BOOTSTRAP_TIMEOUT_SECONDS=30
   ```

   The project URL and model/voice values above are the selected POC configuration.
   Replace credential and bootstrap-origin placeholders before voice activation.
   Match the agent name exactly
   to the new OfficePulse dispatcher; use a distinct name from existing workers.
   Match the route attribute to the actual LiveKit SIP trunk mapping for
   `X-Aida-Route-Token`. The bootstrap URL is an HTTPS origin without credentials,
   path, query, or fragment. Verify DNS/TLS/reachability from the container network.
   Do not put per-call bootstrap/route tokens in `.env`: OfficePulse mints them.
   If dedicated LiveKit Inference credentials are required, provision the SDK's
   `LIVEKIT_INFERENCE_API_KEY`/`LIVEKIT_INFERENCE_API_SECRET` through the same secret
   process. Report missing setting names only.

5. Update the existing canonical Compose service; do not start the repository's
   separate Compose project alongside it. Preserve project, container, alias,
   network and restart policy. Use the immutable `aida-agent:dev-<short-sha>` image,
   add `env_file: [.env]`, and set `stop_grace_period: 60s`. Keep `command: [preview]`
   until all voice-activation gates below pass. Compose should continue to expose
   no host port. Do not create an NPM hostname for this outbound worker.
6. Recreate only this service from `/opt/platform-local/agent`:

   ```sh
   docker compose up -d --no-deps agent
   docker compose ps agent
   ```

   Confirm the running image matches the recorded build SHA and the process runs
   as the non-root image user. Check status via the container/network, without
   printing secrets. In preview mode `/healthz` must return 200 and `/readyz` 503;
   report **deployed in status-only mode**, not voice-ready.

## Gates before changing `command` to `[start]`

Read [BOOTSTRAP_CONTRACT.md](BOOTSTRAP_CONTRACT.md) in the deployed revision.
The Agent contract deliberately rejects the old inline business metadata.
At OfficePulse dev `ddb064e28e21728ee7019e655e5378562eb13977`, native FastAGI
admission intentionally returns FALLBACK and the required endpoint is absent.
Recheck the current upstream revision; do not assume environment generation fixes
this code dependency. Leave the updated Agent in preview while it is absent.

Before enabling voice, verify all of the following:

- OfficePulse implements native call admission and dispatches exactly
  `{callSessionId, bootstrapToken, pbxInstanceId, context}` to the matching Agent
  name and LiveKit project (v1 `{callSessionId, bootstrapToken}` is rejected).
- `POST /v1/agent/calls/{callSessionId}/bootstrap` authenticates the call-scoped
  bearer, independently validates the observed SIP leg, atomically consumes both
  credentials, and returns the immutable `schemaVersion` 2 profile carrying the
  same `pbxInstanceId`/`context`. Replay, expiry, scope/call/room/SID mismatches
  and concurrent consumption are tested server-side; the Agent additionally fails
  closed on `bootstrap scope mismatch`.
- OfficePulse injects the SIP route header, and LiveKit exposes the agreed
  participant attribute. The SIP leg must be routed before waiting for the
  conversation-ready event, otherwise startup deadlocks.
- OfficePulse has a bounded readiness/failure watchdog that routes the caller to
  PBX-owned local fallback. Agent disconnection alone is not telephony fallback.
- No incompatible worker is registered under the same dispatch name; model/voice
  settings are explicitly configured and available to the development project.

When those gates pass, set `command: [start]`. Change the Compose healthcheck
from preview's `/healthz` to the SDK process-health root `/` on port 8081:

```yaml
healthcheck:
  test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/', timeout=2)"]
  interval: 15s
  timeout: 5s
  retries: 3
```

Recreate only `agent` and verify outbound registration under the selected name.
An SDK process-health 200 does not establish per-call readiness or working audio.

## Acceptance

Use approved development test calls. Confirm ready once after authorization,
English business prompt/greeting, caller audio and barge-in, transcripts on the
handset, startup/active human takeover, and failed takeover. Exercise invalid,
missing, expired and reused credentials; each must prevent normal turns and
produce the actual local PBX fallback. Test two businesses for isolation. Do not
claim real-provider/PBX acceptance based on fake-provider unit tests.

If verification fails, diagnose and fix the development deployment, rebuild as
needed, and repeat the affected checks. Keep voice disabled while the documented
OfficePulse prerequisites are missing. Producer and worker must use the same
bootstrap contract and dispatch name.

Report: source SHA, image tag/ID, Compose path, command/mode, health result,
registration/acceptance evidence, and remaining blockers. Report no secret values.


## Selected LiveKit UI settings

The user selected an STT–LLM–TTS pipeline: English Deepgram Nova-3 monolingual,
Gemma 4 31B, and Deepgram Aura-2 with the Asteria US-English voice. These map to
the environment values above; the Inference voice is `asteria`, not the direct
Deepgram plugin's combined `aura-2-asteria-en` model identifier. The English call
profile produces `language="en"` for STT and TTS. No keyterms are supplied;
keyterm recognition was disabled in the screenshot.

The screenshot also selects Quail VF S noise cancellation and Office background
audio. Those preferences are recorded but **not implemented** in this worker.
They need SDK/plugin integration, lifecycle/cleanup and takeover-silencing tests;
there are no supported environment variables for them yet. UI configuration does
not automatically apply to the repository-owned worker. Do not claim full audio
parity or invent environment variables to represent unsupported features.

References: [Gemma model ID](https://docs.livekit.io/agents/models/llm/),
[Deepgram Inference TTS](https://docs.livekit.io/agents/models/tts/deepgram/),
[noise cancellation](https://docs.livekit.io/transport/media/noise-cancellation/),
[background audio](https://docs.livekit.io/agents/multimodality/audio/background-audio/).

## OfficePulse environment handoff

“OfficePulseAidaInfrastructure” is interpreted here as the runtime service
`localsplash/OfficePulseAidaIntegration`. Verify the intended service before
editing any environment file. At dev `ddb064e28e21728ee7019e655e5378562eb13977`,
`src/config.ts` accepts the following LiveKit settings:

```dotenv
LIVEKIT_URL=wss://officepulse-localsplash-dev-wx1v0ch5.livekit.cloud
LIVEKIT_API_KEY=<full-key-for-this-development-project>
LIVEKIT_API_SECRET=<full-secret-for-this-development-project>
LIVEKIT_AGENT_NAME=aida-prime-bootstrap-dev
LIVEKIT_SIP_HOST=c01ntkak7mh.sip.livekit.cloud
LIVEKIT_TIMEOUT_MS=5000
LIVEKIT_TRUNK_ENDPOINT=<actual-Asterisk-PJSIP-endpoint-name>
```

`LIVEKIT_SIP_HOST` is the hostname only, without `sip:`. The PJSIP endpoint name
is a PBX configuration identifier, not that SIP hostname or the LiveKit project
ID (`p_c01ntkak7mh`). This runtime has no `LIVEKIT_PROJECT_ID` requirement.
Both services must authenticate to the same development LiveKit project.
`LIVEKIT_AGENT_NAME` in OfficePulse must equal `LIVEKIT_AGENT_NAME` in the Agent.
Keep `AIDA_STT_MODEL`, `AIDA_LLM_MODEL`, `AIDA_TTS_MODEL` and `AIDA_TTS_VOICE` on
the Agent; OfficePulse does not load those variables or send provider overrides.

Also resolve the actual deployment-specific settings already owned by OfficePulse:

- `OFFICEPULSE_INSTANCE_ID`: stable identity of the PBX integration instance.
- `ARI_URL`, `ARI_USERNAME`, `ARI_PASSWORD`, `ARI_APP`: actual reachable PBX ARI
  service/account and application (the default app is `aida`). `localhost` inside
  the integration container does not address a separate PBX host.
- `RUNTIME_MYSQL_HOST`, `RUNTIME_MYSQL_PORT`, `RUNTIME_MYSQL_USER`,
  `RUNTIME_MYSQL_PASSWORD`, `RUNTIME_MYSQL_DATABASE=aidacalls_db`: its runtime store.
  Keep this separate from native Asterisk inventory/provisioning database access.
- `NOCODB_BASE_URL`, `NOCODB_API_TOKEN`: approved PlatformConfig access. Effective
  settings resolve nonblank environment overrides over `officepulse`, `aida`, `*`.
  Preserve the deployment's selected configuration mode; explicit
  `PLATFORM_CONFIG_MODE=environment` bypasses PlatformConfig resolution and is
  not necessary simply to override these LiveKit values.
- `TRUSTED_SERVER_CIDRS`, `TRUSTED_PROXY_CIDRS`: actual private callers/proxies.
  The default private HTTP port is 8085, public HTTP port 8086, FastAGI port 4573.
  Do not publish the private administrative listener to provide agent bootstrap.

Keep `VOICE_ENABLED=false` for administration/status-only deployment until PBX,
LiveKit and the documented admission prerequisites are ready. `VOICE_ENABLED=true`
starts voice connectors but does not implement the missing native admission path.
Recreate the relevant container after changing environment/startup settings;
creating a host `.env` alone is insufficient unless Compose injects it.

The Agent's `OFFICEPULSE_API_BASE_URL` must point to the HTTPS origin exposing the new
call-credential-authenticated OfficePulse endpoint. This is an Agent-side variable;
OfficePulse has no matching magic environment switch that creates the endpoint.
Configure its public route and reverse proxy when implementing the endpoint.
Map `X-Aida-Route-Token` to `sip.aidaRouteToken` in the LiveKit SIP configuration,
and align the producer's token injection with that mapping. No existing OfficePulse
route-token-attribute environment variable is defined in the inspected revision.

No secret values were provided in this brief. The trimmed credentials shared in
conversation are not usable configuration; obtain the full values through the
approved secret source without placing them in Git or task output.
