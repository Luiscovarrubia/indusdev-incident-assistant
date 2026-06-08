from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
import base64
import os
import shutil

import pytz
import qrcode
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

CHILE_TZ = pytz.timezone("America/Santiago")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

app = FastAPI(title="Indusdev Incident Assistant", version="0.6")

os.makedirs("static/uploads", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class IncidenteEntrada(BaseModel):
    maquina: str
    descripcion: Optional[str] = "Incidente creado desde dispositivo"
    estado_maquina: Optional[str] = "OPERATIVA_CON_ANOMALIA"


@app.get("/")
def inicio():
    return {
        "sistema": "Indusdev Incident Assistant",
        "estado": "Operativo",
        "base_datos": "Supabase",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    incidentes = supabase_get("incidentes?select=*&order=id.desc")

    for inc in incidentes:
        inc["estado_maquina"] = obtener_ultimo_estado_maquina(inc["id"])
        formatear_fechas_incidente(inc)

    resumen = calcular_resumen(incidentes)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidentes": incidentes,
            "resumen": resumen,
        },
    )


@app.get("/incidentes")
def listar_incidentes():
    incidentes = supabase_get("incidentes?select=*&order=id.desc")

    for inc in incidentes:
        inc["estado_maquina"] = obtener_ultimo_estado_maquina(inc["id"])
        formatear_fechas_incidente(inc)

    return {
        "cantidad": len(incidentes),
        "incidentes": incidentes,
    }


@app.post("/incidente")
def crear_incidente(data: IncidenteEntrada):
    payload_incidente = {
        "maquina": data.maquina,
        "estado": "ABIERTO",
        "prioridad": "MEDIA",
        "descripcion": data.descripcion,
        "m5_activo": True,
    }

    creado = supabase_post("incidentes", payload_incidente)

    if not creado:
        return {
            "ok": False,
            "mensaje": "No se pudo crear el incidente",
        }

    incidente = creado[0]
    incidente_id = incidente["id"]

    payload_comentario_inicial = {
        "incidente_id": incidente_id,
        "autor": "Dispositivo",
        "tipo": "OPERADOR",
        "comentario": data.descripcion,
        "foto_url": None,
        "estado_maquina": data.estado_maquina,
        "prioridad": "MEDIA",
        "visible_cliente": True,
    }

    supabase_post("comentarios", payload_comentario_inicial)

    incidente["estado_maquina"] = data.estado_maquina

    return {
        "ok": True,
        "mensaje": "Incidente creado",
        "incidente": incidente,
    }


@app.get("/incidente/{incidente_id}", response_class=HTMLResponse)
def ver_incidente_web(request: Request, incidente_id: int):
    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return HTMLResponse("Incidente no encontrado", status_code=404)

    incidente = incidentes[0]
    incidente["estado_maquina"] = obtener_ultimo_estado_maquina(incidente_id)
    formatear_fechas_incidente(incidente)

    comentarios = obtener_comentarios_incidente(incidente_id)

    for comentario in comentarios:
        formatear_fechas_comentario(comentario)

    url_incidente = str(request.url)
    qr_img = generar_qr_base64(url_incidente)

    return templates.TemplateResponse(
        "incidente.html",
        {
            "request": request,
            "incidente": incidente,
            "comentarios": comentarios,
            "qr_img": qr_img,
            "url_incidente": url_incidente,
        },
    )


@app.get("/api/incidente/{incidente_id}")
def ver_incidente_api(incidente_id: int):
    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return {
            "ok": False,
            "mensaje": "Incidente no encontrado",
        }

    incidente = incidentes[0]
    incidente["estado_maquina"] = obtener_ultimo_estado_maquina(incidente_id)
    comentarios = obtener_comentarios_incidente(incidente_id)

    return {
        "ok": True,
        "incidente": incidente,
        "comentarios": comentarios,
    }


@app.get("/api/maquina/{maquina}")
def estado_maquina_para_m5(maquina: str):
    incidentes = supabase_get(
        "incidentes?"
        "select=*"
        f"&maquina=eq.{maquina}"
        "&order=id.desc"
        "&limit=1"
    )

    if not incidentes:
        return {
            "ok": True,
            "maquina": maquina,
            "estado_dispositivo": "LIBRE",
            "mensaje": "Sin incidentes",
        }

    incidente = incidentes[0]
    estado = incidente.get("estado")
    m5_activo = incidente.get("m5_activo", False)

    if estado == "CERRADO" or not m5_activo:
        return {
            "ok": True,
            "maquina": maquina,
            "estado_dispositivo": "LIBRE",
            "mensaje": "Sin incidente activo",
        }

    incidente_id = incidente["id"]
    estado_maquina = obtener_ultimo_estado_maquina(incidente_id)

    return {
        "ok": True,
        "maquina": maquina,
        "estado_dispositivo": "INCIDENTE",
        "incidente_id": incidente_id,
        "estado_incidente": estado,
        "estado_maquina": estado_maquina,
        "url_incidente": f"/incidente/{incidente_id}",
    }


