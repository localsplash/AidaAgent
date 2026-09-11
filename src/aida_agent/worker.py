"""LiveKit SDK adapter. No identity database, carrier API, or room-admin client."""

import asyncio
import json
import logging
import os
import re
import sys

from livekit import rtc
from livekit.agents import (
    Agent, AgentServer, AgentSession, AutoSubscribe, JobContext, cli, inference, room_io,
)
from livekit.plugins import silero

from .admission import SipAdmission
from .bootstrap import BootstrapClient
from .config import (
    BootstrapConfiguration, CallConfiguration, DeploymentConfiguration,
    DispatchConfiguration, InvalidConfiguration,
)
from .control import ControlHandler
from .transcripts import TranscriptPublisher, TranscriptStream

logger = logging.getLogger("aida_agent")


def prewarm(proc):
    # Silero's model is included in the pinned wheel; no model download or paid API.
    proc.userdata["vad"] = silero.VAD.load()


def create_session(deployment: DeploymentConfiguration, call: CallConfiguration, vad):
    language = call.locale.split("-")[0].lower()
    return AgentSession(
        stt=inference.STT(model=deployment.stt_model, language=language),
        llm=inference.LLM(model=deployment.llm_model),
        tts=inference.TTS(model=deployment.tts_model, voice=deployment.tts_voice, language=language),
        vad=vad,
        turn_handling={
            "turn_detection": "vad",
            "interruption": {"enabled": True},
            "preemptive_generation": {"enabled": False},
        },
    )


async def entrypoint(ctx: JobContext):
    try:
        dispatch = DispatchConfiguration.parse(ctx.job.metadata, ctx.room.name)
        deployment = DeploymentConfiguration.from_env(os.environ)
        bootstrap = BootstrapConfiguration.from_env(os.environ)
    except InvalidConfiguration:
        logger.warning("agent job rejected: invalid configuration")
        ctx.shutdown(reason="invalid configuration")
        return

    session = None
    publisher = None
    startup = None
    aborted = False
    cleaned = False
    control = ControlHandler(
        dispatch.call_id, None, ctx.room.disconnect,
        lambda: ctx.shutdown(reason="human takeover"), ready=False,
    )

    def silence():
        if session is not None:
            session.input.set_audio_enabled(False)
            session.output.set_audio_enabled(False)
            try:
                session.interrupt(force=True)
                session.shutdown(drain=False)
            except RuntimeError:
                pass

    def abort():
        nonlocal aborted
        if aborted or cleaned:
            return
        aborted = True
        silence()
        if startup is not None and not startup.done():
            startup.cancel()
        ctx.shutdown(reason="call admission ended")

    admission = SipAdmission(ctx.room, bootstrap.route_token_attribute, abort)

    @ctx.room.on("data_received")
    def on_data(packet):
        if not aborted:
            control.receive(packet)
            if control.stopping:
                abort()

    async def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        control.ready = False
        admission.close()
        ctx.room.off("data_received", on_data)
        silence()
        if startup is not None and not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        # Each close is bounded; a provider cannot hold the worker indefinitely.
        for resource in (publisher, control, session):
            if resource is not None:
                try:
                    close = resource.aclose if resource is session else resource.close
                    await asyncio.wait_for(close(), timeout=2)
                except Exception:
                    pass

    ctx.add_shutdown_callback(cleanup)

    async def start_authorized():
        nonlocal session, publisher
        # One deadline covers join, SIP attributes, HTTP, session start and ready delivery.
        async with asyncio.timeout(bootstrap.timeout_seconds):
            await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE)
            leg = await admission.wait()
            call = await BootstrapClient(bootstrap).authorize(dispatch, ctx.room.name, leg)
            admission.validate()
            if aborted or control.stopping:
                return
            session = create_session(deployment, call, ctx.proc.userdata["vad"])
            session.input.set_audio_enabled(False)
            session.output.set_audio_enabled(False)
            control.session = session
            control.failed_statement = call.failed_transfer_statement
            stream = TranscriptStream(call.call_id)
            publisher = TranscriptPublisher(ctx.room.local_participant, abort)

            @session.on("user_input_transcribed")
            def on_caller(event):
                if control.ready and not aborted and not control.stopping:
                    publisher.enqueue(stream.caller(event.transcript, event.is_final))

            @session.on("conversation_item_added")
            def on_item(event):
                if (control.ready and not aborted and not control.stopping
                        and event.item.role == "assistant"):
                    publisher.enqueue(stream.assistant(event.item.text_content or "", event.item.id))

            @session.on("close")
            def on_close(_event):
                abort()

            await session.start(
                agent=Agent(instructions=call.instructions()), room=ctx.room,
                room_options=room_io.RoomOptions(
                    participant_identity=leg.identity,
                    participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_SIP],
                    text_input=False, text_output=False, video_input=False,
                    close_on_disconnect=True, delete_room_on_close=False,
                ),
                session_host=False, record=False,
            )
            admission.validate()
            if aborted or control.stopping:
                return
            local = ctx.room.local_participant
            if not local.identity or not local.sid:
                raise InvalidConfiguration("agent identity unavailable")
            await local.publish_data(json.dumps({
                "type": "aida.event.agent_ready", "schemaVersion": 1,
                "callSessionId": dispatch.call_id,
                "agentIdentity": local.identity, "agentParticipantSid": local.sid,
            }).encode(), topic="aida.event.agent_ready", reliable=True)
            admission.validate()
            if aborted or control.stopping:
                return
            # No await between the final checks and enabling normal turns.
            publisher.start()
            control.ready = True
            session.output.set_audio_enabled(True)
            session.input.set_audio_enabled(True)
            if call.opening_statement:
                session.say(call.opening_statement, allow_interruptions=True)
            else:
                session.generate_reply(
                    instructions="Greet the caller briefly in English and ask how you can help.",
                    allow_interruptions=True,
                )

    startup = asyncio.create_task(start_authorized())
    try:
        await startup
    except asyncio.CancelledError:
        # SIP loss/takeover and SDK job shutdown cancel all pending startup work.
        abort()
    except Exception:
        # Upstream/provider errors can contain credentials or prompts: log no payload.
        logger.warning("agent job rejected: call admission failed")
        abort()
    finally:
        if aborted or control.stopping:
            await cleanup()
            try:
                await asyncio.wait_for(ctx.room.disconnect(), timeout=2)
            except Exception:
                pass


def make_server() -> AgentServer:
    agent_name = os.environ.get("AIDA_AGENT_NAME", "aida-prime")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", agent_name):
        raise InvalidConfiguration("invalid AIDA_AGENT_NAME")
    server = AgentServer(setup_fnc=prewarm, num_idle_processes=1, log_level="INFO")
    server.rtc_session(agent_name=agent_name)(entrypoint)
    return server


def main():
    # Help/import/download-files work without credentials or contacting providers.
    if any(arg in ("start", "dev") for arg in sys.argv[1:]) and "--help" not in sys.argv:
        try:
            DeploymentConfiguration.from_env(os.environ)
            BootstrapConfiguration.from_env(os.environ)
        except InvalidConfiguration as error:
            raise SystemExit(str(error)) from None
    cli.run_app(make_server())


if __name__ == "__main__":
    main()
