# Analisi: Reset di `params['strategy']` e `params` in `handle_message()`

## Obiettivo
Trovare esattamente dove e perché `params` si azzera (in particolare `strategy=[]` e periodi `None`) dentro `handle_message()` nello step `strategy`, PRIMA del salvataggio DB.

## Log Aggiunti

### Punto A) - Dopo inizializzazione
- **Posizione**: Subito dopo `cs = state["config_state"]`, `params = _coerce_params(cs.get("params"))`, `cs["params"] = params`
- **Log**: `[ANALYSIS_A]` - Mostra `current_step`, `cs['step']`, `params` completo (strategy, rsi_period, atr_period, ema_period, error_count)

### Punto B) - Prima/Dopo `_extract_step_value`
- **Posizione**: Subito prima e subito dopo `_extract_step_value(user_text, current_step, params)`
- **Log**: 
  - `[ANALYSIS_B_BEFORE]` - `user_text`, `current_step`, snapshot `params`
  - `[ANALYSIS_B_AFTER]` - `extracted_value` (tipo e valore), `current_step`, snapshot `params`

### Punto C) - Blocco `elif current_step == "strategy"`
- **Posizione**: Nel blocco `elif current_step == "strategy"`
- **Log**:
  - `[ANALYSIS_C_ENTRY]` - All'ingresso: `_is_step_filled("strategy", params)`, `params["strategy"]` (prima)
  - `[ANALYSIS_C]` - Dopo costruzione `strategy_list`
  - `[ANALYSIS_C]` - Prima/dopo ogni chiamata a `_sync_strategy_from_periods(params)`
  - `[ANALYSIS_C]` - Prima di ogni `_sync_state(...)`

### Punto D) - Blocco "periodi"
- **Posizione**: Nel blocco che gestisce inserimento numerico durante strategy (`if current_step == "strategy" and _is_step_filled("strategy", params)`)
- **Log**:
  - `[ANALYSIS_D_ENTRY]` - All'ingresso: `missing_step`, `period_value`, `params` prima/dopo assegnazione
  - `[ANALYSIS_D]` - Prima/dopo `_sync_strategy_from_periods(params)`
  - `[ANALYSIS_D]` - Prima/dopo `_sync_state(...)`

### REPORT Finale
- **Posizione**: Prima di ogni return principale
- **Log**: `[REPORT_FINAL_*]` - Una riga compatta con: step finale, strategy, rsi_period/atr_period/ema_period, timeframe/leverage/risk_pct/sl/tp

## Ipotesi più Probabili

### Ipotesi 1: `_sync_strategy_from_periods()` resetta `strategy` quando i periodi sono `None`
**Probabilità: ALTA**

**Evidenza nel codice**:
- Riga 2975-2982: C'è un FIX che imposta `params["strategy"] = strategy_list` PRIMA di `_sync_strategy_from_periods()` per preservarla
- Commento: "(altrimenti _sync_strategy_from_periods la resetta a [] se i periodi sono None)"

**Scenario**:
1. L'utente seleziona una strategy (es. "RSI+ATR")
2. `params["strategy"] = ["RSI", "ATR"]` viene impostato
3. I periodi sono ancora `None` (non ancora inseriti)
4. `_sync_strategy_from_periods(params)` viene chiamato e resetta `strategy` a `[]` perché i periodi sono `None`
5. `_sync_state()` salva `params` con `strategy=[]`

**Verifica**: Controllare i log `[ANALYSIS_C]` prima/dopo `_sync_strategy_from_periods` per vedere se `strategy` viene resettata.

### Ipotesi 2: `_is_step_filled("strategy", params)` risulta falso quando dovrebbe essere vero
**Probabilità: MEDIA**

**Scenario**:
1. `params["strategy"]` viene impostato correttamente
2. Ma `_is_step_filled("strategy", params)` ritorna `False` (forse perché i periodi sono `None`?)
3. Il blocco periodi (riga 2615) NON entra perché la condizione `current_step == "strategy" and _is_step_filled("strategy", params)` è falsa
4. Il codice procede nel blocco `elif current_step == "strategy"` (riga 2909) che potrebbe resettare `strategy`

**Verifica**: Controllare i log `[ANALYSIS_C_ENTRY]` per vedere il valore di `_is_step_filled("strategy", params)` e `params["strategy"]`.

### Ipotesi 3: `_sync_state()` modifica `params` in modo inaspettato
**Probabilità: MEDIA**

**Scenario**:
1. `params` viene modificato correttamente nel codice
2. `_sync_state(state, cs, params)` viene chiamato
3. Dentro `_sync_state()`, `cs["params"]` viene aggiornato, ma potrebbe esserci una logica che resetta `strategy` se i periodi sono `None`
4. `params` viene ricreato da `cs["params"]` e perde `strategy`

**Verifica**: Controllare i log `[ANALYSIS_C]` e `[ANALYSIS_D]` prima/dopo `_sync_state()` per vedere se `params["strategy"]` cambia.

### Ipotesi 4: Il blocco periodi non entra perché `_is_step_filled("strategy", params)` è falso
**Probabilità: ALTA**

**Scenario**:
1. L'utente inserisce una strategy
2. `params["strategy"]` viene impostato a `["RSI", "ATR"]`
3. Ma `_is_step_filled("strategy", params)` ritorna `False` (forse perché richiede anche i periodi?)
4. Il blocco periodi (riga 2615) NON entra
5. Il codice procede nel blocco `elif current_step == "strategy"` (riga 2909) che gestisce solo strategy non filled
6. Qualche logica in quel blocco resetta `strategy`

**Verifica**: Controllare i log `[ANALYSIS_D_ENTRY]` per vedere se il blocco periodi viene eseguito o meno.

## Come Analizzare i Log

1. **Cercare `[ANALYSIS_A]`**: Verificare lo stato iniziale di `params` dopo `_coerce_params`
2. **Cercare `[ANALYSIS_B_AFTER]`**: Verificare cosa estrae `_extract_step_value` per strategy
3. **Cercare `[ANALYSIS_C_ENTRY]`**: Verificare se `_is_step_filled("strategy", params)` è `True` o `False` e il valore di `params["strategy"]`
4. **Cercare `[ANALYSIS_D_ENTRY]`**: Verificare se il blocco periodi viene eseguito
5. **Cercare `[ANALYSIS_C]` e `[ANALYSIS_D]` prima/dopo `_sync_strategy_from_periods`**: Verificare se `strategy` viene resettata
6. **Cercare `[ANALYSIS_C]` e `[ANALYSIS_D]` prima/dopo `_sync_state`**: Verificare se `strategy` viene resettata
7. **Cercare `[REPORT_FINAL_*]`**: Confrontare lo stato finale prima di ogni return

## Raccomandazioni

1. **Verificare la funzione `_sync_strategy_from_periods()`**: Controllare se resetta `strategy` a `[]` quando i periodi sono `None`
2. **Verificare la funzione `_is_step_filled("strategy", params)`**: Controllare se considera solo `strategy` o anche i periodi
3. **Verificare la funzione `_sync_state()`**: Controllare se modifica `params["strategy"]` in modo inaspettato
4. **Verificare l'ordine delle chiamate**: Assicurarsi che `params["strategy"] = strategy_list` venga chiamato PRIMA di `_sync_strategy_from_periods()` in tutti i percorsi

## Note

- I log sono stati aggiunti SENZA modificare la logica esistente
- Tutti i log includono snapshot di `params` per tracciare le modifiche
- I REPORT finali permettono di confrontare lo stato prima di ogni return

