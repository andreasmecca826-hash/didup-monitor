import os
import json
import requests
from argofamiglia import ArgoFamiglia

# Leggi le credenziali dai Secrets di GitHub
CODICE_SCUOLA = os.environ.get("CODICE_SCUOLA")
USERNAME = os.environ.get("USERNAME")
PASSWORD = os.environ.get("PASSWORD")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

STATE_FILE = "last_state.json"

def invia_notifica(testo):
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=testo.encode('utf-8'))
    except Exception as e:
        print(f"Errore invio notifica: {e}")

def carica_ultimo_stato():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def salva_nuovo_stato(stato):
    with open(STATE_FILE, "w") as f:
        json.dump(stato, f)

def main():
    if not all([CODICE_SCUOLA, USERNAME, PASSWORD, NTFY_TOPIC]):
        print("Errore: Mancano le variabili d'ambiente.")
        return

    try:
        # Connessione a DidUP
        session = ArgoFamiglia(CODICE_SCUOLA, USERNAME, PASSWORD)
        
        # Recupera gli ultimi voti
        voti = session.getVoti()
        
        ultimo_stato = carica_ultimo_stato()
        ultimo_id_voto = ultimo_stato.get("ultimo_voto_id", None)
        
        if voti:
            ultimo_voto = voti[0] 
            corrente_id = str(ultimo_voto.get("id", ultimo_voto.get("data", "")))
            
            if corrente_id != ultimo_id_voto:
                materia = ultimo_voto.get("materia", "Materia")
                valore = ultimo_voto.get("valore", "Nuovo voto")
                
                invia_notifica(f"🔔 Nuovo voto su DidUP!\n{materia}: {valore}")
                salva_nuovo_stato({"ultimo_voto_id": corrente_id})
                print("Nuovo voto rilevato e notifica inviata.")
            else:
                print("Nessun nuovo voto.")
                
    except Exception as e:
        print(f"Errore durante il controllo di DidUP: {e}")

if __name__ == "__main__":
    main()
