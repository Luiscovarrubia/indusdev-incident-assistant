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


# ==========================================================
# INDUSDEV INCIDENT ASSISTANT
# main.py
#
# Backend principal:
# - FastAPI
# - Supabase REST API
# - Dashboard
# - Gestión de incidentes
# - Evidencias, fotos y QR
# - Modo técnico
# - Tiempo detenido
# - Ficha de máquina
# - Base preparada para IoT
# ==========================================================


# ==========================================================
# 1. CONFIGURACIÓN GENERAL
# ==========================================================

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Clave temporal para habilitar el modo técnico.
# En producción esto debería evolucionar a usuarios y roles.
CLAVE_TECNICA = os.getenv("CLAVE_TECNICA", "tec2026")

# Zona horaria usada para mostrar fechas al usuario.
# Supabase guarda timestamptz normalmente en UTC.
CHILE_TZ = pytz.timezone("America/Santiago")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

app = FastAPI(title="Indusdev Incident Assistant", version="0.9")

os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ==========================================================
# 2. MODELOS DE ENTRADA
# ==========================================================

class IncidenteEntrada(BaseModel):
    """
    Datos recibidos desde M5Stack / ESP32 / terminal.
    """

    maquina: str
    descripcion: Optional[str] = "Incidente creado desde dispositivo"
    estado_maquina: Optional[str] = "OPERATIVA_CON_ANOMALIA"


class TelemetriaEntrada(BaseModel):
    """
    Modelo inicial para futuras variables IoT.
    Requiere crear tabla 'telemetria' en Supabase antes de usarlo.
    """

    temperatura: Optional[float] = None
    presion: Optional[float] = None
    vibracion: Optional[float] = None
    corriente: Optional[float] = None
    estado_sensor: Optional[str] = "OK"


# ==========================================================
# 3. FUNCIONES SUPABASE
# ==========================================================

def supabase_get(path: str):
    """Ejecuta GET contra Supabase REST."""

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
    """Ejecuta POST contra una tabla Supabase."""

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
    """Ejecuta PATCH contra Supabase."""

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
        return []

    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    response = requests.patch(url, headers=HEADERS, json=payload)

    if response.status_code >= 400:
        print("Error Supabase PATCH:", response.status_code, response.text)
        return []

    return response.json()


# ==========================================================
# 4. FUNCIONES DE FORMATO
# ==========================================================

def convertir_fecha_chile(valor):
    """Convierte fechas ISO/UTC de Supabase a horario Chile."""

    if not valor:
        return ""

    try:
        dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(CHILE_TZ).strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        return valor


def formatear_duracion(minutos):
    """Convierte minutos a formato legible: 29 min, 2 h 15 min, 1 d 2 h."""

    if minutos is None:
        return "-"

    try:
        minutos = int(minutos)
    except Exception:
        return "-"

    if minutos <= 0:
        return "-"

    dias = minutos // 1440
    resto = minutos % 1440
    horas = resto // 60
    mins = resto % 60

    partes = []

    if dias > 0:
        partes.append(f"{dias} d")
    if horas > 0:
        partes.append(f"{horas} h")
    if mins > 0:
        partes.append(f"{mins} min")

    return " ".join(partes)


def formatear_fechas_incidente(inc):
    """Aplica formato horario Chile a campos de fecha del incidente."""

    for campo in [
        "created_at",
        "cerrado_at",
        "inicio_parada",
        "fin_parada",
        "ultima_actualizacion",
    ]:
        if campo in inc:
            inc[campo] = convertir_fecha_chile(inc.get(campo))


def formatear_fechas_comentario(comentario):
    """Aplica formato horario Chile a fechas de comentarios."""

    if "fecha" in comentario:
        comentario["fecha"] = convertir_fecha_chile(comentario.get("fecha"))

    if "created_at" in comentario:
        comentario["created_at"] = convertir_fecha_chile(
            comentario.get("created_at")
        )


# ==========================================================
# 5. FUNCIONES DE NEGOCIO
# ==========================================================

def generar_qr_base64(url: str):
    """Genera QR en base64 para mostrarlo directamente en HTML."""

    qr = qrcode.make(url)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{qr_base64}"


def preparar_incidente_para_vista(inc):
    """
    Normaliza un incidente para HTML/API.
    Agrega estado_maquina, duración y campos resumen con fallback.
    """

    inc["estado_maquina"] = (
        inc.get("estado_maquina_actual")
        or inc.get("estado_maquina")
        or "SIN_DATO"
    )

    formatear_fechas_incidente(inc)

    inc["duracion_detenido"] = formatear_duracion(
        inc.get("minutos_detenido")
    )
    inc["ultima_falla"] = inc.get("ultima_falla") or "-"
    inc["ultimo_procedimiento"] = inc.get("ultimo_procedimiento") or "-"
    inc["ultimos_componentes"] = inc.get("ultimos_componentes") or "-"

    return inc


