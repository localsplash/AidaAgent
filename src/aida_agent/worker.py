"""LiveKit SDK adapter. No identity database, carrier API, or room-admin client."""

import logging
import os
import re
import sys

from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, inference, room_io
from livekit.plugins import silero

from .config import CallConfiguration, DeploymentConfiguration, InvalidConfiguration
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
        call = CallConfiguration.parse(ctx.job.metadata, ctx.room.name)
        deployment = DeploymentConfiguration.from_env(os.environ)
    except InvalidConfiguration:
        logger.warning("agent job rejected: invalid configuration")
        ctx.shutdown(reason="invalid configuration")
        return

    session = create_session(deployment, call, ctx.proc.userdata["vad"])
    stream = TranscriptStream(call.call_id)
    control = ControlHandler(
        call.call_id, session, ctx.room.disconnect,
        lambda: ctx.shutdown(reason="human takeover"),
        failed_statement=call.failed_transfer_statement,
    )
    publisher: TranscriptPublisher | None = None

    @session.on("user_input_transcribed")
    def on_caller(event):
        if publisher is not None and not control.stopping:
            publisher.enqueue(stream.caller(event.transcript, event.is_final))

    @session.on("conversation_item_added")
    def on_item(event):
        # User history items would duplicate STT finals. Assistant history is the
        # SDK's spoken/committed text, including its interruption reconciliation.
        if publisher is not None and event.item.role == "assistant":
            publisher.enqueue(stream.assistant(event.item.text_content or "", event.item.id))

    @ctx.room.on("data_received")
    def on_data(packet):
        control.receive(packet)

    @session.on("close")
    def on_close(_event):
        ctx.shutdown(reason="agent session closed")

    async def cleanup():
        ctx.room.off("data_received", on_data)
        if publisher is not None:
            await publisher.close()
        await control.close()
        await session.aclose()

    ctx.add_shutdown_callback(cleanup)
    await ctx.connect()
    if control.stopping:
        return
    # The RTC SDK has no local_participant before connect() completes.
    publisher = TranscriptPublisher(
        ctx.room.local_participant,
        lambda: ctx.shutdown(reason="transcript delivery unavailable"),
    )
    publisher.start()
    await session.start(
        agent=Agent(instructions=call.instructions()), room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_SIP],
            text_input=False, text_output=False, video_input=False,
            close_on_disconnect=True, delete_room_on_close=False,
        ),
        record=False,
    )
    if not control.stopping:
        if call.opening_statement:
            session.say(call.opening_statement, allow_interruptions=True)
        else:
            session.generate_reply(
                instructions="Greet the caller briefly in the configured locale and ask how you "
                "can help.",
                allow_interruptions=True,
            )


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
        except InvalidConfiguration as error:
            raise SystemExit(str(error)) from None
    cli.run_app(make_server())


if __name__ == "__main__":
    main()
