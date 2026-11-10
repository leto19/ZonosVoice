-- ZonosVoice player script: speaks NPC dialogue with voices that the Zonos TTS server makes while you play.
--
-- 1. Each dialogue response (greeting, topic, persuasion) prints one ZONOSVOICE line to openmw.log: the speaker's
--    id, race, sex and the response text with the %PCName-style variables filled in. An opened book or scroll does
--    the same with the narrator voice; the reading plays on the player and stops when the book closes.
-- 2. The voice daemon (uv run zonosvoice.py run) reads the log, makes the audio and copies each part of the line into a
--    slot file under Sound/Vo/zonosvoice/. It then lists the part in zonosvoice/ready.txt.
-- 3. This script reads ready.txt every POLL seconds (onFrame runs while the dialogue window pauses the game) and
--    asks the global script to play each part on the speaker with core.sound.say, so the lips move.
-- The engine indexes data files at start, so the slot files must exist before the game starts (zonosvoice.py
-- install makes them); the daemon replaces their contents while the game runs.
local core = require('openmw.core')
local self = require('openmw.self')
local types = require('openmw.types')
local vfs = require('openmw.vfs')
local ui = require('openmw.ui')
local storage = require('openmw.storage')
local I = require('openmw.interfaces')

local READY = 'zonosvoice/ready.txt'
local SLOTS = 'Sound/Vo/zonosvoice/'
local POLL = 0.1            -- seconds between reads of ready.txt
local START_WAIT = 1.5      -- seconds for a part to start playing before it counts as failed
local GIVE_UP = 40          -- seconds to wait for the first part of a line
local VOICED = { greeting = true, topic = true, persuasion = true }

I.Settings.registerPage({
    key = 'ZonosVoice',
    l10n = 'ZonosVoice',
    name = 'pageName',
    description = 'pageDescription',
})
I.Settings.registerGroup({
    key = 'SettingsPlayerZonosVoice',
    page = 'ZonosVoice',
    l10n = 'ZonosVoice',
    name = 'groupName',
    permanentStorage = true,
    settings = {
        { key = 'enabled', renderer = 'checkbox', default = true, name = 'enabledName', description = 'enabledDescription' },
        { key = 'stopOnClose', renderer = 'checkbox', default = true, name = 'stopOnCloseName',
          description = 'stopOnCloseDescription' },
        { key = 'readBooks', renderer = 'checkbox', default = true, name = 'readBooksName',
          description = 'readBooksDescription' },
    },
})
local settings = storage.playerSection('SettingsPlayerZonosVoice')

-- One id per game session, so ready.txt entries from an earlier session (or another save) are not played.
local session = string.format('%x', math.floor((core.getRealTime() % 100000) * 1000))
local counter = 0
local current = nil         -- the line being spoken: { n, actor, parts = {part -> slot}, nparts, next, started, ... }
local lastRead, lastText = -1, nil
local warned, shownError = false, nil

local function log(...) print('ZONOSVOICE', ...) end

local function findInfo(kind, recordId, infoId)
    local store = core.dialogue[kind]
    local rec = store and store.records[recordId]
    if not rec then return nil end
    for _, info in ipairs(rec.infos) do
        if info.id == infoId then return info end
    end
end

local function className(id)
    local rec = id and id ~= '' and types.NPC.classes.records[id]
    return rec and rec.name or ''
end

local function raceName(id)
    local rec = id and id ~= '' and types.NPC.races.records[id]
    return rec and rec.name or ''
end

local function factionAndRank(actor)
    if not types.NPC.objectIsInstance(actor) then return nil, nil end
    local fid = types.NPC.getFactions(actor)[1]
    if not fid then return nil, nil end
    return core.factions.records[fid], types.NPC.getFactionRank(actor, fid)
end

local function rankName(faction, rank)
    local r = faction and rank and rank > 0 and faction.ranks[rank]
    return r and r.name or ''
end

-- The same variables as the engine's dialogue text (Interpreter::fixDefinesDialog), as far as Lua can see them.
local function fillVariables(text, actor)
    local isNpc = types.NPC.objectIsInstance(actor)
    local arec = isNpc and types.NPC.record(actor) or types.Creature.record(actor)
    local prec = types.NPC.record(self)
    local faction, rank = factionAndRank(actor)
    local pcRank = faction and types.NPC.getFactionRank(self, faction.id) or 0
    local values = {
        pcname = prec.name,
        pcrace = raceName(prec.race),
        pcclass = className(prec.class),
        name = arec.name,
        race = isNpc and raceName(arec.race) or '',
        class = isNpc and className(arec.class) or '',
        faction = faction and faction.name or '',
        rank = rankName(faction, rank),
        pcrank = rankName(faction, pcRank),
        nextpcrank = rankName(faction, pcRank + 1),
        cell = self.cell and self.cell.name or '',
        pccrimelevel = tostring(types.Player.getCrimeLevel and types.Player.getCrimeLevel(self) or 0),
    }
    return (text:gsub('%%(%a+)', function(key)
        local v = values[key:lower()]
        return v or ''        -- unknown variables (globals) are left out of the spoken text
    end))
end

local function alreadyVoiced(info)
    if info.sound and info.sound ~= '' then return true end
    local script = (info.resultScript or ''):lower()
    return script:find('say%s+"') ~= nil or script:find('say%s+sound') ~= nil
