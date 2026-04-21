# bridge_runner.py
# Wrapper semplice attorno a BybitBridge.
# Non apre/chiude nulla da solo: serve come base per runner reali.

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass

from .bybit_bridge import BybitBridge


@dataclass
class RunnerConfig:
    session: str
    pair: str = "BTCUSDT"
    interval: int = 15  # secondi tra un ciclo e l'altro
    env: str = "bybit_testnet"  # descrizione ambiente (testnet / live)


class BridgeRunner:
    """
    Piccolo wrapper attorno a BybitBridge.
    Per ora:
    - inizializza il bridge
    - espone step() per script esterni

    La logica (strategie, ordini ecc.) resta fuori.
    """

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.bridge = BybitBridge()

    def step(self) -> None:
        # Placeholder: la logica verrà implementata in runner reali.
        return

    def run_forever(self) -> None:
        cfg = self.config
        print(
            f"[bridge_runner] starting "
            f"session={cfg.session} pair={cfg.pair} env={cfg.env} interval={cfg.interval}s"
        )

        try:
            while True:
                try:
                    self.step()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print("[bridge_runner] errore nel ciclo:", e)
                    traceback.print_exc()

                time.sleep(cfg.interval)
        except KeyboardInterrupt:
            print("\n[bridge_runner] stop manuale (Ctrl+C).")


def run_loop(session: str, pair: str = "BTCUSDT", interval: int = 15, env: str = "bybit_testnet") -> None:
    cfg = RunnerConfig(session=session, pair=pair, interval=interval, env=env)
    runner = BridgeRunner(cfg)
    runner.run_forever()


def main(session: str, pair: str = "BTCUSDT", interval: int = 15, env: str = "bybit_testnet") -> None:
    run_loop(session=session, pair=pair, interval=interval, env=env)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True, help="Nome della sessione Idith")
    parser.add_argument("--pair", default="BTCUSDT", help="Coppia es. BTCUSDT")
    parser.add_argument("--interval", type=int, default=15, help="Intervallo in secondi")
    parser.add_argument("--env", default="bybit_testnet", help="Descrizione ambiente")

    args = parser.parse_args()
    main(session=args.session, pair=args.pair, interval=args.interval, env=args.env)
