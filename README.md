# ZonosVoice

ZonosVoice gives a voice to every NPC in Morrowind (OpenMW). It also reads books and scrolls aloud. The ZONOS3 Mini
text-to-speech model on Zyphra Cloud makes the audio while you play. No line is recorded in advance.

- Each NPC speaks with a voice for its race and sex, made from the game's own voice actors (from your copy of the
  game). An NPC always keeps the same voice.
- Greetings, topic answers and persuasion answers are spoken. Your character's name and the other text variables
  are filled in.
- Lines that already have a voice (vanilla lines, or mods that add audio) are not spoken again.
- Dagoth Ur, Vivec, Almalexia and Yagrum Bagarn speak with their own voices.
- A narrator reads a book or scroll when you open it, and stops when you close it.
- The NPC's lips move, because the game plays the audio as the NPC's voice.
- A line that was spoken once is kept on disk. The next time, it plays at once.

## What you need

- **OpenMW 0.51** (tested). Older versions are not tested.
- **Morrowind**, set up in OpenMW (the OpenMW launcher or wizard does this).
- **uv**, which runs the voice program and installs what it needs: https://docs.astral.sh/uv/getting-started/installation/
- **A Zyphra Cloud API key**: make an account at https://cloud.zyphra.com and make a key on the Keys tab. Zyphra
  Cloud bills the speech that the mod makes to your account. The mod uses the ZONOS3 Mini model (`zyphra/ZONOS3-Mini`).

## Set up

Do these steps once. Unzip the mod to any folder, and open a terminal in the `ZonosVoice` folder (on Windows:
PowerShell).

1. Save your Zyphra Cloud API key (or set the `ZYPHRA_API_KEY` environment variable):

       uv run zonosvoice.py key

2. Add the "OpenMW Launcher (with voices)" shortcut to your application menu (Linux), Start menu (Windows) or
   `~/Applications` (macOS):

       uv run zonosvoice.py shortcut

   `uv run zonosvoice.py shortcut --remove` removes it again. Nothing else is installed: no background program and
   no change to OpenMW's own files.

## Play

Start **OpenMW Launcher (with voices)** from your menu. It opens the normal OpenMW launcher. Press **Launch OpenMW**
as usual. The voices run while you play, and stop when you quit the game (or when you close the launcher without
playing).

At the first start, the shortcut also:

- adds ZonosVoice to `openmw.cfg` (it keeps a backup of the file), so the launcher shows it as ticked;
- makes the voice clips from your copy of Morrowind (about 5 seconds; see "Voices");
- asks for the API key in a small window, if you did not save it in step 1.

In a terminal, `uv run zonosvoice.py launcher` does the same as the shortcut. `uv run zonosvoice.py play` starts
the game without the launcher.

If you start the plain OpenMW launcher (or a mod manager), the game runs without voices. After 40 seconds of the
first dialogue, it shows "ZonosVoice: no voice after 40 s" once. To voice that session, start
`uv run zonosvoice.py run` in a terminal and keep it open while you play.

**Wait times.** The text shows in the dialogue window at once. The voice of a new line starts about 2 to 3 seconds
later (Zyphra Cloud). A line that was spoken before starts at once. Long lines are cut into parts at the sentence
ends. The first part is short, so the voice starts early, and the other parts are made while the first part plays.

The first game takes about a minute more: the program prepares each voice clip on Zyphra Cloud once (it makes the
clip's voice codes, and keeps them in the cache folder).

### In-game settings

Options → Scripts → ZonosVoice:

- **Speak dialogue**: turn the voices on or off.
- **Stop when the dialogue window closes**: stop the NPC's line when you leave the conversation.
- **Read books aloud**: read books and scrolls with the narrator voice.

## Voices

The shortcut makes the voice clips at its first start. You can also make them yourself with
`uv run zonosvoice.py extract-game-voices`. This command reads the voice lines of `Morrowind.esm` and their sound
files in your `Data Files` folder. It finds that folder from the `data=` lines in `openmw.cfg`. It writes clips
into `voices/`:

- **Race and sex.** In the base game, each race and sex has one voice actor. The program uses the actor's "Hello"
  lines. It keeps calm, full sentences (no shouting) and joins several of them into a clip of 10 to 12 seconds.
- **Manner.** Each race and sex gets three clips: warm, neutral and curt. They come from the friendly, normal and
  unfriendly greetings. An NPC always gets the same one, so NPCs of one race share the actor but differ in manner.
- **Characters.** Dagoth Ur, Vivec, Almalexia and Yagrum Bagarn get clips of their own recordings.
- **Creatures.** Other talking creatures get a deep voice, made from the Dark Elf male clip.
- **Expansions.** Only base-game lines are used. Some Tribunal and Bloodmoon lines come from other actors.

The clips come from your copy of the game. Keep them, and the audio that the mod makes, for your own game. Do not
share or upload them.

To see and hear the voices:

    uv run zonosvoice.py voices                                 # which clip each race and sex uses
    uv run zonosvoice.py samples                                # one line per race and sex into samples/
    uv run zonosvoice.py say --npc "dulnea ralaal" "Good day."  # one line in one NPC's voice

`samples` also writes `samples/<race>_<sex>_compare.wav`: about 5 seconds of the game's voice, a pause, then the
line that the mod made.

Without any clips, the mod still works: each race and sex then uses Zyphra Cloud's default voices (for example
`rupert` and `imogen`; see `config.example.toml`).

