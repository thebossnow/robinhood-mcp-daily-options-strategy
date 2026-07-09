import subprocess
import json
from pathlib import Path

def run_backtest():
    # Run your backtest script and parse metrics
    result = subprocess.run(['python', 'scripts/backtest.py'], capture_output=True, text=True)
    # Parse output for EV, win_rate, max_dd etc. (implement parsing)
    metrics = {'composite': 1.0}  # placeholder
    return metrics

def compute_stability(results):
    # ICIR-like, rolling, decay check
    return {'stability': 0.8, 'decay': 0}

def evaluate(current, baseline, results):
    improved = current['composite'] > baseline.get('composite', 0) * 1.05
    return improved, current

# Main logic for verifier
if __name__ == '__main__':
    print('Verifier running...')