#!/usr/bin/env python3
"""Monitor DidUP (Argo Famiglia): nuovi voti e bacheca -> notifiche ntfy.

Variabili d'ambiente (da impostare come GitHub Secrets):
    DIDUP_SCHOOL    codice scuola (es. SC12345)
    DIDUP_USER      nome utente
    DIDUP_PASSWORD  password
    NTFY_TOPIC      (opzionale) default: Morselli_alerts_didup
    STATE_FILE      (opzionale) default: state.json

Uso:
    python monitor.py          # esecuzione normale
    python monitor.py --dump   # stampa SOLO i nomi dei campi dei modelli
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

import requests
from didupwrapper import DiDUPClientSync

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "Morselli_alerts_didup")
NTFY_URL = "https://ntfy.sh"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Oltre questa soglia di novità per sezione, invio un solo riepilogo
MAX_NOTIFICHE = 10

# Campi "identificativo" da cercare, in ordine. Se nessuno esiste si usa un hash.
ID_FIELDS = {
    "voti": (),
    "bacheca": ("prg_messaggio", "prg_bacheca", "prg", "id"),
}
# Campi che cambiano nel tempo senza che l'elemento sia "nuovo" (es. presa visione)
NOISY_HINTS = ("visione", "letto", "letta", "conferma", "read")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("monitor")


# ----------------------------------------------------------------- utilità
def as_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):  # Pydantic v2
        return item.model_dump(mode="json")
    return dict(vars(item))


def item_key(item: Any, id_fields: tuple[str, ...]) -> str:
    data = as_dict(item)
    for field in id_fields:
        value = data.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    stable = {
        k: v
        for k, v in data.items()
        if v is not None
        and not isinstance(v, bool)
        and not any(h in k.lower() for h in NOISY_HINTS)
    }
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return "h:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def first_attr(item: Any, names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = getattr(item, name, None)
        if value not in (None, ""):
            return str(value)
    return default


# ----------------------------------------------------------------- stato
def load_state() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.warning("state.json illeggibile: riparto da zero (nessuna notifica)")
        return {}


def save_state(state: dict[str, list[str]]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------- ntfy
def notify(title: str, message: str, tags: list[str], priority: int = 3) -> bool:
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message[:1500],
        "priority": priority,
        "tags": tags,
    }
    try:
        resp = requests.post(NTFY_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Invio ntfy fallito: %s", exc)
        return False


# ----------------------------------------------------------------- formattazione
def fmt_voto(v: Any) -> tuple[str, str, list[str]]:
    materia = first_attr(v, ("des_materia",), "Materia")
    valore = first_attr(v, ("cod_codice", "dec_valore"), "?")
    data = first_attr(v, ("dat_giorno",))
    prova = first_attr(v, ("des_prova", "des_commento"))
    righe = [f"{materia}: {valore}"]
    if data:
        righe.append(f"Data: {data}")
    if prova:
        righe.append(prova)
    return "Nuovo voto", "\n".join(righe), ["books"]


def fmt_bacheca(c: Any) -> tuple[str, str, list[str]]:
    oggetto = first_attr(
        c, ("des_oggetto", "oggetto", "titolo", "des_titolo"), "Nuova comunicazione"
    )
    testo = first_attr(c, ("des_messaggio", "messaggio", "testo", "des_testo"))
    data = first_attr(c, ("dat_giorno", "data"))
    righe = [oggetto]
    if data:
        righe.append(f"Data: {data}")
    if testo:
        righe.append(testo)
    return "Bacheca DidUP", "\n".join(righe), ["bell"]


# ----------------------------------------------------------------- logica
def process_section(
    name: str,
    items: list[Any],
    state: dict[str, list[str]],
    formatter: Callable[[Any], tuple[str, str, list[str]]],
) -> None:
    current: dict[str, Any] = {}
    for item in items:
        base = key = item_key(item, ID_FIELDS[name])
        n = 1
        while key in current:  # elementi identici (stesso hash) restano distinti
            n += 1
            key = f"{base}#{n}"
        current[key] = item

    if name not in state:  # primo avvio: salvo la base, nessuna notifica
        state[name] = sorted(current)
        log.info("[%s] primo avvio: %d elementi salvati, nessuna notifica", name, len(current))
        return

    seen = set(state[name])
    new = [(k, i) for k, i in current.items() if k not in seen]
    log.info("[%s] %d elementi, %d nuovi", name, len(current), len(new))

    if len(new) > MAX_NOTIFICHE:
        if notify(f"{len(new)} novità su DidUP ({name})", "Apri il registro per i dettagli.", ["warning"]):
            seen.update(k for k, _ in new)
    else:
        for key, item in new:
            title, message, tags = formatter(item)
            if notify(title, message, tags):
                seen.add(key)  # se ntfy fallisce, ritenta alla prossima esecuzione

    state[name] = sorted(seen)


def fetch_all(school: str, user: str, password: str) -> dict[str, list[Any] | None]:
    """Login + download. Riprova con auto-rilevamento versione app se il primo tentativo fallisce."""
    last_exc: Exception | None = None
    for auto in (False, True):
        try:
            kwargs = {"auto_versione": True} if auto else {}
            with DiDUPClientSync(school, user, password, **kwargs) as didup:
                out: dict[str, list[Any] | None] = {}
                for name, getter in (("voti", didup.get_voti), ("bacheca", didup.get_bacheca)):
                    try:
                        out[name] = list(getter())
                    except Exception:
                        log.exception("[%s] download fallito", name)
                        out[name] = None
                return out
        except Exception as exc:  # login/rete
            last_exc = exc
            log.warning("Tentativo (auto_versione=%s) fallito: %s", auto, exc)
    raise RuntimeError(f"Login DidUP non riuscito: {last_exc}")


def main() -> int:
    school = os.environ.get("DIDUP_SCHOOL")
    user = os.environ.get("DIDUP_USER")
    password = os.environ.get("DIDUP_PASSWORD")
    if not (school and user and password):
        log.error("Mancano DIDUP_SCHOOL / DIDUP_USER / DIDUP_PASSWORD")
        return 2

    try:
        data = fetch_all(school, user, password)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    if "--dump" in sys.argv:
        for name, items in data.items():
            fields = list(as_dict(items[0]).keys()) if items else []
            print(f"{name}: {fields}")
        return 0

    state = load_state()
    ok = True
    for name, formatter in (("voti", fmt_voto), ("bacheca", fmt_bacheca)):
        items = data.get(name)
        if items is None:
            ok = False
            continue
        process_section(name, items, state, formatter)

    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
