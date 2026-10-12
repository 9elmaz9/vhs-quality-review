"""Validated per-job options, never mutable global detector thresholds."""
import math

DETECTORS = {
    "BLACK": ("Zwart beeld", 10.0),
    "FREEZE": ("Stilstaand beeld", 5.0),
    "GLITCH": ("Kleurafwijkingen", 10.0),
    "RUIS/STRIPES": ("Grijze ruis / strepen", 10.0),
    "1KHZ_TONE": ("1 kHz-toon", 5.0),
}


def validate_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Ongeldige analyse-instellingen.")
    selected = value.get("detectors", list(DETECTORS))
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) or x not in DETECTORS for x in selected):
        raise ValueError("Selecteer minimaal één geldige controle.")
    durations = value.get("min_durations", {})
    if not isinstance(durations, dict):
        raise ValueError("Ongeldige minimumduur.")
    clean = {}
    for key, (_, default) in DETECTORS.items():
        raw = durations.get(key, default)
        try:
            number = float(raw)
        except (ValueError, TypeError):
            raise ValueError("De minimumduur moet een getal zijn.")
        if isinstance(raw, bool) or not math.isfinite(number) or not 0.5 <= number <= 600:
            raise ValueError("De minimumduur moet tussen 0,5 en 600 seconden liggen.")
        clean[key] = number
    return {"detectors": [key for key in DETECTORS if key in selected], "min_durations": clean}
