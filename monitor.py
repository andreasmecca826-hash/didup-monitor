import os
import json
import requests
from datetime import datetime
from argofamiglia import Argo

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
    return {"grades": [], "bacheca": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def main():
    if not USERNAME or not PASSWORD:
        print("Errore: Credenziali DidUP non configurate nei secrets.")
        return

    print("Connessione a DidUP in corso...")
    client = Argo()
    
    try:
        client.login(USERNAME, PASSWORD)
    except Exception as e:
        print(f"Errore durante il login a DidUP: {e}")
        send_ntfy("Errore DidUP", "Impossibile effettuare il login: credenziali errate o servizio offline.")
        return

    state = load_state()
    
    # --- 1. CONTROLLO VOTI ---
    try:
        grades = client.voti() or []
        known_grades = set(tuple(g.items()) for g in state.get("grades", []))
        
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

        if new_grades and state.get("grades"):
            for g in new_grades:
                msg = f"Materia: {g['materia']}\nVoto: {g['valore']}\nData: {g['data']}\nTipo: {g['tipo']}"
                send_ntfy("Nuovo Voto Registrato! 📚", msg)
                print(f"Nuovo voto trovato: {g}")
        
        state["grades"] = current_grades_list
    except Exception as e:
        print(f"Errore nel recupero dei voti: {e}")

    # --- 2. CONTROLLO BACHECA / CIRCOLARI ---
    try:
        bacheca_items = client.bacheca() or []
        known_bacheca = set(str(item.get("ID", item.get("id", i))) for i, item in enumerate(state.get("bacheca", [])))
        
        new_bacheca = []
        current_bacheca_list = []
        
        for item in bacheca_items:
            item_id = str(item.get("ID", item.get("id", item.get("titolo", ""))))
            item_data = {
                "ID": item_id,
                "titolo": str(item.get("titolo", "Nuova Circolare")),
                "data": str(item.get("data", ""))
            }
            current_bacheca_list.append(item_data)
            
            if item_id not in known_bacheca:
                new_bacheca.append(item_data)

        if new_bacheca and state.get("bacheca"):
            for item in new_bacheca:
                msg = f"Titolo: {item['titolo']}\nData: {item['data']}"
                send_ntfy("Nuova Circolare in Bacheca! 📢", msg)
                print(f"Nuova bacheca trovata: {item}")
                
        state["bacheca"] = current_bacheca_list
    except Exception as e:
        print(f"Errore nel recupero della bacheca: {e}")

    save_state(state)
    print("Controllo completato con successo.")

if __name__ == "__main__":
    main()
