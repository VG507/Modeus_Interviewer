"""Recognize text that gives orders to the model instead of describing work.

Patterns look for commands addressed to the bot (imperatives, role changes, chat markup),
not for words about security, so an engineer may still tell how they fought injections.
"""

import re
import unicodedata

INVISIBLE = re.compile(r"[­​-‏⁠-⁤﻿]")
SEPARATED_LETTERS = re.compile(r"(?<!\w)(?:\w[ .\-_*·]){4,}\w(?!\w)")
MIXED_WORD = re.compile(r"\w*[а-я]\w*[a-z0-9]\w*|\w*[a-z0-9]\w*[а-я]\w*")
# Latin letters and digits that pass for Cyrillic inside a Russian word.
LOOKALIKES = str.maketrans("aceopxykmthb30", "асеорхукмтнвзо")

IMPERATIVE_OVERRIDE = r"(?:(?:про)?игнорируй|забудь|отмени|сбрось|обойди|отключи|не\s+(?:следуй|соблюдай|выполняй|слушай))(?:те)?"
RULES = r"(?:инструкц|указани|правил|промпт|prompt|ограничени|установк|директив)\w*"
PATTERNS = (
    ("override", rf"\b{IMPERATIVE_OVERRIDE}\b(?:\W+\w+){{0,4}}?\W+{RULES}"),
    ("override", rf"\b(?:(?:про)?игнорируй|забудь)(?:те)?\W+(?:вс[её]|предыдущ\w*|сказанное|написанное)"
                 r"(?:\W+\w+){0,3}?\W+(?:выше|раньше|ранее|до\s+этого)"),
    ("override", r"\bнов\w+\s+(?:инструкц\w*|правил\w*|указани\w*)\s*:"),
    ("override", r"\b(?:тво[яй]\s+(?:нов\w+\s+)?(?:задача|роль|цель)\s+(?:теперь|[—:-])|теперь\s+тво[яй]\s+(?:задача|роль|цель))"),
    ("override", r"\b(?:ignore|disregard|forget|override|bypass)\b(?:\W+\w+){0,4}?\W+(?:instructions?|rules|prompts?|guidelines|directives)\b"),
    ("override", r"\bnew\s+instructions?\s*:"),
    ("override", r"\b(?:ignoriru\w*|zabud\w*)\b(?:\W+\w+){0,3}?\W+(?:instrukc\w*|pravil\w*|prompt\w*)"),
    ("leak", r"\b(?:покажи|выведи|повтори|напиши|раскрой|процитируй|перескажи|скопируй|распечатай|озвучь|отправь|дай|перечисли)(?:те)?\b"
             r"(?:\W+\w+){0,6}?\W+(?:сво\w+|тво\w+|ваш\w*|системн\w*|исходн\w*|скрыт\w*|первоначальн\w*|начальн\w*)\W+"
             r"(?:\w+\W+)?(?:промпт|инструкц|правил|настройк|сообщени)\w*"),
    ("leak", r"\b(?:повтори|выведи|процитируй|скопируй)(?:те)?\b(?:\W+\w+){0,4}?\W+(?:текст\W+)?(?:выше|сверху)\b"),
    ("leak", r"\b(?:промпт|инструкц)\w*\W+(?:\w+\W+)?у\s+(?:тебя|вас)\b|\bу\s+(?:тебя|вас)\W+(?:\w+\W+){0,2}(?:промпт|инструкц)\w*"),
    ("leak", r"\b(?:reveal|show|print|repeat|output|display|tell\s+me)\b(?:\W+\w+){0,3}?\W+"
             r"(?:system\s+prompt|your\s+(?:instructions|prompt|rules)|initial\s+(?:prompt|instructions)|the\s+text\s+above)"),
    ("leak", r"\bwhat\s+(?:is|are)\s+your\s+(?:system\s+)?(?:prompt|instructions|rules)\b"),
    ("roleplay", r"\b(?:ты|вы)\s+(?:теперь|отныне|больше\s+не)\b"),
    ("roleplay", r"\bпредставь(?:те)?\W+(?:себе\W+)?что\s+(?:ты|вы)\b"),
    ("roleplay", r"\b(?:притворись|притворитесь|веди(?:те)?\s+себя\s+как|сыграй(?:те)?\s+роль|играй(?:те)?\s+роль|войди(?:те)?\s+в\s+роль)"),
    ("roleplay", r"\b(?:ты|вы)\s+(?:\w+\s+){0,2}без\s+(?:каких-либо\s+|всяких\s+)?(?:ограничений|цензуры|фильтров|правил)\b"),
    ("roleplay", r"\bрежим\w*\s+(?:разработчика|бога|без\s+ограничений)|\bdeveloper\s+mode\b|\bjail\s*break\w*|\bджейлбрейк\w*"
                 r"|\bdo\s+anything\s+now\b|(?:\bты|\byou\s+are|\bрежим\w*|\bmode)\W+dan\b|\bdan\W+(?:mode|режим)"),
    ("roleplay", r"\byou\s+are\s+now\b|\bpretend\s+(?:to\s+be|you\s+are)\b|\brole-?play\b"
                 r"|\bact\s+as\s+(?:an?\s+|my\s+)?(?:\w+\s+)?(?:assistant|ai|gpt|chatbot|character|hacker|dan)\b"),
    ("format", r"<\s*/?\s*(?:system|assistant|user|developer|instructions?|prompt|answer|sentences?)\s*>"),
    ("format", r"(?:^|\n)\s*(?:system|assistant|developer|системное\s+сообщение|ассистент)\s*:"),
    ("format", r"\[/?\s*(?:inst|system|sys)\s*\]|<<\s*/?sys\s*>>|<\|im_(?:start|end)\|>|<\|(?:system|user|assistant)\|>"),
    ("format", r"[\"'«]?\b(?:refused|answer_fit|topic_title|source_number|not_applicable|fact_numbers)\b[\"'»]?\s*[:=]"),
    ("format", r"#{2,}\s*(?:instruction|system|инструкц)"),
    ("output", r"\bв\s+(?:следующ\w+|сво[её]м|ответн\w+)\s+(?:вопрос|ответ|сообщени)\w*\W+(?:\w+\W+){0,2}?"
               r"(?:напиши|вставь|добавь|укажи|упомяни|скажи|спроси)"),
    ("output", r"\b(?:назови|озаглавь|подпиши)(?:те)?\s+(?:эту\s+|мою\s+|этот\s+)?(?:тему|случай|карточку|историю)\b"),
    ("output", r"\b(?:в\s+)?(?:названи|заголовк)\w*\s+(?:темы|карточки)\W+(?:\w+\W+)?(?:напиши|укажи|поставь|должно\s+быть)"),
    ("output", r"\b(?:напиши|скажи|ответь|выведи)(?:те)?\s+(?:мне\s+)?(?:только|лишь|одно\s+слово|одним\s+словом)\b"),
    ("output", r"\b(?:ответь|отвечай)(?:те)?\s+(?:мне\s+)?(?:на\s+английском|по-английски|in\s+english)\b"),
    ("output", r"\b(?:напиши|вставь|добавь|укажи|упомяни)(?:те)?\s+в\s+(?:следующ\w+\s+)?(?:вопрос|ответ|тем|карточк|заголов)\w*"),
)
COMPILED = tuple((category, re.compile(pattern)) for category, pattern in PATTERNS)


def normalized(text):
    """Undo the usual disguises: invisible characters, s p a c e d letters and Latin letters in Russian words."""
    text = INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).casefold().replace("ё", "е")
    text = SEPARATED_LETTERS.sub(lambda match: re.sub(r"[ .\-_*·]", "", match.group()), text)
    return MIXED_WORD.sub(lambda match: match.group().translate(LOOKALIKES), text)


def injection_attempt(text):
    """The kind of command found in the text, or None for an ordinary answer."""
    if not text:
        return None
    text = normalized(text)
    return next((category for category, pattern in COMPILED if pattern.search(text)), None)
