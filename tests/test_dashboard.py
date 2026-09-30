# test_dashboard.py
# Standalone mock dashboard: serves the real static page with synthetic data
# (temperature, aggregate history, and per-user overlays) so the UI can be
# previewed without the bot or real sensors.
import math
import os
import random
import time

from flask import Flask, jsonify, request, send_from_directory

STATIC = os.path.join(os.path.dirname(__file__), '..', 'static')
app = Flask(__name__, static_folder=STATIC)

THRESHOLDS = {'cpu': 90, 'ram': 90, 'disk': 90, 'disk_2': 90, 'temp': 85}
USERS = ['alice', 'bob', 'carol', 'dave', 'root']
STEP = 15


def sample():
    return {
        'cpu':   random.uniform(20, 95),
        'ram':   random.uniform(40, 90),
        'disk':  random.uniform(30, 70),
        'disk2': random.uniform(60, 95),
        'temp':  random.uniform(45, 88),
    }


@app.route('/metrics')
def metrics():
    return jsonify(sample())


@app.route('/history')
def history():
    seconds = request.args.get('seconds', default=3600, type=int)
    now = int(time.time())
    rows = []
    for t in range(now - seconds, now + 1, STEP):
        row = sample()
        row['ts'] = t
        rows.append(row)
    return jsonify(rows)


@app.route('/users')
def users():
    return jsonify({'users': USERS})


@app.route('/users/current')
def users_current():
    rows = [{'user': u, 'cpu': round(random.uniform(0, 60), 1), 'ram': round(random.uniform(0, 40), 1)} for u in USERS]
    rows.sort(key=lambda r: r['cpu'], reverse=True)
    return jsonify(rows)


@app.route('/users/history')
def users_history():
    seconds = request.args.get('seconds', default=3600, type=int)
    names = [n for n in request.args.get('users', '').split(',') if n]
    now = int(time.time())
    rows = []
    for t in range(now - seconds, now + 1, STEP):
        for i, u in enumerate(names):
            phase = i * 1.3
            rows.append({
                'ts': t, 'user': u,
                'cpu':   round(20 + 18 * (1 + math.sin(t / 300 + phase)), 1),
                'ram':   round(10 + 12 * (1 + math.cos(t / 400 + phase)), 1),
                'disk':  round(5 + i * 7, 1),
                'disk2': round(3 + i * 5, 1),
            })
    return jsonify(rows)


@app.route('/config')
def config():
    return jsonify({'thresholds': THRESHOLDS, 'default_users': ['alice', 'carol']})


@app.route('/')
def index():
    return send_from_directory(STATIC, 'index.html')


if __name__ == '__main__':
    app.run(port=8080)
