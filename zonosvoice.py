#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "scipy", "soundfile"]
# ///
"""ZonosVoice: voices for every Morrowind NPC, made while you play by ZONOS3 Mini on Zyphra Cloud.

  uv run zonosvoice.py key                   save your Zyphra Cloud API key (once; or set ZYPHRA_API_KEY)
  uv run zonosvoice.py shortcut              add an "OpenMW Launcher (with voices)" shortcut to your menu (once)
  uv run zonosvoice.py launcher              open the OpenMW launcher; voices run until the game exits
  uv run zonosvoice.py extract-game-voices   make voice clips from YOUR copy of Morrowind (the launcher does it once)
  uv run zonosvoice.py install               make the slot files and add the mod to openmw.cfg (the launcher does it)
  uv run zonosvoice.py play                  start the voice daemon and OpenMW together (no launcher)
  uv run zonosvoice.py run                   the voice daemon alone: keep it running while you play
  uv run zonosvoice.py voices                show which voice each race and sex uses
  uv run zonosvoice.py say TEXT --npc ID     speak one line in an NPC's voice
  uv run zonosvoice.py samples               one sample line per race and sex, written to samples/

How it works: the mod's player script prints one ZONOSVOICE line to openmw.log for each dialogue response (and for
each book that you open). The daemon reads the log, picks the speaker's voice (a stable choice per NPC from the clips
in voices/<race>_<sex>/), splits the text into parts, asks Zyphra Cloud for each part, and copies the audio into a
slot file under Sound/Vo/zonosvoice/. It then lists the part in zonosvoice/ready.txt, and the player script plays it
on the NPC with core.sound.say. OpenMW indexes data files at start, so the slot files exist from `install` on; the
daemon only replaces them. Audio is cached by (model, voice, text), so a line that was spoken once plays at once.

The audio comes from the released ZONOS3 Mini model, in one of two ways ([tts] backend in config.toml, or --backend):
  zyphra   Zyphra Cloud (default), model zyphra/ZONOS3-Mini. Needs an API key from https://cloud.zyphra.com. Each
           voice clip is sent once to /audio/codes (or /audio/embedding); the codes are kept in the cache folder.
  cpp      zonos3.cpp on this computer ([cpp] dir: a built checkout with its models): the voice program starts
           zonos3-server while the voices run and sends it the clips. No network and no API key; slower on a CPU.
"""
import argparse, base64, concurrent.futures as cf, hashlib, io, json, os, re, shutil, subprocess, sys
import threading, time, tomllib, urllib.error, urllib.request, uuid, wave
from dataclasses import dataclass, field

import numpy as np

VERSION = '0.4.0'
HERE = os.path.dirname(os.path.abspath(__file__))
SLOT_DIR = os.path.join(HERE, 'Sound', 'Vo', 'zonosvoice')
READY = os.path.join(HERE, 'zonosvoice', 'ready.txt')
VOICES = os.path.join(HERE, 'voices')
CONFIG = os.path.join(HERE, 'config.toml')
LEXICON = os.path.join(HERE, 'lexicon.tsv')
NSLOTS = 48
WINDOW = 4          # parts made ahead of the part that plays
RATE = 44100
AUDIO_EXT = ('.wav', '.mp3', '.ogg', '.flac')
USER_AGENT = f'zonosvoice/{VERSION}'
MODEL = 'zyphra/ZONOS3-Mini'   # the released ZONOS3 Mini on Zyphra Cloud: the only model this version uses

# Lua race id -> voices/ folder name. Other races (from mods) use the Imperial voices of the same sex.
RACES = {'argonian': 'argonian', 'breton': 'breton', 'dark elf': 'darkelf', 'high elf': 'highelf',
         'imperial': 'imperial', 'khajiit': 'khajiit', 'nord': 'nord', 'orc': 'orc', 'redguard': 'redguard',
         'wood elf': 'woodelf'}


def say(*parts):
    print(time.strftime('[%H:%M:%S]'), *parts, flush=True)


def die_with_parent():
    """Child processes (the ssh tunnel, the daemon under `play`) must not outlive their parent. Linux only."""
    if sys.platform.startswith('linux'):
        import ctypes, signal
        ctypes.CDLL('libc.so.6', use_errno=True).prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG


# ---------------------------------------------------------------------------------------------------- settings

def user_dirs():
    """(OpenMW config folders to try, cache folder) for this operating system."""
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        docs = os.path.join(os.environ.get('USERPROFILE', home), 'Documents', 'My Games', 'OpenMW')
        cache = os.path.join(os.environ.get('LOCALAPPDATA', home), 'zonosvoice')
        return [docs], cache
    if sys.platform == 'darwin':
        return [os.path.join(home, 'Library', 'Preferences', 'openmw')], os.path.join(home, 'Library', 'Caches',
                                                                                     'zonosvoice')
    xdg = os.environ.get('XDG_CONFIG_HOME', os.path.join(home, '.config'))
    return ([os.path.join(xdg, 'openmw'), os.path.join(home, '.var', 'app', 'org.openmw.OpenMW', 'config', 'openmw')],
            os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.join(home, '.cache')), 'zonosvoice'))


OPENMW_DIRS, CACHE = user_dirs()

DEFAULTS = {
    'tts': {
        'backend': 'zyphra',           # zyphra: Zyphra Cloud; cpp: zonos3.cpp on this computer ([cpp])
        'workers': 6,                  # parallel TTS requests (the cpp backend uses 1)
        # Zyphra Cloud
        'base_url': 'https://api.zyphracloud.com/api/v1',
        'api_key_env': 'ZYPHRA_API_KEY',
        'api_key_file': os.path.join(CACHE, 'zyphra_api_key'),
        'clone': 'codes',              # codes: the clip's codec codes (the faithful clone); embedding: a looser clone
        'params': {},                  # extra /audio/speech fields, e.g. {temperature = 1.0}
    },
    'cpp': {                           # zonos3.cpp (https://github.com/Zyphra/zonos3.cpp), backend = "cpp"
        'dir': '',                     # the zonos3.cpp checkout: build/zonos3-server and models/*.gguf
        'server': '',                  # the zonos3-server program; default: <dir>/build/zonos3-server
        'model': 'zonos3-qat.gguf',    # the backbone GGUF in <dir>/models (or a full path)
        'url': '',                     # a zonos3-server that already runs, instead of starting one
        'port': 18871,                 # the port of the server that the voice program starts
        'gpu': False,                  # --gpu (a CUDA/Metal/Vulkan build) or --cpu
        'dac_threads': 8,              # decoder threads (--dac-threads)
        'clone': 'embedding',          # speaker_mode: embedding (spk-encoder.gguf) or reference (dac-encoder + ssl-teacher)
        'params': {},                  # extra /tts/generate fields, e.g. {temperature = 0.75, speaker_cfg_scale = 1.5}
    },
    'game': {
        'openmw_cfg': '',              # default: openmw.cfg in the OpenMW config folder of this system
        'openmw_log': '',              # default: openmw.log next to it
        'data_files': '',              # default: the data= folder in openmw.cfg that has Morrowind.esm
        'openmw': 'openmw',            # the program that `play` starts
        'launcher': '',                # the OpenMW launcher; default: next to `openmw`, on PATH, or a usual folder
    },
    'voices': {
        # Zyphra Cloud default voices (GET /audio/default-voices): used when voices/ has no clip for a race and
        # sex, and for the narrator. An unknown name is an HTTP 400 voice_not_found error.
        'cloud_male': ['rupert', 'wyatt', 'cillian', 'thaddeus'],
        'cloud_female': ['imogen', 'savannah', 'willow', 'morgana'],
        'cloud_narrator': 'rupert',
        # Generic lines (lines that any NPC who matches the filters can say) and one shared clip per race and sex:
        # each such line is then made once per race and sex and cached for every NPC. "auto": share when the folder
        # has a neutral.* clip (the layout of extract-game-voices: all clips of a folder are one actor); "shared":
        # always share the first clip; "own": every NPC uses its own clip for all lines.
        'generic_lines': 'auto',
    },
}


def load_settings(args=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        with open(CONFIG, 'rb') as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)
    if args is not None and getattr(args, 'backend', None):
        cfg['tts']['backend'] = args.backend
    for key in ('model', 'url'):   # settings of version 0.3: Zyphra Cloud now always uses MODEL; url moved to [cpp]
        if key in cfg['tts']:
            say(f'warning: [tts] {key} in config.toml has no effect in this version (see config.example.toml)')
    return cfg


def openmw_cfg_path(cfg):
    if cfg['game']['openmw_cfg']:
        return os.path.expanduser(cfg['game']['openmw_cfg'])
    for d in OPENMW_DIRS:
        if os.path.exists(os.path.join(d, 'openmw.cfg')):
            return os.path.join(d, 'openmw.cfg')
    return os.path.join(OPENMW_DIRS[0], 'openmw.cfg')


def openmw_log_path(cfg):
    if cfg['game']['openmw_log']:
        return os.path.expanduser(cfg['game']['openmw_log'])
    return os.path.join(os.path.dirname(openmw_cfg_path(cfg)), 'openmw.log')


def cfg_data_dirs(cfg):
    """The data= folders of openmw.cfg, in order (OpenMW writes them quoted, with & as the escape character)."""
    path = openmw_cfg_path(cfg)
    out = []
    if os.path.exists(path):
        for line in open(path, encoding='utf-8', errors='replace'):
            line = line.strip()
            if line.startswith('data='):
                v = line[5:].strip()
                if v.startswith('"') and v.endswith('"'):
                    v = re.sub(r'&(.)', r'\1', v[1:-1])
                out.append(os.path.expanduser(v))
    return out


