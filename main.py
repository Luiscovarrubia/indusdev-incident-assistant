from fastapi import FastAPI

app = FastAPI(
    title="Indusdev Incident Assistant",
    version="0.1"
)

@app.get("/")
def inicio():
    return {
        "sistema": "Indusdev Incident Assistant",
        "estado": "Operativo"
    }

@app.get("/health")
def health():
    return {
        "ok": True
    }