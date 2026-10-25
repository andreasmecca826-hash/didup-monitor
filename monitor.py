#!/usr/bin/env python3
"""Monitor DidUP (Argo Famiglia): voti, bacheca, note, promemoria e compiti -> ntfy.

Funzionalita':
  * media generale dei voti con trend (freccia su / giu' / stabile)
  * priorita' ntfy differenziate: urgente per insufficienze e note, silenziosa per i compiti
  * storico persistente in history.md

Variabili d'ambiente (da impostare come GitHub Secrets):
    DIDUP_SCHOOL    codice scuola (es. SC12345)
    DIDUP_USER      nome utente
    DIDUP_PASSWORD  password
    NTFY_TOPIC      (opzionale) default: Morselli_alerts_didup
    STATE_FILE      (opzionale) default: state.json
    HISTORY_FILE    (opzionale) default: history.md

Uso:
    python monitor.py          # esecuzione normale
    python monitor.py --dump   # stampa SOLO i nomi dei campi dei modelli
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

import requests
from didupwrapper import DiDUPClientSync

NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "Morselli_alerts_didup"
NTFY_URL = "https://ntfy.sh"
STATE_FILE = Path(os.environ.get("STATE_FILE") or "state.json")
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE") or "history.md")

# Oltre questa soglia di novita' per sezione, invio un solo riepilogo
MAX_NOTIFICHE = 10

# Tentativi di login/download per ogni esecuzione (massimo 3 login in totale).
MAX_RETRIES = 3
RETRY_DELAY = 5  # secondi di attesa tra un tentativo e l'altro

# Livelli di priorita' ntfy: 1 = minima (silenziosa) ... 5 = massima (urgente).
# Il suono, la vibrazione e il pop-over dipendono da questo valore.
PRIORITA_URGENTE = 5
PRIORITA_ALTA = 4
PRIORITA_NORMALE = 3
PRIORITA_SILENZIOSA = 2

SUFFICIENZA = 6.0

# Campi che cambiano nel tempo senza che l'elemento sia "nuovo" (es. presa visione):
NOISY_HINTS = ("visione", "letto", "letta", "conferma", "read")

# Possibili nomi del campo "compiti" nelle lezioni del registro
COMPITI_FIELDS = ("des_compiti", "compiti", "des_compito", "compito")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("monitor")


# ------------------------------------------------------------------ utilita'
def as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):  # Pydantic v2
        return item.model_dump(mode="json")
    if hasattr(item, "__dict__"):
        return dict(vars(item))
    return {}


def first_attr(item: Any, names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        valore = getattr(item, name, None)
        if valore in (None, "") and isinstance(item, dict):
            valore = item.get(name)
        if valore not in (None, ""):
            return str(valore)
    return default


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "materia"


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


# ------------------------------------------------------- voti e statistiche
NUMERO_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _to_float(raw: Any) -> float | None:
    """Trasforma '7', '7.5', '7,5', '7½', '8+' nel numero corrispondente."""
    if raw in (None, ""):
        return None
    testo = str(raw).strip().replace("½", ",5").replace(",", ".")
    match = NUMERO_RE.search(testo)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def voto_valore(v: Any) -> float | None:
    """Estrae il valore numerico di un voto, se esiste."""
    for campo in ("dec_valore", "cod_codice", "valore", "voto"):
        numero = _to_float(first_attr(v, (campo,)))
        if numero is not None:
            return numero
    return None


def compute_stats(voti: list[Any]) -> dict[str, Any]:
    valori = [x for x in (voto_valore(v) for v in voti) if x is not None]
    if not valori:
        return {}
    return {
        "media": round(sum(valori) / len(valori), 2),
        "num_voti": len(valori),
        "minimo": min(valori),
        "massimo": max(valori),
    }


def voto_tipo(voto: Any) -> str:
    codice = first_attr(voto, ("cod_voto_pratico", "tipo_valutazione", "cod_tipo")).lower()
    if codice.startswith("s") or "scrit" in codice:
        return "scritto"
    if codice.startswith("n") or "oral" in codice:
        return "orale"
    if "prat" in codice or "lab" in codice:
        return "pratico"
    return "altro"


def compito_recente(data: str) -> bool:
    if not data:
        return True
    testo = data.strip()
    try:
        if "-" in testo:
            giorno = datetime.strptime(testo[:10], "%Y-%m-%d").date()
        else:
            giorno = datetime.strptime(testo[:10], "%d/%m/%Y").date()
    except ValueError:
        return True
    return giorno >= datetime.now(timezone.utc).date() - timedelta(days=1)


def build_voto_data(data: dict[str, list[Any] | None], stats: dict[str, Any]) -> dict[str, Any]:
    """Converte i record DidUP completi nel formato letto dall'app VOTO."""
    grouped: dict[str, dict[str, Any]] = {}
    for index, voto in enumerate(data.get("voti") or []):
        materia = first_attr(voto, ("des_materia",), "Materia")
        subject_id = slug(materia) or f"materia-{index}"
        subject = grouped.setdefault(subject_id, {"id": subject_id, "name": materia, "grades": []})
        raw_value = first_attr(voto, ("cod_codice", "dec_valore", "valore", "voto"), "")
        value = voto_valore(voto)
        if value is None:
            continue
        subject["grades"].append({
            "id": first_attr(voto, ("prg_voto",)) or item_key(voto, ("prg_voto",)),
            "value": raw_value or value,
            "date": first_attr(voto, ("dat_evento", "dat_giorno")),
            "type": voto_tipo(voto),
            "weight": 1,
            "comment": first_attr(voto, ("des_commento", "descrizione_prova", "descrizione_voto")) or None,
        })

    assignments = []
    for index, compito in enumerate(data.get("compiti") or []):
        title = first_attr(compito, COMPITI_FIELDS)
        if not title:
            continue
        due_date = first_attr(compito, ("dat_compiti", "dat_consegna", "dat_giorno", "data"))
        if not compito_recente(due_date):
            continue
        assignments.append({
            "id": item_key(compito, ("prg", "id")) or f"compito-{index}",
            "title": title,
            "subject": first_attr(compito, ("des_materia", "materia")) or None,
            "dueDate": due_date or None,
            "completed": False,
        })

    eventi = data.get("assenze") or []
    assenze = sum(first_attr(evento, ("cod_evento",)).upper() == "A" for evento in eventi)
    ritardi = sum(first_attr(evento, ("cod_evento",)).upper() == "I" for evento in eventi)
    uscite = sum(first_attr(evento, ("cod_evento",)).upper() == "U" for evento in eventi)
    notifications = []
    for section, kind, title_default in (("note", "note", "Nota disciplinare"), ("bacheca", "bacheca", "Comunicazione")):
        for index, item in enumerate(data.get(section) or []):
            title = first_attr(item, ("des_oggetto", "oggetto", "titolo", "des_titolo"), title_default)
            message = first_attr(item, ("des_messaggio", "messaggio", "testo", "des_testo", "nota", "descrizione"))
            event_date = first_attr(item, ("dat_giorno", "dat_evento", "data"))
            attachment_url = first_attr(item, ("url", "des_url"))
            attachment_name = first_attr(item, ("nome_file", "file_name"))
            allegati = getattr(item, "lista_allegati", None) or []
            if allegati:
                allegato = allegati[0]
                allegato_url = first_attr(allegato, ("url",))
                allegato_path = first_attr(allegato, ("path",))
                if allegato_path and not allegato_path.startswith("http"):
                    base = allegato_url or attachment_url
                    attachment_url = f"{base.rstrip('/')}/{allegato_path.lstrip('/')}" if base else allegato_path
                else:
                    attachment_url = allegato_path or allegato_url or attachment_url
                attachment_name = attachment_name or first_attr(allegato, ("nome_file", "descrizione_file"))
            notifications.append({
                "id": item_key(item, ("prg_messaggio", "prg_bacheca", "prg", "id")) or f"{kind}-{index}",
                "type": "note" if kind == "note" else "notice",
                "title": title,
                "message": message or None,
                "date": event_date or None,
                "read": False,
                "attachmentUrl": attachment_url or None,
                "attachmentName": attachment_name or None,
            })

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "subjects": list(grouped.values()),
        "assignments": assignments,
        "notifications": notifications,
        "attendance": {
            "absenceDays": assenze,
            "absenceHours": assenze * 6,
            "delays": ritardi,
            "earlyExits": uscite,
            "annualHours": 990,
            "absenceLimitHours": 247.5,
        },
    }