def data_files(cfg):
    """The Morrowind "Data Files" folder: from config.toml, else the data= folder in openmw.cfg with Morrowind.esm."""
    if cfg['game']['data_files']:
        return os.path.expanduser(cfg['game']['data_files'])
    for d in cfg_data_dirs(cfg):
        if os.path.exists(os.path.join(d, 'Morrowind.esm')):
            return d
    sys.exit(f'Morrowind.esm was not found in the data= folders of {openmw_cfg_path(cfg)}. Set [game] data_files in '
             f'{CONFIG}.')


# ---------------------------------------------------------------------------------------------------- audio

def load_audio(path, max_s=None):
    """Any audio file (wav, mp3, ogg, flac) -> float32 mono 44.1 kHz."""
    import soundfile
    x, sr = soundfile.read(path, dtype='float32', always_2d=True, frames=int(max_s * 48000) if max_s else -1)
    x = x.mean(axis=1)
    if sr != RATE:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, RATE)
        x = resample_poly(x, RATE // g, sr // g).astype(np.float32)
    return x


def wav_bytes(x):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype('<i2').tobytes())
    return buf.getvalue()


def write_wav(path, x):
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        f.write(wav_bytes(x))
    os.replace(tmp, path)


def frame_db(x, frame=441):
    n = len(x) // frame
    if n == 0:
        return np.array([-120.0])
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-7))


