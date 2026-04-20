from .orchestrator import run

SID = "test-session-1"

def step(user_input, new_chat=False):
    payload = {
        "session_id": SID,
        "msgId": None,
        "user_input": user_input,
        "new_chat": new_chat
    }
    resp = run(payload)
    print(f"\nUSER: {user_input}")
    print(f"BOT : {resp['reply']}")
    if 'action' in resp:
        print(f"ACTION: {resp['action']}")
        print(f"BLUEPRINT: {resp.get('blueprint')}")
    return resp

# ---- simulazione conversazione ----
step("ciao", new_chat=True) # saluto + onboarding
step("si ho account e chiavi") # aggiorna stato chiavi
step("inizia") # parte il flusso
step("Falco 2407") # nome LIBERO
step("futures") # mode
step("BTCUSDT, ETHUSDT") # coppie
step("15m") # timeframe
step("spiega") # prima spiegazione strategia
step("spiega") # seconda spiegazione strategia (più ricca)
step("trend") # strategia
step("3%") # rischio (vedrai warning se >=3)
step("ATR 2x") # stop
step("1.5x") # take profit
step("10x") # leva (warning)
step("24/7") # operatività
step("sì") # notifiche
step("demo") # ambiente
step("no") # warm-up
r = step("riassunto") # riepilogo
step("genera codice") # produce action generate_code
