from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
import os
import shutil
import requests
import qrcode
from io import BytesIO
import base64

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

app = FastAPI(title="Indusdev Incident Assistant", version="0.3")

os.makedirs("static/uploads", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class IncidenteEntrada(BaseModel):
    maquina: str
    descripcion: Optional[str] = "Incidente creado desde dispositivo"


@app.get("/")
def inicio():
    return {
        "sistema": "Indusdev Incident Assistant",
        "estado": "Operativo",
        "base_datos": "Supabase"
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    incidentes = supabase_get("incidentes?select=*&order=id.desc")

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "incidentes": incidentes}
    )


@app.get("/incidentes")
def listar_incidentes():
    incidentes = supabase_get("incidentes?select=*&order=id.desc")
    return {
        "cantidad": len(incidentes),
        "incidentes": incidentes
    }


@app.post("/incidente")
def crear_incidente(data: IncidenteEntrada):
    payload = {
        "maquina": data.maquina,
        "estado": "ABIERTO",
        "prioridad": "MEDIA",
        "descripcion": data.descripcion
    }

    creado = supabase_post("incidentes", payload)

    return {
        "ok": True,
        "mensaje": "Incidente creado",
        "incidente": creado[0] if creado else None
    }


@app.get("/incidente/{incidente_id}", response_class=HTMLResponse)
def ver_incidente_web(request: Request, incidente_id: int):
    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return HTMLResponse("Incidente no encontrado", status_code=404)

    comentarios = supabase_get(
        f"comentarios?select=*&incidente_id=eq.{incidente_id}&order=id.asc"
    )

    # return templates.TemplateResponse(
    #     "incidente.html",
    #     {
    #         "request": request,
    #         "incidente": incidentes[0],
    #         "comentarios": comentarios
    #     }
    # )

    url_incidente = str(request.url)
    qr_img = generar_qr_base64(url_incidente)

    return templates.TemplateResponse(
    "incidente.html",
    {
        "request": request,
        "incidente": incidentes[0],
        "comentarios": comentarios,
        "qr_img": qr_img,
        "url_incidente": url_incidente
    }
    )




@app.get("/api/incidente/{incidente_id}")
def ver_incidente_api(incidente_id: int):
    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return {
            "ok": False,
            "mensaje": "Incidente no encontrado"
        }

    comentarios = supabase_get(
        f"comentarios?select=*&incidente_id=eq.{incidente_id}&order=id.asc"
    )

    return {
        "ok": True,
        "incidente": incidentes[0],
        "comentarios": comentarios
    }


@app.post("/incidente/{incidente_id}/comentario")
async def agregar_comentario(
    incidente_id: int,
    autor: str = Form("Operador"),
    tipo: str = Form("OPERADOR"),
    comentario: str = Form(""),
    foto: Optional[UploadFile] = File(None)
):
    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return {
            "ok": False,
            "mensaje": "Incidente no encontrado"
        }

    foto_url = None

    if foto and foto.filename:
        extension = os.path.splitext(foto.filename)[1]
        nombre_archivo = f"incidente_{incidente_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{extension}"
        ruta_archivo = f"static/uploads/{nombre_archivo}"

        with open(ruta_archivo, "wb") as buffer:
            shutil.copyfileobj(foto.file, buffer)

        foto_url = f"/static/uploads/{nombre_archivo}"

    payload = {
        "incidente_id": incidente_id,
        "autor": autor,
        "tipo": tipo,
        "comentario": comentario,
        "foto_url": foto_url,
        "estado_maquina": "OPERATIVA_CON_ANOMALIA",
        "prioridad": "MEDIA",
        "visible_cliente": True
    }

    supabase_post("comentarios", payload)

    return RedirectResponse(
        url=f"/incidente/{incidente_id}",
        status_code=303
    )


def supabase_get(path: str):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    response = requests.get(url, headers=HEADERS)

    if response.status_code >= 400:
        print("Error Supabase GET:", response.status_code, response.text)
        return []

    return response.json()


def supabase_post(table: str, payload: dict):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code >= 400:
        print("Error Supabase POST:", response.status_code, response.text)
        return []

    return response.json()

def generar_qr_base64(url: str):
    qr = qrcode.make(url)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{qr_base64}"
