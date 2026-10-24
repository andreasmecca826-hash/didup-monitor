import os
import json
import requests
from argofamiglia import Famiglia

# Leggi le credenziali dai segreti di GitHub
USERNAME = os.getenv("DIDUP_USERNAME")
PASSWORD = os.getenv("DIDUP_PASSWORD")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "Morselli_alerts_didup")

STATE_FILE = "state.json"

def send_ntfy(title, message):
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    try:
        requests.post(url, data=message.encode('utf-8'), headers={
            "Title": title.encode('utf-8'),
            "Priority": "default",
            "Tags": "school"
        })
    except Exception as e:
        print(f"Errore nell'invio della notifica ntfy: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def main():
    if not USERNAME or not PASSWORD:
        print("Errore: Credenziali DidUP non configurate nei secrets.")
        return

    print("Connessione a DidUP in corso...")
    client = Famiglia()
    
    try:
        client.login(USERNAME, PASSWORD)
    except Exception as e:
        print(f"Errore durante il login a DidUP: {e}")
        send_ntfy("Errore DidUP", "Impossibile effettuare il login: credenziali errate o servizio offline.")
        return

    try:
        grades = client.voti() or []
    except Exception as e:
        print(f"Errore nel recupero dei voti: {e}")
        return

    known_grades = set(tuple(g.items()) for g in load_state())
    new_grades = []
    current_grades_list = []

    for g in grades:
        g_item = {
            "materia": str(g.get("materia", "")),
            "valore": str(g.get("valore", "")),
            "data": str(g.get("data", "")),
            "tipo": str(g.get("tipo", ""))
        }
        current_grades_list.append(g_item)
        
        g_tuple = tuple(g_item.items())
        if g_tuple not in known_grades:
            new_grades.append(g_item)

    if new_grades and load_state():
        for g in new_grades:
            msg = f"Materia: {g['materia']}\nVoto: {g['valore']}\nData: {g['data']}\nTipo: {g['tipo']}"
            send_ntfy("Nuovo Voto Registrato! 📚", msg)
            print(f"Nuovo voto trovato: {g}")
    
    save_state(current_grades_list)
    print("Controllo voti completato con successo.")

if __name__ == "__main__":
    main()
 
