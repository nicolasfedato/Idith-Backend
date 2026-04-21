# =======================================================
# guard_autofix.py
# Script di controllo e auto-correzione per orchestrator
# =======================================================

import os
import sys

def run_guard(input_file: str):
    """
    Controlla un file di orchestrator e segnala eventuali problemi di struttura.
    """
    if not os.path.exists(input_file):
        print(f"❌ File non trovato: {input_file}")
        return [("file_check", False, "File inesistente")]

    results = []

    try:
        with open(input_file, "r", encoding="utf-8") as f:
            code = f.read()

        # Controllo di base: presenza di parole chiave fondamentali
        keywords = ["def", "if", "for", "return"]
        missing = [k for k in keywords if k not in code]
        if missing:
            results.append(("keyword_check", False, f"Mancano: {', '.join(missing)}"))
        else:
            results.append(("keyword_check", True, "ok"))

        # Controllo indentazione (molto semplice)
        for line_num, line in enumerate(code.splitlines(), 1):
            if line.strip().startswith(("for ", "if ", "while ", "def ", "class ")):
                # Controlla che la riga successiva sia indentata
                next_index = code.splitlines().index(line) + 1
                if next_index < len(code.splitlines()):
                    next_line = code.splitlines()[next_index]
                    if next_line.strip() and not next_line.startswith(" "):
                        results.append(("indentation", False, f"Indentazione mancante alla riga {line_num+1}"))
                        break
        else:
            results.append(("indentation", True, "ok"))

    except Exception as e:
        results.append(("exception", False, f"Errore: {e}"))

    return results


def report_results(results):
    """
    Stampa il report finale e restituisce codice di uscita 0/1.
    """
    print("\n=== REPORT ===")
    failed = [r for r in results if not r[1]]

    for name, ok, msg in results:
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] {name} → {msg}")

    if failed:
        print("\n❌ Alcuni controlli sono falliti.")
        sys.exit(1)
    else:
        print("\n✅ Tutti i controlli sono superati.")
        sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--in":
        print("Uso corretto: python guard_autofix.py --in orchestrator_v2.py")
        sys.exit(1)

    target_file = sys.argv[2]
    results = run_guard(target_file)
    report_results(results)