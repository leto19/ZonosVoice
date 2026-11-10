#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy", "scipy", "soundfile"]
# ///
"""Developer tool (not in the release): end-to-end test of ZonosVoice in the real game, without sound on your
speakers. Needs the repository's tools/ (in-game harness helpers), Xephyr and the Checks-Twice test save.

  ./test_ingame.py                      the default NPC list (all 10 races + Caius)
  ./test_ingame.py --npc "Balmora, Eight Plates=dulnea ralaal" --book bk_firmament:30 ...

1. Starts the voice daemon (zonosvoice.py run --trace).
2. Starts OpenMW in a nested Xephyr display (:77) with the masters, the Checks-Twice test save, ZonosVoice and a
   small test script. OpenAL writes the game's sound to a WAV file (its "wave" driver) instead of the speakers.
3. The test script teleports the player to each NPC, starts the conversation (activateBy, as a click does), waits
   until ZonosVoice has spoken the whole line (I.ZonosVoice.isSpeaking), closes the dialogue window and goes to the
   next NPC. Then it puts each book in the player's inventory, opens it, and closes it at the end of the reading or
   after the given number of seconds.
4. Afterwards, each part the daemon made is found in the recorded game sound by cross-correlation.
   A part counts as played when the correlation is 0.9 or more.
Output: a table per NPC and the files in --out (game_audio.wav, daemon.log, openmw.log).
"""
import argparse, json, os, shutil, subprocess, sys, time

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'tools'))
from ingame_test import DATA_FILES, LOG, find_plugin, save_masters  # noqa: E402

SAVE = os.path.expanduser('~/.local/share/openmw/saves/Checks_Twice/test_start.omwsave')
DEFAULT_NPCS = [
    'Balmora, Eight Plates=dulnea ralaal',             # Dark Elf F
    'Balmora, Eight Plates=itermerel',                 # High Elf M
    'Balmora, South Wall Cornerclub=bacola closcius',  # Imperial M
    'Balmora, South Wall Cornerclub=sottilde',         # Nord F
    'Balmora, South Wall Cornerclub=chirranirr',       # Khajiit F
    'Balmora, South Wall Cornerclub=arathor',          # Wood Elf M
    'Balmora, Guild of Mages=sharn gra-muzgob',        # Orc F
    'Balmora, Guild of Mages=masalinie merian',        # Breton F
    'Balmora, Guild of Fighters=wayn',                 # Redguard M
    'Balmora, Itan\'s House=itan',                     # Argonian M
    'Balmora, Caius Cosades\' House=caius cosades',    # override voice (no request when CaiusVoiced is loaded)
    'Balmora, South Wall Cornerclub=mib_nestd_jold',   # Orc M from NestedInBalmora: needs --plugin NestedInBalmora.esp
]
DEFAULT_BOOKS = [
    'bk_BlueBookOfRiddles',        # 408 characters: read to the end
    'BookSkill_Alteration3:20',    # "Sithis", 1509 characters: closed after 20 s, the rest is not made
]

PLAYER = r"""
local core = require('openmw.core')
local self = require('openmw.self')
local types = require('openmw.types')
local I = require('openmw.interfaces')
local PLAN = { %(plan)s }
local step, phase, t0, npc, wasActive, book = 0, 'next', nil, nil, false, nil
local function log(...) print('ZVE2E', ...) end
return {
    eventHandlers = {
        ZVE2ENpc = function(e)
            npc = e.npc
            log('found', PLAN[step].id, tostring(npc ~= nil))
        end,
        ZVE2EBook = function(e) book = e.item end,
    },
    engineHandlers = {
        onFrame = function()
            local now = core.getRealTime()
            if not t0 then t0 = now; log('started') end
            if phase == 'next' and now - t0 > 3 then
                step = step + 1
                if step > #PLAN then log('done'); core.quit(); phase = 'quit'; return end
                npc, wasActive = nil, false
                if PLAN[step].book then
                    core.sendGlobalEvent('ZVE2EGiveBook', { id = PLAN[step].book, player = self.object })
                    phase, t0 = 'book', now
                    return
                end
                core.sendGlobalEvent('ZVE2EGo', PLAN[step])
                phase, t0 = 'arrive', now
            elseif phase == 'book' and book and now - t0 > 1 then
                I.UI.setMode(types.Book.record(book).isScroll and 'Scroll' or 'Book', { target = book })
                log('open book', PLAN[step].book, string.format('%%.3f', now))
                phase, t0 = 'read', now
            elseif phase == 'read' then
                local limit = PLAN[step].limit
                if not I.ZonosVoice.isSpeaking() and now - t0 > 2 then
                    log('book done', PLAN[step].book, string.format('%%.2f', now - t0))
                    book, phase, t0 = nil, 'close', now
                elseif limit and now - t0 > limit then
                    log('book closed early', PLAN[step].book, string.format('%%.2f', now - t0))
                    book, phase, t0 = nil, 'close', now
                elseif now - t0 > %(talk_timeout)f * 4 then
                    log('timeout', PLAN[step].book)
                    book, phase, t0 = nil, 'close', now
                end
            elseif phase == 'arrive' and now - t0 > 2.5 then
                if not npc then log('skip', PLAN[step].id); phase, t0 = 'next', now - 3; return end
                core.sendGlobalEvent('ZVE2EActivate', { npc = npc })
                log('talk', PLAN[step].id, string.format('%%.3f', now))
                phase, t0 = 'talk', now
            elseif phase == 'talk' then
                -- I.ZonosVoice.isSpeaking() covers the whole line (all parts), not a vanilla "Hello" voice
                local speaking = I.ZonosVoice.isSpeaking()
                local active = core.sound.isSayActive(npc)
                if active and not wasActive then log('voice start', PLAN[step].id, string.format('%%.2f', now - t0)) end
                if wasActive and not active then log('voice end', PLAN[step].id, string.format('%%.2f', now - t0)) end
                wasActive = active
                if not speaking and now - t0 > 1 then
                    log('line done', PLAN[step].id, string.format('%%.2f', now - t0))
                    phase, t0 = 'close', now
                elseif now - t0 > %(talk_timeout)f then
                    log('timeout', PLAN[step].id)
                    phase, t0 = 'close', now
                end
            elseif phase == 'close' and now - t0 > 1.0 then
                I.UI.setMode()
                phase, t0 = 'next', now - 2
            end
        end,
    },
}
"""

