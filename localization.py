"""NL is the canonical source language; stored analysis data stays language-neutral."""
import json
from pathlib import Path

ENGLISH = json.loads(Path(__file__).with_name("translations.json").read_text(encoding="utf-8"))
LANGUAGES = {"nl": "Nederlands", "en": "English"}


def translate(message, language="nl", **values):
    text = ENGLISH.get(message, message) if language == "en" else message
    return text.format(**values) if values else text


def translate_result(message, language):
    if not message or language != "en":
        return message
    if message in ENGLISH:
        return ENGLISH[message]
    if message.endswith(" afgerond"):
        return translate(message[:-9], language) + " completed"
    prefix = "Het videobestand kon niet worden verwerkt. "
    if message.startswith(prefix):
        return "The video file could not be processed. " + message[len(prefix):]
    suffix = " is niet geïnstalleerd of niet beschikbaar."
    if message.endswith(suffix):
        return message[:-len(suffix)] + " is not installed or is unavailable."
    return message