### Choose voice clips by hand

Use this method if `extract-game-voices` cannot find your game files, or if you want to choose the lines yourself.

1. Find the voice files in your Morrowind folder: `Data Files/Sound/Vo/<letter>/<m or f>/`.

   | Letter | Race | Letter | Race |
   |---|---|---|---|
   | `a` | Argonian | `k` | Khajiit |
   | `b` | Breton | `n` | Nord |
   | `d` | Dark Elf | `o` | Orc |
   | `h` | High Elf | `r` | Redguard |
   | `i` | Imperial | `w` | Wood Elf |

   The greetings are the `Hlo_*.mp3` files. Unique characters are in `Sound/Vo/Misc` (for example `Yagrum_1.mp3`).

2. Listen, and choose 3 to 5 lines that are calm full sentences. Do not use shouts, laughs, attack or hit sounds.

3. Join the lines into one clip of 8 to 12 seconds, with a short pause between them. Any audio editor can do this.
   With ffmpeg:

       ffmpeg -i Hlo_DM221.mp3 -i Hlo_DM219.mp3 -i Hlo_DM224.mp3 -filter_complex "[0][1][2]concat=n=3:v=0:a=1" warm.wav

4. Put the clip into the right folder of `voices/`:

   | Folder | Use |
   |---|---|
   | `voices/<race>_<sex>/` | NPCs of one race and sex, for example `voices/darkelf_m/warm.wav`. Each clip is one voice. |
   | `voices/npc/` | One character. The file name is the NPC id, for example `voices/npc/caius cosades.wav`. |
   | `voices/creature/` | Talking creatures that have no clip in `voices/npc/`. |
   | `voices/narrator/` | The voice that reads books. |

   Race folder names: `argonian`, `breton`, `darkelf`, `highelf`, `imperial`, `khajiit`, `nord`, `orc`, `redguard`,
   `woodelf`. Sex: `m` or `f`. Clips can be `.wav`, `.mp3`, `.ogg` or `.flac`.

5. Run `uv run zonosvoice.py voices` to check, then restart the voice program.

You can also use any other voice that you have the right to use. `extract-game-voices` does not replace clips
that are already in `voices/` (use `--force` to make them again).

## Cache and API use

Each line that the TTS makes is kept on disk (the cache) and plays at once the next time. The cache key is the
voice clip and the text, so a line is made once per voice clip, not once per NPC.

- **Generic lines** are lines that any NPC who matches the filters can say, for example "What do you want?" or the
  answers to "latest rumors". A Dark Elf male can say the same generic line as hundreds of other Dark Elf males.
  These lines use one shared clip per race and sex (`neutral.wav`), so each one is made once per race and sex and
  then comes from the cache for every NPC. The NPC still speaks with the same actor, because all clips of a folder
  come from one actor; only the manner of the generic lines is neutral.
