"""Making sure the caller's answers reach the agent: relay logging, an echo check and a silence fallback.

Twilio's echoPromptFiltering drops words from the caller's transcript that match what the agent just
said. An answer made only of such words ("Royal Adelaide" after "Royal Adelaide or Royal North Shore?")
can vanish with no message at all, so it is off. In its place this module checks, in the app, only
the case echo actually causes: a longer prompt that arrives while (or just after) the agent was
speaking and repeats the agent's own words in order. Short answers are never treated as echo.

Twilio documents no "playback finished" message, so playback time is estimated from the words sent.
"""
import re

SILENCE_SECONDS = 9.0       # after the agent's question has finished playing
WORDS_PER_SECOND = 2.6      # ElevenLabs at normal pace, about 155 words a minute
PLAYBACK_MARGIN = 0.8       # TTS start-up and the gap before the first word
ECHO_MIN_WORDS = 4          # shorter prompts are always the caller's (names, yes/no, bed numbers)
ECHO_AFTER_PLAYBACK = 1.5   # seconds after estimated playback in which echo can still arrive
REPROMPT = "Sorry, I didn't catch that."
CALLER_MESSAGES = {'prompt', 'interrupt', 'dtmf'}  # what counts towards the per-call message limit

_WORD = re.compile(r"[a-z0-9']+")


def words(text):
    return _WORD.findall(str(text).lower().replace('’', "'"))


def playback_seconds(text):
    """Estimated time to speak text aloud."""
    return PLAYBACK_MARGIN + len(words(text)) / WORDS_PER_SECOND


def is_echo(prompt, agent_text):
    """True when the prompt is the agent's own speech picked up by the caller's microphone: at least
    ECHO_MIN_WORDS words, all of them a run of consecutive words the agent was saying."""
    said, heard = words(agent_text), words(prompt)
    if len(heard) < ECHO_MIN_WORDS:
        return False
    return any(said[i:i + len(heard)] == heard for i in range(len(said) - len(heard) + 1))


def sentences(text):
    return [s for s in re.split(r'(?<=[.!?])\s+', str(text).strip()) if s]


def question_in(reply):
    """The question a reply ends with, or None."""
    parts = sentences(reply)
    return parts[-1] if parts and parts[-1].endswith('?') else None


def reprompt_for(reply, readback=None):
    """What to say when the caller hasn't answered a question: the read-back again if that was the
    question, otherwise the question (with the sentence before it when the question is very short)."""
    question = question_in(reply)
    if not question:
        return None
    if readback and readback in reply:
        return f'{REPROMPT} {readback}'
    parts = sentences(reply)
    if len(words(question)) <= 4 and len(parts) > 1:  # "Can you see it?" needs what "it" is
        question = f'{parts[-2]} {question}'
    return f'{REPROMPT} {question}'


_NEVER_LOGGED = {'customParameters', 'from', 'to', 'forwardedFrom', 'callerName', 'accountSid', 'parentCallSid', 'callSid'}


def relay_record(message):
    """A Twilio ConversationRelay message as a transcript event: its type and fields, with phone
    numbers, account and the call's nonce left out and long values shortened."""
    if not isinstance(message, dict):
        return {'type': 'unreadable'}
    record = {}
    for key, value in message.items():
        if key in _NEVER_LOGGED:
            continue
        if isinstance(value, (dict, list)):
            value = str(value)
        if isinstance(value, str) and len(value) > 300:
            value = value[:299] + '…'
        record[key] = value
    record.setdefault('type', 'unknown')
    return record