def calcular_resumen(incidentes):
    """Calcula totales para el dashboard."""

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


def construir_panel_maquinas(incidentes):
    """
    Construye tarjetas superiores del dashboard.
    Los incidentes llegan ordenados desc, por eso el primero por máquina es el más reciente.
    """

    maquinas = {}

    for inc in incidentes:
        nombre = inc.get("maquina", "SIN_MAQUINA")

        if nombre not in maquinas:
            maquinas[nombre] = {
                "maquina": nombre,
                "incidente_id": inc.get("id"),
                "estado_incidente": inc.get("estado", "SIN_DATO"),
                "estado_maquina": inc.get("estado_maquina", "SIN_DATO"),
                "created_at": inc.get("created_at", ""),
                "minutos_detenido": inc.get("minutos_detenido", 0),
                "duracion_detenido": inc.get("duracion_detenido", "-"),
                "ultima_falla": inc.get("ultima_falla") or "-",
            }

    return list(maquinas.values())


def obtener_ultimo_estado_maquina(incidente_id: int):
    """
    Fallback histórico.
    Se prefiere usar incidentes.estado_maquina_actual.
    """

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
    """Devuelve historial completo del incidente."""

    return supabase_get(
        "comentarios?"
        "select=*"
        f"&incidente_id=eq.{incidente_id}"
        "&order=id.asc"
    )


def actualizar_resumen_tecnico_incidente(
    incidente_id: int,
    autor: str,
    falla: str,
    procedimiento: str,
    componentes: str,
):
    """
    Actualiza campos resumen en incidentes.

    comentarios conserva el historial completo.
    incidentes guarda el resumen para cargar dashboards y fichas rápido.
    """

    payload = {
        "ultima_actualizacion": datetime.now(timezone.utc).isoformat(),
    }

    if falla.strip():
        payload["ultima_falla"] = falla
    if procedimiento.strip():
        payload["ultimo_procedimiento"] = procedimiento
    if componentes.strip():
        payload["ultimos_componentes"] = componentes
    if autor.strip():
        payload["ultimo_tecnico"] = autor

    supabase_patch("incidentes", f"id=eq.{incidente_id}", payload)


def actualizar_tiempo_detencion_por_estado(incidente_id: int, estado_maquina: str):
    """
    Calcula tiempo real de detención.
    DETENIDA inicia contador; OPERATIVA termina contador.
    CERRADO solo es cierre administrativo.
    """

    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return

    incidente = incidentes[0]

    if estado_maquina == "DETENIDA":
        if not incidente.get("inicio_parada"):
            supabase_patch(
                "incidentes",
                f"id=eq.{incidente_id}",
                {
                    "inicio_parada": datetime.now(timezone.utc).isoformat(),
                    "fin_parada": None,
                    "tipo_incidente": "DETENIDA",
                    "minutos_detenido": 0,
                },
            )
        return

    if estado_maquina == "OPERATIVA":
        inicio_parada = incidente.get("inicio_parada")

        if not inicio_parada:
            return

        try:
            inicio = datetime.fromisoformat(inicio_parada.replace("Z", "+00:00"))
            fin = datetime.now(timezone.utc)
            segundos = int((fin - inicio).total_seconds())
            minutos = 0 if segundos <= 0 else max(1, round(segundos / 60))

            supabase_patch(
                "incidentes",
                f"id=eq.{incidente_id}",
                {
                    "fin_parada": fin.isoformat(),
                    "minutos_detenido": minutos,
                },
            )

        except Exception as error:
            print("Error calculando tiempo detenido:", error)


def guardar_foto_local(incidente_id: int, foto: Optional[UploadFile]):
    """Guarda fotografía local en static/uploads y devuelve URL relativa."""

    if not foto or not foto.filename:
        return None

    extension = os.path.splitext(foto.filename)[1]
    nombre_archivo = (
        f"incidente_{incidente_id}_"
        f"{datetime.now().strftime('%Y%m%d%H%M%S')}{extension}"
    )
    ruta_archivo = f"static/uploads/{nombre_archivo}"

    with open(ruta_archivo, "wb") as buffer:
        shutil.copyfileobj(foto.file, buffer)

    return f"/static/uploads/{nombre_archivo}"


# ==========================================================
# 6. RUTAS BÁSICAS
# ==========================================================

@app.get("/")
def inicio():
    return {
        "sistema": "Indusdev Incident Assistant",
        "estado": "Operativo",
        "version": "0.9",
        "base_datos": "Supabase",
    }


