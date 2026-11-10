# Voice clips

This folder holds the reference clips that ZonosVoice clones. The mod does not include any clips.

Make them from your own copy of Morrowind with one command:

    uv run zonosvoice.py extract-game-voices

Or add clips by hand (see "Choose voice clips by hand" in `../README.md`).

## Layout

| Folder | Use |
|---|---|
| `<race>_<sex>/` | Voices for NPCs of one race and sex, for example `darkelf_m/` or `khajiit_f/`. Each clip is one voice. An NPC always gets the same clip of its folder. |
| `npc/` | One clip for one character. The file name is the NPC or creature id, for example `npc/caius cosades.wav` or `npc/dagoth_ur_1.wav`. |
| `creature/` | Voices for talking creatures that have no clip in `npc/`. |
| `narrator/` | The voice that reads books. The first clip is used. |

Race folder names: `argonian`, `breton`, `darkelf`, `highelf`, `imperial`, `khajiit`, `nord`, `orc`, `redguard`,
`woodelf`. Sex: `m` or `f`.

Clips can be `.wav`, `.mp3`, `.ogg` or `.flac`. A good clip has one speaker, 8 to 12 seconds of calm speech, and no
music or noise.

Clips that you make from the game are for your own game only. Do not share or upload them.
