"""
🚗 Scraper RosarioGarage - Versión Producción
Con normalización de marcas/modelos/versiones y extracción de descripción.

Características:
- Scraping distribuido con múltiples workers
- Extracción de descripción, visitas y días de expiración
- Normalización inteligente con catálogo y fuzzy matching
- Output en minúsculas
"""

import httpx
import asyncio
import os
import re
import sys
import sqlite3
import glob
from datetime import datetime, date
from bs4 import BeautifulSoup
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
import logging

# Async SQLite
try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

# Importar normalizador
try:
    from normalizer import (
        init_normalizer, 
        normalize_vehicle, 
        get_normalization_stats,
        NORM_STATS
    )
    HAS_NORMALIZER = True
except ImportError:
    HAS_NORMALIZER = False


# --- CONFIGURACIÓN ---
BASE_URL = "https://www.rosariogarage.com"
LISTING_URL = "https://www.rosariogarage.com/index.php?action=carro/showRubro&rbrId=107"
DEBUG_MODE = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'

# Paginación
ITEMS_PER_PAGE = 95
MAX_PAGES = 100

# Archivos de base de datos
MASTER_DB = 'rosariogarage.db'
WORKER_DB_PREFIX = 'rosariogarage_worker_'

# ═══════════════════════════════════════════════════════════════
# 🔀 CONFIGURACIÓN DE SHARDING (DISTRIBUCIÓN)
# ═══════════════════════════════════════════════════════════════
WORKER_ID = int(os.environ.get('WORKER_ID', 0))
TOTAL_WORKERS = int(os.environ.get('TOTAL_WORKERS', 1))
CONSOLIDATE_MODE = os.environ.get('CONSOLIDATE_MODE', 'false').lower() == 'true'
WORKER_DB = f'{WORKER_DB_PREFIX}{WORKER_ID}.db'

# ═══════════════════════════════════════════════════════════════
# 🚀 CONFIGURACIÓN DE RENDIMIENTO
# ═══════════════════════════════════════════════════════════════
@dataclass
class ScraperConfig:
    max_concurrent_listings: int = 8
    max_concurrent_details: int = 30
    max_connections: int = 50
    max_keepalive: int = 20
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_retries: int = 3
    retry_delay: float = 0.5
    delay_between_batches: float = 0.05
    batch_size: int = 25


CONFIG = ScraperConfig()


# ═══════════════════════════════════════════════════════════════
# 📊 ESTADÍSTICAS EN TIEMPO REAL
# ═══════════════════════════════════════════════════════════════
@dataclass
class Stats:
    requests_made: int = 0
    requests_success: int = 0
    requests_failed: int = 0
    requests_429: int = 0
    retries: int = 0
    bytes_downloaded: int = 0
    vehicles_new: int = 0
    vehicles_updated: int = 0
    vehicles_price_changed: int = 0
    start_time: datetime = field(default_factory=datetime.now)

    def rps(self) -> float:
        elapsed = (datetime.now() - self.start_time).total_seconds()
        return self.requests_made / elapsed if elapsed > 0 else 0

    def success_rate(self) -> float:
        total = self.requests_success + self.requests_failed
        return (self.requests_success / total * 100) if total > 0 else 0


STATS = Stats()


# ═══════════════════════════════════════════════════════════════
# 🔧 LOGGING Y MANEJO DE ERRORES
# ═══════════════════════════════════════════════════════════════
def setup_logging():
    logging.basicConfig(
        level=logging.INFO if not DEBUG_MODE else logging.DEBUG,
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )
    return logging.getLogger(__name__)


logger = setup_logging()


def fatal_error(message: str, exception: Exception = None):
    """Error fatal: loguea y termina la ejecución."""
    logger.critical("=" * 60)
    logger.critical(f"💀 ERROR FATAL: {message}")
    if exception:
        logger.critical(f"   Excepción: {type(exception).__name__}: {exception}")
        if DEBUG_MODE:
            import traceback
            logger.critical(traceback.format_exc())
    logger.critical("=" * 60)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# 🔧 FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════
HTML_ENTITIES = str.maketrans({'\xa0': ' '})
ENTITY_PATTERN = re.compile(r'&(\w+);')
ENTITY_MAP = {
    'nbsp': ' ', 'oacute': 'ó', 'aacute': 'á', 'eacute': 'é',
    'iacute': 'í', 'uacute': 'ú', 'ntilde': 'ñ', 'Ntilde': 'Ñ',
    'amp': '&', 'quot': '"', 'lt': '<', 'gt': '>',
}


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.translate(HTML_ENTITIES)
    text = ENTITY_PATTERN.sub(lambda m: ENTITY_MAP.get(m.group(1), m.group(0)), text)
    return ' '.join(text.split())


def clean_description(text: str) -> str:
    """Limpia la descripción manteniendo saltos de línea."""
    if not text:
        return ""
    # Reemplazar <br>, <br/>, <br /> por saltos de línea
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Eliminar otras etiquetas HTML
    text = re.sub(r'<[^>]+>', '', text)
    # Limpiar entidades HTML
    text = text.translate(HTML_ENTITIES)
    text = ENTITY_PATTERN.sub(lambda m: ENTITY_MAP.get(m.group(1), m.group(0)), text)
    # Limpiar espacios pero mantener saltos de línea
    lines = text.split('\n')
    lines = [' '.join(line.split()) for line in lines]
    return '\n'.join(line for line in lines if line.strip())


def decode_response(resp: httpx.Response) -> str:
    try:
        return resp.content.decode('iso-8859-1', errors='ignore')
    except:
        return resp.text