def trim_and_level(x, target_db=-20.0, head_ms=60, tail_ms=180):
    """Cut silence at both ends, set the RMS of the speech to target_db and keep peaks below -1 dBFS."""
    if len(x) < RATE // 20:
        return x
    db = frame_db(x)
    loud = np.nonzero(db > db.max() - 40)[0]
    if len(loud):
        a = max(0, loud[0] * 441 - head_ms * RATE // 1000)
        b = min(len(x), (loud[-1] + 1) * 441 + tail_ms * RATE // 1000)
        x = x[a:b]
    db = frame_db(x)
    mask = np.repeat(db > db.max() - 30, 441)
    speech = x[:len(mask)][mask] if len(mask) else x
    x = x * (10 ** (target_db / 20) / (np.sqrt(np.mean(speech ** 2)) + 1e-9))
    peak = np.abs(x).max()
    if peak > 0.89:
        x = x * (0.89 / peak)
    fade = min(len(x) // 4, RATE // 100)
    x[-fade:] *= np.linspace(1, 0, fade)
    return x.astype(np.float32)


def reference_cut(x, max_s=12.5):
    """At most max_s seconds of speech, cut at the quietest point in the last 2 s so no word is cut."""
    x = trim_and_level(x)
    n = int(max_s * RATE)
    if len(x) <= n:
        return x
    cut = n - 2 * RATE + int(np.argmin(frame_db(x[n - 2 * RATE:n]))) * 441
    return trim_and_level(x[:cut])


def join_clips(paths, target_s=10.0, max_s=12.5, gap_s=0.3):
    """Several short lines -> one reference: each line trimmed and levelled, joined with short pauses."""
    pieces, total = [], 0.0
    for p in paths:
        x = trim_and_level(load_audio(p), head_ms=20, tail_ms=60)
        pieces += [x, np.zeros(int(gap_s * RATE), np.float32)]
        total += len(x) / RATE + gap_s
        if total >= target_s:
            break
    return reference_cut(np.concatenate(pieces[:-1]), max_s=max_s)


# ---------------------------------------------------------------------------------------------------- game files

def esm_records(path, tags):
    """(tag, [(subrecord tag, bytes)]) for the records of the given types in an ESM/ESP file."""
    with open(path, 'rb') as f:
        buf = f.read()
    pos = 0
    while pos + 16 <= len(buf):
        tag = buf[pos:pos + 4].decode('latin-1')
        size = int.from_bytes(buf[pos + 4:pos + 8], 'little')
        if tag in tags:
            data, subs, p = buf[pos + 16:pos + 16 + size], [], 0
            while p + 8 <= len(data):
                n = int.from_bytes(data[p + 4:p + 8], 'little')
                subs.append((data[p:p + 4].decode('latin-1'), data[p + 8:p + 8 + n]))
                p += 8 + n
            yield tag, subs
        pos += 16 + size


def zstr(b):
    return b.split(b'\0')[0].decode('cp1252', 'replace')


def find_npc(cfg, npc_id):
    """(race id, sex, name) of an NPC or creature from the masters, the last master that defines it winning."""
    found = None
    folder = data_files(cfg)
    for master in ['Morrowind.esm', 'Tribunal.esm', 'Bloodmoon.esm']:
        path = os.path.join(folder, master)
        if not os.path.exists(path):
            continue
        for tag, subs in esm_records(path, {'NPC_', 'CREA'}):
            d = dict(subs)
            if zstr(d.get('NAME', b'')).lower() == npc_id.lower():
                if tag == 'CREA':
                    found = ('creature', 'm', zstr(d.get('FNAM', b'')))
                else:
                    flags = int.from_bytes(d.get('FLAG', b'\0\0\0\0')[:4], 'little')
                    found = (zstr(d.get('RNAM', b'')).lower(), 'f' if flags & 1 else 'm', zstr(d.get('FNAM', b'')))
    if not found:
        sys.exit(f'no NPC or creature {npc_id!r} in the masters')
    return found


# The game's own voices: one actor per race and sex, from the vanilla "Hello" lines (Sound/Vo/<letter>/<sex>/). The
# dialogue records hold the transcript and the disposition of each line, so calm full sentences can be chosen. Three
# clips per race and sex give NPCs a manner: warm (disposition 60-100), neutral (30-59) and curt (0-29).
GAME_RACE_LETTERS = {'a': 'argonian', 'b': 'breton', 'd': 'darkelf', 'h': 'highelf', 'i': 'imperial',
                     'k': 'khajiit', 'n': 'nord', 'o': 'orc', 'r': 'redguard', 'w': 'woodelf'}
MOODS = [('warm', 60, 100), ('neutral', 30, 59), ('curt', 0, 29)]
# Unique characters with their own recordings in Sound/Vo/Misc (all are creature records in the masters).
UNIQUE = {
    'yagrum bagarn': ['Yagrum_1.mp3', 'Yagrum_3.mp3'],
    'dagoth_ur_1': ['Dagoth Ur Welcome A.mp3', 'Dagoth Ur Welcome B.mp3', 'Dagoth Ur Welcome C.mp3',
                    'Dagoth Ur Welcome D.mp3'],
    'vivec_god': ['viv_hlo1.mp3', 'viv_idl1.mp3', 'viv_alm1.mp3'],
    'almalexia': ['tr_almgreet1.mp3', 'tr_almgreet2.mp3', 'tr_almgreet3.mp3'],
}
UNIQUE_ALIASES = {'dagoth_ur_2': 'dagoth_ur_1', 'almalexia_warrior': 'almalexia'}


def calm_line(text):
    """A full, calm sentence: no shouting, no special-state lines (vampire, werewolf)."""
    return (15 <= len(text) <= 140 and len(text.split()) >= 3 and '!' not in text and '*' not in text
            and not re.search(r'vampire|werewolf', text, re.I))


def game_hello_lines(folder):
    """(race folder, sex) -> [(disposition, file, transcript)] for the calm Hello lines of Morrowind.esm. Only the
    base game: the expansions' lines can come from other actors."""
    sound = os.path.join(folder, 'Sound')
    files = {}   # the records and the files do not agree on letter case: look up by lower case
    for root, _, names in os.walk(os.path.join(sound, 'Vo')):
        for n in names:
            files[os.path.relpath(os.path.join(root, n), sound).replace('\\', '/').lower()] = os.path.join(root, n)
    out, seen, hello = {}, set(), False
    for tag, subs in esm_records(os.path.join(folder, 'Morrowind.esm'), {'DIAL', 'INFO'}):
        d = dict(subs)
        if tag == 'DIAL':
            hello = d.get('DATA', b'\0')[:1] == b'\x01' and zstr(d.get('NAME', b'')).lower() == 'hello'
            continue
        if not hello:
            continue
        snd = zstr(d.get('SNAM', b'')).replace('\\', '/')
        text = zstr(d.get('NAME', b'')).strip()
        m = re.match(r'(?i)vo/([a-z])/([mf])/', snd)
        path = files.get(snd.lower())
        if not m or not path or m.group(1).lower() not in GAME_RACE_LETTERS or snd.lower() in seen:
            continue
        if not calm_line(text):
            continue
        seen.add(snd.lower())
        disposition = int.from_bytes(d.get('DATA', b'\0' * 8)[4:8], 'little', signed=True)
        out.setdefault((GAME_RACE_LETTERS[m.group(1).lower()], m.group(2).lower()), []).append((disposition, path, text))
    return out


def cmd_extract(args):
    extract_voices(load_settings(args), args.force)


def extract_voices(cfg, force=False):
    folder = data_files(cfg)
    if not os.path.isdir(os.path.join(folder, 'Sound', 'Vo')):
        sys.exit(f'{folder} has no Sound/Vo folder. See "Choose voice clips by hand" in README.md.')
    say(f'reading the voice lines of {folder}')
    made, kept = [], 0

    def out(rel, make):
        nonlocal kept
        path = os.path.join(VOICES, rel)
        if os.path.exists(path) and not force:
            kept += 1
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        x = make()
        write_wav(path, x)
        made.append(f'{rel} ({len(x) / RATE:.1f} s)')

    for (race, sex), rows in sorted(game_hello_lines(folder).items()):
        for mood, lo, hi in MOODS:
            pick = sorted([r for r in rows if lo <= r[0] <= hi], key=lambda r: (-len(r[2]), r[1]))
            if sum(len(r[2]) for r in pick) < 120:   # too few lines in this mood: take the nearest others too
                pick += sorted([r for r in rows if r not in pick], key=lambda r: (abs(r[0] - (lo + hi) / 2), r[1]))
            out(f'{race}_{sex}/{mood}.wav', lambda pick=pick: join_clips([r[1] for r in pick]))
    misc = os.path.join(folder, 'Sound', 'Vo', 'Misc')
    for npc, names in UNIQUE.items():
        paths = [os.path.join(misc, n) for n in names if os.path.exists(os.path.join(misc, n))]
        if paths:
            for who in [npc] + [a for a, target in UNIQUE_ALIASES.items() if target == npc]:
                out(f'npc/{who}.wav', lambda paths=paths: join_clips(paths))
    # Talking creatures without their own voice: the Dark Elf male voice, slowed to be deeper and larger.
    base = os.path.join(VOICES, 'darkelf_m', 'neutral.wav')
    if os.path.exists(base):
        from scipy.signal import resample_poly
        out('creature/deep.wav', lambda: trim_and_level(resample_poly(load_audio(base), 5, 4).astype(np.float32)))
    with open(os.path.join(VOICES, 'EXTRACTED.txt'), 'w') as f:
        f.write(f'Made by zonosvoice.py extract-game-voices {VERSION} on {time.strftime("%Y-%m-%d")} from\n{folder}\n\n'
                'These clips come from your own copy of Morrowind. They are for your own game only: do not share or '
                'upload them.\n')
    for m in made:
        say('  ' + m)
    say(f'{len(made)} clips made in {VOICES}' + (f', {kept} kept (use --force to make them again)' if kept else ''))


# ---------------------------------------------------------------------------------------------------- voices

@dataclass(frozen=True)
class Voice:
    label: str
    path: str = ''      # a reference clip
    preset: str = ''    # or a Zyphra Cloud default voice


def stable_index(key, n):
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) % n


class VoiceSet:
    """The clips in voices/: <race>_<sex>/ (any number of clips; each is one voice of the pool), creature/,
    narrator/, and npc/<npc id>.<ext> for one character."""

    def __init__(self, cfg, presets=True):
        self.cfg, self.presets = cfg, presets   # presets: the Zyphra Cloud default voices (not on the cpp backend)
        self.pools, self.npc, self.narrator = {}, {}, []
        if os.path.isdir(VOICES):
            for d in sorted(os.listdir(VOICES)):
                full = os.path.join(VOICES, d)
                if not os.path.isdir(full):
                    continue
                clips = sorted(os.path.join(full, f) for f in os.listdir(full) if f.lower().endswith(AUDIO_EXT))
                if d == 'npc':
                    self.npc = {os.path.splitext(os.path.basename(c))[0].lower(): c for c in clips}
                elif d == 'narrator':
                    self.narrator = clips
                elif clips:
                    self.pools[d] = clips

    def pool(self, key):
        clips = [Voice(f'{key}/{os.path.basename(c)}', path=c) for c in self.pools.get(key, [])]
        if not clips and self.presets and key != 'creature':
            names = self.cfg['voices']['cloud_female' if key.endswith('_f') else 'cloud_male']
            clips = [Voice(f'preset {n}', preset=n) for n in names]
        return clips

    def shared_clip(self, key):
        """The clip that generic lines of this pool use, or None (then every NPC keeps its own clip)."""
        mode, clips = self.cfg['voices'].get('generic_lines', 'auto'), self.pools.get(key, [])
        if mode == 'own' or not clips:
            return None
        neutral = [c for c in clips if os.path.splitext(os.path.basename(c))[0].lower() == 'neutral']
        return neutral[0] if neutral else (clips[0] if mode == 'shared' else None)

    def pick(self, actor, race, sex, generic=False):
        """The voice of a speaker. An npc/ clip always wins. A generic line uses the pool's shared clip if it has
        one, so the line is made once per race and sex."""
        actor, race = actor.lower(), race.lower()
        if race == 'narrator':
            if self.narrator:
                return Voice(f'narrator/{os.path.basename(self.narrator[0])}', path=self.narrator[0])
            if self.presets:
                name = self.cfg['voices']['cloud_narrator']
                return Voice(f'preset {name}', preset=name)
            race, sex = 'imperial', 'm'
        if actor in self.npc:
            return Voice(f'npc/{os.path.basename(self.npc[actor])}', path=self.npc[actor])
        keys = (['creature', 'darkelf_m'] if race == 'creature' else []) + [f'{RACES.get(race, "imperial")}_{sex}',
                                                                            f'imperial_{sex}']
        keys += [k for k in sorted(self.pools) if k.endswith('_' + sex)] + sorted(self.pools)
        for key in keys:
            clips = self.pool(key)
            if clips:
                shared = self.shared_clip(key) if generic else None
                if shared:
                    return Voice(f'{key}/{os.path.basename(shared)} (shared)', path=shared)
                return clips[stable_index(actor, len(clips))]
        sys.exit('no voices: run "uv run zonosvoice.py extract-game-voices" or put clips in voices/<race>_<sex>/')

    def all_clips(self):
        clips = [Voice(os.path.basename(c), path=c) for c in self.narrator + list(self.npc.values())]
        order = ['darkelf', 'imperial', 'nord', 'orc', 'argonian', 'redguard', 'woodelf', 'breton', 'khajiit',
                 'highelf', 'creature']   # the most common speakers first: half of all NPCs are Dark Elves
        for key in sorted(self.pools, key=lambda k: (order.index(k.split('_')[0]) if k.split('_')[0] in order else 99,
                                                     k)):
            clips += [Voice(os.path.basename(c), path=c) for c in self.pools[key]]
        return list(dict.fromkeys(clips))


# ---------------------------------------------------------------------------------------------------- text

def load_lexicon():
    rules = []
    if os.path.exists(LEXICON):
        for line in open(LEXICON, encoding='utf-8'):
            if line.strip() and not line.startswith('#') and '\t' in line:
                word, spoken = line.rstrip('\n').split('\t')[:2]
                rules.append((re.compile(r"(?<![\w'])" + re.escape(word) + r"(?![\w'])", re.I), spoken))
    return rules


_ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve',
         'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen', 'Twenty']
_ORDINALS = ['', 'First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth', 'Seventh', 'Eighth', 'Ninth', 'Tenth',
             'Eleventh', 'Twelfth', 'Thirteenth', 'Fourteenth', 'Fifteenth', 'Sixteenth', 'Seventeenth',
             'Eighteenth', 'Nineteenth', 'Twentieth']


def _roman_value(r):
    vals = {'I': 1, 'V': 5, 'X': 10}
    total = 0
    for a, b in zip(r, r[1:] + ' '):
        total += -vals[a] if vals.get(b, 0) > vals[a] else vals[a]
    return total


_ROMAN = r'(?:XX|X?(?:IX|IV|V?I{0,3}))'
_BOOKISH = r'(?i:Book|Volume|Vol\.|Part|Verse|Chapter|Section|Act|Scene|Canto|Sermon|Tablet|Number)'


def normalize_caps_and_numbers(text):
    """Text fixes for the model, which reads the text as written (ZONOS3 Mini has no text normalization on the
    server; it reads numbers, ordinals, times and "Dr." well by itself). It spells out words in capital letters as if
    they were acronyms ("I CANNOT" is read C-A-N-N-O-T), and it reads Roman numerals as letters."""
    def cardinal(m):
        v = _roman_value(m.group(2))
        return f'{m.group(1)} {_ONES[v]}' if 0 < v <= 20 else m.group(0)

    def ordinal(m):
        v = _roman_value(m.group(2))
        return f'{m.group(1)} the {_ORDINALS[v]}' if 0 < v <= 20 else m.group(0)

    # "Book II", "Part IV" -> "Book Two"; a numeral alone as a heading ("III. The Fall") -> "Three."
    text = re.sub(rf'\b({_BOOKISH}) ({_ROMAN}|I)\b(?!\')', cardinal, text)
    text = re.sub(rf'(^|[.!?:]\s+)((?=[IVX]{{2,}})({_ROMAN}))(?=[.:]\s|[.:]?$)',
                  lambda m: m.group(1) + (_ONES[_roman_value(m.group(2))] if 0 < _roman_value(m.group(2)) <= 20
                                          else m.group(2)), text)
    # A ruler's numeral after a name: "Uriel Septim VII" -> "Uriel Septim the Seventh" ("Uriel V", "k'Thojj III").
    text = re.sub(rf"\b([A-Z][a-z][\w']*|[a-z]'[A-Z][\w']*) ((?=[IVX])(?:{_ROMAN}))\b(?!')",
                  lambda m: ordinal(m) if m.group(2) != 'I' and m.group(1) not in ('And', 'But', 'If', 'As', 'So',
                                                                                     'Then', 'When', 'That', 'Now')
                  else m.group(0), text)
    text = re.sub(r'\bSt\.(?=\s+[A-Z])', 'Saint', text)
    text = re.sub(r'\s*&\s*', ' and ', text)
    text = re.sub(r'\s*(?:--|—)\s*$', '...', text)
    text = re.sub(r'\s*(?:--|—)\s*', ', ', text)
    # Words in capitals (emphasis, shouting, headings): lower case. "I'M" -> "I'm".
    text = re.sub(r"\bI'([A-Z]+)\b", lambda m: "I'" + m.group(1).lower(), text)
    text = re.sub(r"\b[A-Z]*[A-Z]'?[A-Z]+\b", lambda m: m.group(0).lower() if len(m.group(0).replace("'", '')) > 1
                  else m.group(0), text)
    # Capital letter again at the start of each sentence and after an opening quote.
    text = re.sub(r"(^|[.!?]\s+|(?:^|\s)[\"'])([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text


def clean_text(text, lexicon):
    text = text.replace('@', '').replace('#', '')
    text = re.sub(r'\s*\[[^\]]*\]\s*', ' ', text)   # stage directions: "[Yagrum Bagarn takes the artifact...]"
    text = normalize_caps_and_numbers(text)
    if '||' in text:   # book lines: a title or heading line without an end mark gets one, so the voice pauses
        lines = [l.strip() for l in text.split('||') if l.strip()]
        text = ' '.join(l if l[-1] in '.!?:;,"\'' else l + '.' for l in lines)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+([,.!?;:])', r'\1', text)
    for pattern, spoken in lexicon:
        text = pattern.sub(spoken, text)
    if text and text[-1] not in '.!?"\'':
        text += '.'
    return text


def split_parts(text, first_min=40, first_max=120, max_len=280, growth=None, single_max=None):
    """Sentence groups: a short first part (first_min to first_max characters where the sentences allow) so the
    voice starts soon, then parts of up to max_len characters. The parts after the first are made while the first
    one plays. With growth, a part is at most growth x the previous one (for a server that makes one part at a
    time: each part is then ready before the previous one ends). A text up to single_max characters (default
    first_max + 40) stays one part."""
    if len(text) <= (single_max or first_max + 40):
        return [text]
    sentences = [s for s in re.split(r'(?<=[.!?])\s+(?=["\'(A-Z0-9])', text) if s]
    parts, cur = [], ''
    for s in sentences:
        limit = first_max if not parts else max_len if not growth else min(max_len, int(growth * len(parts[-1])))
        if not parts and len(cur) >= first_min:
            parts.append(cur)
            cur = s
        elif cur and len(cur) + 1 + len(s) > limit:
            parts.append(cur)
            cur = s
        else:
            cur = f'{cur} {s}'.strip()
    if cur:
        parts.append(cur)
    out = []
    for p in parts:   # a single very long sentence: split at commas or semicolons
        while len(p) > max_len + 120:
            cut = max(p.rfind(', ', 0, max_len), p.rfind('; ', 0, max_len))
            if cut < 40:
                break
            out.append(p[:cut + 1])
            p = p[cut + 2:]
        out.append(p)
    return out


# ---------------------------------------------------------------------------------------------------- backends

_file_hashes = {}


def file_hash(path):
    if path not in _file_hashes:
        with open(path, 'rb') as f:
            _file_hashes[path] = hashlib.sha1(f.read()).hexdigest()[:12]
    return _file_hashes[path]


def http(url, data=None, headers=None, timeout=240, tries=3):
    """POST (or GET) with retries on 429 and 5xx. Returns the body as bytes."""
    headers = {'User-Agent': USER_AGENT, **(headers or {})}   # Cloudflare rejects the default Python agent
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                say(f'  HTTP {exc.code} from {url.split("/")[2]}; trying again in {2 ** attempt} s')
                time.sleep(2 ** attempt)
                continue
            raise


class Backend:
    """synth(text, voice, seed) -> float32 PCM at 44.1 kHz. line() adds the disk cache."""
    presets = False     # the backend has preset voices (Voice.preset)
    serial = False      # the backend makes one request at a time: voice preparation must yield to game lines
    split = {}          # split_parts() options for this backend

    def __init__(self, cfg):
        self.cfg = cfg

    def start(self):
        pass

    def tag(self):
        return 'unknown'

    def prepare(self, voice):
        """Get a voice ready (upload, embedding) before its first line."""

    def line(self, text, voice):
        """The cached wav of this part, made now if needed: (path, from the cache)."""
        key = hashlib.sha1(json.dumps([self.tag(), file_hash(voice.path) if voice.path else 'preset:' + voice.preset,
                                       text]).encode()).hexdigest()
        path = os.path.join(CACHE, 'audio', key[:2], key + '.wav')
        if os.path.exists(path):
            return path, True
        x = self.synth(text, voice, seed=int(key[:7], 16))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_wav(path, trim_and_level(x))
        return path, False


class ZyphraCloud(Backend):
    """Zyphra Cloud, OpenAI-compatible /audio/speech, with the released ZONOS3 Mini (MODEL). A clip is sent once to
    /audio/codes (its codec codes: the faithful clone) or, with [tts] clone = "embedding", to /audio/embedding (a
    192-value speaker embedding: a looser clone). The result is kept in the cache folder and sent with each request
    as speaker_codes_base64 or speaker_embedding_base64. A preset voice (a default voice of Zyphra Cloud) needs no
    clip."""
    presets = True

    def __init__(self, cfg):
        super().__init__(cfg)
        t = cfg['tts']
        self.base, self.clone, self.params = t['base_url'].rstrip('/'), t['clone'], t['params']
        if self.clone not in ('codes', 'embedding'):
            sys.exit(f'[tts] clone must be "codes" or "embedding", not {self.clone!r}')
        self.key = os.environ.get(t['api_key_env'], '').strip()
        key_file = os.path.expanduser(t['api_key_file'])
        if not self.key and os.path.exists(key_file):
            self.key = open(key_file).read().strip()
        if not self.key:
            sys.exit(f'no Zyphra Cloud API key: set {t["api_key_env"]}, or run "uv run zonosvoice.py key". '
                     'Get a key at https://cloud.zyphra.com (Keys tab).')
        self.lock = threading.Lock()
        self.voice_locks = {}

    def tag(self):
        return f'zyphra:{MODEL}:{self.clone}:{json.dumps(self.params, sort_keys=True)}'

    def headers(self, extra=None):
        return {'Authorization': f'Bearer {self.key}', **(extra or {})}

    def explain(self, exc):
        detail = exc.read().decode(errors='replace')
        try:
            detail = json.loads(detail)['error']['message']
        except (ValueError, KeyError, TypeError):
            pass
        hint = {400: 'the request was refused', 401: 'the API key was not accepted',
                402: 'the account has no credit left', 403: 'access refused',
                404: f'{MODEL} is not available to this account', 503: 'the TTS service is down for now'}.get(exc.code, '')
        if exc.code == 400 and 'voice' in detail:
            hint += ' (check the [voices] names in config.toml against the default voices of Zyphra Cloud)'
        return RuntimeError(f'Zyphra Cloud HTTP {exc.code}{": " + hint if hint else ""}: {detail[:200]}')

    def conditioning(self, voice):
        """The clip's codes (or embedding) for MODEL, from /audio/codes (or /audio/embedding): made once per clip and
        kept in <cache>/speakers/<model>/<clip hash>.<codes|embedding>.json."""
        path = os.path.join(CACHE, 'speakers', MODEL.split('/')[-1], f'{file_hash(voice.path)}.{self.clone}.json')
        with self.lock:
            lock = self.voice_locks.setdefault(voice.path, threading.Lock())
        with lock:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)['value']
            boundary = uuid.uuid4().hex
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{MODEL}\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="ref.wav"\r\n'
                    f'Content-Type: audio/wav\r\n\r\n').encode() + wav_bytes(load_audio(voice.path)) + \
                   f'\r\n--{boundary}--\r\n'.encode()
            try:
                r = json.loads(http(self.base + '/audio/' + self.clone, body, self.headers(
                    {'Content-Type': f'multipart/form-data; boundary={boundary}'}), timeout=120))
            except urllib.error.HTTPError as exc:
                raise self.explain(exc) from None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            info = {k: v for k, v in r.items() if k != self.clone}
            with open(path + '.part', 'w') as f:
                json.dump({'value': r[self.clone], 'model': MODEL, 'clone': self.clone, 'source': voice.path, **info}, f)
            os.replace(path + '.part', path)
            return r[self.clone]

    def prepare(self, voice):
        """Get a voice ready (its codes or embedding) before its first line."""
        if voice.path:
            self.conditioning(voice)

    def synth(self, text, voice, seed):
        # No text_norm: ZONOS3 Mini reads the text as written (the field is refused), see clean_text().
        body = dict(model=MODEL, input=text, response_format='pcm', stream=False, seed=seed, **self.params)
        if voice.preset:
            body['voice'] = voice.preset
        else:
            body['speaker_codes_base64' if self.clone == 'codes' else 'speaker_embedding_base64'] = self.conditioning(voice)
        try:
            pcm = http(self.base + '/audio/speech', json.dumps(body).encode(),
                       self.headers({'Content-Type': 'application/json'}))
        except urllib.error.HTTPError as exc:
            raise self.explain(exc) from None
        return np.frombuffer(pcm, dtype='<f4').copy()


