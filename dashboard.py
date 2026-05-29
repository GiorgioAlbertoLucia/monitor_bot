# dashboard.py
import psutil
from flask import Flask, jsonify, send_from_directory

from pyngrok import ngrok


app = Flask(__name__, static_folder="static")

@app.route("/metrics")
def metrics():
    return jsonify({
        "cpu":   psutil.cpu_percent(interval=0.5),
        "ram":   psutil.virtual_memory().percent,
        "disk":  psutil.disk_usage("/home").percent,
        "disk2": psutil.disk_usage("/data").percent,
    })

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

def start(host="0.0.0.0", port=8080):
    tunnel = ngrok.connect(port, domain="your-free-domain.ngrok-free.app")
    app.run(host=host, port=port, use_reloader=False)
    return tunnel.public_url