def save_voto_data(data: dict[str, list[Any] | None], stats: dict[str, Any]) -> None:
    output = Path(os.environ.get("VOTO_FILE") or "grades.json")
    output.write_text(json.dumps(build_voto_data(data, stats), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_trend_line(stats: dict[str, Any], precedente: dict[str, Any]) -> str:
    """Riga tipo 'Media generale: 7.42 ⬆️ (+0.15)' da mettere nelle notifiche."""
    if not stats:
        return ""
    media = float(stats["media"])
    vecchia = precedente.get("media")
    if vecchia is None:
        return f"📊 Media generale: {media:.2f}"
    delta = media - float(vecchia)
    if delta > 0.004:
        freccia = "⬆️"
    elif delta < -0.004:
        freccia = "⬇️"
    else:
        freccia = "➡️"
    return f"📊 Media generale: {media:.2f} {freccia} ({delta:+.2f})"


# -------------------------------------------------------------------- stato
def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.warning("state.json illeggibile: riparto da zero (nessuna notifica)")
        return {}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------- ntfy
def notify(title: str, message: str, tags: list[str], priority: int = PRIORITA_NORMALE) -> bool:
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


# ---------------------------------------------------------------- storico md
def ensure_history() -> None:
    if not HISTORY_FILE.exists():
        HISTORY_FILE.write_text(
            "# Storico DidUP\n\nEventi registrati automaticamente dal monitor.\n",
            encoding="utf-8",
        )


def append_history(eventi: list[tuple[str, str]]) -> None:
    if not eventi:
        return
    adesso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    righe = [f"\n## {adesso}\n"]
    for titolo, messaggio in eventi:
        corpo = " — ".join(messaggio.splitlines())
        righe.append(f"- **{titolo}** — {corpo}")
    with HISTORY_FILE.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(righe) + "\n")
    log.info("Storico aggiornato: %d eventi in %s", len(eventi), HISTORY_FILE)


# --------------------------------------------------------------- formattatori
Msg = tuple[str, str, list[str], int]  # titolo, messaggio, tags, priorita'


def fmt_voto(v: Any, ctx: dict[str, Any]) -> Msg:
    materia = first_attr(v, ("des_materia",), "Materia")
    valore = first_attr(v, ("cod_codice", "dec_valore"), "?")
    numero = voto_valore(v)
    righe = [f"{materia}: {valore}"]
    if ctx.get("trend"):
        righe.append(str(ctx["trend"]))
    if numero is not None and numero < SUFFICIENZA:
        return (
            "❗ Insufficienza",
            "\n".join(righe),
            ["rotating_light", "books"],
            PRIORITA_URGENTE,
        )
    return "Nuovo voto", "\n".join(righe), ["books"], PRIORITA_ALTA


def fmt_bacheca(c: Any, _ctx: dict[str, Any]) -> Msg:
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
    return "Bacheca DidUP", "\n".join(righe), ["bell"], PRIORITA_NORMALE


def fmt_nota(_n: Any, _ctx: dict[str, Any]) -> Msg:
    return (
        "⚠️ Nuova nota sul registro",
        "Apri DidUP per leggerla",
        ["warning", "rotating_light"],
        PRIORITA_URGENTE,
    )


def fmt_promemoria(p: Any, _ctx: dict[str, Any]) -> Msg:
    testo = first_attr(
        p,
        ("des_annotazioni", "des_promemoria", "des_testo", "testo", "messaggio"),
        "Nuovo promemoria",
    )
    data = first_attr(p, ("dat_giorno", "data"))
    righe = [testo]
    if data:
        righe.append(f"Data: {data}")
    return "Promemoria DidUP", "\n".join(righe), ["memo"], PRIORITA_NORMALE


def fmt_compiti(r: Any, _ctx: dict[str, Any]) -> Msg:
    materia = first_attr(r, ("des_materia", "materia"), "Materia")
    testo = first_attr(r, COMPITI_FIELDS)
    data = first_attr(r, ("dat_compiti", "dat_consegna", "dat_giorno", "data"))
    righe = [materia]
    if data:
        righe.append(f"Data: {data}")
    if testo:
        righe.append(testo)
    return "Nuovi compiti", "\n".join(righe), ["pencil2"], PRIORITA_SILENZIOSA


def fmt_assenza(evento: Any, _ctx: dict[str, Any]) -> Msg:
    codice = first_attr(evento, ("cod_evento",)).upper()
    titolo = {"A": "Assenza", "I": "Ritardo", "U": "Uscita"}.get(codice, "Evento presenze")
    data = first_attr(evento, ("dat_evento", "data"))
    descrizione = first_attr(evento, ("descrizione", "nota"))
    righe = [x for x in (data, descrizione) if x]
    return titolo, "\n".join(righe) or "Evento registrato su DidUP", ["calendar"], PRIORITA_SILENZIOSA


def has_compiti(r: Any) -> bool:
    return bool(first_attr(r, COMPITI_FIELDS).strip())


class Section(NamedTuple):
    method: str
    formatter: Callable[[Any, dict[str, Any]], Msg]
    id_fields: tuple[str, ...] = ()
    keep: Callable[[Any], bool] | None = None
    required: bool = False


SECTIONS: dict[str, Section] = {
    "voti": Section("get_voti", fmt_voto, required=True),
    "assenze": Section("get_assenze", fmt_assenza, id_fields=("dat_evento", "data", "cod_evento", "descrizione")),
    "bacheca": Section(
        "get_bacheca", fmt_bacheca,
        id_fields=("prg_messaggio", "prg_bacheca", "prg", "id"), required=True,
    ),
    "note": Section("get_note_disciplinari", fmt_nota),
    "promemoria": Section("get_promemoria", fmt_promemoria),
    "compiti": Section("get_registro", fmt_compiti, keep=has_compiti),
}


# ------------------------------------------------------------------ logica
def process_section(
    name: str,
    items: list[Any],
    state: dict[str, Any],
    section: Section,
    ctx: dict[str, Any],
) -> list[tuple[str, str]]:
    """Notifica gli elementi nuovi e restituisce quelli registrati (per lo storico)."""
    current: dict[str, Any] = {}
    for item in items:
        base = key = item_key(item, section.id_fields)
        n = 1
        while key in current:
            n += 1
            key = f"{base}#{n}"
        current[key] = item

    if name not in state:
        state[name] = sorted(current)
        log.info("[%s] primo avvio: %d elementi salvati, nessuna notifica", name, len(current))
        return []

    seen = set(state[name])
    nuovi = [(k, i) for k, i in current.items() if k not in seen]
    log.info("[%s] %d elementi, %d nuovi", name, len(current), len(nuovi))

    registrati: list[tuple[str, str]] = []
    if len(nuovi) > MAX_NOTIFICHE:
        titolo = f"{len(nuovi)} novità su DidUP ({name})"
        messaggio = "Apri il registro per i dettagli."
        if notify(titolo, messaggio, ["warning"], PRIORITA_ALTA):
            seen.update(k for k, _ in nuovi)
            registrati.append((titolo, messaggio))
    else:
        for key, item in nuovi:
            titolo, messaggio, tags, priorita = section.formatter(item, ctx)
            if notify(titolo, messaggio, tags, priorita):
                seen.add(key)  # se ntfy fallisce, ritenta alla prossima esecuzione
                registrati.append((titolo, messaggio))

    state[name] = sorted(seen)
    return registrati


def is_auth_error(exc: BaseException) -> bool:
    """Credenziali rifiutate: riconosco l'AuthError di didupwrapper dal nome della classe."""
    return type(exc).__name__ == "AuthError"


def fetch_all_once(
    school: str, user: str, password: str, apply_filters: bool, auto: bool
) -> dict[str, list[Any] | None]:
    """Un solo login + download di tutte le sezioni."""
    kwargs = {"auto_versione": True} if auto else {}
    with DiDUPClientSync(school, user, password, **kwargs) as didup:
        out: dict[str, list[Any] | None] = {}
        for name, section in SECTIONS.items():
            try:
                items = list(getattr(didup, section.method)())
                if apply_filters and section.keep:
                    items = [i for i in items if section.keep(i)]
                out[name] = items
            except Exception:
                log.exception("[%s] download fallito", name)
                out[name] = None
        return out


def fetch_all(
    school: str, user: str, password: str, apply_filters: bool = True
) -> dict[str, list[Any] | None]:
    """Fino a MAX_RETRIES login. Se le credenziali vengono rifiutate si ferma subito
    (al massimo 2 tentativi: uno con versione di default, uno con versione rilevata),
    per non rischiare il blocco dell'account."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        auto = attempt > 1
        try:
            return fetch_all_once(school, user, password, apply_filters, auto)
        except Exception as exc:
            last_exc = exc
            auth = is_auth_error(exc)
            log.warning("Tentativo %d/%d fallito (%s): %s", attempt, MAX_RETRIES, type(exc).__name__, exc)
            if auth and auto:
                raise RuntimeError(
                    "Credenziali rifiutate da DidUP: non riprovo. "
                    "Controlla codice scuola, username e password nei secrets."
                ) from exc
            if attempt < MAX_RETRIES and not auth:
                log.info("Attesa di %d secondi prima del prossimo tentativo...", RETRY_DELAY)
                time.sleep(RETRY_DELAY)
    raise RuntimeError(
        f"Tutti i {MAX_RETRIES} tentativi di connessione a DidUP sono falliti: {last_exc}"
    ) from last_exc


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

    # --- statistiche e trend (calcolati PRIMA di notificare, cosi' la media
    #     aggiornata finisce dentro la notifica del nuovo voto) --------------
    voti = data.get("voti") or []
    stats = compute_stats(voti)
    trend = build_trend_line(stats, state.get("_stats") or {})
    ctx: dict[str, Any] = {"trend": trend}
    if trend:
        log.info("%s", trend)

    ensure_history()

    ok = True
    eventi: list[tuple[str, str]] = []
    for name, section in SECTIONS.items():
        items = data.get(name)
        if items is None:
            if section.required:
                ok = False
            else:
                print(f"::warning::Sezione '{name}' non disponibile, la salto")
            continue
        prodotti = process_section(name, items, state, section, ctx)
        eventi.extend(prodotti)
        if name == "voti" and prodotti and trend:
            eventi.append(("Media aggiornata", trend))

    if stats:
        state["_stats"] = stats

    if eventi:
        append_history(eventi)

    save_voto_data(data, stats)
    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
