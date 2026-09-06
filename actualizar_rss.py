import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


URL_BASE = "https://reigjofre.com"
URL_NOTICIAS = "https://reigjofre.com/es/noticias/"

ARCHIVO_RSS = Path("rss.xml")
ZONA_HORARIA = ZoneInfo("Europe/Madrid")

MAX_ARTICULOS = 3000
MAX_PAGINAS_HEMEROTECA = 30

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Referer": URL_BASE + "/",
}


def dentro_del_horario():
    """
    Ejecuciones automáticas:
    - De lunes a viernes.
    - Cada hora.
    - Desde las 07:00 hasta las 19:59.
    - Hora peninsular española.

    Las ejecuciones manuales funcionan cualquier día y hora.
    """
    evento = os.environ.get("GITHUB_EVENT_NAME", "")

    if evento == "workflow_dispatch":
        print("Ejecución manual: se ignora el límite horario.")
        return True

    ahora = datetime.now(ZONA_HORARIA)
    print(f"Hora española: {ahora:%Y-%m-%d %H:%M:%S %Z}")

    # Monday=0 ... Saturday=5, Sunday=6
    if ahora.weekday() >= 5:
        print("Fin de semana: no se actualiza el RSS.")
        return False

    if not 7 <= ahora.hour <= 19:
        print("Fuera del horario permitido: 07:00-19:59.")
        return False

    return True


def limpiar_texto(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def normalizar_url(url):
    url = limpiar_texto(url)

    if not url:
        return ""

    return urljoin(URL_BASE, url).split("#")[0]


def pertenece_a_reig_jofre(url):
    try:
        dominio = urlparse(url).netloc.lower()

        return dominio in (
            "reigjofre.com",
            "www.reigjofre.com",
        )
    except ValueError:
        return False


def es_enlace_noticia(url):
    try:
        partes = urlparse(url)
        ruta = partes.path.lower().rstrip("/")

        if not pertenece_a_reig_jofre(url):
            return False

        if "/es/noticias/item/" not in ruta:
            return False

        return len(ruta.split("/")) >= 5

    except ValueError:
        return False


def es_documento(url):
    try:
        ruta = urlparse(url).path.lower()

        return ruta.endswith(
            (".pdf", ".doc", ".docx", ".xls", ".xlsx")
        )
    except ValueError:
        return False


def descargar(sesion, url):
    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = sesion.get(
                url,
                headers=CABECERAS,
                timeout=45,
                allow_redirects=True,
            )
            respuesta.raise_for_status()

            if not respuesta.text.strip():
                raise RuntimeError("La página se descargó vacía.")

            return respuesta.text

        except Exception as error:
            ultimo_error = error
            print(
                f"Intento {intento}/3 fallido para {url}: {error}"
            )

            if intento < 3:
                time.sleep(intento * 2)

    raise RuntimeError(
        f"No se pudo descargar {url}: {ultimo_error}"
    )


def convertir_fecha(valor):
    valor = limpiar_texto(valor).lower()

    meses = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "setiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12,
    }

    patron_texto = (
        r"\b(\d{1,2})\s+"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|"
        r"agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
        r"\s+(\d{4})\b"
    )

    coincidencia = re.search(
        patron_texto,
        valor,
        flags=re.IGNORECASE,
    )

    if coincidencia:
        dia = int(coincidencia.group(1))
        mes = meses[coincidencia.group(2).lower()]
        anio = int(coincidencia.group(3))

        try:
            fecha = datetime(
                anio,
                mes,
                dia,
                12,
                0,
                tzinfo=ZONA_HORARIA,
            )

            return format_datetime(
                fecha.astimezone(timezone.utc)
            )

        except ValueError:
            pass

    coincidencia = re.search(
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
        valor,
    )

    if coincidencia:
        dia, mes, anio = map(int, coincidencia.groups())

        try:
            fecha = datetime(
                anio,
                mes,
                dia,
                12,
                0,
                tzinfo=ZONA_HORARIA,
            )

            return format_datetime(
                fecha.astimezone(timezone.utc)
            )

        except ValueError:
            pass

    return format_datetime(datetime.now(timezone.utc))


