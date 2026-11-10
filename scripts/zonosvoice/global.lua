-- ZonosVoice global script: plays a voice part on the speaker. A local script may only call core.sound.say on
-- itself, so the player script sends these events here. Events still run while the dialogue window pauses the game.
local core = require('openmw.core')

return {
    eventHandlers = {
        ZonosVoiceSay = function(e)
            if e.actor and e.actor:isValid() then core.sound.say(e.file, e.actor) end
        end,
        ZonosVoiceStop = function(e)
            if e.actor and e.actor:isValid() and core.sound.isSayActive(e.actor) then core.sound.stopSay(e.actor) end
        end,
    },
}
