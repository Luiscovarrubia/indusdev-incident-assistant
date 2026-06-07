from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def inicio():
    return jsonify({
        "sistema": "Indusdev Incident Assistant",
        "estado": "Operativo"
    })

@app.route("/health")
def health():
    return jsonify({"ok": True})