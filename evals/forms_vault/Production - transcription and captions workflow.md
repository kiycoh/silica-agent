---
type: note
---
# Production - transcription and captions workflow

1. Export the edit audio as 48kHz mono WAV.
2. Whisper large-v3 locally, overnight batch. About 4 minutes per 40 minutes of audio.
3. Hand-fix proper nouns. The model still cannot spell sponsor names.
4. Burn captions in Resolve, export the SRT for the upload.

Documented in [[ep-171 I built a zero-cost editing pipeline]].