def calculate_page_range() -> Tuple[int, int]:
    """Calcula el rango de páginas para este worker."""
    pages_per_worker = MAX_PAGES // TOTAL_WORKERS
    remainder = MAX_PAGES % TOTAL_WORKERS

    if WORKER_ID < remainder:
        start_page = WORKER_ID * (pages_per_worker + 1)
        end_page = start_page + pages_per_worker
    else:
        start_page = WORKER_ID * pages_per_worker + remainder
        end_page = start_page + pages_per_worker - 1

    return start_page, end_page


def find_worker_databases() -> List[Tuple[int, str]]:
    """Busca todas las bases de datos de workers disponibles."""
    found = []
    for worker_id in range(TOTAL_WORKERS):
        db_name = f'{WORKER_DB_PREFIX}{worker_id}.db'
        if os.path.exists(db_name):
            found.append((worker_id, db_name))
            continue
        # Buscar en subdirectorios
        pattern = f'**/{db_name}'
        matches = glob.glob(pattern, recursive=True)
        if matches:
            found.append((worker_id, matches[0]))
    return found


# ═══════════════════════════════════════════════════════════════
# 💾 BASE DE DATOS SQLITE (SCHEMA ACTUALIZADO)
# ═══════════════════════════════════════════════════════════════
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    titulo TEXT,
    descripcion TEXT,
    marca TEXT,
    modelo TEXT,
    version TEXT,
    marca_original TEXT,
    modelo_original TEXT,
    version_original TEXT,
    año INTEGER,
    kilometros INTEGER,
    transmision TEXT,
    combustible TEXT,
    vendedor TEXT,
    agencia TEXT,
    imagen TEXT,
    precio_ars REAL,
    precio_usd REAL,
    moneda_original TEXT,
    visitas INTEGER,
    expira_dias INTEGER,
    normalizacion TEXT,
    primera_vista DATE NOT NULL,
    ultima_vista DATE NOT NULL,
    activo INTEGER DEFAULT 1,
    dias_publicado INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_vehicles_activo ON vehicles(activo);
