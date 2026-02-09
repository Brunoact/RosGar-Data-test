"""
🚗 Scraper RosarioGarage - Versión Producción v2.3
==================================================
Extrae datos de cada publicación y los normaliza contra catálogo.

Flujo:
1. Obtiene ID y URL del listado
2. Extrae datos crudos de cada publicación (marca_raw, version_raw, etc.)
3. Usa el normalizador para obtener marca/modelo/versión correctos del catálogo
4. Guarda valores normalizados (o NULL si no se pudo determinar)
"""

import httpx
import asyncio
import os
import re
import sys
import sqlite3
import glob
import hashlib
from datetime import datetime, date
from bs4 import BeautifulSoup
from typing import Optional, Dict, List, Tuple
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
        extract_urgency_signals
    )
    HAS_NORMALIZER = True
except ImportError:
    HAS_NORMALIZER = False

# --- CONFIGURACIÓN ---
BASE_URL = "https://www.rosariogarage.com"
LISTING_URL = "https://www.rosariogarage.com/index.php?action=carro/showRubro&rbrId=107"
DEBUG_MODE = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'

ITEMS_PER_PAGE = 95
MAX_PAGES = 100

MASTER_DB = 'rosariogarage.db'
WORKER_DB_PREFIX = 'rosariogarage_worker_'

WORKER_ID = int(os.environ.get('WORKER_ID', 0))
TOTAL_WORKERS = int(os.environ.get('TOTAL_WORKERS', 1))
CONSOLIDATE_MODE = os.environ.get('CONSOLIDATE_MODE', 'false').lower() == 'true'
WORKER_DB = f'{WORKER_DB_PREFIX}{WORKER_ID}.db'


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
    vehicles_republished: int = 0
    start_time: datetime = field(default_factory=datetime.now)

    def rps(self) -> float:
        elapsed = (datetime.now() - self.start_time).total_seconds()
        return self.requests_made / elapsed if elapsed > 0 else 0


STATS = Stats()


def setup_logging():
    logging.basicConfig(
        level=logging.INFO if not DEBUG_MODE else logging.DEBUG,
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S'
    )
    return logging.getLogger(__name__)


logger = setup_logging()


def fatal_error(message: str, exception: Exception = None):
    logger.critical("=" * 60)
    logger.critical(f"💀 ERROR FATAL: {message}")
    if exception:
        logger.critical(f"   Excepción: {type(exception).__name__}: {exception}")
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

RE_DIGITS = re.compile(r'[^\d]')
RE_PRICE = re.compile(r'(\d+\.?\d*)')
RE_WHATSAPP = re.compile(r'wa\.me/(\d+)\?', re.I)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.translate(HTML_ENTITIES)
    text = ENTITY_PATTERN.sub(lambda m: ENTITY_MAP.get(m.group(1), m.group(0)), text)
    return ' '.join(text.split())


def clean_html_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.translate(HTML_ENTITIES)
    text = ENTITY_PATTERN.sub(lambda m: ENTITY_MAP.get(m.group(1), m.group(0)), text)
    lines = text.split('\n')
    lines = [' '.join(line.split()) for line in lines]
    return '\n'.join(line for line in lines if line.strip())


def decode_response(resp: httpx.Response) -> str:
    try:
        return resp.content.decode('iso-8859-1', errors='ignore')
    except:
        return resp.text


def calculate_page_range() -> Tuple[int, int]:
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
    found = []
    for worker_id in range(TOTAL_WORKERS):
        db_name = f'{WORKER_DB_PREFIX}{worker_id}.db'
        if os.path.exists(db_name):
            found.append((worker_id, db_name))
            continue
        matches = glob.glob(f'**/{db_name}', recursive=True)
        if matches:
            found.append((worker_id, matches[0]))
    return found