@app.get("/health")
def health():
    return {"ok": True}


# ==========================================================
# 7. DASHBOARD
# ==========================================================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """Dashboard general optimizado."""

    incidentes = supabase_get("incidentes?select=*&order=id.desc")

    for inc in incidentes:
        preparar_incidente_para_vista(inc)

    resumen = calcular_resumen(incidentes)
    maquinas_panel = construir_panel_maquinas(incidentes)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "incidentes": incidentes,
            "resumen": resumen,
            "maquinas_panel": maquinas_panel,
        },
    )


@app.get("/incidentes")
def listar_incidentes():
    """API simple para listar incidentes."""

    incidentes = supabase_get("incidentes?select=*&order=id.desc")

    for inc in incidentes:
        preparar_incidente_para_vista(inc)

    return {"cantidad": len(incidentes), "incidentes": incidentes}


# ==========================================================
# 8. FICHA DE MÁQUINA
# ==========================================================

@app.get("/maquina/{nombre_maquina}", response_class=HTMLResponse)
def ver_maquina(request: Request, nombre_maquina: str):
    """Ficha operacional de máquina."""

    incidentes = supabase_get(
        "incidentes?"
        "select=*"
        f"&maquina=eq.{nombre_maquina}"
        "&order=id.desc"
    )

    for inc in incidentes:
        preparar_incidente_para_vista(inc)

    estado_actual = "SIN_DATO"

    if incidentes:
        estado_actual = incidentes[0].get("estado_maquina", "SIN_DATO")

    return templates.TemplateResponse(
        request=request,
        name="maquina.html",
        context={
            "maquina": nombre_maquina,
            "estado_actual": estado_actual,
            "incidentes": incidentes,
        },
    )


# ==========================================================
# 9. INCIDENTES
# ==========================================================

@app.post("/incidente")
def crear_incidente(data: IncidenteEntrada):
    """Crea incidente desde M5Stack / ESP32 / terminal."""

    ahora_utc = datetime.now(timezone.utc).isoformat()

    payload_incidente = {
        "maquina": data.maquina,
        "estado": "ABIERTO",
        "prioridad": "MEDIA",
        "descripcion": data.descripcion,
        "m5_activo": True,
        "tipo_incidente": data.estado_maquina,
        "estado_maquina_actual": data.estado_maquina,
        "ultima_actualizacion": ahora_utc,
    }

    if data.estado_maquina == "DETENIDA":
        payload_incidente["inicio_parada"] = ahora_utc
        payload_incidente["fin_parada"] = None
        payload_incidente["minutos_detenido"] = 0

    creado = supabase_post("incidentes", payload_incidente)

    if not creado:
        return {"ok": False, "mensaje": "No se pudo crear el incidente"}

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

    return {"ok": True, "mensaje": "Incidente creado", "incidente": incidente}


@app.get("/incidente/{incidente_id}", response_class=HTMLResponse)
def ver_incidente_web(request: Request, incidente_id: int):
    """Vista web del incidente."""

    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return HTMLResponse("Incidente no encontrado", status_code=404)

    incidente = preparar_incidente_para_vista(incidentes[0])
    comentarios = obtener_comentarios_incidente(incidente_id)

    for comentario in comentarios:
        formatear_fechas_comentario(comentario)

    url_incidente = str(request.url)
    qr_img = generar_qr_base64(url_incidente)

    return templates.TemplateResponse(
        request=request,
        name="incidente.html",
        context={
            "incidente": incidente,
            "comentarios": comentarios,
            "qr_img": qr_img,
            "url_incidente": url_incidente,
        },
    )


@app.get("/api/incidente/{incidente_id}")
def ver_incidente_api(incidente_id: int):
    """API para obtener incidente completo."""

    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return {"ok": False, "mensaje": "Incidente no encontrado"}

    incidente = preparar_incidente_para_vista(incidentes[0])
    comentarios = obtener_comentarios_incidente(incidente_id)

    return {"ok": True, "incidente": incidente, "comentarios": comentarios}


@app.post("/incidente/{incidente_id}/estado")
def actualizar_estado_incidente(
    incidente_id: int,
    estado: str = Form("ABIERTO"),
):
    """Cambia el estado administrativo del incidente."""

    estados_validos = ["ABIERTO", "EN_REVISION", "CERRADO"]

    if estado not in estados_validos:
        return {"ok": False, "mensaje": "Estado no válido"}

    payload = {"estado": estado}

    if estado == "CERRADO":
        payload["m5_activo"] = False
        payload["cerrado_at"] = datetime.now(timezone.utc).isoformat()
    else:
        payload["m5_activo"] = True
        payload["cerrado_at"] = None

    actualizado = supabase_patch("incidentes", f"id=eq.{incidente_id}", payload)

    if not actualizado:
        return {"ok": False, "mensaje": "No se pudo actualizar el estado"}

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

    return RedirectResponse(url=f"/incidente/{incidente_id}", status_code=303)


