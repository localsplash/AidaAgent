# AidaAgent

Implement the predefined LiveKit Cloud agent aida-prime (CA_Lbh5CTq2Rxhd).

## Responsibilities

- Inherit LiveKit-managed model, STT, and TTS defaults
- Apply allowlisted session prompt and business-context overrides
- Publish normative transcript and speech-lifecycle topics
- Support caller barge-in during screening
- Reject new turns after answer and leave by the drain deadline

## Stack

Python, LiveKit Agents/Cloud, pytest, Docker, GitHub Actions

## System specification

[Canonical Aida Voice Platform specification](https://github.com/localsplash/AidaInfrastructureSetupInstructions/blob/main/docs/AIDA_VOICE_PLATFORM_TECHNICAL_SPECIFICATION.md)

## Project invariant

Default tests use fake AI and LiveKit providers and incur no third-party charges.