def generate_vehicle_fingerprint(
    marca: str, modelo: str, año: int, km: int,
    whatsapp: str = None, vendedor_id: str = None
) -> str:
    km_rango = (km // 5000) * 5000 if km else 0
    components = [
        (marca or '').lower().strip(),
        (modelo or '').lower().strip(),
        str(año or 0),
        str(km_rango),
        (whatsapp or vendedor_id or '').strip()
    ]
    return hashlib.md5('|'.join(components).encode()).hexdigest()[:16]


def parse_km(km_str) -> int:
    if not km_str:
        return 0
    clean = RE_DIGITS.sub('', str(km_str))
    try:
        return int(clean) if clean else 0
    except:
        return 0


def parse_year(year_str) -> Optional[int]:
    if not year_str:
        return None
    try:
        digits = RE_DIGITS.sub('', str(year_str))
        if len(digits) == 4:
            year = int(digits)
            if 1950 <= year <= 2030:
                return year
    except:
        pass
    return None


def parse_transmision(trans_str) -> Optional[str]:
    if not trans_str:
        return None
    trans = trans_str.lower()
    if 'manual' in trans or 'mt' in trans:
        return 'manual'
    elif 'auto' in trans or 'at' in trans:
        return 'automatico'
    return trans


def parse_precio(precio_text: str, dolar_mep: float) -> Optional[float]:
    if not precio_text or 'consultar' in precio_text.lower():
        return None
    try:
        precio_clean = precio_text.replace('$', '').replace('.', '').replace(',', '.')
        match = RE_PRICE.search(precio_clean)
        if match:
            val = float(match.group(1))
            is_usd = any(x in precio_text.upper() for x in ['U$S', 'USD', 'U$D', 'DÓLAR', 'DOLARES'])
            if is_usd:
                return val
            elif dolar_mep:
                return round(val / dolar_mep, 2)
    except:
        pass
    return None


def is_particular(vendedor_str: str) -> int:
    if not vendedor_str:
        return 1
    vendedor = vendedor_str.lower()
    if 'due' in vendedor or 'particular' in vendedor or 'directo' in vendedor:
        return 1
    return 0


# ═══════════════════════════════════════════════════════════════
# 💾 BASE DE DATOS
# ═══════════════════════════════════════════════════════════════
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    fingerprint TEXT,
    
    marca TEXT,
    modelo TEXT,
    version TEXT,
    año INTEGER,
    kilometros INTEGER,
    transmision TEXT,
    combustible TEXT,
    
    precio_usd REAL,
    
    es_particular INTEGER DEFAULT 1,
    avisos_vendedor INTEGER DEFAULT 1,
    whatsapp TEXT,
    
    ciudad TEXT,
    provincia TEXT,
    
    visitas INTEGER,
    expira_dias INTEGER,
    tiene_urgencia INTEGER DEFAULT 0,
    
    primera_vista DATE NOT NULL,
    ultima_vista DATE NOT NULL,
    activo INTEGER DEFAULT 1,
    dias_publicado INTEGER DEFAULT 0,
    
    norm_status TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_vehicles_activo ON vehicles(activo);
CREATE INDEX IF NOT EXISTS idx_vehicles_mercado ON vehicles(marca, modelo, año) WHERE activo = 1;
CREATE INDEX IF NOT EXISTS idx_vehicles_precio ON vehicles(precio_usd) WHERE activo = 1;
CREATE INDEX IF NOT EXISTS idx_vehicles_fingerprint ON vehicles(fingerprint);
CREATE INDEX IF NOT EXISTS idx_vehicles_ultima_vista ON vehicles(ultima_vista);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL,
    precio_usd REAL,
    fecha DATE NOT NULL,
    variacion_pct REAL,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_vehicle ON price_history(vehicle_id);

CREATE TABLE IF NOT EXISTS scrape_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS republication_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    old_vehicle_id TEXT,
    new_vehicle_id TEXT,
    fecha DATE NOT NULL,
    dias_acumulados INTEGER
);
"""


async def init_worker_db(db_path: str):
    if not HAS_AIOSQLITE:
        fatal_error("aiosqlite no está instalado")
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        logger.info(f"✅ Base de datos inicializada: {db_path}")
    except Exception as e:
        fatal_error(f"Error inicializando BD {db_path}", e)


def init_master_db(db_path: str):
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()
        logger.info(f"✅ BD maestra inicializada: {db_path}")
    except Exception as e:
        fatal_error(f"Error inicializando BD maestra", e)


# ═══════════════════════════════════════════════════════════════
# 🌐 HTTP CLIENT
# ═══════════════════════════════════════════════════════════════
class FastHTTPClient:
    def __init__(self):
        self.client = None
        self.sem_listings = None
        self.sem_details = None

    async def __aenter__(self):
        limits = httpx.Limits(max_connections=CONFIG.max_connections, max_keepalive_connections=CONFIG.max_keepalive)
        timeout = httpx.Timeout(connect=CONFIG.connect_timeout, read=CONFIG.read_timeout, write=10.0, pool=5.0)
        self.client = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True, http2=True)
        self.sem_listings = asyncio.Semaphore(CONFIG.max_concurrent_listings)
        self.sem_details = asyncio.Semaphore(CONFIG.max_concurrent_details)
        logger.info("🌐 HTTP Client inicializado")
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def fetch(self, url: str, semaphore: asyncio.Semaphore) -> Optional[httpx.Response]:
        async with semaphore:
            for attempt in range(CONFIG.max_retries):
                try:
                    STATS.requests_made += 1
                    resp = await self.client.get(url)
                    if resp.status_code == 200:
                        STATS.requests_success += 1
                        STATS.bytes_downloaded += len(resp.content)
                        return resp
                    if resp.status_code == 429:
                        STATS.requests_429 += 1
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    if resp.status_code >= 500:
                        await asyncio.sleep(CONFIG.retry_delay * (attempt + 1))
                        continue
                    return None
                except (httpx.TimeoutException, httpx.ConnectError):
                    if attempt < CONFIG.max_retries - 1:
                        await asyncio.sleep(CONFIG.retry_delay * (attempt + 1))
                except Exception:
                    pass
            STATS.requests_failed += 1
            return None

    async def fetch_listing(self, url: str):
        return await self.fetch(url, self.sem_listings)

    async def fetch_detail(self, url: str):
        return await self.fetch(url, self.sem_details)


# ═══════════════════════════════════════════════════════════════
# 💵 DÓLAR
# ═══════════════════════════════════════════════════════════════
async def get_dolar_from_api() -> float:
    apis = [
        ("https://dolarapi.com/v1/dolares/bolsa", lambda r: r.json()['venta']),
        ("https://api.bluelytics.com.ar/v2/latest", lambda r: r.json()['blue']['value_sell']),
    ]
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url, parser in apis:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    value = float(parser(resp))
                    if 100 < value < 5000:
                        logger.info(f"💵 Dólar: ${value:.2f}")
                        return value
            except:
                continue
    fatal_error("No se pudo obtener cotización del dólar")


# ═══════════════════════════════════════════════════════════════
# 📋 PARSING DEL LISTADO
# ═══════════════════════════════════════════════════════════════
def parse_listing_item(item_div) -> Optional[Dict]:
    try:
        item_id = item_div.get('data-rel', '')
        if not item_id:
            return None

        link = item_div.select_one('a[href*="itmId"]')
        url = link.get('href', '') if link else f"{BASE_URL}/index.php?action=carro/showProduct&itmId={item_id}&rbrId=107"
        if url and not url.startswith('http'):
            url = f"{BASE_URL}/{url.lstrip('/')}"

        return {'id': item_id, 'url': url}
    except:
        return None


async def fetch_listing_page(http: FastHTTPClient, offset: int) -> List[Dict]:
    url = f"{LISTING_URL}&o={offset}"
    resp = await http.fetch_listing(url)
    if not resp:
        return []
    try:
        html = decode_response(resp)
        soup = BeautifulSoup(html, 'html.parser')
        return [v for div in soup.select('div[data-rel]') if (v := parse_listing_item(div))]
    except:
        return []


async def fetch_listings_for_worker(http: FastHTTPClient) -> List[Dict]:
    start_page, end_page = calculate_page_range()
    logger.info(f"🔀 Worker {WORKER_ID}: Páginas {start_page}-{end_page}")

    all_vehicles = []
    seen_ids = set()
    offsets = [page * ITEMS_PER_PAGE for page in range(start_page, end_page + 1)]
    empty_count = 0

    for i in range(0, len(offsets), CONFIG.max_concurrent_listings):
        batch = offsets[i:i + CONFIG.max_concurrent_listings]
        results = await asyncio.gather(*[fetch_listing_page(http, o) for o in batch], return_exceptions=True)

        batch_new = 0
        all_empty = True
        for result in results:
            if isinstance(result, Exception):
                continue
            if result:
                all_empty = False
                for v in result:
                    if v['id'] not in seen_ids:
                        seen_ids.add(v['id'])
                        all_vehicles.append(v)
                        batch_new += 1

        if all_empty:
            empty_count += 1
            if empty_count >= 2:
                break
        else:
            empty_count = 0

        logger.info(f"  Páginas: +{batch_new} | Total: {len(all_vehicles)}")

        if CONFIG.delay_between_batches > 0:
            await asyncio.sleep(CONFIG.delay_between_batches)

    if not all_vehicles:
        fatal_error(f"Worker {WORKER_ID}: No se encontraron vehículos")

    return all_vehicles


# ═══════════════════════════════════════════════════════════════
# 🚗 EXTRACCIÓN DE DETALLES
# ═══════════════════════════════════════════════════════════════
def extract_field_from_box(box, field_name: str) -> Optional[str]:
    if not box:
        return None
    try:
        for span in box.find_all('span'):
            if field_name.lower() in span.get_text().lower():
                next_text = span.next_sibling
                if next_text:
                    return clean_text(str(next_text)).lstrip(':').strip() or None
    except:
        pass
    return None


def extract_all_from_page(soup: BeautifulSoup) -> Dict:
    """Extrae TODOS los datos crudos de la página."""
    data = {
        'titulo': None,
        'marca_raw': None,
        'version_raw': None,
        'año': None,
        'kilometros': 0,
        'transmision': None,
        'combustible': None,
        'es_particular': 1,
        'avisos_vendedor': 1,
        'precio_text': None,
        'ciudad': None,
        'provincia': None,
        'whatsapp': None,
        'visitas': None,
        'expira_dias': None,
        'descripcion': None,
    }

    # Título
    try:
        title_tag = soup.find('title')
        if title_tag:
            parts = clean_text(title_tag.get_text()).split(' - ')
            if len(parts) >= 2:
                data['titulo'] = parts[1].strip()
    except:
        pass

    # Descripción del anuncio
    desc_box = None
    for subtitle in soup.find_all('div', class_='subtitle'):
        text = clean_text(subtitle.get_text()).lower()
        if 'descripci' in text and 'anuncio' in text:
            desc_box = subtitle.find_next_sibling('div', class_='box-text')
            break

    if desc_box:
        data['marca_raw'] = extract_field_from_box(desc_box, 'Marca')
        data['version_raw'] = extract_field_from_box(desc_box, 'Versi')
        
        año = extract_field_from_box(desc_box, 'Año') or extract_field_from_box(desc_box, 'Ano')
        data['año'] = parse_year(año)
        
        km = extract_field_from_box(desc_box, 'Kilometraje')
        data['kilometros'] = parse_km(km)
        
        trans = extract_field_from_box(desc_box, 'Transmisi')
        data['transmision'] = parse_transmision(trans)
        
        comb = extract_field_from_box(desc_box, 'Combustible')
        data['combustible'] = comb.lower().strip() if comb else None
        
        vendedor = extract_field_from_box(desc_box, 'Vendedor')
        data['es_particular'] = is_particular(vendedor)

    # Precio
    for subtitle in soup.find_all('div', class_='subtitle'):
        if clean_text(subtitle.get_text()).lower() == 'precio':
            price_box = subtitle.find_next_sibling('div', class_='box-text')
            if price_box:
                data['precio_text'] = clean_text(price_box.get_text())
            break

    # Contacto
    contact_box = None
    for subtitle in soup.find_all('div', class_='subtitle'):
        text = clean_text(subtitle.get_text()).lower()
        if 'datos' in text and 'contacto' in text:
            contact_box = subtitle.find_next_sibling('div', class_='box-text')
            break

    if contact_box:
        ciudad = extract_field_from_box(contact_box, 'Ciudad')
        data['ciudad'] = ciudad.lower().strip() if ciudad else None
        
        provincia = extract_field_from_box(contact_box, 'Provincia')
        data['provincia'] = provincia.lower().strip() if provincia else None

    # WhatsApp
    try:
        wa_link = soup.find('a', class_='btn-contact-whatsapp')
        if wa_link:
            match = RE_WHATSAPP.search(wa_link.get('href', ''))
            if match:
                data['whatsapp'] = match.group(1)
    except:
        pass

    # Avisos publicados
    try:
        btn = soup.find('div', class_='btn-publicados')
        if btn:
            span = btn.find('span')
            if span:
                digits = RE_DIGITS.sub('', span.get_text())
                if digits:
                    data['avisos_vendedor'] = int(digits)
    except:
        pass

    # Descripción ampliada
    for subtitle in soup.find_all('div', class_='subtitle'):
        text = clean_text(subtitle.get_text()).lower()
        if 'descripci' in text and 'ampliada' in text:
            next_box = subtitle.find_next_sibling('div', class_='box-text')
            if next_box:
                data['descripcion'] = clean_html_to_text(str(next_box))
            break

    # Visitas y expiración
    for box in soup.find_all('div', class_='box-text'):
        text = box.get_text()
        if 'Visitas hasta el momento' in text:
            for span in box.find_all('span'):
                if 'Visitas' in span.get_text():
                    next_text = span.next_sibling
                    if next_text:
                        digits = RE_DIGITS.sub('', str(next_text))
                        if digits:
                            data['visitas'] = int(digits)
        if 'Expira en' in text:
            for span in box.find_all('span'):
                if 'Expira' in span.get_text():
                    next_text = span.next_sibling
                    if next_text:
                        digits = RE_DIGITS.sub('', str(next_text))
                        if digits:
                            data['expira_dias'] = int(digits)

    return data


async def fetch_vehicle_details(
    http: FastHTTPClient,
    vehicle_basic: Dict,
    dolar_mep: float
) -> Optional[Dict]:
    """Extrae datos y normaliza contra catálogo."""
    
    url = vehicle_basic.get('url', '')
    if not url:
        return None

    result = {
        'id': vehicle_basic['id'],
        'url': url,
        'fingerprint': None,
        'marca': None,
        'modelo': None,
        'version': None,
        'año': None,
        'kilometros': 0,
        'transmision': None,
        'combustible': None,
        'precio_usd': None,
        'es_particular': 1,
        'avisos_vendedor': 1,
        'whatsapp': None,
        'ciudad': None,
        'provincia': None,
        'visitas': None,
        'expira_dias': None,
        'tiene_urgencia': 0,
        'norm_status': 'no_match',
    }

    resp = await http.fetch_detail(url)
    if not resp:
        return None

    try:
        html = decode_response(resp)
        soup = BeautifulSoup(html, 'html.parser')

        # Extraer datos crudos
        raw = extract_all_from_page(soup)

        # Copiar datos que no necesitan normalización
        result['año'] = raw['año']
        result['kilometros'] = raw['kilometros']
        result['transmision'] = raw['transmision']
        result['combustible'] = raw['combustible']
        result['es_particular'] = raw['es_particular']
        result['avisos_vendedor'] = raw['avisos_vendedor']
        result['ciudad'] = raw['ciudad']
        result['provincia'] = raw['provincia']
        result['whatsapp'] = raw['whatsapp']
        result['visitas'] = raw['visitas']
        result['expira_dias'] = raw['expira_dias']

        # Precio
        result['precio_usd'] = parse_precio(raw['precio_text'], dolar_mep)

        # ═══════════════════════════════════════════════════════
        # 🎯 NORMALIZACIÓN CONTRA CATÁLOGO
        # ═══════════════════════════════════════════════════════
        if HAS_NORMALIZER:
            try:
                norm_result = normalize_vehicle(
                    titulo=raw.get('titulo') or '',
                    descripcion=raw.get('descripcion') or '',
                    marca_raw=raw.get('marca_raw') or '',
                    modelo_raw='',  # En rosariogarage no hay campo modelo separado
                    version_raw=raw.get('version_raw') or ''
                )

                # Usar valores normalizados (None si no se encontró en catálogo)
                result['marca'] = norm_result.get('marca')
                result['modelo'] = norm_result.get('modelo')
                result['version'] = norm_result.get('version')
                result['norm_status'] = norm_result.get('norm_status', 'no_match')

            except Exception as e:
                if DEBUG_MODE:
                    logger.debug(f"Error normalizando: {e}")
                result['norm_status'] = 'error'

        # Detectar urgencia en descripción
        if raw.get('descripcion') and HAS_NORMALIZER:
            try:
                urgency = extract_urgency_signals(raw['descripcion'])
                result['tiene_urgencia'] = 1 if urgency.get('has_urgency', False) else 0
            except:
                pass

        # Generar fingerprint (usa marca normalizada o raw)
        marca_fp = result['marca'] or (raw.get('marca_raw') or '').lower()
        modelo_fp = result['modelo'] or (raw.get('version_raw') or '').lower().split()[0] if raw.get('version_raw') else ''
        
        result['fingerprint'] = generate_vehicle_fingerprint(
            marca=marca_fp,
            modelo=modelo_fp,
            año=result['año'],
            km=result['kilometros'],
            whatsapp=result['whatsapp']
        )

        return result

    except Exception as e:
        if DEBUG_MODE:
            logger.debug(f"Error en {url}: {e}")
        return None


async def fetch_all_details(
    http: FastHTTPClient,
    vehicles: List[Dict],
    dolar_mep: float
) -> List[Dict]:
    """Obtiene todos los detalles."""
    total = len(vehicles)
    logger.info(f"🚗 Obteniendo detalles de {total} vehículos...")

    results = []
    
    for i in range(0, total, CONFIG.batch_size):
        batch = vehicles[i:i + CONFIG.batch_size]
        batch_results = await asyncio.gather(
            *[fetch_vehicle_details(http, v, dolar_mep) for v in batch],
            return_exceptions=True
        )

        for r in batch_results:
            if r and not isinstance(r, Exception):
                results.append(r)

        # Stats
        with_marca = sum(1 for r in results if r.get('marca'))
        logger.info(
            f"  {min(i + CONFIG.batch_size, total)}/{total} | "
            f"Válidos: {len(results)} ({with_marca} normalizados)"
        )

        if CONFIG.delay_between_batches > 0:
            await asyncio.sleep(CONFIG.delay_between_batches)

    if not results:
        fatal_error("No se pudo procesar ningún vehículo")

    return results


# ═══════════════════════════════════════════════════════════════
# 💾 GUARDAR DATOS
# ═══════════════════════════════════════════════════════════════
async def save_worker_results(results: List[Dict], dolar_mep: float):
    today = date.today().isoformat()

    # Stats
    with_marca = sum(1 for v in results if v.get('marca'))
    full_match = sum(1 for v in results if v.get('norm_status') == 'full_match')
    partial = sum(1 for v in results if v.get('norm_status') == 'partial_match')
    marca_only = sum(1 for v in results if v.get('norm_status') == 'marca_only')

    try:
        await init_worker_db(WORKER_DB)

        async with aiosqlite.connect(WORKER_DB) as db:
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('scrape_date', today))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('dolar_mep', str(dolar_mep)))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('worker_id', str(WORKER_ID)))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('vehicles_count', str(len(results))))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('vehicles_with_marca', str(with_marca)))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('norm_full_match', str(full_match)))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('norm_partial', str(partial)))
            await db.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('norm_marca_only', str(marca_only)))

            for v in results:
                await db.execute("""
                    INSERT OR REPLACE INTO vehicles (
                        id, url, fingerprint, marca, modelo, version,
                        año, kilometros, transmision, combustible, precio_usd,
                        es_particular, avisos_vendedor, whatsapp, ciudad, provincia,
                        visitas, expira_dias, tiene_urgencia,
                        primera_vista, ultima_vista, activo, norm_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    v['id'], v['url'], v['fingerprint'],
                    v['marca'], v['modelo'], v['version'],
                    v['año'], v['kilometros'], v['transmision'], v['combustible'], v['precio_usd'],
                    v['es_particular'], v['avisos_vendedor'], v['whatsapp'], v['ciudad'], v['provincia'],
                    v['visitas'], v['expira_dias'], v['tiene_urgencia'],
                    today, today, v['norm_status']
                ))

            await db.commit()

        logger.info(f"✅ Guardados {len(results)} vehículos")
        logger.info(f"   📊 Normalizados: {with_marca} | Full: {full_match} | Partial: {partial} | Marca: {marca_only}")

    except Exception as e:
        fatal_error(f"Error guardando", e)


