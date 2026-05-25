# Serenity Voice Clone Drop Zone

Drop an audio file here and Serenity will clone that voice for all TTS output.
Remove it and she reverts to her default preset voice (Cherry).

---

## How to use

1. Record or find a clean audio clip of the voice you want to clone
2. Drop the file into this folder (any name, any of the formats below)
3. Restart Serenity — she will detect the file automatically on next TTS call
4. To go back to the default voice, delete or move the file out of this folder

Only **one file** should be in this folder at a time.
If there are multiple, Serenity picks the most recently modified one.

---

## Audio requirements

| Thing        | Recommendation                                      |
|--------------|-----------------------------------------------------|
| **Length**   | 5–30 seconds is ideal. 3 s minimum, 60 s maximum.  |
| **Content**  | Clear speech, no background music, minimal noise    |
| **Format**   | `.wav`, `.mp3`, `.flac`, `.ogg`, `.m4a`             |
| **Sample rate** | Any (Serenity resamples to 24 kHz automatically) |
| **Channels** | Mono or stereo (stereo is mixed down automatically) |

**Longer = better clone quality up to ~30 seconds.**
Past 30 seconds there is no noticeable improvement.

---

## What happens under the hood

1. Serenity detects the file and switches to `Qwen/Qwen3-TTS-12Hz-1.7B-Base`
   (the voice-cloning variant of Qwen3 TTS — open-weight, Apache 2.0, runs on your GPU)
2. The audio is passed as a reference prompt to the model
3. All speech output will match the timbre, accent, and rhythm of the reference voice
4. When you remove the file, she switches back to `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`
   and uses the preset voice configured in `config.json` (default: Cherry)

---

## GPU vs CPU

- **GPU (CUDA)** — strongly recommended. ~97 ms first audio, real-time synthesis
- **CPU** — works but is slow (~5–15 seconds per sentence depending on CPU)

Serenity always tries GPU first and falls back to CPU automatically.

---

## Tips

- A quiet room recording (phone memo app works fine) gives excellent results
- Avoid clips with background music or multiple speakers
- The model clones prosody (rhythm/pacing) as well as timbre — use a clip
  that reflects how you want Serenity to *speak*, not just what she sounds like
- This folder is ignored by git (`.gitignore`) — your voice file stays private

---

## Voices available without cloning

If this folder is empty, Serenity uses a preset voice from the CustomVoice model:

| Voice   | Character                    |
|---------|------------------------------|
| Cherry  | Warm, natural female (default) |
| Vivian  | Bright, expressive female    |
| Ryan    | Friendly, natural male       |
| Sohee   | Soft, calm female            |
| Alloy   | Neutral, clear               |
| Echo    | Deep, resonant               |
| Fable   | Storytelling, warm male      |
| Onyx    | Deep, authoritative male     |
| Nova    | Energetic, young female      |

Change the preset voice in `~/.serenity/config.json`:

```json
"voice": {
  "ttsProvider": "qwen3-local",
  "ttsLocalVoice": "Nova"
}
```
