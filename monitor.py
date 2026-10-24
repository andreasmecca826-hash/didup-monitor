#!/usr/bin/env python3
"""Monitor DidUP (Argo Famiglia): voti, bacheca, note, promemoria e compiti -> ntfy.

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
from typing import Any, Callable, NamedTuple

import requests
from didupwrapper import DiDUPClientSync

NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "Morselli_alerts_didup"
NTFY_URL = "https://ntfy.sh"
STATE_FILE = Path(os.environ.get("STATE_FILE") or "state.json")

# Oltre questa soglia di novità per sezione, invio un solo riepilogo
MAX_NOTIFICHE = 10

# Campi che cambiano nel tempo senza che l'elemento sia "nuovo" (es. presa visione):
# vengono ignorati nel calcolo dell'hash che identifica ogni elemento.
NOISY_HINTS = ("visione", "letto", "letta", "conferma", "read")

# Possibili nomi del campo "compiti" nelle lezioni del registro
COMPITI_FIELDS = ("des_compiti", "compiti", "des_compito", "compito")

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
Msg = tuple[str, str, list[str]]


def fmt_voto(v: Any) -> Msg:
    # Il valore del voto non compare nella notifica (si scopre aprendo DidUP).
    materia = first_attr(v, ("des_materia",), "Materia")
    return "Nuovo voto", f"{materia}: apri DidUP per vedere il voto", ["books"]


def fmt_bacheca(c: Any) -> Msg:
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


def fmt_nota(n: Any) -> Msg:
    # Contenuto volutamente omesso: il canale ntfy è pubblico.
    return "Nuova nota sul registro", "Apri DidUP per leggerla", ["warning"]


def fmt_promemoria(p: Any) -> Msg:
    testo = first_attr(
        p,
        ("des_annotazioni", "des_promemoria", "des_testo", "testo", "messaggio"),
        "Nuovo promemoria",
    )
    data = first_attr(p, ("dat_giorno", "data"))
    righe = [testo]
    if data:
        righe.append(f"Data: {data}")
    return "Promemoria DidUP", "\n".join(righe), ["memo"]


def fmt_compiti(r: Any) -> Msg:
    materia = first_attr(r, ("des_materia", "materia"), "Materia")
    testo = first_attr(r, COMPITI_FIELDS)
    data = first_attr(r, ("dat_compiti", "dat_consegna", "dat_giorno", "data"))
    righe = [materia]
    if data:
        righe.append(f"Data: {data}")
    if testo:
        righe.append(testo)
    return "Nuovi compiti", "\n".join(righe), ["pencil2"]


def has_compiti(r: Any) -> bool:
    return bool(first_attr(r, COMPITI_FIELDS).strip())


# ----------------------------------------------------------------- sezioni
class Section(NamedTuple):
    method: str                                  # metodo di didupwrapper
    formatter: Callable[[Any], Msg]
    id_fields: tuple[str, ...] = ()              # vuoto = identifico con l'hash
    keep: Callable[[Any], bool] | None = None    # filtro sugli elementi
    required: bool = False                       # se fallisce, il workflow va in errore


SECTIONS: dict[str, Section] = {
    "voti": Section("get_voti", fmt_voto, required=True),
    "bacheca": Section(
        "get_bacheca", fmt_bacheca,
        id_fields=("prg_messaggio", "prg_bacheca", "prg", "id"), required=True,
    ),
    "note": Section("get_note_disciplinari", fmt_nota),
    "promemoria": Section("get_promemoria", fmt_promemoria),
    "compiti": Section("get_registro", fmt_compiti, keep=has_compiti),
}


# ----------------------------------------------------------------- logica
def process_section(
    name: str, items: list[Any], state: dict[str, list[str]], section: Section
) -> None:
    current: dict[str, Any] = {}
    for item in items:
        base = key = item_key(item, section.id_fields)
        n = 1
        while key in current:  # elementi identici (stesso hash) restano distinti
            n += 1
            key = f"{base}#{n}"
        current[key] = item

    if name not in state:  # primo avvio della sezione: salvo la base, nessuna notifica
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
            title, message, tags = section.formatter(item)
            if notify(title, message, tags):
                seen.add(key)  # se ntfy fallisce, ritenta alla prossima esecuzione

    state[name] = sorted(seen)


def fetch_all(
    school: str, user: str, password: str, apply_filters: bool = True
) -> dict[str, list[Any] | None]:
    """Login + download. Riprova con auto-rilevamento versione app se il primo tentativo fallisce."""
    last_exc: Exception | None = None
    for auto in (False, True):
        try:
            kwargs = {"auto_versione": True} if auto else {}
            with DiDUPClientSync(school, user, password, **kwargs) as didup:
                out: dict[str, list[Any] | None] = {}
                for name, section in SECTIONS.items():
                    try:
                        items = list(getattr(didup, section.method)())
                        campi = list(as_dict(items[0]).keys()) if items else []
                        log.info("[%s] %d elementi dal server; campi: %s", name, len(items), campi)
                        if apply_filters and section.keep:
                            items = [i for i in items if section.keep(i)]
                        out[name] = items
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

    dump = "--dump" in sys.argv
    try:
        data = fetch_all(school, user, password, apply_filters=not dump)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    if dump:
        for name, items in data.items():
            fields = list(as_dict(items[0]).keys()) if items else []
            print(f"{name}: {fields}")
        return 0

    state = load_state()
    ok = True
    for name, section in SECTIONS.items():
        items = data.get(name)
        if items is None:
            if section.required:
                ok = False
            else:  # sezione opzionale: avviso visibile in Actions, ma il workflow resta verde
                print(f"::warning::Sezione '{name}' non disponibile, la salto")
            continue
        process_section(name, items, state, section)

    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