@app.post("/incidente/{incidente_id}/estado")
def actualizar_estado_incidente(
    incidente_id: int,
    estado: str = Form("ABIERTO"),
):
    estados_validos = ["ABIERTO", "EN_REVISION", "CERRADO"]

    if estado not in estados_validos:
        return {
            "ok": False,
            "mensaje": "Estado no válido",
        }

    payload = {"estado": estado}

    if estado == "CERRADO":
        payload["m5_activo"] = False
        payload["cerrado_at"] = datetime.now(timezone.utc).isoformat()
    else:
        payload["m5_activo"] = True
        payload["cerrado_at"] = None

    actualizado = supabase_patch(
        "incidentes",
        f"id=eq.{incidente_id}",
        payload,
    )

    if not actualizado:
        return {
            "ok": False,
            "mensaje": "No se pudo actualizar el estado",
        }

    if estado == "CERRADO":
        payload_comentario_cierre = {
            "incidente_id": incidente_id,
            "autor": "Supervisor",
            "tipo": "SUPERVISOR",
            "comentario": "Incidente cerrado desde dashboard",
            "foto_url": None,
            "estado_maquina": "OPERATIVA",
            "prioridad": "MEDIA",
            "visible_cliente": True,
        }

        supabase_post("comentarios", payload_comentario_cierre)

    return RedirectResponse(
        url=f"/incidente/{incidente_id}",
        status_code=303,
    )


@app.post("/incidente/{incidente_id}/comentario")
async def agregar_comentario(
    incidente_id: int,
    autor: str = Form("Operador"),
    tipo: str = Form("OPERADOR"),
    estado_maquina: str = Form("OPERATIVA_CON_ANOMALIA"),
    comentario: str = Form(""),
    foto: Optional[UploadFile] = File(None),
):
    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return {
            "ok": False,
            "mensaje": "Incidente no encontrado",
        }

    foto_url = None

    if foto and foto.filename:
        extension = os.path.splitext(foto.filename)[1]
        nombre_archivo = (
            f"incidente_{incidente_id}_"
            f"{datetime.now().strftime('%Y%m%d%H%M%S')}{extension}"
        )
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
        "estado_maquina": estado_maquina,
        "prioridad": "MEDIA",
        "visible_cliente": True,
    }

    supabase_post("comentarios", payload)

    return RedirectResponse(
        url=f"/incidente/{incidente_id}",
        status_code=303,
    )


def calcular_resumen(incidentes):
    resumen = {
        "total": len(incidentes),
        "incidentes_abiertos": 0,
        "incidentes_revision": 0,
        "incidentes_cerrados": 0,
        "maquinas_operativas": 0,
        "maquinas_anomalia": 0,
        "maquinas_detenidas": 0,
        "maquinas_mantenimiento": 0,
        "maquinas_sin_dato": 0,
    }

    for inc in incidentes:
        estado_incidente = inc.get("estado", "SIN_DATO")
        estado_maquina = inc.get("estado_maquina", "SIN_DATO")

        if estado_incidente == "ABIERTO":
            resumen["incidentes_abiertos"] += 1
        elif estado_incidente == "EN_REVISION":
            resumen["incidentes_revision"] += 1
        elif estado_incidente == "CERRADO":
            resumen["incidentes_cerrados"] += 1

        if estado_maquina == "OPERATIVA":
            resumen["maquinas_operativas"] += 1
        elif estado_maquina == "OPERATIVA_CON_ANOMALIA":
            resumen["maquinas_anomalia"] += 1
        elif estado_maquina == "DETENIDA":
            resumen["maquinas_detenidas"] += 1
        elif estado_maquina == "MANTENIMIENTO":
            resumen["maquinas_mantenimiento"] += 1
        else:
            resumen["maquinas_sin_dato"] += 1

    return resumen


def obtener_ultimo_estado_maquina(incidente_id: int):
    comentarios = supabase_get(
        "comentarios?"
        "select=estado_maquina"
        f"&incidente_id=eq.{incidente_id}"
        "&order=id.desc"
        "&limit=1"
    )

    if comentarios:
        return comentarios[0].get("estado_maquina") or "SIN_DATO"

    return "SIN_DATO"


def obtener_comentarios_incidente(incidente_id: int):
    return supabase_get(
        "comentarios?"
        "select=*"
        f"&incidente_id=eq.{incidente_id}"
        "&order=id.asc"
    )


def generar_qr_base64(url: str):
    qr = qrcode.make(url)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{qr_base64}"


def convertir_fecha_chile(valor):
    if not valor:
        return ""

    try:
        dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        dt_chile = dt.astimezone(CHILE_TZ)
        return dt_chile.strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        return valor


def formatear_fechas_incidente(inc):
    if "created_at" in inc:
        inc["created_at"] = convertir_fecha_chile(inc.get("created_at"))

    if "cerrado_at" in inc:
        inc["cerrado_at"] = convertir_fecha_chile(inc.get("cerrado_at"))


def formatear_fechas_comentario(comentario):
    if "fecha" in comentario:
        comentario["fecha"] = convertir_fecha_chile(comentario.get("fecha"))

    if "created_at" in comentario:
        comentario["created_at"] = convertir_fecha_chile(comentario.get("created_at"))


def supabase_get(path: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    url = f"{SUPABASE_URL}/rest/v1/{path}"
    response = requests.get(url, headers=HEADERS)

    if response.status_code >= 400:
        print("Error Supabase GET:", response.status_code, response.text)
        return []

    return response.json()


def supabase_post(table: str, payload: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    response = requests.post(url, headers=HEADERS, json=payload)

    if response.status_code >= 400:
        print("Error Supabase POST:", response.status_code, response.text)
        return []

    return response.json()


def supabase_patch(table: str, query: str, payload: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    response = requests.patch(url, headers=HEADERS, json=payload)

    if response.status_code >= 400:
        print("Error Supabase PATCH:", response.status_code, response.text)
        return []

    return response.json()