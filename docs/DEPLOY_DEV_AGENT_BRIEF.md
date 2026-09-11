# Optimus task: deploy AidaAgent on the development server

Deploy the GitHub `dev` revision of `localsplash/AidaAgent` on
`dockerappvm01.localsplash.dev`. Generate the required environment configuration
using approved secret/configuration sources. Preserve the canonical single Agent
service and distinguish a status-only deployment from an enabled voice worker.

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

`start` runs the LiveKit worker, registering outbound under `AIDA_AGENT_NAME`.
LiveKit assigns a job; the worker validates dispatch, joins its room, observes
the SIP leg, authorizes bootstrap/route credentials over HTTPS, starts the muted
voice session, publishes readiness, then enables conversation. STT/LLM/TTS use
LiveKit Inference; Silero VAD runs in the worker. OfficePulse owns PBX routing and
fallback. The worker neither reads a database/NocoDB nor controls other call legs.

## Deployment steps

1. Inspect Git status and remote `dev`. Preserve any unrelated local work. Fetch
   via `git@github.com:localsplash/AidaAgent.git`, and build an exact fetched `dev`
   commit from a clean checkout/worktree. Record the full SHA.
2. Preserve the current Compose configuration and any existing secret file
   without copying their contents into task output. Record the current container
   image ID and keep that image for rollback; do not prune it.
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
   LIVEKIT_URL=wss://<development-project>
   LIVEKIT_API_KEY=<development-key>
   LIVEKIT_API_SECRET=<development-secret>
   AIDA_AGENT_NAME=aida-prime-bootstrap-dev
   AIDA_STT_MODEL=<approved-STT-model>
   AIDA_LLM_MODEL=<approved-LLM-model>
   AIDA_TTS_MODEL=<approved-TTS-model>
   AIDA_TTS_VOICE=<approved-voice-id>
   AIDA_BOOTSTRAP_URL=https://<OfficePulse-bootstrap-origin>
   AIDA_ROUTE_TOKEN_ATTRIBUTE=sip.aidaRouteToken
   AIDA_BOOTSTRAP_TIMEOUT_SECONDS=30
   ```

   These are placeholders, not deployable values. Match the agent name exactly
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
  `{callSessionId, bootstrapToken}` to the matching Agent name and LiveKit project.
- `POST /v1/agent/calls/{callSessionId}/bootstrap` authenticates the call-scoped
  bearer, independently validates the observed SIP leg, atomically consumes both
  credentials, and returns the immutable, versioned profile. Replay, expiry,
  tenant/call/room/SID mismatches and concurrent consumption are tested server-side.
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

## Acceptance and rollback

Use approved development test calls. Confirm ready once after authorization,
English business prompt/greeting, caller audio and barge-in, transcripts on the
handset, startup/active human takeover, and failed takeover. Exercise invalid,
missing, expired and reused credentials; each must prevent normal turns and
produce the actual local PBX fallback. Test two businesses for isolation. Do not
claim real-provider/PBX acceptance based on fake-provider unit tests.

If verification fails, silence/stop only this Agent service and restore the prior
image/configuration or preview mode. Restore the corresponding healthcheck.
Producer/consumer contracts must roll back together; never restart an incompatible
inline-metadata worker against a token-only producer. Do not delete LiveKit rooms,
remove human/SIP participants, restart the whole platform, or modify live bridges.

Report: source SHA, image tag/ID, Compose path, command/mode, health result,
registration/acceptance evidence, and remaining blockers. Report no secret values.