# ==========================================================
# 10. COMENTARIOS / EVIDENCIAS / MODO TÉCNICO
# ==========================================================

@app.post("/incidente/{incidente_id}/comentario")
async def agregar_comentario(
    incidente_id: int,
    autor: str = Form("Supervisor"),
    tipo: str = Form("SUPERVISOR"),
    estado_maquina: str = Form("OPERATIVA_CON_ANOMALIA"),
    comentario: str = Form(""),
    falla: str = Form(""),
    procedimiento: str = Form(""),
    componentes: str = Form(""),
    clave_tecnica: str = Form(""),
    foto: Optional[UploadFile] = File(None),
):
    """Agrega evidencia a un incidente."""

    incidentes = supabase_get(f"incidentes?select=*&id=eq.{incidente_id}")

    if not incidentes:
        return {"ok": False, "mensaje": "Incidente no encontrado"}

    es_registro_tecnico = any([
        falla.strip(),
        procedimiento.strip(),
        componentes.strip(),
    ])

    if es_registro_tecnico and clave_tecnica != CLAVE_TECNICA:
        return {"ok": False, "mensaje": "Clave técnica incorrecta"}

    foto_url = guardar_foto_local(incidente_id, foto)

    payload_comentario = {
        "incidente_id": incidente_id,
        "autor": autor,
        "tipo": tipo,
        "comentario": comentario,
        "foto_url": foto_url,
        "estado_maquina": estado_maquina,
        "prioridad": "MEDIA",
        "visible_cliente": True,
        "falla": falla,
        "procedimiento": procedimiento,
        "componentes": componentes,
    }

    supabase_post("comentarios", payload_comentario)

    if es_registro_tecnico:
        actualizar_resumen_tecnico_incidente(
            incidente_id=incidente_id,
            autor=autor,
            falla=falla,
            procedimiento=procedimiento,
            componentes=componentes,
        )

    actualizar_tiempo_detencion_por_estado(incidente_id, estado_maquina)

    supabase_patch(
        "incidentes",
        f"id=eq.{incidente_id}",
        {
            "estado_maquina_actual": estado_maquina,
            "ultima_actualizacion": datetime.now(timezone.utc).isoformat(),
        },
    )

    return RedirectResponse(url=f"/incidente/{incidente_id}", status_code=303)


# ==========================================================
# 11. API M5 / ESP32
# ==========================================================

@app.get("/api/maquina/{maquina}")
def estado_maquina_para_terminal(maquina: str):
    """API usada por M5Stack / ESP32 para consultar si hay incidente activo."""

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
    estado_maquina = (
        incidente.get("estado_maquina_actual")
        or obtener_ultimo_estado_maquina(incidente_id)
    )

    return {
        "ok": True,
        "maquina": maquina,
        "estado_dispositivo": "INCIDENTE",
        "incidente_id": incidente_id,
        "estado_incidente": estado,
        "estado_maquina": estado_maquina,
        "url_incidente": f"/incidente/{incidente_id}",
    }


# ==========================================================
# 12. PREPARACIÓN IoT
# ==========================================================

@app.post("/api/iot/{maquina}")
def recibir_iot(maquina: str, data: TelemetriaEntrada):
    """
    Endpoint preparado para futuros ESP32 IoT.
    Requiere crear tabla 'telemetria' en Supabase antes de usarlo.
    """

    payload = {
        "maquina": maquina,
        "temperatura": data.temperatura,
        "presion": data.presion,
        "vibracion": data.vibracion,
        "corriente": data.corriente,
        "estado_sensor": data.estado_sensor,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    creado = supabase_post("telemetria", payload)

    if not creado:
        return {
            "ok": False,
            "mensaje": "No se pudo guardar telemetría. Verifique tabla telemetria.",
        }

    return {
        "ok": True,
        "mensaje": "Telemetría registrada",
        "maquina": maquina,
        "telemetria": creado[0],
    }


@app.get("/api/iot/{maquina}/ultima")
def obtener_ultima_iot(maquina: str):
    """Devuelve la última muestra IoT de una máquina."""

    datos = supabase_get(
        "telemetria?"
        "select=*"
        f"&maquina=eq.{maquina}"
        "&order=id.desc"
        "&limit=1"
    )

    if not datos:
        return {
            "ok": True,
            "maquina": maquina,
            "telemetria": None,
            "mensaje": "Sin datos IoT",
        }

    return {"ok": True, "maquina": maquina, "telemetria": datos[0]}