class Zonos3Cpp(Backend):
    """zonos3.cpp (the ggml port of ZONOS3 Mini) on this computer. start() reuses a zonos3-server at [cpp] url or on
    [cpp] port, or starts <dir>/build/zonos3-server with the backbone GGUF from <dir>/models and waits until /health
    answers; a started server ends with this program (Linux: PR_SET_PDEATHSIG). Each clip is uploaded once per server
    (POST /tts/speakers, header X-TTS-Session-ID), and its speaker embedding is made on the first use; requests then
    send reference_id. The server makes one line at a time, so the daemon sends one request at a time ([tts] workers
    = 1) and the parts then arrive in order. The first part is short (30-90 characters; a text over 100 characters is
    split) and each later part at most 1.6x the one before, so each part is ready before the previous one ends when
    the computer makes audio at about real time."""
    serial = True
    split = dict(first_min=30, first_max=90, max_len=280, growth=1.6, single_max=100)

    def __init__(self, cfg):
        cfg['tts']['workers'] = 1
        super().__init__(cfg)
        c = cfg['cpp']
        self.dir = os.path.expanduser(c['dir']) if c['dir'] else ''
        self.server_bin = os.path.expanduser(c['server']) if c['server'] else os.path.join(self.dir, 'build', 'zonos3-server')
        if sys.platform == 'win32' and not os.path.exists(self.server_bin) and os.path.exists(self.server_bin + '.exe'):
            self.server_bin += '.exe'
        model = os.path.expanduser(c['model'])
        self.model = model if os.path.isabs(model) else os.path.join(self.dir, 'models', model)
        self.url = c['url'].rstrip('/') or f'http://127.0.0.1:{int(c["port"])}'
        self.clone, self.params = c['clone'], c['params']
        if self.clone not in ('embedding', 'reference'):
            sys.exit(f'[cpp] clone must be "embedding" or "reference", not {self.clone!r}')
        self.session = 'zonosvoice-' + uuid.uuid4().hex[:8]
        self.lock = threading.Lock()
        self.speakers, self.voice_locks = {}, {}
        self.proc = None

    def tag(self):
        return f'zonos3.cpp:{os.path.basename(self.model)}:{self.clone}:{json.dumps(self.params, sort_keys=True)}'

    def healthy(self):
        try:
            with urllib.request.urlopen(self.url + '/health', timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    def start(self):
        if self.healthy():
            return
        if self.cfg['cpp']['url']:
            raise RuntimeError(f'no zonos3-server at {self.url} ([cpp] url)')
        if not self.dir or not os.path.isfile(self.server_bin):
            sys.exit(f'the cpp backend needs [cpp] dir in {CONFIG}: a zonos3.cpp checkout with build/zonos3-server '
                     f'(not found: {self.server_bin})')
        if not os.path.isfile(self.model):
            sys.exit(f'the backbone GGUF was not found: {self.model} ([cpp] model)')
        c = self.cfg['cpp']
        log_path = os.path.join(CACHE, 'zonos3_server.log')
        os.makedirs(CACHE, exist_ok=True)
        log = open(log_path, 'a')
        argv = [self.server_bin, self.model, '--host', '127.0.0.1', '--port', str(int(c['port'])),
                '--gpu' if c['gpu'] else '--cpu', '--dac-threads', str(int(c['dac_threads']))]
        say(f'starting zonos3-server ({os.path.basename(self.model)}, {"GPU" if c["gpu"] else "CPU"}); log {log_path}')
        self.proc = subprocess.Popen(argv, cwd=self.dir, stdout=log, stderr=subprocess.STDOUT,
                                     preexec_fn=die_with_parent if sys.platform.startswith('linux') else None)
        t0 = time.time()
        while time.time() - t0 < 300:
            if self.proc.poll() is not None:
                raise RuntimeError(f'zonos3-server stopped (exit {self.proc.returncode}); see {log_path}')
            if self.healthy():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f'zonos3-server did not answer in 300 s; see {log_path}')
        # The server answers /health before its warm-up (the backbone and four decoder sizes, 20-30 s on a CPU) is
        # done, and the first request waits behind it. A small request here ends when the server is really ready.
        self.request('/tts/generate', dict(text='Welcome to Vvardenfell.', seed=1, stream=False, format='pcm'),
                     timeout=600)
        say(f'zonos3-server ready ({time.time() - t0:.0f} s)')

    def request(self, path, body, timeout=600):
        return http(self.url + path, json.dumps(body).encode(),
                    {'Content-Type': 'application/json', 'X-TTS-Session-ID': self.session}, timeout=timeout, tries=1)

    def speaker_id(self, voice):
        """Upload the clip once per server (this session); the server then makes the speaker embedding (or the
        reference codes) on the first use."""
        with self.lock:
            lock = self.voice_locks.setdefault(voice.path, threading.Lock())
        with lock:
            sid = self.speakers.get(voice.path)
            if not sid:
                body = dict(reference_audio_base64=base64.b64encode(wav_bytes(load_audio(voice.path))).decode(),
                            reference_audio_name=os.path.basename(voice.path), label=voice.label)
                sid = json.loads(self.request('/tts/speakers', body, timeout=120))['id']
                self.speakers[voice.path] = sid
            return sid

    def prepare(self, voice):
        if voice.path:
            sid = self.speaker_id(voice)
            if self.clone == 'embedding':   # makes the embedding now, not at the voice's first line
                http(f'{self.url}/tts/speakers/{sid}/embedding', headers={'X-TTS-Session-ID': self.session},
                     timeout=300, tries=1)

    def synth(self, text, voice, seed):
        if voice.preset:
            raise RuntimeError('the cpp backend has no preset voices; add clips to voices/')
        self.start()
        body = dict(text=text, seed=seed, stream=False, format='pcm', speaker_mode=self.clone, **self.params)
        for attempt in range(2):
            body['reference_id'] = self.speaker_id(voice)
            try:
                return np.frombuffer(self.request('/tts/generate', body), dtype='<f4').copy()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors='replace')
                if 'unknown reference_id' in detail and attempt == 0:   # the server restarted: upload again
                    self.speakers.pop(voice.path, None)
                    continue
                raise RuntimeError(f'zonos3-server HTTP {exc.code}: {detail[:200]}') from None