- **Lines for one character** (for example Caius Cosades' quest lines) use that NPC's own clip.

At the end of each game session, the voice program writes a summary to its log (the terminal, or `launcher.log` for
the shortcut), for example:

    session summary: 5 lines (3 generic, 2 one speaker, 0 books), 7 parts: 2 from the cache (29%, 0.1 min of
    audio), 5 made by the TTS (0.7 min of audio, 5 API requests), 0 not made (line stopped), 0 failed

`uv run zonosvoice.py stats` shows the totals of all sessions and the size of the cache. The cache grows by about
320 MB per hour of new audio. You can delete it (`audio/` in the cache folder) at any time; lines are then made
again when they are needed. Lines made by an earlier version of the mod with the ZONOS2 model are not used again;
you can delete them with the rest of the cache.

Set `[voices] generic_lines = "own"` in `config.toml` to give every NPC its own clip for all lines (more variety,
more API requests). If you fill a race folder with clips of different actors by hand, sharing is off unless a clip
is named `neutral`.

## Pronunciation and text

Edit `lexicon.tsv` to change how a name is spoken. Each line is the written form, a tab, then the spoken form (for
example `Hlaalu` → `Lahloo`).

Before the program sends a line to the speech model, it changes text that the model would read wrong:

| Game text | Spoken text |
|---|---|
| `I CANNOT think. PLEASE leave me alone.` | `I cannot think. Please leave me alone.` (capitals are read as letters) |
| `Uriel Septim VII`, `Pelagius III` | `Uriel Septim the Seventh`, `Pelagius the Third` |
| `Book II`, `Part IV` | `Book Two`, `Part Four` |
| `St. Olms` | `Saint Olms` |
| `-- and hurry`, `I was going to--` | `, and hurry`, `I was going to...` |
| `[Yagrum Bagarn takes the artifact...]` | not spoken (a stage direction) |

## Settings file

Copy `config.example.toml` to `config.toml` to change settings: how a clip is cloned (`codes`, the faithful clone,
or `embedding`), the number of parallel requests, extra request fields such as the temperature, the paths of
`openmw.cfg`, `openmw.log` and `Data Files`, the program that `play` starts, and the default voices.

### Voices from your own computer (zonos3.cpp)

ZONOS3 Mini also runs on your own computer with [zonos3.cpp](https://github.com/Zyphra/zonos3.cpp). No API key and
no network are needed then. Build zonos3.cpp and put its models (`zonos3-qat.gguf`, `dac.gguf`, `spk-encoder.gguf`)
in its `models` folder as its README says. Then set in `config.toml`:

    [tts]
    backend = "cpp"

    [cpp]
    dir = "C:/Users/you/zonos3.cpp"

`uv run zonosvoice.py --backend cpp shortcut` adds a second shortcut, "OpenMW Launcher (with local voices)". The
voice program starts `zonos3-server` with the game and stops it when the game exits. It makes one line at a time.
On a laptop CPU, a new line starts after about 5 to 15 seconds (the first part is kept short; the first game is
slower while the voices are prepared), and the server uses about 6 GB of memory. With a GPU build set `gpu = true`.
The lines made this way are kept in the same cache. The cpp backend has no default voices, so it needs the voice
clips from your game (the shortcut makes them).

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| "ZonosVoice: no voice after 40 s" in the game | The voice program does not run: start the game from the "OpenMW Launcher (with voices)" shortcut, or run `uv run zonosvoice.py run`. |
| The shortcut opens nothing | See the log: `~/.cache/zonosvoice/launcher.log` (Linux), `~/Library/Caches/zonosvoice/launcher.log` (macOS), `%LOCALAPPDATA%\zonosvoice\launcher.log` (Windows). If the launcher was not found, set `[game] launcher` in `config.toml`. |
| "no Zyphra Cloud API key" | Run `uv run zonosvoice.py key`, or set `ZYPHRA_API_KEY`. |
| "Zyphra Cloud HTTP 401" or "402" | The key is wrong, or the account has no credit. See https://cloud.zyphra.com. |
| "Zyphra Cloud HTTP 404" (model not found) | Your account has no access to `zyphra/ZONOS3-Mini`. See https://cloud.zyphra.com. |
| "Zyphra Cloud HTTP 400" (voice not found) | A name in `[voices]` in `config.toml` is not a Zyphra Cloud default voice. Use the names in `config.example.toml`. |
| "the cpp backend needs [cpp] dir" or "zonos3-server stopped" | Set `[cpp] dir` to your built zonos3.cpp folder, and check `zonos3_server.log` in the cache folder (missing model files, not enough memory). |
| No sound, and `openmw.log` says "Failed to load audio from sound/vo/zonosvoice" | The files `Sound/Vo/zonosvoice/s00.wav` to `s47.wav` are missing. Unzip the mod again (or run `uv run zonosvoice.py install`), then start OpenMW again. The game finds sound files only when it starts. |
| `Morrowind.esm was not found` | Set `[game] data_files` in `config.toml`. |
| An NPC from a mod with a new race speaks with an Imperial voice | Add a clip for that NPC in `voices/npc/`. |
| Two voices speak at once, or each line is spoken twice | Two voice programs ran at the same time (for example, two shortcuts started one after the other). Only one can run now: the second one shows "ZonosVoice already runs" and opens the launcher without voices. Close the game and start it again from one shortcut. |

## How it works

1. The mod's Lua script writes each dialogue line (speaker, race, sex and text) to `openmw.log`.
2. The voice program reads the log. It picks the speaker's voice clip and asks ZONOS3 Mini (on Zyphra Cloud, or
   zonos3.cpp on your computer) for the audio.
3. It writes the audio into one of the 48 sound files in `Sound/Vo/zonosvoice/`, and lists it in `zonosvoice/ready.txt`.
4. The Lua script reads that list and plays the sound file on the NPC.

OpenMW Lua cannot use the network or write files, so the log and the list carry the messages. OpenMW finds sound
files only when it starts, so the mod comes with 48 silent sound files, and the program only changes their
contents. The dialogue window pauses the game, but the scripts and the voice still run.

## Files

| File | Use |
|---|---|
| `ZonosVoice.omwscripts` | Loads the two Lua scripts. |
| `scripts/zonosvoice/player.lua` | Sends dialogue and book lines, plays the parts, the settings page. |
| `scripts/zonosvoice/global.lua` | Plays and stops a part on the speaker. |
| `l10n/ZonosVoice/en.yaml` | Text of the settings page. |
| `zonosvoice.py` | The voice program: voice extraction, setup, the daemon, test commands. |
| `lexicon.tsv` | Pronunciation fixes. |
| `config.example.toml` | All settings, with notes. |
| `voices/` | Your voice clips (made by `extract-game-voices`, or added by you). |
| `LICENSE` | The Apache License 2.0. |

## Licence

ZonosVoice is released under the Apache License 2.0 (see `LICENSE`), the same licence as the ZONOS3 Mini model.
The voice clips that `extract-game-voices` makes come from your copy of Morrowind and are not part of the mod.
