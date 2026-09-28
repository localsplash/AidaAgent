# Admin history catch-up

AidaAdmin observers call the active agent's `get_transcript` RPC after each join or
reconnect, then merge `lk.transcription` text streams. Deploy this agent contract
before the matching Admin version. Existing handset `transcript` data packets
retain their previous format and delivery path.

`TranscriptHistory` reads committed caller/assistant text from `session.history`.
It excludes instructions, tools, audio, configuration and metrics. It also includes
a pending caller utterance so an observer joining mid-turn can catch up immediately.
The SDK's internally generated transcription IDs are not ChatMessage IDs, so this
adapter publishes the standard `lk.transcription` topic itself (RoomIO text output
stays disabled to avoid duplicates). It preserves the caller turn's segment ID
through STT revisions and history commit. Assistant messages use their history ID.
Assistant output is sent when committed, as in the existing handset feed.

Text-stream attributes are `lk.segment_id`, `lk.transcription_final`,
`aida.call_id`, `aida.speaker` (`caller` or `assistant`), and monotonic
`aida.sequence`. The sender is always the bound agent. Each stream contains a
complete revision of that segment; chunks within that stream append. New revisions
replace the segment text. Observers must buffer updates during catch-up and merge
by segment ID and revision, never replacing a final with a partial.

The RPC accepts `{}` for the first page and returns
`{snapshotId, text, nextOffset}`. `text` is a piece of an ASCII-escaped JSON snapshot:
`{callId, items: [{id, segment_id, role, content: [text], is_final, sequence}]}`.
If `nextOffset` is non-null, request `{snapshotId, offset: nextOffset}` and concatenate
`text` until the final page, then parse the snapshot. Snapshots are frozen per
observer for 60 seconds, discarded after their final page, bounded to 2 MiB, and
limited to 32 simultaneous transfers. Pages fit the 15 KiB RPC response limit,
including JSON escaping. Empty history is a valid snapshot. Failure must be shown
as unavailable history, not a successfully caught-up empty transcript.

The RPC checks every caller against the room's current participants. Only a
server-minted `admin-observer-*` identity with the signed, immutable attribute
`aida.transcriptObserver` equal to the room name is accepted. Admin grants data
publication (needed for RPC) but prohibit media publication/subscription and
metadata changes. Observers are visible because hidden participants cannot RPC.
The agent remains linked to the admitted SIP caller and ignores participant-origin
call-control packets. Transcript delivery errors never terminate the telephone call.

History is in session memory only and disappears when the agent session ends.
No transcript persistence or content logging is introduced. Test coverage includes
partial/final identity, history filtering, multi-page Unicode snapshots, concurrent
turns during transfer, caller scoping, and cleanup.