# ═══════════════════════════════════════════════════════════════
# 🔄 CONSOLIDACIÓN
# ═══════════════════════════════════════════════════════════════
def read_worker_data(db_path: str) -> Tuple[List[tuple], Dict[str, str]]:
    vehicles = []
    metadata = {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        for row in conn.execute("SELECT key, value FROM scrape_metadata"):
            metadata[row['key']] = row['value']

        cursor = conn.execute("""
            SELECT id, url, fingerprint, marca, modelo, version,
                   año, kilometros, transmision, combustible, precio_usd,
                   es_particular, avisos_vendedor, whatsapp, ciudad, provincia,
                   visitas, expira_dias, tiene_urgencia, norm_status
            FROM vehicles
        """)
        vehicles = [tuple(row) for row in cursor]
        conn.close()
    except Exception as e:
        logger.error(f"Error leyendo {db_path}: {e}")
    return vehicles, metadata


def consolidate_worker_results():
    logger.info("=" * 60)
    logger.info("🔄 CONSOLIDACIÓN")
    logger.info("=" * 60)

    today = date.today().isoformat()
    available = find_worker_databases()

    if not available:
        fatal_error("No hay workers para consolidar")

    logger.info(f"📊 Workers: {len(available)}/{TOTAL_WORKERS}")

    # Leer datos
    all_data = []
    dolar_mep = None
    stats = {'full': 0, 'partial': 0, 'marca': 0, 'with_marca': 0}

    for worker_id, db_path in available:
        vehicles, metadata = read_worker_data(db_path)
        if vehicles:
            all_data.append((worker_id, vehicles, metadata))
            with_marca = sum(1 for v in vehicles if v[3])
            logger.info(f"   Worker {worker_id}: {len(vehicles)} ({with_marca} normalizados)")

            if not dolar_mep and 'dolar_mep' in metadata:
                dolar_mep = float(metadata['dolar_mep'])

            stats['full'] += int(metadata.get('norm_full_match', 0))
            stats['partial'] += int(metadata.get('norm_partial', 0))
            stats['marca'] += int(metadata.get('norm_marca_only', 0))
            stats['with_marca'] += int(metadata.get('vehicles_with_marca', 0))

    if not all_data or not dolar_mep:
        fatal_error("Sin datos para consolidar")

    total_read = sum(len(d[1]) for d in all_data)
    logger.info(f"\n📊 Total: {total_read} | Normalizados: {stats['with_marca']}")

    # BD maestra
    init_master_db(MASTER_DB)
    conn = sqlite3.connect(MASTER_DB)
    cursor = conn.cursor()

    cursor.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('last_scrape_date', today))
    cursor.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('last_dolar_mep', str(dolar_mep)))
    cursor.execute("INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)", ('workers_consolidated', str(len(all_data))))

    seen_ids = set()

    for worker_id, vehicles, _ in all_data:
        for v in vehicles:
            vehicle_id = v[0]
            seen_ids.add(vehicle_id)
            new_precio = v[10]

            existing = cursor.execute(
                "SELECT precio_usd, primera_vista FROM vehicles WHERE id = ?", (vehicle_id,)
            ).fetchone()

            if existing:
                old_precio = existing[0]
                cursor.execute("""
                    UPDATE vehicles SET
                        url=?, fingerprint=?, marca=?, modelo=?, version=?,
                        año=?, kilometros=?, transmision=?, combustible=?, precio_usd=?,
                        es_particular=?, avisos_vendedor=?, whatsapp=?, ciudad=?, provincia=?,
                        visitas=?, expira_dias=?, tiene_urgencia=?,
                        ultima_vista=?, activo=1,
                        dias_publicado=julianday(?)-julianday(primera_vista),
                        norm_status=?
                    WHERE id=?
                """, (*v[1:], today, today, vehicle_id))
                STATS.vehicles_updated += 1

                if old_precio and new_precio and abs(old_precio - new_precio) > 0.01:
                    var = ((new_precio - old_precio) / old_precio) * 100
                    cursor.execute(
                        "INSERT INTO price_history VALUES (NULL, ?, ?, ?, ?)",
                        (vehicle_id, new_precio, today, round(var, 2))
                    )
                    STATS.vehicles_price_changed += 1
            else:
                cursor.execute("""
                    INSERT INTO vehicles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                """, (*v[:19], today, today, v[19]))

                if new_precio:
                    cursor.execute(
                        "INSERT INTO price_history VALUES (NULL, ?, ?, ?, NULL)",
                        (vehicle_id, new_precio, today)
                    )
                STATS.vehicles_new += 1

    # Marcar inactivos
    if seen_ids:
        placeholders = ','.join('?' * len(seen_ids))
        cursor.execute(f"UPDATE vehicles SET activo=0 WHERE activo=1 AND id NOT IN ({placeholders})", tuple(seen_ids))

    # Purgar
    cursor.execute("DELETE FROM price_history WHERE vehicle_id IN (SELECT id FROM vehicles WHERE activo=0 AND ultima_vista < date('now', '-90 days'))")
    cursor.execute("DELETE FROM vehicles WHERE activo=0 AND ultima_vista < date('now', '-90 days')")

    # Stats finales
    total_active = cursor.execute("SELECT COUNT(*) FROM vehicles WHERE activo=1").fetchone()[0]
    with_marca = cursor.execute("SELECT COUNT(*) FROM vehicles WHERE activo=1 AND marca IS NOT NULL").fetchone()[0]
    total_history = cursor.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]

    conn.commit()
    conn.close()

    # Limpiar
    for _, db_path in available:
        try:
            os.remove(db_path)
        except:
            pass

    logger.info("\n" + "=" * 60)
    logger.info("✅ CONSOLIDACIÓN COMPLETADA")
    logger.info("=" * 60)
    logger.info(f"   🆕 Nuevos: {STATS.vehicles_new}")
    logger.info(f"   🔄 Actualizados: {STATS.vehicles_updated}")
    logger.info(f"   💰 Cambios precio: {STATS.vehicles_price_changed}")
    logger.info(f"   📊 Activos: {total_active} ({with_marca} normalizados)")
    logger.info(f"   📈 Historial: {total_history}")
    logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════
