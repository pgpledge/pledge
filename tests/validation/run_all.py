from bank import BankScenario
from berka import BerkaScenario
from common import run_scenarios
from model import ModelScenario

if __name__ == "__main__":
    raise SystemExit(run_scenarios(BankScenario(), ModelScenario(), BerkaScenario()))
