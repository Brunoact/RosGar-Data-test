"""
🚗 Scraper RosarioGarage - Versión Producción v2.5
==================================================
Extrae datos de cada publicación y normaliza contra catálogo.
Si no encuentra en catálogo, usa datos originales.
NUNCA deja marca vacía ni marca sin modelo.

v2.5: Integración con normalizer_v2 (v4.1)
- Pasa año_raw, km_raw, precio_raw al normalizador
- Aprovecha año corregido del normalizador (años basura, desc)
- Log de warnings/corrections en modo debug
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

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

try:
    from normalizer_v2 import (
        init_normalizer,
        normalize_vehicle,
        get_normalization_stats,
        extract_urgency_signals
    )
    HAS_NORMALIZER = True
except ImportError:
    HAS_NORMALIZER = False

# --- CONFIG ---
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


def fatal_error(msg: str, e: Exception = None):
    logger.critical(f"💀 {msg}")
    if e:
        logger.critical(f"   {type(e).__name__}: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# 🔧 HELPERS
# ═══════════════════════════════════════════════════════════════

HTML_ENTITIES = str.maketrans({'\xa0': ' '})
ENTITY_PATTERN = re.compile(r'&(\w+);')
ENTITY_MAP = {
    'nbsp': ' ', 'oacute': 'ó', 'aacute': 'á',
    'eacute': 'é', 'iacute': 'í', 'uacute': 'ú',
    'ntilde': 'ñ', 'amp': '&'
}

RE_DIGITS = re.compile(r'[^\d]')
RE_PRICE = re.compile(r'(\d+\.?\d*)')
RE_WHATSAPP = re.compile(r'wa\.me/(\d+)\?', re.I)
RE_YEAR = re.compile(r'^(19|20)\d{2}$')


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.translate(HTML_ENTITIES)
    text = ENTITY_PATTERN.sub(
        lambda m: ENTITY_MAP.get(m.group(1), m.group(0)), text
    )
    return ' '.join(text.split())


def clean_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return clean_text(text)


def decode_response(resp: httpx.Response) -> str:
    try:
        return resp.content.decode('iso-8859-1', errors='ignore')
    except:
        return resp.text


def calculate_page_range() -> Tuple[int, int]:
    pages_per_worker = MAX_PAGES // TOTAL_WORKERS
    remainder = MAX_PAGES % TOTAL_WORKERS
    if WORKER_ID < remainder:
        start = WORKER_ID * (pages_per_worker + 1)
        end = start + pages_per_worker
    else:
        start = WORKER_ID * pages_per_worker + remainder
        end = start + pages_per_worker - 1
    return start, end


def find_worker_databases() -> List[Tuple[int, str]]:
    found = []
    for wid in range(TOTAL_WORKERS):
        db = f'{WORKER_DB_PREFIX}{wid}.db'
        if os.path.exists(db):
            found.append((wid, db))
        else:
            matches = glob.glob(f'**/{db}', recursive=True)
            if matches:
                found.append((wid, matches[0]))
    return found


def generate_fingerprint(
    marca: str, modelo: str, año: int, km: int, whatsapp: str = None
) -> str:
    km_rango = (km // 5000) * 5000 if km else 0
    parts = [
        (marca or '').lower(),
        (modelo or '').lower(),
        str(año or 0),
        str(km_rango),
        whatsapp or ''
    ]
    return hashlib.md5('|'.join(parts).encode()).hexdigest()[:16]


def parse_km(s) -> int:
    if not s:
        return 0
    try:
        return int(RE_DIGITS.sub('', str(s))) or 0
    except:
        return 0


def parse_year(s) -> Optional[int]:
    if not s:
        return None
    try:
        digits = RE_DIGITS.sub('', str(s))
        if len(digits) == 4:
            y = int(digits)
            if 1950 <= y <= 2030:
                return y
    except:
        pass
    return None


def parse_trans(s) -> Optional[str]:
    if not s:
        return None
    t = s.lower()
    if 'manual' in t or 'mt' in t:
        return 'manual'
    if 'auto' in t or 'at' in t:
        return 'automatico'
    return t


def parse_precio(s: str, dolar: float) -> Optional[float]:
    if not s or 'consultar' in s.lower():
        return None
    try:
        clean = s.replace('$', '').replace('.', '').replace(',', '.')
        m = RE_PRICE.search(clean)
        if m:
            val = float(m.group(1))
            is_usd = any(
                x in s.upper() for x in ['U$S', 'USD', 'U$D', 'DÓLAR']
            )
            return val if is_usd else round(val / dolar, 2) if dolar else None
    except:
        pass
    return None


def is_particular(s: str) -> int:
    if not s:
        return 1
    t = s.lower()
    return 1 if any(x in t for x in ['due', 'particular', 'directo']) else 0


def extract_model_from_version(version: str, marca: str = "") -> str:
    """Extrae el modelo de version_raw."""
    if not version:
        return ""
    text = version.lower().strip()
    if marca:
        marca_lower = marca.lower()
        if text.startswith(marca_lower):
            text = text[len(marca_lower):].strip()
    words = text.split()
    for w in words:
        if len(w) >= 2 and not RE_YEAR.match(w):
            return w
    return words[0] if words else ""


# ═══════════════════════════════════════════════════════════════
# 💾 DATABASE
# ═══════════════════════════════════════════════════════════════

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_activo ON vehicles(activo);
CREATE INDEX IF NOT EXISTS idx_mercado ON vehicles(marca, modelo, año)
    WHERE activo = 1;
CREATE INDEX IF NOT EXISTS idx_precio ON vehicles(precio_usd)
    WHERE activo = 1;
CREATE INDEX IF NOT EXISTS idx_fingerprint ON vehicles(fingerprint);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL,
    precio_usd REAL,
    fecha DATE NOT NULL,
    variacion_pct REAL,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_hist_vehicle ON price_history(vehicle_id);

CREATE TABLE IF NOT EXISTS scrape_metadata (
    key TEXT PRIMARY KEY, value TEXT
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


async def init_worker_db(path: str):
    if not HAS_AIOSQLITE:
        fatal_error("aiosqlite no instalado")
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info(f"✅ BD inicializada: {path}")


def init_master_db(path: str):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    logger.info(f"✅ BD maestra: {path}")


# ═══════════════════════════════════════════════════════════════
# 🌐 HTTP CLIENT
# ═══════════════════════════════════════════════════════════════

class HTTPClient:
    def __init__(self):
        self.client = None
        self.sem_list = None
        self.sem_detail = None

    async def __aenter__(self):
        limits = httpx.Limits(
            max_connections=CONFIG.max_connections,
            max_keepalive_connections=CONFIG.max_keepalive
        )
        timeout = httpx.Timeout(
            connect=CONFIG.connect_timeout,
            read=CONFIG.read_timeout,
            write=10, pool=5
        )
        self.client = httpx.AsyncClient(
            limits=limits, timeout=timeout,
            follow_redirects=True, http2=True
        )
        self.sem_list = asyncio.Semaphore(CONFIG.max_concurrent_listings)
        self.sem_detail = asyncio.Semaphore(CONFIG.max_concurrent_details)
        logger.info("🌐 HTTP Client listo")
        return self

    async def __aexit__(self, *_):
        if self.client:
            await self.client.aclose()

    async def fetch(
        self, url: str, sem: asyncio.Semaphore
    ) -> Optional[httpx.Response]:
        async with sem:
            for attempt in range(CONFIG.max_retries):
                try:
                    STATS.requests_made += 1
                    r = await self.client.get(url)
                    if r.status_code == 200:
                        STATS.requests_success += 1
                        return r
                    if r.status_code == 429:
                        STATS.requests_429 += 1
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    if r.status_code >= 500:
                        await asyncio.sleep(
                            CONFIG.retry_delay * (attempt + 1)
                        )
                        continue
                    return None
                except (httpx.TimeoutException, httpx.ConnectError):
                    if attempt < CONFIG.max_retries - 1:
                        await asyncio.sleep(
                            CONFIG.retry_delay * (attempt + 1)
                        )
                except:
                    pass
            STATS.requests_failed += 1
            return None

    async def fetch_listing(self, url: str):
        return await self.fetch(url, self.sem_list)

    async def fetch_detail(self, url: str):
        return await self.fetch(url, self.sem_detail)


# ═══════════════════════════════════════════════════════════════
# 💵 DÓLAR
# ═══════════════════════════════════════════════════════════════

async def get_dolar() -> float:
    apis = [
        (
            "https://dolarapi.com/v1/dolares/bolsa",
            lambda r: r.json()['venta']
        ),
        (
            "https://api.bluelytics.com.ar/v2/latest",
            lambda r: r.json()['blue']['value_sell']
        ),
    ]
    async with httpx.AsyncClient(timeout=15) as c:
        for url, parse in apis:
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    v = float(parse(r))
                    if 100 < v < 5000:
                        logger.info(f"💵 Dólar: ${v:.2f}")
                        return v
            except:
                continue
    fatal_error("No se pudo obtener dólar")


# ═══════════════════════════════════════════════════════════════
# 📋 LISTING PARSING
# ═══════════════════════════════════════════════════════════════

def parse_listing_item(div) -> Optional[Dict]:
    try:
        item_id = div.get('data-rel', '')
        if not item_id:
            return None
        link = div.select_one('a[href*="itmId"]')
        url = link.get('href', '') if link else (
            f"{BASE_URL}/index.php?action=carro/showProduct"
            f"&itmId={item_id}&rbrId=107"
        )
        if url and not url.startswith('http'):
            url = f"{BASE_URL}/{url.lstrip('/')}"
        return {'id': item_id, 'url': url}
    except:
        return None


async def fetch_listing_page(
    http: HTTPClient, offset: int
) -> List[Dict]:
    r = await http.fetch_listing(f"{LISTING_URL}&o={offset}")
    if not r:
        return []
    try:
        soup = BeautifulSoup(decode_response(r), 'html.parser')
        return [
            v for d in soup.select('div[data-rel]')
            if (v := parse_listing_item(d))
        ]
    except:
        return []


async def fetch_listings(http: HTTPClient) -> List[Dict]:
    start, end = calculate_page_range()
    logger.info(f"🔀 Worker {WORKER_ID}: Páginas {start}-{end}")

    vehicles = []
    seen = set()
    offsets = [p * ITEMS_PER_PAGE for p in range(start, end + 1)]
    empty = 0

    for i in range(0, len(offsets), CONFIG.max_concurrent_listings):
        batch = offsets[i:i + CONFIG.max_concurrent_listings]
        results = await asyncio.gather(
            *[fetch_listing_page(http, o) for o in batch],
            return_exceptions=True
        )

        new = 0
        all_empty = True
        for res in results:
            if isinstance(res, Exception):
                continue
            if res:
                all_empty = False
                for v in res:
                    if v['id'] not in seen:
                        seen.add(v['id'])
                        vehicles.append(v)
                        new += 1

        if all_empty:
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0

        logger.info(f"  +{new} | Total: {len(vehicles)}")
        if CONFIG.delay_between_batches > 0:
            await asyncio.sleep(CONFIG.delay_between_batches)

    if not vehicles:
        fatal_error(f"Worker {WORKER_ID}: Sin vehículos")

    return vehicles


# ═══════════════════════════════════════════════════════════════
# 🚗 DETAIL EXTRACTION
# ═══════════════════════════════════════════════════════════════

def extract_field(box, name: str) -> Optional[str]:
    if not box:
        return None
    for span in box.find_all('span'):
        if name.lower() in span.get_text().lower():
            nxt = span.next_sibling
            if nxt:
                return clean_text(str(nxt)).lstrip(':').strip() or None
    return None


def extract_raw_data(soup: BeautifulSoup) -> Dict:
    """Extrae datos CRUDOS de la página."""
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
        title = soup.find('title')
        if title:
            parts = clean_text(title.get_text()).split(' - ')
            if len(parts) >= 2:
                data['titulo'] = parts[1].strip()
    except:
        pass

    # Descripción del anuncio
    desc_box = None
    for sub in soup.find_all('div', class_='subtitle'):
        t = clean_text(sub.get_text()).lower()
        if 'descripci' in t and 'anuncio' in t:
            desc_box = sub.find_next_sibling('div', class_='box-text')
            break

    if desc_box:
        data['marca_raw'] = extract_field(desc_box, 'Marca')
        data['version_raw'] = extract_field(desc_box, 'Versi')
        data['año'] = parse_year(
            extract_field(desc_box, 'Año')
            or extract_field(desc_box, 'Ano')
        )
        data['kilometros'] = parse_km(
            extract_field(desc_box, 'Kilometraje')
        )
        data['transmision'] = parse_trans(
            extract_field(desc_box, 'Transmisi')
        )
        comb = extract_field(desc_box, 'Combustible')
        data['combustible'] = comb.lower() if comb else None
        data['es_particular'] = is_particular(
            extract_field(desc_box, 'Vendedor')
        )

    # Precio
    for sub in soup.find_all('div', class_='subtitle'):
        if clean_text(sub.get_text()).lower() == 'precio':
            box = sub.find_next_sibling('div', class_='box-text')
            if box:
                data['precio_text'] = clean_text(box.get_text())
            break

    # Contacto
    contact_box = None
    for sub in soup.find_all('div', class_='subtitle'):
        t = clean_text(sub.get_text()).lower()
        if 'datos' in t and 'contacto' in t:
            contact_box = sub.find_next_sibling(
                'div', class_='box-text'
            )
            break

    if contact_box:
        ciudad = extract_field(contact_box, 'Ciudad')
        data['ciudad'] = ciudad.lower() if ciudad else None
        prov = extract_field(contact_box, 'Provincia')
        data['provincia'] = prov.lower() if prov else None

    # WhatsApp
    try:
        wa = soup.find('a', class_='btn-contact-whatsapp')
        if wa:
            m = RE_WHATSAPP.search(wa.get('href', ''))
            if m:
                data['whatsapp'] = m.group(1)
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
    for sub in soup.find_all('div', class_='subtitle'):
        t = clean_text(sub.get_text()).lower()
        if 'descripci' in t and 'ampliada' in t:
            box = sub.find_next_sibling('div', class_='box-text')
            if box:
                data['descripcion'] = clean_html(str(box))
            break

    # Visitas y expiración
    for box in soup.find_all('div', class_='box-text'):
        text = box.get_text()
        if 'Visitas hasta el momento' in text:
            for span in box.find_all('span'):
                if 'Visitas' in span.get_text():
                    nxt = span.next_sibling
                    if nxt:
                        d = RE_DIGITS.sub('', str(nxt))
                        if d:
                            data['visitas'] = int(d)
        if 'Expira en' in text:
            for span in box.find_all('span'):
                if 'Expira' in span.get_text():
                    nxt = span.next_sibling
                    if nxt:
                        d = RE_DIGITS.sub('', str(nxt))
                        if d:
                            data['expira_dias'] = int(d)

    return data


async def fetch_vehicle_details(
    http: HTTPClient, basic: Dict, dolar: float
) -> Optional[Dict]:
    """Extrae y normaliza un vehículo."""
    url = basic.get('url', '')
    if not url:
        return None

    result = {
        'id': basic['id'],
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
        'norm_status': 'pending',
    }

    r = await http.fetch_detail(url)
    if not r:
        return None

    try:
        soup = BeautifulSoup(decode_response(r), 'html.parser')
        raw = extract_raw_data(soup)

        # Copiar datos que no se normalizan
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
        result['precio_usd'] = parse_precio(raw['precio_text'], dolar)

        # ═══════════════════════════════════════════════════════
        # 🎯 NORMALIZACIÓN v2
        # ═══════════════════════════════════════════════════════
        marca_raw = (raw.get('marca_raw') or '').strip()
        version_raw = (raw.get('version_raw') or '').strip()
        titulo = (raw.get('titulo') or '').strip()
        descripcion = raw.get('descripcion') or ''

        if HAS_NORMALIZER:
            try:
                norm = normalize_vehicle(
                    titulo=titulo,
                    descripcion=descripcion,
                    marca_raw=marca_raw,
                    # modelo_raw vacío: RosarioGarage no tiene campo
                    # "modelo" separado. El normalizer lo infiere
                    # de version_raw, titulo y descripcion.
                    modelo_raw="",
                    version_raw=version_raw,
                    # ── NUEVO v2.5: pasar año, km y precio ──
                    año_raw=raw['año'],
                    km_raw=raw['kilometros'],
                    precio_raw=result['precio_usd'],
                )

                result['marca'] = norm.get('marca')
                result['modelo'] = norm.get('modelo')
                result['version'] = norm.get('version')
                result['norm_status'] = norm.get(
                    'norm_status', 'fallback'
                )

                # ── NUEVO v2.5: usar año corregido si el
                #    normalizer detectó año basura y encontró
                #    uno mejor en descripción/título ──
                año_detectado = norm.get('año_detectado')
                if año_detectado and norm.get('año_fuente') in (
                    'description', 'title', 'text'
                ):
                    if result['año'] != año_detectado:
                        if DEBUG_MODE:
                            logger.debug(
                                f"Año corregido: {result['año']} → "
                                f"{año_detectado} (fuente: "
                                f"{norm.get('año_fuente')})"
                            )
                        result['año'] = año_detectado

                # ── NUEVO v2.5: log de warnings en debug ──
                if DEBUG_MODE and norm.get('warnings'):
                    for w in norm['warnings']:
                        logger.debug(
                            f"  ⚠️ {basic['id']}: {w}"
                        )
                if DEBUG_MODE and norm.get('corrections'):
                    for c in norm['corrections']:
                        logger.debug(
                            f"  🔧 {basic['id']}: "
                            f"{c.get('razon', c)}"
                        )

            except Exception as e:
                result['norm_status'] = 'error'
                if DEBUG_MODE:
                    logger.debug(
                        f"Normalizer error {basic['id']}: {e}"
                    )

        # ═══════════════════════════════════════════════════════
        # FALLBACK: NUNCA dejar marca vacía ni marca sin modelo
        # ═══════════════════════════════════════════════════════

        # Si no hay marca, usar la original
        if not result['marca']:
            result['marca'] = (
                marca_raw.lower() if marca_raw else None
            )

        # Si aún no hay marca, intentar extraer del título
        if not result['marca'] and titulo:
            words = titulo.lower().split()
            if words:
                result['marca'] = words[0]

        # Si hay marca pero no modelo, extraer de version_raw o título
        if result['marca'] and not result['modelo']:
            model = extract_model_from_version(
                version_raw, result['marca']
            )
            if not model and titulo:
                model = extract_model_from_version(
                    titulo, result['marca']
                )
            result['modelo'] = model if model else None

        # Si sigue sin modelo, usar primera palabra de version_raw
        if (result['marca'] and not result['modelo']
                and version_raw):
            words = version_raw.lower().split()
            for w in words:
                if (len(w) >= 2
                        and not RE_YEAR.match(w)
                        and w != result['marca']):
                    result['modelo'] = w
                    break

        # Detectar urgencia
        if descripcion and HAS_NORMALIZER:
            try:
                urg = extract_urgency_signals(descripcion)
                result['tiene_urgencia'] = (
                    1 if urg.get('has_urgency') else 0
                )
            except:
                pass

        # Fingerprint
        result['fingerprint'] = generate_fingerprint(
            result['marca'], result['modelo'],
            result['año'], result['kilometros'],
            result['whatsapp']
        )

        return result

    except Exception as e:
        if DEBUG_MODE:
            logger.debug(f"Error en {url}: {e}")
        return None


async def fetch_all_details(
    http: HTTPClient, vehicles: List[Dict], dolar: float
) -> List[Dict]:
    total = len(vehicles)
    logger.info(f"🚗 Extrayendo detalles de {total} vehículos...")

    results = []
    for i in range(0, total, CONFIG.batch_size):
        batch = vehicles[i:i + CONFIG.batch_size]
        batch_results = await asyncio.gather(
            *[fetch_vehicle_details(http, v, dolar) for v in batch],
            return_exceptions=True
        )

        for r in batch_results:
            if r and not isinstance(r, Exception):
                results.append(r)

        with_marca = sum(1 for x in results if x.get('marca'))
        with_modelo = sum(
            1 for x in results
            if x.get('marca') and x.get('modelo')
        )
        logger.info(
            f"  {min(i + CONFIG.batch_size, total)}/{total} | "
            f"OK: {len(results)} | "
            f"Marca: {with_marca} | Modelo: {with_modelo}"
        )

        if CONFIG.delay_between_batches > 0:
            await asyncio.sleep(CONFIG.delay_between_batches)

    if not results:
        fatal_error("No se procesó ningún vehículo")

    return results


# ═══════════════════════════════════════════════════════════════
# 💾 SAVE
# ═══════════════════════════════════════════════════════════════

async def save_worker_results(results: List[Dict], dolar: float):
    today = date.today().isoformat()

    with_marca = sum(1 for v in results if v.get('marca'))
    with_modelo = sum(
        1 for v in results
        if v.get('marca') and v.get('modelo')
    )
    full = sum(
        1 for v in results
        if v.get('norm_status') == 'full_match'
    )
    partial = sum(
        1 for v in results
        if v.get('norm_status') == 'partial_match'
    )

    await init_worker_db(WORKER_DB)

    async with aiosqlite.connect(WORKER_DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('scrape_date', today)
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('dolar_mep', str(dolar))
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('worker_id', str(WORKER_ID))
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('vehicles_count', str(len(results)))
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('with_marca', str(with_marca))
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('with_modelo', str(with_modelo))
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('norm_full', str(full))
        )
        await db.execute(
            "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
            ('norm_partial', str(partial))
        )

        for v in results:
            await db.execute("""
                INSERT OR REPLACE INTO vehicles (
                    id, url, fingerprint, marca, modelo, version,
                    año, kilometros, transmision, combustible,
                    precio_usd, es_particular, avisos_vendedor,
                    whatsapp, ciudad, provincia, visitas,
                    expira_dias, tiene_urgencia,
                    primera_vista, ultima_vista, activo, norm_status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, 1, ?
                )
            """, (
                v['id'], v['url'], v['fingerprint'],
                v['marca'], v['modelo'], v['version'],
                v['año'], v['kilometros'], v['transmision'],
                v['combustible'], v['precio_usd'],
                v['es_particular'], v['avisos_vendedor'],
                v['whatsapp'], v['ciudad'], v['provincia'],
                v['visitas'], v['expira_dias'],
                v['tiene_urgencia'],
                today, today, v['norm_status']
            ))

        await db.commit()

    logger.info(f"✅ Guardados {len(results)} vehículos")
    logger.info(
        f"   📊 Marca: {with_marca} | Modelo: {with_modelo} | "
        f"Full: {full} | Partial: {partial}"
    )


# ═══════════════════════════════════════════════════════════════
# 🔄 CONSOLIDATE
# ═══════════════════════════════════════════════════════════════

def read_worker_data(path: str) -> Tuple[List[tuple], Dict]:
    vehicles, metadata = [], {}
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT key, value FROM scrape_metadata"
        ):
            metadata[row['key']] = row['value']
        cursor = conn.execute("""
            SELECT id, url, fingerprint, marca, modelo, version,
                   año, kilometros, transmision, combustible,
                   precio_usd, es_particular, avisos_vendedor,
                   whatsapp, ciudad, provincia, visitas,
                   expira_dias, tiene_urgencia, norm_status
            FROM vehicles
        """)
        vehicles = [tuple(row) for row in cursor]
        conn.close()
    except Exception as e:
        logger.error(f"Error leyendo {path}: {e}")
    return vehicles, metadata


def consolidate():
    logger.info("=" * 60)
    logger.info("🔄 CONSOLIDACIÓN")
    logger.info("=" * 60)

    today = date.today().isoformat()
    workers = find_worker_databases()
    if not workers:
        fatal_error("Sin workers")

    logger.info(f"📊 Workers: {len(workers)}/{TOTAL_WORKERS}")

    all_data = []
    dolar = None
    stats = {'marca': 0, 'modelo': 0, 'full': 0, 'partial': 0}

    for wid, path in workers:
        vehicles, meta = read_worker_data(path)
        if vehicles:
            all_data.append((wid, vehicles, meta))
            m = sum(1 for v in vehicles if v[3])
            mo = sum(1 for v in vehicles if v[3] and v[4])
            logger.info(
                f"   Worker {wid}: {len(vehicles)} "
                f"(marca: {m}, modelo: {mo})"
            )
            if not dolar and 'dolar_mep' in meta:
                dolar = float(meta['dolar_mep'])
            stats['marca'] += int(meta.get('with_marca', 0))
            stats['modelo'] += int(meta.get('with_modelo', 0))
            stats['full'] += int(meta.get('norm_full', 0))
            stats['partial'] += int(meta.get('norm_partial', 0))

    if not all_data or not dolar:
        fatal_error("Sin datos")

    total = sum(len(d[1]) for d in all_data)
    logger.info(
        f"\n📊 Total: {total} | "
        f"Marca: {stats['marca']} | Modelo: {stats['modelo']}"
    )

    init_master_db(MASTER_DB)

    conn = sqlite3.connect(MASTER_DB)

    # Migración: reparar datos corruptos
    conn.execute(
        "UPDATE vehicles SET ultima_vista = primera_vista "
        "WHERE ultima_vista NOT LIKE '____-__-__'"
    )
    conn.execute(
        "UPDATE vehicles SET norm_status = 'unknown' "
        "WHERE norm_status LIKE '____-__-__'"
    )
    conn.commit()
    logger.info("🔧 Migración: datos corruptos reparados")

    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
        ('last_scrape_date', today)
    )
    cursor.execute(
        "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
        ('last_dolar_mep', str(dolar))
    )
    cursor.execute(
        "INSERT OR REPLACE INTO scrape_metadata VALUES (?, ?)",
        ('workers_consolidated', str(len(all_data)))
    )

    seen = set()
    for wid, vehicles, _ in all_data:
        for v in vehicles:
            vid = v[0]
            seen.add(vid)
            precio = v[10]

            existing = cursor.execute(
                "SELECT precio_usd, primera_vista "
                "FROM vehicles WHERE id = ?", (vid,)
            ).fetchone()

            if existing:
                old_precio = existing[0]
                cursor.execute("""
                    UPDATE vehicles SET
                        url=?, fingerprint=?, marca=?, modelo=?,
                        version=?, año=?, kilometros=?,
                        transmision=?, combustible=?, precio_usd=?,
                        es_particular=?, avisos_vendedor=?,
                        whatsapp=?, ciudad=?, provincia=?,
                        visitas=?, expira_dias=?, tiene_urgencia=?,
                        ultima_vista=?, activo=1,
                        dias_publicado=julianday(?)-julianday(primera_vista),
                        norm_status=?
                    WHERE id=?
                """, (
                    *v[1:19], today, today, v[19], vid
                ))
                STATS.vehicles_updated += 1

                if (old_precio and precio
                        and abs(old_precio - precio) > 0.01):
                    var = ((precio - old_precio) / old_precio) * 100
                    cursor.execute(
                        "INSERT INTO price_history "
                        "VALUES (NULL, ?, ?, ?, ?)",
                        (vid, precio, today, round(var, 2))
                    )
                    STATS.vehicles_price_changed += 1
            else:
                cursor.execute("""
                    INSERT INTO vehicles VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?
                    )
                """, (*v[:19], today, today, v[19]))
                if precio:
                    cursor.execute(
                        "INSERT INTO price_history "
                        "VALUES (NULL, ?, ?, ?, NULL)",
                        (vid, precio, today)
                    )
                STATS.vehicles_new += 1

    if seen:
        ph = ','.join('?' * len(seen))
        cursor.execute(
            f"UPDATE vehicles SET activo=0 "
            f"WHERE activo=1 AND id NOT IN ({ph})",
            tuple(seen)
        )

    cursor.execute(
        "DELETE FROM price_history WHERE vehicle_id IN "
        "(SELECT id FROM vehicles WHERE activo=0 "
        "AND ultima_vista < date('now', '-90 days'))"
    )
    cursor.execute(
        "DELETE FROM vehicles WHERE activo=0 "
        "AND ultima_vista < date('now', '-90 days')"
    )

    active = cursor.execute(
        "SELECT COUNT(*) FROM vehicles WHERE activo=1"
    ).fetchone()[0]
    with_marca = cursor.execute(
        "SELECT COUNT(*) FROM vehicles "
        "WHERE activo=1 AND marca IS NOT NULL"
    ).fetchone()[0]
    with_modelo = cursor.execute(
        "SELECT COUNT(*) FROM vehicles "
        "WHERE activo=1 AND marca IS NOT NULL "
        "AND modelo IS NOT NULL"
    ).fetchone()[0]
    history = cursor.execute(
        "SELECT COUNT(*) FROM price_history"
    ).fetchone()[0]

    conn.commit()
    conn.close()

    for _, path in workers:
        try:
            os.remove(path)
        except:
            pass

    logger.info("\n" + "=" * 60)
    logger.info("✅ CONSOLIDACIÓN COMPLETADA")
    logger.info("=" * 60)
    logger.info(f"   🆕 Nuevos: {STATS.vehicles_new}")
    logger.info(f"   🔄 Actualizados: {STATS.vehicles_updated}")
    logger.info(
        f"   💰 Cambios precio: {STATS.vehicles_price_changed}"
    )
    logger.info(
        f"   📊 Activos: {active} | "
        f"Marca: {with_marca} | Modelo: {with_modelo}"
    )
    logger.info(f"   📈 Historial: {history}")
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

    # Normalizer v2
    if HAS_NORMALIZER:
        if init_normalizer(
            dicts_path="config/normalizer_dicts.json",
            brand_aliases_path="config/brand_aliases.json"
        ):
            logger.info("✅ Normalizador v2 con catálogo")
        else:
            logger.warning("⚠️ Sin catálogo (usará fallback)")
    else:
        logger.warning("⚠️ Módulo normalizer_v2 no disponible")

    async with HTTPClient() as http:
        dolar = await get_dolar()

        logger.info("\n📋 Fase 1: Listados...")
        vehicles = await fetch_listings(http)
        logger.info(f"✅ IDs: {len(vehicles)}")

        logger.info("\n🚗 Fase 2: Detalles...")
        results = await fetch_all_details(http, vehicles, dolar)

        logger.info("\n💾 Guardando...")
        await save_worker_results(results, dolar)

    elapsed = (datetime.now() - start).total_seconds()
    with_marca = sum(1 for r in results if r.get('marca'))
    with_modelo = sum(
        1 for r in results
        if r.get('marca') and r.get('modelo')
    )

    # Stats del normalizer v2
    if HAS_NORMALIZER:
        try:
            norm_stats = get_normalization_stats()
            logger.info(f"\n📊 Normalizer: {norm_stats.summary()}")
        except:
            pass

    logger.info("\n" + "=" * 60)
    logger.info(f"✅ WORKER {WORKER_ID} COMPLETADO")
    logger.info(
        f"   ⏱️ {elapsed:.1f}s | 🚗 {len(results)}"
    )
    logger.info(
        f"   📊 Marca: {with_marca} | Modelo: {with_modelo}"
    )
    logger.info("=" * 60)


def main():
    try:
        if CONSOLIDATE_MODE:
            consolidate()
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