def buscar_fecha(texto_completo):
    patron = (
        r"\b\d{1,2}\s+"
        r"(?:enero|febrero|marzo|abril|mayo|junio|julio|"
        r"agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
        r"\s+\d{4}\b"
    )

    coincidencia = re.search(
        patron,
        texto_completo,
        flags=re.IGNORECASE,
    )

    if coincidencia:
        return convertir_fecha(coincidencia.group(0))

    coincidencia = re.search(
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        texto_completo,
    )

    if coincidencia:
        return convertir_fecha(coincidencia.group(0))

    return format_datetime(datetime.now(timezone.utc))


def buscar_contenedor(enlace_html):
    contenedor = enlace_html

    for _ in range(8):
        if contenedor.parent is None:
            break

        contenedor = contenedor.parent
        contenido = contenedor.get_text(" ", strip=True)

        tiene_fecha = bool(
            re.search(
                r"\b\d{1,2}(?:/\d{1,2}/|\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+)"
                r"\d{4}\b",
                contenido,
                flags=re.IGNORECASE,
            )
        )

        numero_noticias = sum(
            1
            for enlace in contenedor.find_all("a", href=True)
            if es_enlace_noticia(
                normalizar_url(enlace.get("href"))
            )
        )

        if tiene_fecha and numero_noticias == 1:
            return contenedor

    return enlace_html.parent or enlace_html


def obtener_titulo(enlace_html, contenedor):
    titulo = limpiar_texto(
        enlace_html.get_text(" ", strip=True)
    )

    genericos = {
        "leer más",
        "read more",
        "descargar",
        "saber más",
    }

    if titulo.lower() in genericos:
        titulo = ""

    if len(titulo) >= 12:
        return titulo

    for selector in (
        "h1",
        "h2",
        "h3",
        "h4",
        ".entry-title",
        ".post-title",
        ".title",
        ".titulo",
    ):
        elemento = contenedor.select_one(selector)

        if elemento:
            titulo = limpiar_texto(
                elemento.get_text(" ", strip=True)
            )

            if (
                len(titulo) >= 12
                and titulo.lower() not in genericos
            ):
                return titulo

    return ""


def obtener_resumen(contenedor, titulo):
    candidatos = []

    for elemento in contenedor.find_all(
        ["p", "div", "span"],
    ):
        contenido = limpiar_texto(
            elemento.get_text(" ", strip=True)
        )

        if not contenido or contenido == titulo:
            continue

        if contenido.lower() in (
            "leer más",
            "descargar",
            "descargue el documento completo",
        ):
            continue

        if re.fullmatch(
            r"\d{1,2}(?:/\d{1,2}/|\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+)"
            r"\d{4}",
            contenido,
        ):
            continue

        if 30 <= len(contenido) <= 1000:
            candidatos.append(contenido)

    if candidatos:
        candidatos.sort(key=len)
        return candidatos[0]

    return ""


def obtener_imagen(contenedor):
    imagen = contenedor.find("img")

    if not imagen:
        return ""

    for atributo in (
        "data-src",
        "data-lazy-src",
        "data-original",
        "src",
    ):
        url = normalizar_url(imagen.get(atributo))

        if url and not url.startswith("data:"):
            return url

    srcset = limpiar_texto(
        imagen.get("data-srcset")
        or imagen.get("srcset")
    )

    if srcset:
        primera = srcset.split(",")[0].strip().split(" ")[0]
        return normalizar_url(primera)

    return ""


def obtener_documento(contenedor):
    for enlace_html in contenedor.find_all("a", href=True):
        url = normalizar_url(enlace_html.get("href"))
        etiqueta = limpiar_texto(
            enlace_html.get_text(" ", strip=True)
        ).lower()

        if es_documento(url):
            return url

        if (
            "descargar" in etiqueta
            or "documento completo" in etiqueta
        ):
            if url:
                return url

    return ""


def obtener_paginas_hemeroteca(sopa):
    paginas = [URL_NOTICIAS]
    vistas = {URL_NOTICIAS.rstrip("/")}

    for enlace_html in sopa.find_all("a", href=True):
        etiqueta = limpiar_texto(
            enlace_html.get_text(" ", strip=True)
        )
        url = normalizar_url(enlace_html.get("href"))
        ruta = urlparse(url).path.lower()

        es_anio = bool(re.fullmatch(r"20\d{2}", etiqueta))
        es_archivo = (
            "/es/noticias/" in ruta
            and "/item/" not in ruta
        )

        clave = url.rstrip("/")

        if (
            es_anio
            and es_archivo
            and pertenece_a_reig_jofre(url)
            and clave not in vistas
        ):
            vistas.add(clave)
            paginas.append(url)

        if len(paginas) >= MAX_PAGINAS_HEMEROTECA:
            break

    return paginas