end

local function stopCurrent()
    if current then
        core.sendGlobalEvent('ZonosVoiceStop', { actor = current.actor })
        log('stop', session, current.n)
        current = nil
    end
end

local function speakerFields(actor)
    if types.NPC.objectIsInstance(actor) then
        local r = types.NPC.record(actor)
        return r.id, r.race, r.isMale and 'm' or 'f'
    end
    return actor.recordId, 'creature', 'm'
end

-- scope: "speaker" for a line that belongs to one named NPC, "generic" for a line that any NPC who matches the
-- filters can say (the daemon speaks those with one shared clip per race and sex, so they are cached once), "book".
local function request(actor, id, race, sex, kind, recordId, infoId, scope, text)
    stopCurrent()
    counter = counter + 1
    current = { n = counter, actor = actor, kind = kind, parts = {}, nparts = nil, next = 1, asked = core.getRealTime() }
    -- Fields are tab-separated; the text is the last field.
    log('req', session, counter, id, race, sex, kind, recordId, infoId, scope, text)
end

local function onDialogueResponse(d)
    if not settings:get('enabled') or not VOICED[d.type] or not d.actor then return end
    local info = findInfo(d.type, d.recordId, d.infoId)
    if not info or alreadyVoiced(info) then return end
    local text = fillVariables(info.text or '', d.actor):gsub('[\r\n\t]+', ' ')
    if not text:find('%a') then return end
    local id, race, sex = speakerFields(d.actor)
    local scope = (info.filterActorId and info.filterActorId ~= '') and 'speaker' or 'generic'
    request(d.actor, id, race, sex, d.type, d.recordId, d.infoId, scope, text)
end

-- Book markup: <BR> and <P> end a line (sent as " || " so the daemon can pause there); other tags are dropped.
local function bookText(book)
    local rec = types.Book.record(book)
    local text = (rec.text or ''):gsub('<[Bb][Rr]%s*/?>', '\n'):gsub('<[Pp]>', '\n'):gsub('<[^>]*>', '')
    text = text:gsub('&nbsp;', ' '):gsub('[ \t]*\r?\n[\r\n \t]*', ' || '):gsub('%s+', ' ')
    return rec.id, text
end

local function onUiModeChanged(data)
    if (data.newMode == 'Book' or data.newMode == 'Scroll') and data.arg and settings:get('enabled')
            and settings:get('readBooks') and types.Book.objectIsInstance(data.arg) then
        local id, text = bookText(data.arg)
        if text:find('%a') then request(self.object, 'narrator', 'narrator', 'm', 'book', id, '-', 'book', text) end
    elseif data.newMode == nil and current and (current.kind == 'book' or settings:get('stopOnClose')) then
        stopCurrent()
    end
end

local function readReady()
    local f = vfs.open(READY)
    if not f then return nil end
    local s = f:read('*a')
    f:close()
    return s
end

-- ready.txt: "status <state> <message>" then lines "<session> <n> <part> <nparts> <slot file>".
local function parseReady(s)
    if not current then return end
    for sid, n, part, nparts, slot in s:gmatch('(%x+) (%d+) (%d+) (%d+) (%S+)') do
        if sid == session and tonumber(n) == current.n then
            current.parts[tonumber(part)] = slot
            current.nparts = tonumber(nparts)
        end
    end
    local state, message = s:match('^status (%S+) ?([^\n]*)')
    if state == 'error' and message ~= shownError then
        shownError = message
        ui.showMessage('ZonosVoice: ' .. message)
    elseif state == 'ok' then
        shownError = nil
    end
end

local function advance(now)
    if not current then return end
    if current.playing then
        local active = core.sound.isSayActive(current.actor)
        if active then
            current.started = true
            return
        end
        if not current.started and now - current.playing < START_WAIT then return end
        current.playing, current.started = nil, false
        current.next = current.next + 1
    end
    if current.nparts and current.next > current.nparts then
        current = nil
        return
    end
    local slot = current.parts[current.next]
    if slot then
        core.sendGlobalEvent('ZonosVoiceSay', { actor = current.actor, file = SLOTS .. slot })
        log('play', session, current.n, current.next, slot, string.format('%.2f', now - current.asked))
        current.playing, current.started = now, false
    elseif current.next == 1 and now - current.asked > GIVE_UP then
        if not warned then
            warned = true
            ui.showMessage('ZonosVoice: no voice after ' .. GIVE_UP .. ' s. Start the voice daemon: '
                .. '"uv run zonosvoice.py run" in the ZonosVoice folder.')
        end
        current = nil
    end
end

return {
    -- For other scripts (and test_ingame.py): I.ZonosVoice.isSpeaking() is true from a dialogue response until its
    -- last part has played, or until the line is stopped or given up.
    interfaceName = 'ZonosVoice',
    interface = {
        version = 1,
        isSpeaking = function() return current ~= nil end,
    },
    eventHandlers = {
        DialogueResponse = onDialogueResponse,
        UiModeChanged = onUiModeChanged,
    },
    engineHandlers = {
        onFrame = function()
            if not current then return end
            local now = core.getRealTime()
            if now - lastRead >= POLL then
                lastRead = now
                local s = readReady()
                if s and s ~= lastText then
                    lastText = s
                    parseReady(s)
                end
            end
            advance(now)
        end,
    },
}