GLOBAL = r"""
local world = require('openmw.world')
local types = require('openmw.types')
local util = require('openmw.util')
return {
    eventHandlers = {
        ZVE2EGo = function(e)
            local p = world.players[1]
            local cell = world.getCellByName(e.cell)
            for _, n in ipairs(cell:getAll(types.NPC)) do
                if n.recordId == e.id then
                    local front = n.rotation:apply(util.vector3(0, 1, 0))
                    p:teleport(cell, n.position + front * 80, { onGround = true })
                    p:sendEvent('ZVE2ENpc', { npc = n })
                    return
                end
            end
            p:sendEvent('ZVE2ENpc', { npc = nil })
        end,
        ZVE2EActivate = function(e) e.npc:activateBy(world.players[1]) end,
        ZVE2EGiveBook = function(e)
            local item = world.createObject(e.id, 1)
            item:moveInto(types.Actor.inventory(e.player))
            e.player:sendEvent('ZVE2EBook', { item = item })
        end,
    },
}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--npc', action='append', help='"<cell>=<npc id>" (repeatable); default: all 10 races + Caius')
    ap.add_argument('--book', action='append', help='"<book id>[:<seconds before closing>]" (repeatable)')
    ap.add_argument('--out', default=os.path.join(HERE, 'test_out'))
    ap.add_argument('--talk-timeout', type=float, default=45.0)
    ap.add_argument('--save', default=SAVE)
    ap.add_argument('--plugin', action='append', default=[], help='extra plugin (.esp) to load, e.g. CaiusVoiced')
    ap.add_argument('--backend', choices=['zyphra', 'cpp'], help='TTS backend for the daemon')
    args = ap.parse_args()
    use_default = not args.npc and not args.book
    npcs = [n.split('=', 1) for n in (args.npc or (DEFAULT_NPCS if use_default else []))]
    books = [b.split(':') for b in (args.book or (DEFAULT_BOOKS if use_default else []))]
    shutil.rmtree(args.out, ignore_errors=True)
    tmp = os.path.join(args.out, 'data')
    os.makedirs(os.path.join(tmp, 'scripts', 'zve2e'))
    plan = ', '.join(['{ cell = %s, id = %s }' % (json.dumps(c), json.dumps(i)) for c, i in npcs] +
                     ['{ book = %s, limit = %s }' % (json.dumps(b[0]), b[1] if len(b) > 1 else 'nil') for b in books])
    open(os.path.join(tmp, 'scripts/zve2e/player.lua'), 'w').write(PLAYER % dict(plan=plan, talk_timeout=args.talk_timeout))
    open(os.path.join(tmp, 'scripts/zve2e/global.lua'), 'w').write(GLOBAL)
    open(os.path.join(tmp, 'ZVE2E.omwscripts'), 'w').write('PLAYER: scripts/zve2e/player.lua\nGLOBAL: scripts/zve2e/global.lua\n')
    open(os.path.join(tmp, 'mwtest.omwscripts'), 'w').write('')   # the test save was made with the harness script
    audio = os.path.join(args.out, 'game_audio.wav')
    conf = os.path.join(args.out, 'alsoft.conf')
    open(conf, 'w').write(f'[general]\ndrivers = wave\nchannels = stereo\nfrequency = 44100\n[wave]\nfile = {audio}\n')

    data_dirs, content = [DATA_FILES], ['Morrowind.esm', 'Tribunal.esm', 'Bloodmoon.esm']
    for m in save_masters(args.save):
        p = find_plugin(m)
        if p and m.lower() not in (c.lower() for c in content):
            if os.path.dirname(p) not in data_dirs:
                data_dirs.append(os.path.dirname(p))
            content.append(m)
    for p in args.plugin:
        p = os.path.abspath(p)
        if os.path.dirname(p) not in data_dirs:
            data_dirs.append(os.path.dirname(p))
        content.append(os.path.basename(p))
    data_dirs += [HERE, tmp]
    content += ['mwtest.omwscripts', 'ZonosVoice.omwscripts', 'ZVE2E.omwscripts']
    cmd = ['openmw', '--replace=data', '--replace=content', '--skip-menu', '--load-savegame', args.save]
    for d in data_dirs:
        cmd += ['--data', d]
    for c in content:
        cmd += ['--content', c]

    subprocess.run([sys.executable, os.path.join(HERE, 'zonosvoice.py'), 'install', '--slots-only'], check=True, capture_output=True)
    daemon_log = open(os.path.join(args.out, 'daemon.log'), 'w')
    daemon = subprocess.Popen([sys.executable, os.path.join(HERE, 'zonosvoice.py')] +
                              (['--backend', args.backend] if args.backend else []) + ['run', '--trace'],
                              stdout=daemon_log, stderr=subprocess.STDOUT,
                              env=dict(os.environ, ZONOSVOICE_STATS=os.path.join(args.out, 'stats.json')))
    time.sleep(4)   # the daemon starts and begins to prepare the voices
    xephyr = subprocess.Popen(['Xephyr', ':77', '-screen', '1280x720', '-ac', '-br', '-noreset'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    env = dict(os.environ, DISPLAY=':77', SDL_VIDEODRIVER='x11', ALSOFT_CONF=conf)
    env.pop('WAYLAND_DISPLAY', None)
    print('running OpenMW:', ' '.join(f'"{c}"' if ' ' in c else c for c in cmd), flush=True)
    game = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        game.wait(timeout=60 + len(npcs) * (args.talk_timeout + 15) + len(books) * (args.talk_timeout * 4 + 15))
    except subprocess.TimeoutExpired:
        game.kill()
        print('OpenMW did not quit in time; killed')
    finally:
        xephyr.terminate()
        time.sleep(1)
        daemon.terminate()   # the daemon prints its session summary on SIGTERM
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        daemon_log.close()
    shutil.copyfile(LOG, os.path.join(args.out, 'openmw.log'))
    report(args.out, audio)


def report(out, audio):
    reqs, parts, events = {}, [], []
    for line in open(os.path.join(out, 'daemon.log'), errors='replace'):
        if 'stopped' in line or 'failed' in line or 'session summary' in line:
            events.append('daemon:' + line.strip()[10:])
        if line.startswith('TRACE '):
            e = json.loads(line[6:])
            if e['event'] == 'req':
                reqs[(e['session'], e['n'])] = e
            else:
                parts.append(e)
    for line in open(os.path.join(out, 'openmw.log'), errors='replace'):
        if 'ZVE2E\t' in line or 'ZONOSVOICE\tstop' in line or 'ZONOSVOICE\tplay' in line:
            events.append(line.split(']:', 1)[-1].strip().replace('\t', ' '))
        elif ' E] ' in line and 'zonosvoice' in line.lower():
            events.append('ERROR ' + line.strip())
    sr, game = wavfile.read(audio)
    game = game.astype(np.float32)
    game = game.mean(axis=1) if game.ndim > 1 else game
    print(f'\ngame sound: {len(game) / sr:.1f} s recorded')
    print('\n'.join('  ' + e for e in events))
    played = 0
    for e in parts:
        clip = wavfile.read(e['wav'])[1].astype(np.float32) / 32768
        corr = fftconvolve(game, clip[::-1], mode='valid')
        k = int(np.argmax(np.abs(corr)))
        seg = game[k:k + len(clip)]
        c = float(np.corrcoef(seg, clip)[0, 1]) if len(seg) == len(clip) and seg.std() > 0 else 0.0
        e['corr'], e['at'] = c, k / sr
        played += c >= 0.9
    print(f'\n{"NPC":22s} {"race/sex":12s} {"voice":34s} {"part":5s} {"ready":>6s} {"played at":>9s} {"corr":>5s}  text')
    for (session, n), r in reqs.items():
        for e in sorted((p for p in parts if p['session'] == session and p['n'] == n), key=lambda p: p['part']):
            text = r['parts'][e['part'] - 1]
            print(f'{r["actor"][:22]:22s} {r["race"][:9] + " " + r["sex"]:12s} {r["voice"][:34]:34s} '
                  f'{e["part"]}/{e["nparts"]:<3d} {e["t"]:5.1f}s {e["at"]:8.1f}s {e["corr"]:5.2f}  {text[:60]}')
    print(f'\n{len(reqs)} lines, {len(parts)} parts made, {played} found in the game sound (corr >= 0.9)')


if __name__ == '__main__':
    main()