def extraer_articulos_pagina(sopa):
    articulos = []
    vistos = set()

    for enlace_html in sopa.find_all("a", href=True):
        enlace = normalizar_url(enlace_html.get("href"))

        if not es_enlace_noticia(enlace):
            continue

        enlace = enlace.split("?")[0].rstrip("/")

        if enlace in vistos:
            continue

        contenedor = buscar_contenedor(enlace_html)
        titulo = obtener_titulo(enlace_html, contenedor)

        if not titulo:
            continue

        contenido = contenedor.get_text(" ", strip=True)
        fecha = buscar_fecha(contenido)
        resumen = obtener_resumen(contenedor, titulo)
        imagen = obtener_imagen(contenedor)
        documento = obtener_documento(contenedor)

        descripcion = ""

        if resumen:
            descripcion += f"<p>{resumen}</p>"

        if documento:
            descripcion += (
                f'<p><a href="{documento}">'
                f"Descargar documento de Reig Jofre"
                f"</a></p>"
            )

        descripcion += (
            f'<p><a href="{enlace}">'
            f"Leer la noticia completa en Reig Jofre"
            f"</a></p>"
        )

        articulos.append(
            {
                "title": titulo,
                "link": enlace,
                "guid": enlace,
                "pubDate": fecha,
                "description": descripcion,
                "author": "Laboratorio Reig Jofre",
                "categories": [
                    "Reig Jofre",
                    "Noticias corporativas",
                ],
                "image": imagen,
            }
        )

        vistos.add(enlace)

    return articulos


def extraer_noticias():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    html_principal = descargar(sesion, URL_NOTICIAS)
    sopa_principal = BeautifulSoup(
        html_principal,
        "html.parser",
    )

    paginas = obtener_paginas_hemeroteca(sopa_principal)
    articulos = []
    paginas_visitadas = set()

    for numero, pagina in enumerate(paginas, start=1):
        clave_pagina = pagina.rstrip("/")

        if clave_pagina in paginas_visitadas:
            continue

        paginas_visitadas.add(clave_pagina)

        try:
            if clave_pagina == URL_NOTICIAS.rstrip("/"):
                sopa = sopa_principal
            else:
                print(f"Descargando hemeroteca: {pagina}")
                html = descargar(sesion, pagina)
                sopa = BeautifulSoup(html, "html.parser")
                time.sleep(0.4)

            encontrados = extraer_articulos_pagina(sopa)
            articulos.extend(encontrados)

            print(
                f"Página {numero}/{len(paginas)}: "
                f"{len(encontrados)} noticias."
            )

        except Exception as error:
            print(
                f"AVISO: no se pudo procesar {pagina}: {error}"
            )

    resultado = []
    noticias_vistas = set()

    for articulo in articulos:
        clave = articulo["link"].lower().rstrip("/")

        if clave in noticias_vistas:
            continue

        noticias_vistas.add(clave)
        resultado.append(articulo)

    print(
        f"Noticias únicas localizadas: {len(resultado)}"
    )

    return resultado


def leer_articulos_anteriores():
    if not ARCHIVO_RSS.exists():
        return []

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
    except ET.ParseError:
        print("El RSS anterior no es válido; se reconstruirá.")
        return []

    articulos = []

    for item in raiz.findall("./channel/item"):
        categorias = [
            limpiar_texto(elemento.text)
            for elemento in item.findall("category")
            if limpiar_texto(elemento.text)
        ]

        enclosure = item.find("enclosure")
        imagen = ""

        if enclosure is not None:
            imagen = limpiar_texto(enclosure.get("url"))

        articulos.append(
            {
                "title": limpiar_texto(item.findtext("title")),
                "link": limpiar_texto(item.findtext("link")),
                "guid": limpiar_texto(item.findtext("guid")),
                "pubDate": limpiar_texto(
                    item.findtext("pubDate")
                ),
                "description": limpiar_texto(
                    item.findtext("description")
                ),
                "author": limpiar_texto(
                    item.findtext("author")
                ),
                "categories": categorias,
                "image": imagen,
            }
        )

    print(
        f"Noticias recuperadas del RSS anterior: "
        f"{len(articulos)}"
    )

    return articulos


