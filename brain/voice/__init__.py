"""Winston voice subsystem — STT, TTS, audio pipeline, voice engine.

Lives next to brain/ (not inside it) because it's an alternative input/
output channel, not part of the LLM brain itself. Both the commentary
engine (text loop) and the voice engine (audio loop) speak to the same
brain.client and the same memory.

See voice_engine.py for the orchestrator, audio.py for the mic/speaker
plumbing, stt.py for faster-whisper, tts.py for the TTS providers.
"""