def make_backend(cfg):
    kind = cfg['tts']['backend']
    classes = {'zyphra': ZyphraCloud, 'cpp': Zonos3Cpp}
    if kind not in classes:
        sys.exit(f'unknown backend {kind!r}: use zyphra (Zyphra Cloud) or cpp (zonos3.cpp on this computer)')
    return classes[kind](cfg)


# ---------------------------------------------------------------------------------------------------- daemon

@dataclass
class Line:
    session: str
    n: int
    actor: str
    race: str
    sex: str
    kind: str
    text: str
    t0: float = field(default_factory=time.time)
    futures: list = field(default_factory=list)
    scope: str = 'speaker'   # speaker, generic or book
    voice: Voice = None
    parts: list = field(default_factory=list)
    submitted: int = 0       # parts 1..submitted are made or queued


class Stats:
    """What one game session (and all sessions, in <cache>/stats.json) cost: parts from the cache and parts made by
    Zyphra Cloud (one API request each), with their audio length."""
    KEYS = ['lines', 'generic', 'speaker', 'book', 'parts', 'cached', 'made', 'cached_s', 'made_s', 'not_made',
            'failed']

    def __init__(self):
        self.lock = threading.Lock()
        self.now = dict.fromkeys(self.KEYS, 0)

    def add(self, **kw):
        with self.lock:
            for k, v in kw.items():
                self.now[k] += v

    @staticmethod
    def text(d):
        parts = max(1, d['parts'])
        return (f"{d['lines']} lines ({d['generic']} generic, {d['speaker']} one speaker, {d['book']} books), "
                f"{d['parts']} parts: {d['cached']} from the cache ({100 * d['cached'] / parts:.0f}%, "
                f"{d['cached_s'] / 60:.1f} min of audio), {d['made']} made by the TTS ({d['made_s'] / 60:.1f} min of "
                f"audio, {d['made']} API requests), {d['not_made']} not made (line stopped), {d['failed']} failed")

    def close_session(self):
        """Print the session summary, add it to the all-time totals, start a new session."""
        with self.lock:
            now, self.now = self.now, dict.fromkeys(self.KEYS, 0)
        if not now['lines']:
            return
        say('session summary: ' + self.text(now))
        path = stats_path()
        try:
            with open(path) as f:
                total = json.load(f)
        except (OSError, ValueError):
            total = {}
        total = {k: total.get(k, 0) + now[k] for k in self.KEYS} | {'sessions': total.get('sessions', 0) + 1,
                                                                     'since': total.get('since', time.strftime('%Y-%m-%d'))}
        os.makedirs(CACHE, exist_ok=True)
        with open(path + '.part', 'w') as f:
            json.dump(total, f)
        os.replace(path + '.part', path)


def stats_path():
    return os.environ.get('ZONOSVOICE_STATS') or os.path.join(CACHE, 'stats.json')   # tests keep their own


def wav_seconds(path):
    return max(0, os.path.getsize(path) - 44) / 2 / RATE