CREATE INDEX IF NOT EXISTS idx_vehicles_marca ON vehicles(marca);
CREATE INDEX IF NOT EXISTS idx_vehicles_modelo ON vehicles(modelo);
CREATE INDEX IF NOT EXISTS idx_vehicles_ultima_vista ON vehicles(ultima_vista);
CREATE INDEX IF NOT EXISTS idx_vehicles_precio_usd ON vehicles(precio_usd);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL,
    precio_ars REAL,
    precio_usd REAL,
    fecha DATE NOT NULL,
    variacion_pct REAL,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_vehicle ON price_history(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_history_fecha ON price_history(fecha);

CREATE TABLE IF NOT EXISTS scrape_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


async def init_worker_db(db_path: str):
    """Inicializa la base de datos del worker."""
    if not HAS_AIOSQLITE:
        fatal_error("aiosqlite no está instalado")
    
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        logger.info(f"✅ Base de datos inicializada: {db_path}")
    except Exception as e:
        fatal_error(f"Error inicializando base de datos {db_path}", e)


def init_master_db(db_path: str):
    """Inicializa la base de datos maestra (síncrono)."""
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()
        logger.info(f"✅ Base de datos maestra inicializada: {db_path}")
    except Exception as e:
        fatal_error(f"Error inicializando base de datos maestra {db_path}", e)


# ═══════════════════════════════════════════════════════════════
# 🌐 HTTP CLIENT OPTIMIZADO
# ═══════════════════════════════════════════════════════════════
class FastHTTPClient:
    """Cliente HTTP optimizado."""

    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.semaphore_listings: Optional[asyncio.Semaphore] = None
        self.semaphore_details: Optional[asyncio.Semaphore] = None

    async def __aenter__(self):
        try:
            limits = httpx.Limits(
                max_connections=CONFIG.max_connections,
                max_keepalive_connections=CONFIG.max_keepalive,
            )
            timeout = httpx.Timeout(
                connect=CONFIG.connect_timeout,
                read=CONFIG.read_timeout,
                write=10.0,
                pool=5.0
            )
            self.client = httpx.AsyncClient(
                limits=limits,
                timeout=timeout,
                follow_redirects=True,
                http2=True,
            )
            self.semaphore_listings = asyncio.Semaphore(CONFIG.max_concurrent_listings)
            self.semaphore_details = asyncio.Semaphore(CONFIG.max_concurrent_details)
            logger.info(f"🌐 HTTP Client inicializado (HTTP/2)")
            return self
        except Exception as e:
            fatal_error("Error inicializando HTTP client", e)

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def fetch(
        self,
        url: str,
        semaphore: asyncio.Semaphore,
        retries: int = CONFIG.max_retries
    ) -> Optional[httpx.Response]:
        """Fetch con reintentos."""
        async with semaphore:
            for attempt in range(retries):
                try:
                    STATS.requests_made += 1
                    resp = await self.client.get(url)

                    if resp.status_code == 200:
                        STATS.requests_success += 1
                        STATS.bytes_downloaded += len(resp.content)
                        return resp

                    if resp.status_code == 429:
                        STATS.requests_429 += 1
                        wait = (attempt + 1) * 2
                        logger.warning(f"⚠️ Rate limit, esperando {wait}s...")
                        await asyncio.sleep(wait)
                        STATS.retries += 1
                        continue

                    if resp.status_code >= 500:
                        STATS.retries += 1
                        await asyncio.sleep(CONFIG.retry_delay * (attempt + 1))
                        continue

                    STATS.requests_failed += 1
                    return None

                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    STATS.retries += 1
                    if attempt < retries - 1:
                        await asyncio.sleep(CONFIG.retry_delay * (attempt + 1))
                        continue

                except Exception as e:
                    fatal_error(f"Error inesperado en request a {url}", e)

            STATS.requests_failed += 1
            logger.error(f"❌ Agotados {retries} reintentos para {url}")

            if STATS.requests_failed > 50:
                fatal_error(f"Demasiados errores de red ({STATS.requests_failed} fallos)")

            return None

    async def fetch_listing(self, url: str) -> Optional[httpx.Response]:
        return await self.fetch(url, self.semaphore_listings)

    async def fetch_detail(self, url: str) -> Optional[httpx.Response]:
        return await self.fetch(url, self.semaphore_details)


# ═══════════════════════════════════════════════════════════════
# 💵 OBTENCIÓN DEL DÓLAR
# ═══════════════════════════════════════════════════════════════
async def get_dolar_from_api() -> float:
    """Obtiene cotización del dólar."""
    apis = [
        ("https://dolarapi.com/v1/dolares/bolsa", lambda r: r.json()['venta']),
        ("https://api.bluelytics.com.ar/v2/latest", lambda r: r.json()['blue']['value_sell']),
        ("https://dolarapi.com/v1/dolares/blue", lambda r: r.json()['venta']),
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        for url, parser in apis:
            try:
                logger.info(f"   Consultando {url.split('/')[2]}...")
                resp = await client.get(url)
                if resp.status_code == 200:
                    value = float(parser(resp))
                    if 100 < value < 5000:
                        logger.info(f"   ✅ Cotización obtenida: ${value:.2f}")
                        return value
            except Exception as e:
                logger.warning(f"   ⚠️ Error con {url.split('/')[2]}: {e}")
                continue

    fatal_error("No se pudo obtener cotización del dólar de ninguna API")


# ═══════════════════════════════════════════════════════════════
# 📋 PARSING
# ═══════════════════════════════════════════════════════════════
RE_YEAR = re.compile(r'^(19|20)\d{2}$')
RE_DIGITS = re.compile(r'[^\d]')
RE_PRICE = re.compile(r'(\d+\.?\d*)')
FUEL_TYPES = {'nafta', 'diesel', 'gnc', 'híbrido', 'eléctrico', 'gas'}

DETAIL_PATTERNS = {
    'marca': re.compile(r'<span>Marca:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
    'version': re.compile(r'<span>Versi[óo]n:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
    'transmision': re.compile(r'<span>Transmisi[óo]n:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
    'año': re.compile(r'<span>A[ñn]o:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
    'kilometraje': re.compile(r'<span>Kilometraje:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
    'combustible': re.compile(r'<span>Combustible:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
    'vendedor': re.compile(r'<span>Vendedor:</span>\s*(?:&nbsp;)?\s*([^<]+)', re.I),
}

# Patrones para visitas y expiración
RE_VISITAS = re.compile(r'<span>Visitas\s+hasta\s+el\s+momento:</span>\s*(\d+)', re.I)
RE_EXPIRA = re.compile(r'<span>Expira\s+en:</span>\s*(\d+)\s*d', re.I)


def extract_description(soup: BeautifulSoup) -> Optional[str]:
    """Extrae la descripción ampliada del vehículo."""
    try:
        # Buscar el subtitle de descripción
        subtitle = soup.find('div', class_='subtitle', 
                            string=re.compile(r'Descripci.*n\s+ampliada', re.I))
        
        if not subtitle:
            for div in soup.find_all('div', class_='subtitle'):
                text = div.get_text()
                if 'descripci' in text.lower() and 'ampliada' in text.lower():
                    subtitle = div
                    break
        
        if subtitle:
            next_box = subtitle.find_next_sibling('div', class_='box-text')
            if next_box:
                html_content = str(next_box)
                return clean_description(html_content)
        
        return None
    except Exception:
        return None


def extract_visitas_expira(html: str) -> Dict:
    """Extrae las visitas y días de expiración."""
    result = {'visitas': None, 'expira_dias': None}
    
    try:
        match_visitas = RE_VISITAS.search(html)
        if match_visitas:
            result['visitas'] = int(match_visitas.group(1))
        
        match_expira = RE_EXPIRA.search(html)
        if match_expira:
            result['expira_dias'] = int(match_expira.group(1))
    except Exception:
        pass
    
    return result


def parse_listing_item(item_div) -> Optional[Dict]:
    """Extrae datos de un item del listado."""
    try:
        item_id = item_div.get('data-rel', '')
        if not item_id:
            return None

        data = {'id': item_id}

        link = item_div.select_one('a[href*="itmId"]')
        data['url'] = link.get('href', '') if link else \
            f"{BASE_URL}/index.php?action=carro/showProduct&itmId={item_id}&rbrId=107"

        title = item_div.select_one('strong.list_type_anuncio, span.list_type_anuncio')
        data['titulo'] = clean_text(title.get_text()) if title else 'N/A'

        specs = item_div.select_one('.box_aviso_tit a, .box_aviso_premium_tit a')
        if specs:
            for span in specs.find_all('span', recursive=False):
                text = span.get_text(strip=True).replace(',', '').replace('\xa0', ' ')
                if not text:
                    continue

                if text in ('MT', 'AT'):
                    data['transmision'] = 'manual' if text == 'MT' else 'automatico'
                elif RE_YEAR.match(text):
                    data['año'] = text
                elif text.lower() in FUEL_TYPES:
                    data['combustible'] = text.lower()
                elif 'km' in text.lower():
                    km_str = RE_DIGITS.sub('', text)
                    data['kilometros'] = int(km_str) if km_str else 0

        agencia = item_div.select_one('div.agencia a')
        if agencia:
            data['agencia'] = clean_text(agencia.get_text())

        precio = item_div.select_one('div.precio a')
        if precio:
            data['precio_text'] = clean_text(precio.get_text())

        img = item_div.select_one('img[data-src]')
        if img:
            data['imagen'] = img.get('data-src', '')

        return data
    except Exception as e:
        if DEBUG_MODE:
            logger.debug(f"Error parseando item: {e}")
        return None


def extract_marca_from_titulo(titulo: str) -> str:
    if not titulo or titulo == 'N/A':
        return ''
    parts = titulo.split()
    return parts[0].lower() if parts else ''


def extract_modelo_from_titulo(titulo: str, marca: str) -> str:
    if not titulo or titulo == 'N/A':
        return ''
    if marca:
        # Remover marca del título para obtener modelo
        modelo = titulo.lower().replace(marca.lower(), '', 1).strip()
        return modelo or ''
    parts = titulo.split(maxsplit=1)
    return parts[1].lower() if len(parts) > 1 else ''


def parse_km(km_str) -> int:
    if not km_str:
        return 0
    clean = RE_DIGITS.sub('', str(km_str))
    try:
        return int(clean) if clean else 0
    except:
        return 0


def parse_precio(precio_text: str, dolar_mep: float) -> Dict:
    result = {'precio_ars': None, 'precio_usd': None, 'moneda_original': None}
    if not precio_text or 'consultar' in precio_text.lower():
        return result

    try:
        precio_clean = precio_text.replace('$', '').replace('.', '').replace(',', '.')
        match = RE_PRICE.search(precio_clean)
        if match:
            val = float(match.group(1))
            is_usd = any(x in precio_text.upper() for x in ['U$S', 'USD', 'U$D', 'DÓLAR', 'DOLARES'])

            if is_usd:
                result['moneda_original'] = 'usd'
                result['precio_usd'] = val
                if dolar_mep:
                    result['precio_ars'] = round(val * dolar_mep, 2)
            else:
                result['moneda_original'] = 'ars'
                result['precio_ars'] = val
                if dolar_mep:
                    result['precio_usd'] = round(val / dolar_mep, 2)
    except Exception as e:
        if DEBUG_MODE:
            logger.debug(f"Error parseando precio '{precio_text}': {e}")

    return result


# ═══════════════════════════════════════════════════════════════
# 📄 SCRAPING DEL LISTADO
# ═══════════════════════════════════════════════════════════════
async def fetch_listing_page(http: FastHTTPClient, offset: int) -> List[Dict]:
    """Obtiene una página del listado."""
    url = f"{LISTING_URL}&o={offset}"
    resp = await http.fetch_listing(url)
    if not resp:
        return []

    try:
        html = decode_response(resp)
        try:
            soup = BeautifulSoup(html, 'lxml')
        except:
            soup = BeautifulSoup(html, 'html.parser')

        vehicles = []
        for div in soup.select('div[data-rel]'):
            v = parse_listing_item(div)
            if v and v.get('id'):
                vehicles.append(v)

        return vehicles
    except Exception as e:
        logger.error(f"Error procesando página offset={offset}: {e}")
        return []


async def fetch_listings_for_worker(http: FastHTTPClient) -> List[Dict]:
    """Obtiene los listados correspondientes a este worker."""
    start_page, end_page = calculate_page_range()
    logger.info(f"🔀 Worker {WORKER_ID}/{TOTAL_WORKERS}: Páginas {start_page} a {end_page}")

    all_vehicles = []
    seen_ids = set()
    offsets = [page * ITEMS_PER_PAGE for page in range(start_page, end_page + 1)]
    batch_size = CONFIG.max_concurrent_listings
    empty_count = 0

    for i in range(0, len(offsets), batch_size):
        batch_offsets = offsets[i:i + batch_size]
        tasks = [fetch_listing_page(http, offset) for offset in batch_offsets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        batch_new = 0
        all_empty = True

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Excepción en batch de listado: {result}")
                continue

            vehicles = result
            if vehicles:
                all_empty = False
                for v in vehicles:
                    if v['id'] not in seen_ids:
                        seen_ids.add(v['id'])
                        all_vehicles.append(v)
                        batch_new += 1

        if all_empty:
            empty_count += 1
            if empty_count >= 2:
                logger.info(f"  ⚠️ 2 batches vacíos consecutivos, terminando...")
                break
        else:
            empty_count = 0

        logger.info(
            f"  Páginas procesadas: +{batch_new} nuevos | "
            f"Total: {len(all_vehicles)} | {STATS.rps():.1f} req/s"
        )

        if CONFIG.delay_between_batches > 0:
            await asyncio.sleep(CONFIG.delay_between_batches)

    if not all_vehicles:
        fatal_error(
            f"Worker {WORKER_ID}: No se encontraron vehículos en páginas {start_page}-{end_page}"
        )

    return all_vehicles


# ═══════════════════════════════════════════════════════════════
# 🚗 SCRAPING DE DETALLES (CON NORMALIZACIÓN)
# ═══════════════════════════════════════════════════════════════
async def fetch_vehicle_details(
    http: FastHTTPClient,
    vehicle_basic: Dict,
    dolar_mep: float
) -> Optional[Dict]:
    """Obtiene detalles de un vehículo con normalización."""
    url = vehicle_basic.get('url', '')
    if not url:
        return None

    titulo = vehicle_basic.get('titulo', '')
    
    # Extraer marca/modelo básicos del título
    marca_raw = extract_marca_from_titulo(titulo)
    modelo_raw = extract_modelo_from_titulo(titulo, marca_raw)
    
    result = {
        'id': vehicle_basic.get('id'),
        'url': url,
        'titulo': titulo.lower() if titulo else None,
        'descripcion': None,
        'marca': None,
        'modelo': None,
        'version': None,
        'marca_original': marca_raw,
        'modelo_original': modelo_raw,
        'version_original': None,
        'año': vehicle_basic.get('año'),
        'kilometros': vehicle_basic.get('kilometros', 0),
        'transmision': vehicle_basic.get('transmision'),
        'combustible': vehicle_basic.get('combustible'),
        'vendedor': None,
        'agencia': vehicle_basic.get('agencia', '').lower() if vehicle_basic.get('agencia') else None,
        'imagen': vehicle_basic.get('imagen', ''),
        'visitas': None,
        'expira_dias': None,
        'normalizacion': 'sin_procesar',
    }

    if result['año']:
        try:
            result['año'] = int(result['año'])
        except:
            result['año'] = None

    resp = await http.fetch_detail(url)
    if resp:
        try:
            html = decode_response(resp)

            try:
                soup = BeautifulSoup(html, 'lxml')
            except:
                soup = BeautifulSoup(html, 'html.parser')

            # Extraer descripción
            descripcion = extract_description(soup)
            if descripcion:
                result['descripcion'] = descripcion.lower()

            # Extraer visitas y expiración
            visitas_expira = extract_visitas_expira(html)
            result['visitas'] = visitas_expira['visitas']
            result['expira_dias'] = visitas_expira['expira_dias']

            # Extraer datos técnicos
            for field, pattern in DETAIL_PATTERNS.items():
                match = pattern.search(html)
                if match:
                    value = clean_text(match.group(1))
                    if value:
                        if field == 'kilometraje':
                            result['kilometros'] = parse_km(value)
                        elif field == 'año':
                            try:
                                result['año'] = int(value)
                            except:
                                pass
                        elif field == 'marca':
                            result['marca_original'] = value.lower()
                        elif field == 'version':
                            result['version_original'] = value.lower()
                        elif field == 'transmision':
                            result['transmision'] = value.lower()
                        elif field == 'combustible':
                            result['combustible'] = value.lower()
                        elif field == 'vendedor':
                            result['vendedor'] = value.lower()

        except Exception as e:
            if DEBUG_MODE:
                logger.debug(f"Error extrayendo detalles de {url}: {e}")

    # ═══════════════════════════════════════════════════════════
    # 🎯 NORMALIZACIÓN DE MARCA/MODELO/VERSIÓN
    # ═══════════════════════════════════════════════════════════
    if HAS_NORMALIZER:
        try:
            norm_result = normalize_vehicle(
                titulo=titulo,
                descripcion=result.get('descripcion', '') or '',
                marca_raw=result.get('marca_original', '') or '',
                modelo_raw=result.get('modelo_original', '') or '',
                version_raw=result.get('version_original', '') or ''
            )
            
            result['marca'] = norm_result.get('marca')
            result['modelo'] = norm_result.get('modelo')
            result['version'] = norm_result.get('version')
            result['normalizacion'] = norm_result.get('normalizacion', 'sin_match')
            
        except Exception as e:
            if DEBUG_MODE:
                logger.debug(f"Error en normalización: {e}")
            # Fallback a datos originales
            result['marca'] = result.get('marca_original')
            result['modelo'] = result.get('modelo_original')
            result['version'] = result.get('version_original')
            result['normalizacion'] = 'error'
    else:
        # Sin normalizador, usar datos originales
        result['marca'] = result.get('marca_original')
        result['modelo'] = result.get('modelo_original')
        result['version'] = result.get('version_original')
        result['normalizacion'] = 'sin_normalizar'

    # Parsear precio
    precio_text = vehicle_basic.get('precio_text', '')
    result.update(parse_precio(precio_text, dolar_mep))

    return result


async def fetch_all_details(
    http: FastHTTPClient,
    vehicles: List[Dict],
    dolar_mep: float
) -> List[Dict]:
    """Obtiene todos los detalles en paralelo."""
    total = len(vehicles)
    logger.info(f"🚗 Worker {WORKER_ID}: Obteniendo detalles de {total} vehículos...")

    results = []
    batch_size = CONFIG.batch_size

    for i in range(0, total, batch_size):
        batch = vehicles[i:i + batch_size]
        tasks = [fetch_vehicle_details(http, v, dolar_mep) for v in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in batch_results:
            if isinstance(result, Exception):
                logger.error(f"Excepción obteniendo detalle: {result}")
                continue
            if result:
                results.append(result)

        processed = min(i + batch_size, total)
        pct = (processed / total) * 100
        logger.info(
            f"  Procesados: {processed}/{total} ({pct:.0f}%) | "
            f"Válidos: {len(results)} | {STATS.rps():.1f} req/s"
        )

        if CONFIG.delay_between_batches > 0:
            await asyncio.sleep(CONFIG.delay_between_batches)

    if not results:
        fatal_error(f"Worker {WORKER_ID}: No se pudo procesar ningún vehículo")

    success_pct = (len(results) / total) * 100
    if success_pct < 90:
        logger.warning(f"⚠️ Solo {len(results)}/{total} vehículos ({success_pct:.1f}%)")

    return results


# ═══════════════════════════════════════════════════════════════
# 💾 GUARDAR DATOS DEL WORKER
# ═══════════════════════════════════════════════════════════════
async def save_worker_results(results: List[Dict], dolar_mep: float):
    """Guarda los resultados del worker en su base de datos."""
    today = date.today().isoformat()

    try:
        await init_worker_db(WORKER_DB)

        async with aiosqlite.connect(WORKER_DB) as db:
            # Metadata
            await db.execute(
                "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                ('scrape_date', today)
            )
            await db.execute(
                "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                ('dolar_mep', str(dolar_mep))
            )
            await db.execute(
                "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                ('rate_limits_429', str(STATS.requests_429))
            )
            await db.execute(
                "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                ('worker_id', str(WORKER_ID))
            )
            await db.execute(
                "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                ('vehicles_count', str(len(results)))
            )

            # Estadísticas de normalización
            if HAS_NORMALIZER:
                norm_stats = get_normalization_stats()
                await db.execute(
                    "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                    ('norm_full_match', str(norm_stats.full_match))
                )
                await db.execute(
                    "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                    ('norm_partial', str(norm_stats.marca_modelo + norm_stats.marca_only))
                )
                await db.execute(
                    "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
                    ('norm_no_match', str(norm_stats.no_match))
                )

            # Insertar vehículos
            for vehicle in results:
                await db.execute("""
                    INSERT OR REPLACE INTO vehicles (
                        id, url, titulo, descripcion,
                        marca, modelo, version,
                        marca_original, modelo_original, version_original,
                        año, kilometros, transmision, combustible,
                        vendedor, agencia, imagen,
                        precio_ars, precio_usd, moneda_original,
                        visitas, expira_dias, normalizacion,
                        primera_vista, ultima_vista, activo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    vehicle['id'],
                    vehicle['url'],
                    vehicle.get('titulo'),
                    vehicle.get('descripcion'),
                    vehicle.get('marca'),
                    vehicle.get('modelo'),
                    vehicle.get('version'),
                    vehicle.get('marca_original'),
                    vehicle.get('modelo_original'),
                    vehicle.get('version_original'),
                    vehicle.get('año'),
                    vehicle.get('kilometros'),
                    vehicle.get('transmision'),
                    vehicle.get('combustible'),
                    vehicle.get('vendedor'),
                    vehicle.get('agencia'),
                    vehicle.get('imagen'),
                    vehicle.get('precio_ars'),
                    vehicle.get('precio_usd'),
                    vehicle.get('moneda_original'),
                    vehicle.get('visitas'),
                    vehicle.get('expira_dias'),
                    vehicle.get('normalizacion'),
                    today,
                    today,
                ))

            await db.commit()

        logger.info(f"✅ Worker {WORKER_ID}: Guardados {len(results)} vehículos en {WORKER_DB}")

    except Exception as e:
        fatal_error(f"Error guardando resultados del worker {WORKER_ID}", e)


# ═══════════════════════════════════════════════════════════════
# 🔄 CONSOLIDACIÓN DE WORKERS
# ═══════════════════════════════════════════════════════════════
def read_worker_data(db_path: str) -> Tuple[List[tuple], Dict[str, str]]:
    """Lee todos los datos de un worker."""
    vehicles = []
    metadata = {}

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != 'ok':
            logger.error(f"   ❌ BD corrupta: {integrity}")
            conn.close()
            return [], {}

        for row in conn.execute("SELECT key, value FROM scrape_metadata"):
            metadata[row['key']] = row['value']

        cursor = conn.execute("""
            SELECT id, url, titulo, descripcion,
                   marca, modelo, version,
                   marca_original, modelo_original, version_original,
                   año, kilometros, transmision, combustible,
                   vendedor, agencia, imagen,
                   precio_ars, precio_usd, moneda_original,
                   visitas, expira_dias, normalizacion
            FROM vehicles
        """)
        
        for row in cursor:
            vehicles.append(tuple(row))

        conn.close()

    except Exception as e:
        logger.error(f"   ❌ Error leyendo {db_path}: {e}")
        return [], {}

    return vehicles, metadata


def consolidate_worker_results():
    """Consolida los resultados de todos los workers."""
    logger.info("=" * 60)
    logger.info("🔄 MODO CONSOLIDACIÓN")
    logger.info("=" * 60)

    try:
        today = date.today().isoformat()

        available_workers = find_worker_databases()
        logger.info(f"\n📂 Buscando bases de datos de workers...")
        
        for worker_id, db_path in available_workers:
            size = os.path.getsize(db_path)
            logger.info(f"   ✅ Worker {worker_id}: {db_path} ({size:,} bytes)")

        if not available_workers:
            logger.error("❌ No se encontraron bases de datos de workers")
            fatal_error("No hay bases de datos de workers para consolidar")

        logger.info(f"\n📊 Workers encontrados: {len(available_workers)}/{TOTAL_WORKERS}")

        # Leer todos los datos
        logger.info("\n📖 Leyendo datos de todos los workers...")
        all_worker_data = []
        dolar_mep = None
        total_rate_limits = 0
        norm_stats_total = {'full': 0, 'partial': 0, 'none': 0}

        for worker_id, db_path in available_workers:
            logger.info(f"   📥 Leyendo Worker {worker_id}...")
            vehicles, metadata = read_worker_data(db_path)

            if vehicles:
                all_worker_data.append((worker_id, vehicles, metadata))
                logger.info(f"      ✅ {len(vehicles)} vehículos leídos")

                if not dolar_mep and 'dolar_mep' in metadata:
                    dolar_mep = float(metadata['dolar_mep'])

                if 'rate_limits_429' in metadata:
                    total_rate_limits += int(metadata['rate_limits_429'])

                # Sumar stats de normalización
                norm_stats_total['full'] += int(metadata.get('norm_full_match', 0))
                norm_stats_total['partial'] += int(metadata.get('norm_partial', 0))
                norm_stats_total['none'] += int(metadata.get('norm_no_match', 0))
            else:
                logger.warning(f"      ⚠️ Sin datos o error")

        if not all_worker_data:
            fatal_error("No se pudo leer datos de ningún worker")

        if not dolar_mep:
            fatal_error("No se encontró cotización del dólar en ningún worker")

        total_vehicles_read = sum(len(data[1]) for data in all_worker_data)
        logger.info(f"\n📊 Total vehículos leídos de workers: {total_vehicles_read}")
        logger.info(f"💵 Dólar MEP: ${dolar_mep:.2f}")
        logger.info(f"📊 Normalización - Completo: {norm_stats_total['full']}, "
                   f"Parcial: {norm_stats_total['partial']}, Sin match: {norm_stats_total['none']}")

        # Inicializar BD maestra
        master_exists = os.path.exists(MASTER_DB)
        if not master_exists:
            logger.info(f"\n📂 Creando nueva BD maestra...")
        init_master_db(MASTER_DB)

        conn = sqlite3.connect(MASTER_DB)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()

        # Metadata
        cursor.execute(
            "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
            ('last_scrape_date', today)
        )
        cursor.execute(
            "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
            ('last_dolar_mep', str(dolar_mep))
        )
        cursor.execute(
            "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
            ('rate_limits_429', str(total_rate_limits))
        )
        cursor.execute(
            "INSERT OR REPLACE INTO scrape_metadata (key, value) VALUES (?, ?)",
            ('workers_consolidated', str(len(all_worker_data)))
        )

        # Insertar vehículos
        logger.info("\n💾 Insertando datos en BD maestra...")
        seen_ids_today = set()

        for worker_id, vehicles, metadata in all_worker_data:
            logger.info(f"   📝 Procesando {len(vehicles)} vehículos del Worker {worker_id}...")

            for v in vehicles:
                # v = (id, url, titulo, descripcion, marca, modelo, version, 
                #      marca_original, modelo_original, version_original,
                #      año, km, trans, comb, vendedor, agencia, imagen,
                #      precio_ars, precio_usd, moneda_original, visitas, expira_dias, normalizacion)
                vehicle_id = v[0]
                seen_ids_today.add(vehicle_id)
                new_precio_usd = v[18]  # precio_usd

                existing = cursor.execute(
                    "SELECT precio_usd, primera_vista FROM vehicles WHERE id = ?",
                    (vehicle_id,)
                ).fetchone()

                if existing:
                    old_precio_usd = existing[0]
                    cursor.execute("""
                        UPDATE vehicles SET
                            url = ?, titulo = ?, descripcion = ?,
                            marca = ?, modelo = ?, version = ?,
                            marca_original = ?, modelo_original = ?, version_original = ?,
                            año = ?, kilometros = ?, transmision = ?, combustible = ?,
                            vendedor = ?, agencia = ?, imagen = ?,
                            precio_ars = ?, precio_usd = ?, moneda_original = ?,
                            visitas = ?, expira_dias = ?, normalizacion = ?,
                            ultima_vista = ?, activo = 1,
                            dias_publicado = julianday(?) - julianday(primera_vista)
                        WHERE id = ?
                    """, (
                        v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9],
                        v[10], v[11], v[12], v[13], v[14], v[15], v[16],
                        v[17], v[18], v[19], v[20], v[21], v[22],
                        today, today, vehicle_id
                    ))
                    STATS.vehicles_updated += 1

                    if old_precio_usd and new_precio_usd and abs(old_precio_usd - new_precio_usd) > 0.01:
                        variacion = ((new_precio_usd - old_precio_usd) / old_precio_usd) * 100
                        cursor.execute("""
                            INSERT INTO price_history 
                            (vehicle_id, precio_ars, precio_usd, fecha, variacion_pct)
                            VALUES (?, ?, ?, ?, ?)
                        """, (vehicle_id, v[17], new_precio_usd, today, round(variacion, 2)))
                        STATS.vehicles_price_changed += 1
                else:
                    cursor.execute("""
                        INSERT INTO vehicles (
                            id, url, titulo, descripcion,
                            marca, modelo, version,
                            marca_original, modelo_original, version_original,
                            año, kilometros, transmision, combustible,
                            vendedor, agencia, imagen,
                            precio_ars, precio_usd, moneda_original,
                            visitas, expira_dias, normalizacion,
                            primera_vista, ultima_vista, activo, dias_publicado
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                    """, (
                        v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9],
                        v[10], v[11], v[12], v[13], v[14], v[15], v[16],
                        v[17], v[18], v[19], v[20], v[21], v[22],
                        today, today
                    ))

                    if new_precio_usd:
                        cursor.execute("""
                            INSERT INTO price_history 
                            (vehicle_id, precio_ars, precio_usd, fecha, variacion_pct)
                            VALUES (?, ?, ?, ?, NULL)
                        """, (vehicle_id, v[17], new_precio_usd, today))

                    STATS.vehicles_new += 1

        # Marcar inactivos
        newly_inactive = 0
        if seen_ids_today:
            placeholders = ','.join('?' * len(seen_ids_today))
            result = cursor.execute(f"""
                UPDATE vehicles SET activo = 0
                WHERE activo = 1 AND id NOT IN ({placeholders})
            """, tuple(seen_ids_today))
            newly_inactive = result.rowcount

        # Purgar antiguos
        cursor.execute("""
            DELETE FROM price_history WHERE vehicle_id IN (
                SELECT id FROM vehicles 
                WHERE activo = 0 AND ultima_vista < date('now', '-90 days')
            )
        """)
        cursor.execute("""
            DELETE FROM vehicles 
            WHERE activo = 0 AND ultima_vista < date('now', '-90 days')
        """)

        # Estadísticas
        total_active = cursor.execute(
            "SELECT COUNT(*) FROM vehicles WHERE activo = 1"
        ).fetchone()[0]
        total_inactive = cursor.execute(
            "SELECT COUNT(*) FROM vehicles WHERE activo = 0"
        ).fetchone()[0]
        total_history = cursor.execute(
            "SELECT COUNT(*) FROM price_history"
        ).fetchone()[0]

        conn.commit()
        conn.close()

        # Limpiar workers
        logger.info("\n🗑️ Limpiando archivos temporales...")
        for worker_id, db_path in available_workers:
            try:
                os.remove(db_path)
                logger.info(f"   Eliminado {db_path}")
            except Exception as e:
                logger.warning(f"   No se pudo eliminar {db_path}: {e}")

        db_size = os.path.getsize(MASTER_DB) / 1024 / 1024

        logger.info("\n" + "=" * 60)
        logger.info("✅ CONSOLIDACIÓN COMPLETADA")
        logger.info("=" * 60)
        logger.info(f"   👷 Workers procesados: {len(all_worker_data)}/{TOTAL_WORKERS}")
        logger.info(f"   📥 Vehículos leídos: {total_vehicles_read}")
        logger.info(f"   🆕 Nuevos: {STATS.vehicles_new}")
        logger.info(f"   🔄 Actualizados: {STATS.vehicles_updated}")
        logger.info(f"   💰 Cambios precio: {STATS.vehicles_price_changed}")
        logger.info(f"   🔴 Nuevos inactivos: {newly_inactive}")
        logger.info(f"   📊 Total activos: {total_active}")
        logger.info(f"   📴 Total inactivos: {total_inactive}")
        logger.info(f"   📈 Historial: {total_history}")
        logger.info(f"   💾 Tamaño BD: {db_size:.2f} MB")
        logger.info("=" * 60)

    except Exception as e:
        fatal_error("Error durante consolidación", e)


# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIÓN PRINCIPAL DEL WORKER
# ═══════════════════════════════════════════════════════════════
async def run_worker():
    """Ejecuta el worker de scraping."""
    global STATS
    STATS = Stats()
    start_time = datetime.now()
    start_page, end_page = calculate_page_range()

    logger.info("=" * 60)
    logger.info(f"🚀 WORKER {WORKER_ID}/{TOTAL_WORKERS} INICIANDO")
    logger.info(f"   Páginas: {start_page} - {end_page}")
    logger.info("=" * 60)

    # Inicializar normalizador
    if HAS_NORMALIZER:
        if init_normalizer():
            logger.info("✅ Normalizador inicializado")
        else:
            logger.warning("⚠️ Normalizador sin catálogo - usando datos crudos")
    else:
        logger.warning("⚠️ Módulo normalizer no disponible")

    try:
        async with FastHTTPClient() as http:
            # 1. Dólar
            logger.info("\n📊 Obteniendo cotización del dólar...")
            dolar_mep = await get_dolar_from_api()

            # 2. Listados
            logger.info(f"\n📋 Fase 1: Obteniendo listados...")
            vehicles = await fetch_listings_for_worker(http)
            logger.info(f"✅ Vehículos encontrados: {len(vehicles)}")

            # 3. Detalles
            logger.info("\n🚗 Fase 2: Obteniendo detalles...")
            results = await fetch_all_details(http, vehicles, dolar_mep)

            # 4. Guardar
            logger.info("\n💾 Guardando resultados...")
            await save_worker_results(results, dolar_mep)

        elapsed = (datetime.now() - start_time).total_seconds()

        # Estadísticas de normalización
        norm_info = ""
        if HAS_NORMALIZER:
            ns = get_normalization_stats()
            norm_info = f"\n   📊 Normalización: {ns.summary()}"

        logger.info("\n" + "=" * 60)
        logger.info(f"✅ WORKER {WORKER_ID} COMPLETADO")
        logger.info("=" * 60)
        logger.info(f"   ⏱️ Tiempo: {elapsed:.1f}s")
        logger.info(f"   📊 Requests: {STATS.requests_made}")
        logger.info(f"   ✅ Exitosos: {STATS.requests_success}")
        logger.info(f"   ❌ Fallidos: {STATS.requests_failed}")
        logger.info(f"   🚗 Vehículos: {len(results)}")
        if norm_info:
            logger.info(norm_info)
        logger.info("=" * 60)

    except Exception as e:
        fatal_error(f"Error en worker {WORKER_ID}", e)


# ═══════════════════════════════════════════════════════════════
# 🎯 PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════
def main():
    """Punto de entrada principal."""
    try:
        if CONSOLIDATE_MODE:
            consolidate_worker_results()
        else:
            asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.warning("⚠️ Interrumpido")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        fatal_error("Error no manejado", e)


if __name__ == "__main__":
    main()
