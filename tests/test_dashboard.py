# test_dashboard.py
import sys
sys.path.append('..')  # Aggiungi la directory corrente al path
import random, time
from dashboard import app

@app.route('/metrics')
def mock_metrics():
    return {
        'cpu':   random.uniform(20, 95),
        'ram':   random.uniform(40, 90),
        'disk':  random.uniform(30, 70),
        'disk2': random.uniform(60, 95),
    }

if __name__ == '__main__':
    app.run(port=8080)