class Daemon:
    def __init__(self, args):
        self.args = args
        self.cfg = load_settings(args)
        self.log = openmw_log_path(self.cfg)
        self.backend = make_backend(self.cfg)
        self.voices = VoiceSet(self.cfg, presets=self.backend.presets)
        self.lexicon = load_lexicon()
        # Dialogue and books use separate workers, so a long book never delays an NPC's answer.
        self.pool = cf.ThreadPoolExecutor(int(self.cfg['tts']['workers']))
        self.book_pool = cf.ThreadPoolExecutor(min(2, int(self.cfg['tts']['workers'])))
        self.lock = threading.Lock()
        self.ready = []          # recent "<session> <n> <part> <nparts> <slot>" entries
        self.status = 'status ok'
        self.slot = 0
        self.lines = {}          # (session, n) -> Line
        self.inflight = 0        # parts queued or being made for the game
        self.stats = Stats()

    def write_ready(self):
        body = self.status + '\n' + ''.join(e + '\n' for e in self.ready[-32:])
        with open(READY + '.part', 'w') as f:
            f.write(body)
        os.replace(READY + '.part', READY)

    def set_status(self, status):
        with self.lock:
            if status != self.status:
                self.status = status
                self.write_ready()

    def publish(self, line, part, nparts, wav):
        with self.lock:
            name = f's{self.slot:02d}.wav'
            self.slot = (self.slot + 1) % NSLOTS
            dst = os.path.join(SLOT_DIR, name)
            shutil.copyfile(wav, dst + '.part')
            os.replace(dst + '.part', dst)
            self.ready.append(f'{line.session} {line.n} {part} {nparts} {name}')
            self.write_ready()
            if self.args.trace:
                print('TRACE', json.dumps(dict(event='part', session=line.session, n=line.n, part=part, nparts=nparts,
                                               slot=name, wav=wav, actor=line.actor, t=round(time.time() - line.t0, 2))),
                      flush=True)

    def handle(self, line):
        line.voice = self.voices.pick(line.actor, line.race, line.sex, generic=line.scope == 'generic')
        text = clean_text(line.text, self.lexicon)
        line.parts = split_parts(text, **self.backend.split)
        self.lines[(line.session, line.n)] = line
        for key in list(self.lines)[:-64]:
            del self.lines[key]
        self.stats.add(lines=1, parts=len(line.parts), **{line.scope if line.scope in ('generic', 'book') else 'speaker': 1})
        say(f'{line.actor} ({line.race} {line.sex}, {line.voice.label}) {line.kind}, {len(line.parts)} part(s): '
            f'{text[:90]}{"..." if len(text) > 90 else ""}')
        if self.args.trace:
            print('TRACE', json.dumps(dict(event='req', session=line.session, n=line.n, actor=line.actor, race=line.race,
                                           sex=line.sex, scope=line.scope, voice=line.voice.label, text=text,
                                           parts=line.parts)),
                  flush=True)
        self.submit(line, WINDOW)

    def submit(self, line, upto):
        """Queue parts up to number `upto`. Only WINDOW parts ahead of the one playing are made, so a long book
        that is closed early does not keep the server busy."""
        pool = self.book_pool if line.kind == 'book' else self.pool
        nparts = len(line.parts)

        def work(i, part):
            try:
                wav, hit = self.backend.line(part, line.voice)
                self.stats.add(**({'cached': 1, 'cached_s': wav_seconds(wav)} if hit else
                                  {'made': 1, 'made_s': wav_seconds(wav)}))
                self.publish(line, i, nparts, wav)
                self.set_status('status ok')
                if i == 1:
                    say(f'  first part ready in {time.time() - line.t0:.1f} s{" (cache)" if hit else ""}')
            except Exception as exc:   # one bad part must not stop the daemon
                self.stats.add(failed=1)
                say(f'  part {i} failed: {type(exc).__name__}: {str(exc)[:200]}')
                self.set_status(f'status error {str(exc)[:120]}')
            finally:
                with self.lock:
                    self.inflight -= 1

        while line.submitted < min(upto, nparts):
            line.submitted += 1
            with self.lock:
                self.inflight += 1
            line.futures.append(pool.submit(work, line.submitted, line.parts[line.submitted - 1]))

    def played(self, session, n, part):
        line = self.lines.get((session, int(n)))
        if line:
            self.submit(line, int(part) + WINDOW)

    def stop(self, session, n):
        line = self.lines.pop((session, int(n)), None)
        if line:
            cancelled = sum(f.cancel() for f in line.futures)
            not_made = cancelled + len(line.parts) - line.submitted
            if not_made:
                self.stats.add(not_made=not_made)
                say(f'  stopped {line.actor}: {not_made} part(s) not made')

    def prewarm(self):
        """Get every clip ready on the backend (codes, upload, embedding) in the background, so the first line of
        an NPC does not wait for it."""
        t0 = time.time()
        clips = self.voices.all_clips()
        try:
            if self.backend.serial:   # one clip at a time, and only while no game line waits for the server
                for clip in clips:
                    while self.inflight:
                        time.sleep(0.2)
                    self.backend.prepare(clip)
            else:
                with cf.ThreadPoolExecutor(4) as ex:
                    list(ex.map(self.backend.prepare, clips))
        except Exception as exc:
            say(f'voice preparation stopped: {type(exc).__name__}: {str(exc)[:160]}')
            return
        if time.time() - t0 > 2:
            say(f'{len(clips)} voices ready ({time.time() - t0:.0f} s)')

    def follow(self):
        """Yield new openmw.log lines. OpenMW truncates the log at each start; a changed first line (or a smaller
        file) means a new session, and the daemon reads that log from the top."""
        f, ino, buf, head, checked, first = None, None, b'', b'', 0.0, True
        while True:
            try:
                st = os.stat(self.log)
            except FileNotFoundError:
                time.sleep(0.5)
                continue
            restarted = f is not None and (st.st_ino != ino or st.st_size < f.tell())
            if f is not None and not restarted and time.time() - checked > 1.0:
                checked = time.time()
                with open(self.log, 'rb') as g:
                    if head:
                        restarted = g.read(len(head)) != head
                    else:
                        head = g.readline()[:120]
            if f is None or restarted:
                if f:
                    f.close()
                    self.stats.close_session()
                    say('OpenMW started a new log (new game session)')
                f, ino, buf = open(self.log, 'rb'), st.st_ino, b''
                head = f.readline()[:120]
                f.seek(0, os.SEEK_END if first and not self.args.from_start else os.SEEK_SET)
                first = False
            chunk = f.read()
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            *lines, buf = buf.split(b'\n')
            for line in lines:
                yield line.decode('utf-8', errors='replace')

    def run(self):
        if not os.path.isdir(SLOT_DIR):
            sys.exit('no slot files: run "uv run zonosvoice.py install" first')
        os.makedirs(os.path.dirname(READY), exist_ok=True)
        self.write_ready()
        try:
            self.backend.start()
        except Exception as exc:
            say(f'warning: {exc}')
        if not self.args.no_prewarm:
            threading.Thread(target=self.prewarm, daemon=True).start()
        say(f'backend {self.cfg["tts"]["backend"]} ({self.backend.tag().rsplit(":", 1)[0]}); reading {self.log}. '
            'Start OpenMW with ZonosVoice loaded. Ctrl+C stops.')
        for raw in self.follow():
            if 'ZONOSVOICE\t' not in raw:
                continue
            fields = raw.rstrip('\r').split('ZONOSVOICE\t', 1)[1].split('\t')
            if fields[0] == 'req' and len(fields) >= 10:
                _, session, n, actor, race, sex, kind, _topic, _info = fields[:9]
                if len(fields) >= 11 and fields[9] in ('speaker', 'generic', 'book'):
                    scope, text = fields[9], '\t'.join(fields[10:])
                else:   # a player script from before 0.3: no scope field
                    scope, text = 'book' if kind == 'book' else 'speaker', '\t'.join(fields[9:])
                self.handle(Line(session, int(n), actor, race, sex, kind, text, scope=scope))
            elif fields[0] == 'stop' and len(fields) >= 3:
                self.stop(fields[1], fields[2])
            elif fields[0] == 'play' and len(fields) >= 4:
                self.played(fields[1], fields[2], fields[3])


# ---------------------------------------------------------------------------------------------------- one daemon only

_daemon_lock = None


def claim_daemon(backend):
    """Only one voice program may follow openmw.log: two would both speak every line (overlapping voices) and
    overwrite each other's slot files. Takes an exclusive lock on <cache>/daemon.lock for the life of this process.
    Returns None when this process now holds it, else a description of the program that holds it."""
    global _daemon_lock
    os.makedirs(CACHE, exist_ok=True)
    info = os.path.join(CACHE, 'daemon.info')
    f = open(os.path.join(CACHE, 'daemon.lock'), 'a+')
    try:
        if sys.platform == 'win32':
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        try:
            with open(info) as g:
                return g.read().strip() or 'another ZonosVoice voice program'
        except OSError:
            return 'another ZonosVoice voice program'
    with open(info, 'w') as g:
        g.write(f'backend {backend}, process {os.getpid()}, started {time.strftime("%H:%M")}\n')
    _daemon_lock = f
    return None


def release_daemon():
    global _daemon_lock
    if _daemon_lock is not None:
        _daemon_lock.close()
        _daemon_lock = None


def already_running_text(holder):
    return (f'ZonosVoice already runs ({holder}). Only one voice program can run: two would speak every line '
            'twice. Close the other one first (or quit the game that it voices).')


def cmd_run(args):
    import signal

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)   # `play` stops the daemon with SIGTERM: print the summary then too
    holder = claim_daemon(load_settings(args)['tts']['backend'])
    if holder:
        sys.exit(already_running_text(holder))
    daemon = Daemon(args)
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.stats.close_session()


# ---------------------------------------------------------------------------------------------------- setup commands

