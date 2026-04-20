
# builder_engine.py - natural command processor

from builder_definitions import DEFAULTS
import re

def handle_builder_command(state, msg):
    txt = msg.lower().strip()
    data = state.setdefault("data", dict(DEFAULTS))
    confirm = state.setdefault("confirm", None)
    stage = state.setdefault("confirm_stage", 0)

    # Confirmation lock
    if confirm:
        if txt in ("si","sì","s"):
            if stage == 1:
                state["confirm_stage"]=2
                return {"reply": confirm["final"] + " (si/no)"}
            else:
                data[confirm["field"]] = confirm["value"]
                state["confirm"]=None
                state["confirm_stage"]=0
                return {"reply": f"Ok, imposto {confirm['field']} a {confirm['value']}."}
        if txt in ("no","n"):
            state["confirm"]=None
            state["confirm_stage"]=0
            return {"reply":"Perfetto, manteniamo un valore prudente."}
        return {"reply":"Rispondi solo si/no."}

    # risk reduction
    if any(k in txt for k in ["riduci rischio","abbassa rischio"]):
        current = float(data["risk_pct"].replace("%",""))
        new = max(0.1, current/2)
        data["risk_pct"]=f"{new:.2f}%"
        return {"reply":f"Rischio ridotto a {data['risk_pct']}."}

    # risk increase
    if any(k in txt for k in ["aumenta rischio","alza rischio"]):
        current=float(data["risk_pct"].replace("%",""))
        new=current*2
        if new>10:
            state["confirm"]={"field":"risk_pct","value":f"{new:.2f}%","final":"Con rischio così alto rischi perdite pesanti. Confermi?"}
            state["confirm_stage"]=1
            return {"reply":f"Aumento da {current}% a {new}%. Procedo? (si/no)"}
        data["risk_pct"]=f"{new:.2f}%"
        return {"reply":f"Rischio aumentato a {data['risk_pct']}."}

    # leverage
    m=re.search(r"(\d+)x",txt)
    if m:
        val=int(m.group(1))
        if val>5:
            state["confirm"]={"field":"leverage","value":f"{val}x","final":f"Leva {val}x è molto rischiosa. Confermi?"}
            state["confirm_stage"]=1
            return {"reply":f"{val}x è pesante. Continuo? (si/no)"}
        data["leverage"]=f"{val}x"
        return {"reply":f"Leva impostata a {val}x."}

    if txt=="riepilogo":
        out="RIEPILOGO:\n"
        for k,v in data.items():
            out+=f"- {k}: {v}\n"
        return {"reply":out}

    return None