# 🚀 MAIN
# ═══════════════════════════════════════════════════════════════
async def run_worker():
    global STATS
    STATS = Stats()
    start = datetime.now()

    logger.info("=" * 60)
    logger.info(f"🚀 WORKER {WORKER_ID}/{TOTAL_WORKERS}")
    logger.info("=" * 60)

    # Inicializar normalizador
    if HAS_NORMALIZER:
        if init_normalizer():
            logger.info("✅ Normalizador con catálogo")
        else:
            logger.warning("⚠️ Sin catálogo - datos no normalizados")
    else:
        logger.warning("⚠️ Módulo normalizer no disponible")

    async with FastHTTPClient() as http:
        dolar = await get_dolar_from_api()

        logger.info("\n📋 Fase 1: Listados...")
        vehicles = await fetch_listings_for_worker(http)
        logger.info(f"✅ IDs: {len(vehicles)}")

        logger.info("\n🚗 Fase 2: Detalles + Normalización...")
        results = await fetch_all_details(http, vehicles, dolar)

        logger.info("\n💾 Guardando...")
        await save_worker_results(results, dolar)

    elapsed = (datetime.now() - start).total_seconds()
    with_marca = sum(1 for r in results if r.get('marca'))

    logger.info("\n" + "=" * 60)
    logger.info(f"✅ WORKER {WORKER_ID} COMPLETADO")
    logger.info(f"   ⏱️ {elapsed:.1f}s | 🚗 {len(results)} ({with_marca} normalizados)")
    logger.info("=" * 60)


def main():
    try:
        if CONSOLIDATE_MODE:
            consolidate_worker_results()
        else:
            asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.warning("Interrumpido")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        fatal_error("Error", e)


if __name__ == "__main__":
    main()