def make_slots():
    os.makedirs(SLOT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(READY), exist_ok=True)
    for i in range(NSLOTS):
        p = os.path.join(SLOT_DIR, f's{i:02d}.wav')
        if not os.path.exists(p):
            write_wav(p, np.zeros(RATE // 10, dtype=np.float32))
    with open(READY, 'w') as f:
        f.write('status idle\n')


def slots_ready():
    return all(os.path.exists(os.path.join(SLOT_DIR, f's{i:02d}.wav')) for i in range(NSLOTS))


def add_to_openmw_cfg(cfg):
    """Add the mod's data folder and content line to openmw.cfg (with a backup). Returns a message."""
    path = openmw_cfg_path(cfg)
    if not os.path.exists(path):
        raise RuntimeError(f'{path} does not exist. Start the OpenMW launcher once, or set [game] openmw_cfg in '
                           f'{CONFIG}.')
    lines = open(path, encoding='utf-8').read().splitlines()
    here = HERE.replace('\\', '/').replace('&', '&&').replace('"', '&"')
    want = [f'data="{here}"', 'content=ZonosVoice.omwscripts']
    have = {l.strip().replace('\\', '/').lower() for l in lines}
    missing = [w for w in want if w.lower() not in have]
    if not missing:
        return f'{path} already loads ZonosVoice'
    backup = path + time.strftime('.zonosvoice-backup-%Y%m%d-%H%M%S')
    shutil.copyfile(path, backup)
    for w in missing:
        if w.startswith('data='):
            last = max([i for i, l in enumerate(lines) if l.startswith('data=')], default=len(lines) - 1)
            lines.insert(last + 1, w)
        else:
            lines.append(w)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return f'added to {path}: {", ".join(missing)} (backup: {backup})'


def cmd_install(args):
    make_slots()
    print(f'{NSLOTS} slot files in {SLOT_DIR}')
    if not args.slots_only:
        try:
            print(add_to_openmw_cfg(load_settings(args)))
        except RuntimeError as exc:
            sys.exit(str(exc))


def cmd_key(args):
    import getpass
    cfg = load_settings(args)
    key = args.value or getpass.getpass('Zyphra Cloud API key (from https://cloud.zyphra.com, Keys tab): ').strip()
    if not key:
        sys.exit('no key given')
    print(f'saved to {save_api_key(cfg, key)}')


def cmd_voices(args):
    cfg = load_settings(args)
    presets = cfg['tts']['backend'] == 'zyphra'
    voices = VoiceSet(cfg, presets=presets)
    print(f'voice clips in {VOICES} (backend {cfg["tts"]["backend"]}):')
    for race in sorted(set(RACES.values())):
        for sex in 'mf':
            key = f'{race}_{sex}'
            own = voices.pools.get(key, [])
            if own:
                text = ', '.join(os.path.basename(c) for c in own)
            elif presets:
                names = cfg['voices']['cloud_female' if sex == 'f' else 'cloud_male']
                text = 'none: Zyphra Cloud default voices (' + ', '.join(names) + ')'
            else:
                text = 'none: uses ' + voices.pick(f'any {key}', race, sex).label
            print(f'  {key:12s} {text}')
    print(f'  {"creature":12s} {", ".join(os.path.basename(c) for c in voices.pools.get("creature", [])) or "none"}')
    print(f'  {"narrator":12s} {voices.pick("narrator", "narrator", "m").label}')
    print(f'  {"npc":12s} {", ".join(sorted(voices.npc)) or "none"}')


def cmd_stats(args):
    try:
        with open(stats_path()) as f:
            total = json.load(f)
    except (OSError, ValueError):
        return print('no sessions yet')
    print(f"all sessions since {total['since']} ({total['sessions']} sessions): {Stats.text(total)}")
    audio = os.path.join(CACHE, 'audio')
    files = [os.path.join(r, n) for r, _, ns in os.walk(audio) for n in ns if n.endswith('.wav')]
    size = sum(os.path.getsize(p) for p in files)
    print(f'audio cache: {len(files)} lines, {sum(wav_seconds(p) for p in files) / 3600:.1f} h of audio, '
          f'{size / 1e6:.0f} MB in {audio}')


def cmd_play(args):
    """Run the daemon and OpenMW; stop the daemon when the game exits."""
    cfg = load_settings(args)
    holder = claim_daemon(cfg['tts']['backend'])    # check only: the `run` child below takes the lock itself
    if holder:
        sys.exit(already_running_text(holder))
    release_daemon()
    daemon_argv = [sys.executable, os.path.abspath(__file__)] + (['--backend', args.backend] if args.backend else [])
    daemon = subprocess.Popen(daemon_argv + ['run'], preexec_fn=die_with_parent if sys.platform != 'win32' else None)
    time.sleep(3)
    try:
        subprocess.run([args.openmw or cfg['game']['openmw']] + args.openmw_args)
    finally:
        daemon.terminate()


# ---------------------------------------------------------------------------------------------------- companion launcher

def gui_message(title, text, error=False):
    """A small window for messages when the program runs from a shortcut (no terminal). Falls back to print."""
    print(f'{title}: {text}', flush=True)
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, text)
        root.destroy()
    except Exception:
        pass


def gui_ask_key():
    """Ask for the Zyphra Cloud API key in a small window. Returns the key or ''."""
    try:
        import tkinter
        from tkinter import simpledialog
        root = tkinter.Tk()
        root.withdraw()
        key = simpledialog.askstring('ZonosVoice', 'Zyphra Cloud API key\n(make one at https://cloud.zyphra.com, '
                                     'Keys tab):', show='*', parent=root)
        root.destroy()
        return (key or '').strip()
    except Exception:
        return ''


def has_api_key(cfg):
    t = cfg['tts']
    return bool(os.environ.get(t['api_key_env'], '').strip() or os.path.exists(os.path.expanduser(t['api_key_file'])))


def save_api_key(cfg, key):
    path = os.path.expanduser(cfg['tts']['api_key_file'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(key + '\n')
    if sys.platform != 'win32':
        os.chmod(path, 0o600)
    return path


def find_launcher(cfg):
    """The OpenMW launcher: [game] launcher, else next to [game] openmw, else on PATH, else a usual install folder."""
    import glob
    if cfg['game'].get('launcher'):
        return os.path.expanduser(cfg['game']['launcher'])
    exe = '.exe' if sys.platform == 'win32' else ''
    candidates = []
    openmw = os.path.expanduser(cfg['game']['openmw'])
    if os.path.dirname(openmw):
        candidates.append(os.path.join(os.path.dirname(openmw), 'openmw-launcher' + exe))
    candidates.append(shutil.which('openmw-launcher' + exe) or '')
    if sys.platform == 'darwin':
        candidates += ['/Applications/OpenMW.app/Contents/MacOS/openmw-launcher',
                       os.path.expanduser('~/Applications/OpenMW.app/Contents/MacOS/openmw-launcher')]
    if sys.platform == 'win32':
        for base in (os.environ.get('ProgramFiles', ''), os.environ.get('ProgramFiles(x86)', '')):
            candidates += sorted(glob.glob(os.path.join(base, 'OpenMW*', 'openmw-launcher.exe')), reverse=True) if base else []
    found = next((os.path.normpath(c) for c in candidates if c and os.path.exists(c)), None)
    if not found and sys.platform.startswith('linux') and shutil.which('flatpak'):
        if subprocess.run(['flatpak', 'info', 'org.openmw.OpenMW'], capture_output=True).returncode == 0:
            return 'flatpak run --command=openmw-launcher org.openmw.OpenMW'
    return found


def game_running():
    """Is an OpenMW game process (not the launcher, the editor or the wizard) running?"""
    if sys.platform.startswith('linux'):
        for pid in os.listdir('/proc'):
            if pid.isdigit():
                try:
                    with open(f'/proc/{pid}/comm') as f:
                        if f.read().strip() in ('openmw', 'openmw.x86_64'):
                            return True
                except OSError:
                    pass
        return False
    if sys.platform == 'win32':
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq openmw.exe', '/NH'], capture_output=True, text=True)
        return 'openmw.exe' in out.stdout.lower()
    return subprocess.run(['pgrep', '-x', 'openmw'], capture_output=True).returncode == 0


def cmd_launcher(args):
    """Open the OpenMW launcher with the voices: the daemon runs in this process until the game exits. Also does
    the first-run setup (slot files, openmw.cfg, voice clips, API key)."""
    if not sys.stdout or not sys.stdout.isatty():   # started from a shortcut: messages go to a log file
        os.makedirs(CACHE, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(CACHE, 'launcher.log'), 'w', buffering=1, encoding='utf-8')
    cfg = load_settings(args)
    launcher = find_launcher(cfg)
    if not launcher:
        gui_message('ZonosVoice', 'The OpenMW launcher was not found. Set [game] launcher in config.toml.', error=True)
        sys.exit(1)
    if not slots_ready():
        make_slots()
        say(f'made the {NSLOTS} slot files')
    try:
        say(add_to_openmw_cfg(cfg))
    except RuntimeError as exc:
        say(f'warning: {exc}')
    voices = VoiceSet(cfg)
    if not voices.pools and not voices.npc:
        try:
            extract_voices(cfg)
        except SystemExit as exc:   # no game files found: the default voices of Zyphra Cloud still work
            say(f'voice clips not made: {exc}')
    if cfg['tts']['backend'] == 'zyphra' and not has_api_key(cfg):
        key = gui_ask_key()
        if key:
            say(f'API key saved to {save_api_key(cfg, key)}')
        else:
            gui_message('ZonosVoice', 'No Zyphra Cloud API key: the game will run without voices. Run '
                        '"uv run zonosvoice.py key", then start the launcher again.', error=True)
    daemon = None
    holder = claim_daemon(cfg['tts']['backend'])
    if holder:
        say(already_running_text(holder))
        gui_message('ZonosVoice', already_running_text(holder) + ' The launcher opens without a second voice program.',
                    error=True)
    elif cfg['tts']['backend'] != 'zyphra' or has_api_key(cfg):
        args.from_start = False
        daemon = Daemon(args)
        threading.Thread(target=daemon.run, daemon=True).start()
    say(f'opening {launcher}')
    proc = subprocess.Popen(launcher.split() if launcher.startswith('flatpak ') else [launcher])
    proc.wait()
    # The launcher starts the game detached and closes. Wait for the game to appear, then for it to exit.
    deadline = time.time() + 20
    while time.time() < deadline and not game_running():
        time.sleep(0.5)
    if not game_running():
        say('the launcher closed without starting the game; ZonosVoice stops')
    else:
        say('the game runs; ZonosVoice speaks until it exits')
        while game_running():
            time.sleep(2)
        say('the game exited; ZonosVoice stops')
    if daemon:
        daemon.stats.close_session()
    sys.stdout.flush()
    os._exit(0)   # do not wait for TTS requests that are still open


SHORTCUT_NAME = 'OpenMW Launcher (with voices)'


def cmd_shortcut(args):
    """Add (or remove) a menu shortcut that runs `zonosvoice.py launcher` without a terminal window. With
    --backend cpp it is a second shortcut, "OpenMW Launcher (with local voices)", that runs zonos3.cpp."""
    global SHORTCUT_NAME
    cfg = load_settings(args)
    script = os.path.abspath(__file__)
    uv = shutil.which('uvw') or shutil.which('uv') if sys.platform == 'win32' else shutil.which('uv')
    local = args.backend == 'cpp'
    backend_args = ['--backend', 'cpp'] if local else []
    run = ([os.path.normpath(uv), 'run', '--script', script] if uv else [sys.executable, script]) + backend_args + ['launcher']
    if local:
        SHORTCUT_NAME = 'OpenMW Launcher (with local voices)'
    if sys.platform.startswith('linux'):
        path = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')), 'applications',
                            'zonosvoice-openmw-launcher-local.desktop' if local else 'zonosvoice-openmw-launcher.desktop')
        if args.remove:
            if os.path.exists(path):
                os.remove(path)
            return print(f'removed {path}')
        launcher = find_launcher(cfg) or ''
        icon = next((p for p in (os.path.join(os.path.dirname(launcher), n) for n in ('openmw.png', 'openmw-launcher.png'))
                     if os.path.exists(p)), 'openmw')
        exec_line = ' '.join(f'"{a}"' if ' ' in a else a for a in run)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(f'[Desktop Entry]\nType=Application\nName={SHORTCUT_NAME}\n'
                    f'Comment=Opens the OpenMW launcher; ZonosVoice speaks NPC dialogue{" with zonos3.cpp on this computer" if local else ""} until the game exits\n'
                    f'Exec={exec_line}\nPath={HERE}\nIcon={icon}\nTerminal=false\nCategories=Game;RolePlaying;\n')
        os.chmod(path, 0o755)
    elif sys.platform == 'win32':
        folder = os.path.join(os.environ['APPDATA'], 'Microsoft', 'Windows', 'Start Menu', 'Programs')
        path = os.path.join(folder, SHORTCUT_NAME + '.lnk')
        if args.remove:
            if os.path.exists(path):
                os.remove(path)
            return print(f'removed {path}')
        launcher = find_launcher(cfg) or ''
        ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ZV_LNK); $s.TargetPath = $env:ZV_EXE; "
              "$s.Arguments = $env:ZV_ARGS; $s.WorkingDirectory = $env:ZV_DIR; "
              "if ($env:ZV_ICON) { $s.IconLocation = $env:ZV_ICON }; $s.WindowStyle = 7; $s.Save()")
        env = dict(os.environ, ZV_LNK=path, ZV_EXE=run[0], ZV_ARGS=subprocess.list2cmdline(run[1:]), ZV_DIR=HERE,
                   ZV_ICON=launcher if launcher.endswith('.exe') else '')
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], env=env, check=True)
    else:   # macOS: a .command file (opens a Terminal window)
        path = os.path.expanduser(f'~/Applications/{SHORTCUT_NAME}.command')
        if args.remove:
            if os.path.exists(path):
                os.remove(path)
            return print(f'removed {path}')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\ncd ' + subprocess.list2cmdline([HERE]) + ' && exec ' +
                    ' '.join(f"'{a}'" for a in run) + '\n')
        os.chmod(path, 0o755)
    print(f'added "{SHORTCUT_NAME}": {path}')