def clave_articulo(articulo):
    enlace = limpiar_texto(articulo.get("link"))

    if enlace:
        return enlace.split("?")[0].rstrip("/").lower()

    return limpiar_texto(
        articulo.get("guid")
        or articulo.get("title")
    ).lower()


def fecha_ordenacion(articulo):
    try:
        fecha = parsedate_to_datetime(
            articulo["pubDate"]
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.timestamp()

    except (
        TypeError,
        ValueError,
        OverflowError,
        KeyError,
    ):
        return 0


def combinar_articulos(nuevos, anteriores):
    nuevos.sort(
        key=fecha_ordenacion,
        reverse=True,
    )

    resultado = []
    vistos = set()

    for articulo in nuevos + anteriores:
        clave = clave_articulo(articulo)

        if not clave or clave in vistos:
            continue

        vistos.add(clave)
        resultado.append(articulo)

        if len(resultado) >= MAX_ARTICULOS:
            break

    return resultado


def añadir_texto(padre, etiqueta, valor):
    elemento = ET.SubElement(padre, etiqueta)
    elemento.text = limpiar_texto(valor)
    return elemento


def crear_rss(articulos):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    canal = ET.SubElement(rss, "channel")

    añadir_texto(
        canal,
        "title",
        "Laboratorio Reig Jofre — Noticias",
    )
    añadir_texto(
        canal,
        "link",
        URL_NOTICIAS,
    )
    añadir_texto(
        canal,
        "description",
        (
            "Noticias, comunicados y documentos publicados "
            "por Laboratorio Reig Jofre."
        ),
    )
    añadir_texto(canal, "language", "es")
    añadir_texto(
        canal,
        "lastBuildDate",
        format_datetime(datetime.now(timezone.utc)),
    )
    añadir_texto(
        canal,
        "generator",
        "GitHub Actions RSS Generator",
    )

    atom = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    atom.set(
        "href",
        (
            "https://raw.githubusercontent.com/"
            "plis2100/reig-jofre-noticias-rss/main/rss.xml"
        ),
    )
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    for articulo in articulos:
        item = ET.SubElement(canal, "item")

        añadir_texto(item, "title", articulo["title"])
        añadir_texto(item, "link", articulo["link"])

        guid = añadir_texto(
            item,
            "guid",
            articulo["guid"],
        )
        guid.set("isPermaLink", "true")

        añadir_texto(
            item,
            "pubDate",
            articulo["pubDate"],
        )
        añadir_texto(
            item,
            "description",
            articulo["description"],
        )
        añadir_texto(
            item,
            "author",
            articulo["author"],
        )

        for categoria in articulo["categories"]:
            añadir_texto(
                item,
                "category",
                categoria,
            )

        if articulo["image"]:
            enclosure = ET.SubElement(
                item,
                "enclosure",
            )
            enclosure.set("url", articulo["image"])
            enclosure.set("type", "image/jpeg")

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")

    temporal = ARCHIVO_RSS.with_suffix(".xml.tmp")

    arbol.write(
        temporal,
        encoding="utf-8",
        xml_declaration=True,
    )

    temporal.replace(ARCHIVO_RSS)


def main():
    if not dentro_del_horario():
        return

    nuevos = extraer_noticias()
    anteriores = leer_articulos_anteriores()

    if not nuevos and not anteriores:
        raise RuntimeError(
            "Reig Jofre no devolvió ninguna noticia y "
            "tampoco existe un RSS anterior."
        )

    if not nuevos and anteriores:
        print(
            "AVISO: no se encontraron noticias nuevas. "
            "Se conservará el RSS anterior."
        )

    articulos = combinar_articulos(
        nuevos,
        anteriores,
    )

    if not articulos:
        raise RuntimeError(
            "No hay artículos para escribir en el RSS."
        )

    crear_rss(articulos)

    print(
        f"RSS creado correctamente con "
        f"{len(articulos)} noticias."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        raise