# ---------------------------------------------------------------------------------------------------- testing aids

def play_file(path):
    for argv in (['pw-play', path], ['aplay', '-q', path], ['afplay', path],
                 ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', path]):
        if shutil.which(argv[0]):
            subprocess.run(argv)
            return
    print('no audio player found; the file is', path)


def speak(backend, voices, lexicon, actor, race, sex, text):
    voice = voices.pick(actor, race, sex)
    parts = split_parts(clean_text(text, lexicon), **backend.split)
    t0 = time.time()
    backend.start()
    with cf.ThreadPoolExecutor(int(backend.cfg['tts']['workers'])) as ex:
        wavs = list(ex.map(lambda p: backend.line(p, voice)[0], parts))
    x = np.concatenate([load_audio(w) for w in wavs])
    return voice, parts, x, time.time() - t0


def cmd_say(args):
    cfg = load_settings(args)
    backend = make_backend(cfg)
    voices = VoiceSet(cfg, presets=backend.presets)
    if args.npc and not args.race:
        race, sex, name = find_npc(cfg, args.npc)
        sex = args.sex or sex
    else:
        race, sex, name = args.race or 'imperial', args.sex or 'm', args.npc
    actor = args.npc or f'{race}-{sex}-{args.seed_name}'
    voice, parts, x, took = speak(backend, voices, load_lexicon(), actor, race, sex, args.text)
    out = args.out or os.path.join(CACHE, 'last_say.wav')
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    write_wav(out, x)
    print(f'{name or actor}: {race} {sex}, voice {voice.label}, {len(parts)} part(s), {len(x) / RATE:.1f} s audio '
          f'in {took:.1f} s -> {out}')
    if not args.no_play:
        play_file(out)


SAMPLE_LINES = {
    'argonian': "This one has walked the Black Marsh and the ash wastes both. Speak, if you have business.",
    'breton': "Ah, a traveler! You look like someone who appreciates a good book. The guild has many, if you can pay.",
    'dark elf': "Another outlander. Mind your manners in Balmora, and keep your hands off what is not yours.",
    'high elf': "I suppose you want something. Be quick about it; my research will not wait on the likes of you.",
    'imperial': "Welcome, citizen. The Legion keeps the peace here, so you have nothing to fear on these roads.",
    'khajiit': "Khajiit has wares, if you have coin. Good prices, for a friend. Very good prices.",
    'nord': "Ha! You call that a sword? Come back when you have killed something bigger than a mudcrab.",
    'orc': "What do you want? Speak plainly. I have no patience for clever words.",
    'redguard': "The desert taught me patience, and the sword taught me the rest. What brings you to me?",
    'wood elf': "Oh, hello! Have you come from the forest roads? I miss the trees, you know. Nothing here but mushrooms.",
    'creature': "You have come far, Nerevar. But your journey ends here, in the heart of the mountain.",
}


def cmd_samples(args):
    cfg = load_settings(args)
    backend = make_backend(cfg)
    voices = VoiceSet(cfg, presets=backend.presets)
    lexicon = load_lexicon()
    out_dir = args.out or os.path.join(HERE, 'samples')
    os.makedirs(out_dir, exist_ok=True)
    backend.start()
    jobs = [(race, sex) for race in list(RACES) + ['creature'] for sex in ('m', 'f')
            if not (race == 'creature' and sex == 'f')]

    def one(rs):
        race, sex = rs
        voice, parts, x, took = speak(backend, voices, lexicon, f'sample {race} {sex} {args.pick}', race, sex,
                                      SAMPLE_LINES[race])
        path = os.path.join(out_dir, f'{race.replace(" ", "")}_{sex}.wav')
        write_wav(path, x)
        if voice.path:   # for listening: about 5 s of the reference, a pause, then the generated line
            ref = load_audio(voice.path)
            cut = ref[:int(5 * RATE)]
            quiet = np.nonzero(frame_db(ref[int(4 * RATE):int(7 * RATE)]) < frame_db(ref).max() - 35)[0]
            if len(quiet):
                cut = ref[:int(4 * RATE) + quiet[0] * 441]
            write_wav(path.replace('.wav', '_compare.wav'),
                      np.concatenate([cut, np.zeros(int(0.8 * RATE), np.float32), x]))
        return f'{path}: voice {voice.label}, {len(x) / RATE:.1f} s in {took:.1f} s'

    with cf.ThreadPoolExecutor(int(cfg['tts']['workers'])) as ex:
        for line in ex.map(one, jobs):
            print(line, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--version', action='version', version=f'zonosvoice {VERSION}')
    ap.add_argument('--backend', choices=['zyphra', 'cpp'], help='overrides [tts] backend in config.toml')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('extract-game-voices', help='make voice clips from your copy of Morrowind into voices/')
    p.add_argument('--force', action='store_true', help='make every clip again (replaces clips with the same name)')
    p = sub.add_parser('install', help='make the slot files and add the mod to openmw.cfg')
    p.add_argument('--slots-only', action='store_true', help='make the slot files, leave openmw.cfg alone')
    p = sub.add_parser('launcher', help='open the OpenMW launcher; the voices run until the game exits')
    p.add_argument('--trace', action='store_true', help='print a JSON TRACE line per request and per part')
    p.add_argument('--no-prewarm', action='store_true', help='do not prepare all voices on the backend at start')
    p = sub.add_parser('shortcut', help='add a menu shortcut that opens the launcher with the voices')
    p.add_argument('--remove', action='store_true', help='remove the shortcut')
    p = sub.add_parser('key', help='save your Zyphra Cloud API key')
    p.add_argument('value', nargs='?', help='the key (default: ask for it)')
    sub.add_parser('voices', help='show which voice each race and sex uses')
    sub.add_parser('stats', help='show the API requests and cache use of all sessions')
    p = sub.add_parser('run', help='the voice daemon')
    p.add_argument('--from-start', action='store_true', help='also speak requests already in openmw.log')
    p.add_argument('--trace', action='store_true', help='print a JSON TRACE line per request and per part')
    p.add_argument('--no-prewarm', action='store_true', help='do not prepare all voices on the backend at start')
    p = sub.add_parser('play', help='run the daemon and start OpenMW')
    p.add_argument('--openmw', help='the OpenMW program (default: [game] openmw, "openmw")')
    p.add_argument('openmw_args', nargs='*', help='extra OpenMW arguments (after --)')
    p = sub.add_parser('say', help='speak one line with a voice from voices/')
    p.add_argument('text')
    p.add_argument('--npc', help='NPC id; race and sex come from the masters unless given')
    p.add_argument('--race', help='race id, e.g. "dark elf" (default: from --npc, else imperial)')
    p.add_argument('--sex', choices=['m', 'f'], help='default: from --npc, else m')
    p.add_argument('--seed-name', default='test', help='without --npc: a name that picks the voice from the pool')
    p.add_argument('--out', help='also keep the wav here')
    p.add_argument('--no-play', action='store_true')
    p = sub.add_parser('samples', help='one sample per race and sex into samples/')
    p.add_argument('--out')
    p.add_argument('--pick', default='1', help='change this to hear other voices from each pool')
    args = ap.parse_args()
    {'extract-game-voices': cmd_extract, 'install': cmd_install, 'key': cmd_key, 'voices': cmd_voices, 'run': cmd_run,
     'play': cmd_play, 'say': cmd_say, 'samples': cmd_samples, 'launcher': cmd_launcher,
     'shortcut': cmd_shortcut, 'stats': cmd_stats}[args.cmd](args)


if __name__ == '__main__':
    main()
