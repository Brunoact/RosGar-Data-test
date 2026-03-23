"""
🔧 Módulo de Normalización de Vehículos v5.0
=============================================
Normaliza marca, modelo y versión usando diccionarios enriquecidos
generados por generate_normalizer_dicts.py v2.0.

CAMBIOS v5.0 vs v4.1:
──────────────────────
- _find_version() REESCRITO: matching por componentes (motor/trim/trans)
  con pesos adaptativos según lo que el usuario proporcionó
- Decomposición de input del usuario usando mismas constantes que el generador
  (TRANS_TOKENS, DRIVE_TOKENS, BODY_TOKENS, MOTOR_SUFFIX_TOKENS, etc.)
- Look-ahead para distinguir torque VW (250 tsi) de trim (156 mt)
- Aliases de motor/transmisión cargados desde JSON
- Extracción de metadata: puertas, tracción, GNC, carrocería, 0km
- known_trims_by_model para fallback de trim en descripción
- Umbral adaptativo: si el usuario solo pone trim, se acepta con score menor
"""

import re
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════
# 📦 DEPENDENCIAS OPCIONALES
# ═══════════════════════════════════════════════════════════════
try:
    from unidecode import unidecode
    HAS_UNIDECODE = True
except ImportError:
    HAS_UNIDECODE = False
    def unidecode(text: str) -> str:
        for old, new in {'á':'a','é':'e','í':'i','ó':'o','ú':'u',
                         'ä':'a','ë':'e','ï':'i','ö':'o','ü':'u',
                         'ñ':'n','ç':'c','Á':'A','É':'E','Í':'I',
                         'Ó':'O','Ú':'U','Ñ':'N','Ü':'U'}.items():
            text = text.replace(old, new)
        return text

try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 🔧 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
CURRENT_YEAR = datetime.now().year

@dataclass
class NormalizerConfig:
    fuzzy_threshold_marca: int = 85
    fuzzy_threshold_modelo: int = 88
    fuzzy_threshold_version: int = 80
    fuzzy_min_word_length: int = 4
    enable_fuzzy: bool = True
    year_tolerance: int = 2
    year_garbage_threshold: int = 10
    year_max_valid: int = CURRENT_YEAR + 1
    year_min_valid: int = 1940
    km_max_for_current_year: int = 50000
    min_confidence_accept: int = 60
    dicts_path: str = "config/normalizer_dicts.json"
    brand_aliases_path: str = "config/brand_aliases.json"

CONFIG = NormalizerConfig()

# ═══════════════════════════════════════════════════════════════
# 📊 ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizationStats:
    total: int = 0
    full_match: int = 0
    partial_match: int = 0
    marca_only: int = 0
    fallback_used: int = 0
    year_corrected: int = 0
    year_garbage_detected: int = 0
    year_from_description: int = 0
    confusion_detected: int = 0
    motorization_filtered: int = 0
    numeric_exact_only: int = 0
    model_from_description: int = 0
    model_from_version_split: int = 0
    version_component_match: int = 0
    version_trim_match: int = 0
    version_exact_match: int = 0
    by_method: Dict[str, int] = field(default_factory=lambda: {
        'exact': 0, 'alias': 0, 'code_mapping': 0, 'base_name': 0,
        'description': 0, 'version_split': 0, 'fuzzy': 0, 'fallback': 0,
    })
    by_confidence: Dict[str, int] = field(default_factory=lambda: {
        'high': 0, 'medium': 0, 'low': 0, 'very_low': 0,
    })
    def summary(self) -> str:
        if self.total == 0: return "Sin datos"
        p = lambda x: f"{(x/self.total*100):.1f}%"
        return (
            f"Total: {self.total} | Full: {self.full_match} ({p(self.full_match)}) | "
            f"Parcial: {self.partial_match} ({p(self.partial_match)}) | "
            f"Fallback: {self.fallback_used} ({p(self.fallback_used)}) | "
            f"VerComponent: {self.version_component_match} | "
            f"VerTrim: {self.version_trim_match} | "
            f"VerExact: {self.version_exact_match}"
        )

STATS = NormalizationStats()

# ═══════════════════════════════════════════════════════════════
# 🔧 CONSTANTES DE DESCOMPOSICIÓN (idénticas al generador v2.0)
# ═══════════════════════════════════════════════════════════════
TRANS_TOKENS = frozenset({
    'at','mt','cvt','at4','at5','at6','at7','at8','at9','at10',
    'mt5','mt6','mt7','dsg','dct','bva','edc','amt','ddct',
    'tiptronic','sportronic','lineartronic','powershift',
    'stronic','s-tronic','pdk','smg','mct','tct','steptronic',
    'xtronic','x-tronic','ecvt','e-cvt','geartronic','ivt',
    'eat6','eat8','9g-tronic','7g-tronic','8g-tronic',
    'automatica','automatico','manual','secuencial',
})

DRIVE_TOKENS = frozenset({
    '4x4','4x2','2x4','awd','fwd','rwd','2wd','4wd',
    'xdrive','sdrive','4matic','4motion','quattro',
    'all4','q4','e-4orce','s-awc','sawc',
})

DOOR_TOKENS = frozenset({
    '2p','3p','4p','5p','2ptas','3ptas','4ptas','5ptas',
})

BODY_TOKENS = frozenset({
    'cabina','chasis','doble','simple','extendida','pickup','pick-up',
    'sedan','hatchback','hatch','berlina','liftback','fastback',
    'sportback','notchback','rural','sw','wagon','familiar','estate',
    'break','touring','tourer','avant','variant','sportwagon',
    'alltrack','allroad','van','furgon','furgoneta','panel',
    'minivan','monovolumen','multispace','spacetourer',
    'coupe','convertible','cabriolet','cabrio','roadster',
    'spider','spyder','targa','speedster','suv',
})

EQUIPMENT_TOKENS = frozenset({
    'cuero','tela','vinilo','alcantara','full','semifull',
    'gnc','glp','gps','navegador','navi','xenon','bixenon','led',
    'sunroof','moonroof','panoramico','bluetooth','multimedia',
    'abs','esp','climatizador','climatronic','camara','sensores',
    'parking','keyless','airbag','airbags','blindado',
})

MOTOR_SUFFIX_TOKENS = frozenset({
    't','td','turbo','biturbo','twinturbo','twinpower','supercharged',
    'kompressor','intercooler','tdi','tfsi','tsi','fsi','sdi','hdi',
    'bluehdi','vti','thp','puretech','mpi','gdi','tgdi','t-gdi',
    'ts','jts','jtd','jtdm','cdti','cdi','cgi','dci','tce','sce',
    'crdi','tdci','ctdi','mjet','multijet','ecoboost','ecoblue',
    'duratec','zetec','ecotec','duramax','vortec','flexpower',
    'multiair','twinair','fire','firefly','etorq','e.torq',
    'vtec','i-vtec','ivtec','i-dtec','idtec',
    'skyactiv','skyactiv-g','skyactiv-d','skyactiv-x',
    'mivec','di-d','did','valvetronic','panther','puma',
    'mwm','maxion','perkins','cummins','sprint','energy',
    'boxer','flat','vvt-i','vvti','d-4d','d4d',
    'diesel','nafta','naftero','gasolina','aspirado','atmosferico',
    'hev','phev','mhev','fhev','ev','bev','fcev',
    'epower','e-power','e-tech','etech','hybrid','hibrido',
    'e:hev','bluetec','plugin','plug-in','lfp',
    'v4','v6','v8','v10','v12','l3','l4','l5','l6',
    'i3','i4','i6','w12','w16',
    '8v','12v','16v','20v','24v','32v',
    'dohc','sohc','ohv','ohc','vvt',
    'sdv','iava','bio','ie','efi','d','i',
})

BARE_NUMBER_MOTOR_TRIGGERS = frozenset({
    'tsi','tfsi','tdi','fsi','mpi','gdi','tgdi','cv','hp','bhp','ps',
    'turbo','diesel','amg','kompressor','hdi','bluehdi','dci','tce',
    'ecoboost','ecoblue','mjet','multijet','jtd','jtdm','crdi',
    'cdti','cdi','tdci','vtec','mivec',
})

# ═══════════════════════════════════════════════════════════════
# 🏷️ ALIASES DE MARCAS
# ═══════════════════════════════════════════════════════════════
DEFAULT_BRAND_ALIASES = {
    'vw': 'volkswagen', 'volks': 'volkswagen', 'volkswagon': 'volkswagen',
    'mercedes': 'mercedes-benz', 'mercedes benz': 'mercedes-benz',
    'mercedesbenz': 'mercedes-benz', 'mb': 'mercedes-benz', 'benz': 'mercedes-benz',
    'chevy': 'chevrolet', 'gm': 'chevrolet',
    'alfa': 'alfa romeo', 'landrover': 'land rover',
    'citroen': 'citroën', 'citröen': 'citroën',
}

# ═══════════════════════════════════════════════════════════════
# 🚫 BLACKLISTS
# ═══════════════════════════════════════════════════════════════
FUZZY_BLACKLIST = {
    'ano','año','años','anio','nafta','naftero','diesel','gasoil','gnc','gas',
    'hibrido','electrico','manual','automatico','automatica','secuencial','cvt','dsg',
    'muy','buen','bueno','buena','mal','estado','impecable','excelente','perfecto',
    'perfecta','inmaculado','nuevo','nueva','usado','unico','unica','dueno','dueño',
    'titular','particular','full','tope','gama','equipo','equipado','completo','cuero',
    'techo','permuto','permuta','financio','vendo','venta','contado',
    'auto','autos','coche','vehiculo','carro','motor','caja','cambios',
    'vtv','papeles','patente','seguro','service','consultar','precio','oferta',
    'urgente','oportunidad','original','funcionando','kilometros','kms','litros',
    'puertas','modelo','version','serie','linea','tipo',
    'funciona','todo','anda','tiene','soy','directo','transferencia',
    'pesos','dolares','usd','efectivo',
}

MOTORIZATION_PATTERNS = [
    r'^\d+\.\d+$', r'^\d+\.\d+\s*[lt]?$',
    r'^\d+\.\d+\s*(tdi|tfsi|hdi|vti|thp|jtd|cdti|dci|tsi|fsi|mpi|ts|jts|jtdm|crdi)i?$',
    r'^\d+\.\d+\s*(turbo|diesel|nafta|naftero|multijet|bluehdi|ecoboost)$',
    r'^\d+(v|cv|hp|bhp)$', r'^(v6|v8|v10|v12|i4|i6|l4|l6|w12|w16)$',
    r'^\d{3,4}\s*(cc)?$', r'^motor\s+\d', r'^\d+\.\d+\s+\d+v$', r'^\d+v$',
]

# ═══════════════════════════════════════════════════════════════
# 🔥 URGENCY KEYWORDS
# ═══════════════════════════════════════════════════════════════
URGENCY_KEYWORDS = {
    'urgente': 30, 'urge': 30, 'urgencia': 30, 'viajo': 25, 'viaje': 25,
    'me voy': 25, 'mudanza': 25, 'oportunidad': 20, 'negociable': 15,
    'escucho': 15, 'escucho ofertas': 20, 'acepto oferta': 20,
    'vendo ya': 20, 'venta rapida': 20, 'liquido': 20,
    'financio': 10, 'permuto': 10, 'contado': 10,
    'rebajado': 15, 'rebaja': 15,
    'precio final': -20, 'no negociable': -25, 'firme': -15,
}

# ═══════════════════════════════════════════════════════════════
# 🛠️ FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════
def normalize_text(text: str) -> str:
    if not text: return ""
    text = str(text).lower().strip()
    text = unidecode(text)
    text = re.sub(r'[^\w\s\-]', ' ', text)
    return ' '.join(text.split())

def normalize_key(text: str) -> str:
    if not text: return ""
    return normalize_text(text)

def make_key(marca: str, modelo: str) -> str:
    return f"{normalize_key(marca)}|{normalize_key(modelo)}"

def find_exact_in_text(needle: str, haystack: str) -> bool:
    if not needle or not haystack: return False
    return bool(re.search(r'\b' + re.escape(needle) + r'\b', haystack, re.IGNORECASE))

def is_numeric_model(modelo: str) -> bool:
    m = normalize_key(modelo)
    return bool(re.match(r'^\d{2,5}$', m) or re.match(r'^[a-z]\d{1,3}$', m))

def is_motorization_pattern(text: str) -> bool:
    if not text: return False
    t = normalize_key(text)
    return any(re.match(p, t) for p in MOTORIZATION_PATTERNS)

def fuzzy_match(needle, candidates, threshold=85):
    if not HAS_RAPIDFUZZ or not needle or not candidates: return None
    if len(needle) < CONFIG.fuzzy_min_word_length: return None
    if needle.lower() in FUZZY_BLACKLIST: return None
    try:
        result = process.extractOne(needle, candidates, scorer=fuzz.ratio, score_cutoff=threshold)
        return (result[0], result[1]) if result else None
    except: return None

# ═══════════════════════════════════════════════════════════════
# 🔩 FUNCIONES DE DESCOMPOSICIÓN DE INPUT DE USUARIO
# ═══════════════════════════════════════════════════════════════
def _is_year_phase_token(token: str) -> bool:
    t = token.lower()
    if t in ('linea', 'fase', 'phase', 'facelift', 'restyling', 'lci'):
        return True
    return bool(re.match(r'^(my|l|mk|g|ph|gen|fase|phase)\.?\d{1,4}(\.\d)?$', t))

def _is_motor_start(token: str, next_token: str = None) -> bool:
    t = token.lower()
    if re.match(r'^\d+\.\d+[a-z]*$', t): return True
    if re.match(r'^t\d{3}$', t): return True
    if t in ('ev','bev','phev','hev','mhev','fhev','fcev',
             'e-power','epower','e-tech','etech','hybrid','hibrido'):
        return True
    if t in ('v6','v8','v10','v12','l3','l4','l6','i3','i4','i6','w12','w16'):
        return True
    if t in ('skyactiv','skyactiv-g','skyactiv-d','skyactiv-x',
             'ecoboost','ecoblue','ecotec','duramax','multijet',
             'multiair','twinair','boxer','puretech','bluehdi',
             'firefly','fire','vtec','i-vtec','mivec'):
        return True
    if re.match(r'^\d{2,3}$', t) and next_token:
        if next_token.lower() in BARE_NUMBER_MOTOR_TRIGGERS:
            return True
    return False

def decompose_user_input(text: str) -> Dict[str, Any]:
    """
    Descompone input de usuario en componentes.
    Misma lógica que el generador pero retorna metadata adicional.
    """
    result = {
        'motor': None, 'trim': None, 'trans': None,
        'traccion': None, 'puertas': None, 'carroceria': None,
        'tiene_gnc': None, 'body_tokens': [], 'equipment_tokens': [],
    }
    if not text: return result

    # Pre-process
    text = text.replace('a/t', 'at').replace('m/t', 'mt')

    # GNC detection before tokenizing
    t_lower = text.lower()
    if re.search(r'\bs(?:in)?[/\s]gnc\b', t_lower):
        result['tiene_gnc'] = False
    elif re.search(r'\bgnc\b|\bgas\s*natural\b|\bc(?:on)?[/\s]gnc\b', t_lower):
        result['tiene_gnc'] = True

    tokens = text.lower().split()
    if not tokens: return result

    motor_parts, trim_parts, body_parts, equip_parts = [], [], [], []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        next_t = tokens[i + 1] if i + 1 < len(tokens) else None

        if t in TRANS_TOKENS and result['trans'] is None:
            result['trans'] = t; i += 1; continue
        if t in DRIVE_TOKENS and result['traccion'] is None:
            result['traccion'] = t; i += 1; continue
        if t in DOOR_TOKENS and result['puertas'] is None:
            result['puertas'] = t; i += 1; continue
        if t in BODY_TOKENS:
            body_parts.append(t); i += 1; continue
        if t in EQUIPMENT_TOKENS:
            if t in ('gnc', 'glp'):
                result['tiene_gnc'] = True
            equip_parts.append(t); i += 1; continue
        if _is_year_phase_token(t):
            i += 1; continue
        if _is_motor_start(t, next_t):
            motor_parts.append(t); i += 1
            while i < len(tokens) and tokens[i] in MOTOR_SUFFIX_TOKENS:
                motor_parts.append(tokens[i]); i += 1
            continue
        trim_parts.append(t); i += 1

    if motor_parts: result['motor'] = ' '.join(motor_parts)
    if trim_parts: result['trim'] = ' '.join(trim_parts)
    if body_parts:
        result['carroceria'] = ' '.join(body_parts)
        result['body_tokens'] = body_parts
    result['equipment_tokens'] = equip_parts
    return result

def _motors_compatible(cat_motor: str, user_motor: str) -> bool:
    """1.4t matches 1.4, 2.8 td matches 2.8."""
    if not cat_motor or not user_motor: return False
    cd = re.match(r'(\d+\.\d+)', cat_motor)
    ud = re.match(r'(\d+\.\d+)', user_motor)
    if cd and ud: return cd.group(1) == ud.group(1)
    return cat_motor in user_motor or user_motor in cat_motor

def _trans_compatible(cat_trans: str, user_trans: str) -> bool:
    """at (generic) matches at6 (specific)."""
    if not cat_trans or not user_trans: return False
    if user_trans == 'at' and cat_trans.startswith('at'): return True
    if user_trans == 'mt' and cat_trans.startswith('mt'): return True
    if cat_trans == 'at' and user_trans.startswith('at'): return True
    if cat_trans == 'mt' and user_trans.startswith('mt'): return True
    return False

def _detect_gnc(text: str) -> Optional[bool]:
    if not text: return None
    t = text.lower()
    if re.search(r'\bs(?:in)?[/\s]gnc\b', t): return False
    if re.search(r'\bgnc\b|\bgas\s*natural\b|\bc(?:on)?[/\s]gnc\b', t): return True
    return None

def _detect_0km(km, text: str) -> bool:
    if km is not None and km == 0 and text:
        return bool(re.search(r'\b0\s*km\b|\bcero\s*km\b|\bokm\b', text.lower()))
    return False

# ═══════════════════════════════════════════════════════════════
# 📅 FUNCIONES DE AÑO
# ═══════════════════════════════════════════════════════════════
def extract_year_from_text(text: str) -> Optional[int]:
    if not text: return None
    matches = re.findall(r'\b(19[4-9]\d|20[0-2]\d)\b', text)
    if not matches: return None
    years = [int(y) for y in matches if int(y) <= CONFIG.year_max_valid]
    return max(years) if years else None

def extract_year_from_description(text: str) -> Optional[int]:
    if not text: return None
    text_lower = text.lower()
    for pat in [r'\bmod(?:elo)?\.?\s+(\d{2})\b', r'\bdel\s+(?:año\s+)?(\d{2})\b', r'\baño\s+(\d{2})\b']:
        m = re.search(pat, text_lower)
        if m:
            y = int(m.group(1))
            if 40 <= y <= 99: return 1900 + y
            elif 0 <= y <= 35: return 2000 + y
    for pat in [r'\bmod(?:elo)?\.?\s+((?:19|20)\d{2})\b', r'\baño\s+((?:19|20)\d{2})\b', r'\bdel\s+((?:19|20)\d{2})\b']:
        m = re.search(pat, text_lower)
        if m:
            y = int(m.group(1))
            if CONFIG.year_min_valid <= y <= CONFIG.year_max_valid: return y
    return None

def assess_year_quality(año_structured=None, año_description=None, km=None, model_year_range=None):
    result = {'quality': 'valid', 'best_year': año_structured, 'year_source': 'structured', 'reason': None}
    if not año_structured:
        if año_description:
            result.update(best_year=año_description, year_source='description',
                          reason='Año de descripción (sin año estructurado)')
        else:
            result.update(quality='missing', reason='Sin año')
        return result
    if año_structured > CONFIG.year_max_valid:
        result['quality'] = 'garbage'
        result['reason'] = f"Año {año_structured} futuro"
        if año_description and CONFIG.year_min_valid <= año_description <= CONFIG.year_max_valid:
            result.update(best_year=año_description, year_source='description')
        else:
            result['best_year'] = None
        return result
    if año_structured < CONFIG.year_min_valid:
        result['quality'] = 'garbage'
        result['reason'] = f"Año {año_structured} < {CONFIG.year_min_valid}"
        result['best_year'] = año_description if (año_description and CONFIG.year_min_valid <= año_description <= CONFIG.year_max_valid) else None
        if result['best_year']: result['year_source'] = 'description'
        return result
    if km and km > CONFIG.km_max_for_current_year and año_structured >= CURRENT_YEAR:
        result.update(quality='suspicious', reason=f"Año {año_structured} con {km:,} km sospechoso")
        if año_description and año_description < año_structured:
            result.update(best_year=año_description, year_source='description')
    if model_year_range:
        desde, hasta = model_year_range.get('desde', 0), model_year_range.get('hasta', 9999)
        dist = max(0, desde - año_structured) if año_structured < desde else max(0, año_structured - hasta)
        if dist > CONFIG.year_garbage_threshold:
            result['quality'] = 'garbage'
            result['reason'] = f"Año {año_structured} a {dist} años de {desde}-{hasta}"
            if año_description and CONFIG.year_min_valid <= año_description <= CONFIG.year_max_valid:
                result.update(best_year=año_description, year_source='description')
            else:
                result['best_year'] = None
    if result['quality'] == 'valid' and año_description and año_structured and abs(año_structured - año_description) > 20:
        result.update(quality='suspicious', reason=f"Año struct {año_structured} ≠ desc {año_description}")
    return result

# ═══════════════════════════════════════════════════════════════
# 📝 MINERÍA DE DESCRIPCIÓN
# ═══════════════════════════════════════════════════════════════
def extract_model_from_description(descripcion, marca, catalog_models):
    if not descripcion or not catalog_models: return None
    desc_norm = normalize_text(descripcion)
    if marca:
        desc_norm = re.sub(r'\b' + re.escape(normalize_key(marca)) + r'\b', ' ', desc_norm)
        desc_norm = re.sub(r'\b' + re.escape(normalize_key(marca).replace('-',' ')) + r'\b', ' ', desc_norm)
    for modelo in catalog_models:
        if find_exact_in_text(modelo, desc_norm):
            pattern = r'(?:^|\s)(?!motor\s)' + re.escape(modelo) + r'\b'
            if re.search(pattern, desc_norm):
                return modelo
    return None

def split_version_into_model_and_trim(version_raw, catalog_models, marca=""):
    if not version_raw or not catalog_models: return None
    vn = normalize_text(version_raw)
    if marca:
        mn = normalize_key(marca)
        if vn.startswith(mn): vn = vn[len(mn):].strip()
        mn_nh = mn.replace('-', ' ')
        if vn.startswith(mn_nh): vn = vn[len(mn_nh):].strip()
    if not vn: return None
    for modelo in catalog_models:
        if vn == modelo: return {'model': modelo, 'trim': ''}
        if vn.startswith(modelo + ' '):
            trim = vn[len(modelo):].strip()
            return {'model': modelo, 'trim': trim if trim and not is_motorization_pattern(trim) else ''}
        if find_exact_in_text(modelo, vn):
            trim = re.sub(r'\b' + re.escape(modelo) + r'\b', '', vn).strip()
            return {'model': modelo, 'trim': trim if trim and not is_motorization_pattern(trim) else ''}
    return None

# ═══════════════════════════════════════════════════════════════
# 🔥 DETECCIÓN DE URGENCIA
# ═══════════════════════════════════════════════════════════════
def extract_urgency_signals(text: str) -> Dict:
    if not text: return {'has_urgency': False, 'score': 0, 'keywords_found': []}
    text_lower, text_norm = text.lower(), normalize_text(text)
    score, kw = 0, []
    for keyword, points in URGENCY_KEYWORDS.items():
        if find_exact_in_text(keyword, text_norm) or keyword in text_lower:
            score += points
            if points > 0: kw.append(keyword)
    score = max(0, min(100, score))
    return {'has_urgency': score >= 15, 'score': score, 'keywords_found': kw}

# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO ENRIQUECIDO (actualizado v5.0)
# ═══════════════════════════════════════════════════════════════
class EnrichedCatalog:
    def __init__(self):
        self._data: Dict = {}
        self._brand_aliases: Dict[str, str] = {}
        self._loaded = False

    def load(self, dicts_path: str, brand_aliases_path: str = None) -> bool:
        if not os.path.exists(dicts_path):
            logger.warning(f"⚠️ No encontrado: {dicts_path}")
            return False
        try:
            with open(dicts_path, 'r', encoding='utf-8') as f:
                self._data = json.load(f)
            self._loaded = True
            for alias, brand in DEFAULT_BRAND_ALIASES.items():
                self._brand_aliases[normalize_key(alias)] = normalize_key(brand)
            if brand_aliases_path and os.path.exists(brand_aliases_path):
                with open(brand_aliases_path, 'r', encoding='utf-8') as f:
                    extra = json.load(f)
                for alias, brand in extra.get('brand_aliases', {}).items():
                    self._brand_aliases[normalize_key(alias)] = normalize_key(brand)
            meta = self._data.get('_metadata', {})
            logger.info(
                f"✅ Catálogo v{meta.get('generator_version','?')}: "
                f"{meta.get('total_brands',0)} marcas, "
                f"{meta.get('total_models',0)} modelos, "
                f"{meta.get('total_versions',0)} versiones, "
                f"{meta.get('total_version_components',0)} descompuestas"
            )
            return True
        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False

    @property
    def is_loaded(self): return self._loaded
    @property
    def catalog_index(self): return self._data.get('catalog_index', {})
    @property
    def code_mappings(self): return self._data.get('model_code_to_catalog', {})
    @property
    def model_aliases(self): return self._data.get('model_aliases', {})
    @property
    def year_ranges(self): return self._data.get('year_ranges', {})
    @property
    def known_confusions(self): return self._data.get('known_confusions', {})
    @property
    def succession_map(self): return self._data.get('succession_map', {})
    @property
    def known_motorizations(self): return self._data.get('known_motorizations', {})
    @property
    def motorization_blacklist(self): return self._data.get('motorization_blacklist', {})
    @property
    def valid_versions(self): return self._data.get('valid_versions', {})
    @property
    def validation_rules(self): return self._data.get('validation_rules', {})
    @property
    def vehicle_classification(self): return self._data.get('vehicle_classification', {})
    @property
    def model_parent_map(self): return self._data.get('model_parent_map', {})
    # ── NUEVOS v5.0 ──
    @property
    def version_components(self): return self._data.get('version_components', {})
    @property
    def motor_aliases_lookup(self): return self._data.get('motor_aliases_lookup', {})
    @property
    def trans_aliases_lookup(self): return self._data.get('trans_aliases_lookup', {})
    @property
    def drive_aliases_lookup(self): return self._data.get('drive_aliases_lookup', {})
    @property
    def trim_index(self): return self._data.get('trim_index', {})
    @property
    def known_trims_by_model(self): return self._data.get('known_trims_by_model', {})

    def resolve_brand(self, brand_raw):
        b = normalize_key(brand_raw)
        if b in self._brand_aliases: return self._brand_aliases[b]
        compact = b.replace(' ', '').replace('-', '')
        if compact in self._brand_aliases: return self._brand_aliases[compact]
        return b
    def has_brand(self, brand): return normalize_key(brand) in self.catalog_index
    def get_brands(self): return sorted(self.catalog_index.keys(), key=len, reverse=True)
    def get_models(self, brand): return sorted(self.catalog_index.get(normalize_key(brand), {}).keys(), key=len, reverse=True)
    def has_model(self, brand, model): return normalize_key(model) in self.catalog_index.get(normalize_key(brand), {})
    def get_versions(self, brand, model): return self.valid_versions.get(make_key(brand, model), [])
    def get_year_range(self, brand, model): return self.year_ranges.get(make_key(brand, model))
    def get_year_suggestion(self, brand, model, year):
        sug = self.validation_rules.get('year_mismatch_suggestions', {}).get(make_key(brand, model))
        if not sug: return None
        r = sug.get('rango_valido', {})
        if year > r.get('hasta', 9999): return sug.get('si_año_posterior')
        elif year < r.get('desde', 0): return sug.get('si_año_anterior')
        return None
    def resolve_model_alias(self, brand, alias): return self.model_aliases.get(normalize_key(brand), {}).get(normalize_key(alias))
    def get_confusions(self, brand, model): return self.known_confusions.get(make_key(brand, model), [])
    def get_successor(self, brand, model): return self.succession_map.get(normalize_key(brand), {}).get(normalize_key(model), {}).get('sucesor')
    def get_predecessor(self, brand, model): return self.succession_map.get(normalize_key(brand), {}).get(normalize_key(model), {}).get('predecesor')

CATALOG = EnrichedCatalog()

# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR v5.0
# ═══════════════════════════════════════════════════════════════
class VehicleNormalizerV4:
    def __init__(self, catalog: EnrichedCatalog = None):
        self.catalog = catalog or CATALOG

    def normalize(self, titulo="", descripcion="", marca_raw="",
                  modelo_raw="", version_raw="",
                  año_raw=None, precio_raw=None, km_raw=None) -> Dict:
        global STATS
        STATS.total += 1

        titulo_n = normalize_text(titulo)
        desc_n = normalize_text(descripcion[:1000] if descripcion else "")
        marca_n = normalize_text(marca_raw)
        modelo_n = normalize_text(modelo_raw)
        version_n = normalize_text(version_raw)
        text_high = f"{titulo_n} {marca_n}"
        text_medium = f"{version_n} {modelo_n}"
        text_low = desc_n
        text_all = f"{text_high} {text_medium} {text_low}"

        result = {
            'marca': None, 'modelo': None, 'version': None,
            'año_detectado': None, 'año_fuente': None, 'año_sospechoso': False,
            'norm_status': 'pending', 'from_catalog': False,
            'match_method': None, 'confidence': 0, 'confidence_level': 'very_low',
            'version_method': None, 'version_score': 0,
            'extracted_data': {
                'puertas': None, 'traccion': None, 'carroceria': None,
                'tiene_gnc': None, 'es_0km': False,
            },
            'warnings': [], 'corrections': [],
        }

        # ═══ PASO 0: AÑO ═══
        año_desc = extract_year_from_description(descripcion or "") or extract_year_from_description(titulo or "")
        año_q = assess_year_quality(año_structured=año_raw, año_description=año_desc, km=km_raw)
        año = año_q['best_year']
        result['año_detectado'] = año
        result['año_fuente'] = año_q['year_source']
        if año_q['quality'] == 'garbage':
            result['año_sospechoso'] = True
            result['warnings'].append(f"Año basura: {año_q['reason']}")
            STATS.year_garbage_detected += 1
            if año_q['year_source'] == 'description': STATS.year_from_description += 1
        elif año_q['quality'] == 'suspicious':
            result['año_sospechoso'] = True
            result['warnings'].append(f"Año sospechoso: {año_q['reason']}")
        if not año:
            año = extract_year_from_text(titulo_n)
            if año: result.update(año_detectado=año, año_fuente='title')
            else:
                año = extract_year_from_text(text_all)
                if año: result.update(año_detectado=año, año_fuente='text')

        # ═══ PASO 1: MARCA ═══
        marca_result = self._find_marca(marca_n, text_all)
        if not marca_result:
            STATS.fallback_used += 1
            result.update(norm_status='no_marca', confidence=5, confidence_level='very_low')
            return result
        result['marca'] = marca_result['value']
        result['from_catalog'] = marca_result['from_catalog']
        marca = result['marca']

        # ═══ PASO 2: FILTRAR MOTORIZACIONES ═══
        for cand in [modelo_n, version_n]:
            if cand and self._is_motorization(cand, marca):
                STATS.motorization_filtered += 1
                result['warnings'].append(f"'{cand}' filtrado como motorización")

        # ═══ PASO 3: MODELO (7 estrategias) ═══
        modelo_result = self._find_modelo(
            marca=marca, titulo=titulo_n, modelo_raw=modelo_n,
            version_raw=version_n, descripcion=desc_n,
            text_high=text_high, text_medium=text_medium, text_all=text_all)
        if modelo_result:
            result['modelo'] = modelo_result['value']
            result['match_method'] = modelo_result['method']
            if modelo_result['from_catalog']: result['from_catalog'] = True
            STATS.by_method[modelo_result['method']] = STATS.by_method.get(modelo_result['method'], 0) + 1

        # ═══ PASO 4: VERSIÓN (REESCRITO v5.0) ═══
        if result['from_catalog'] and result['modelo']:
            ver_result = self._find_version(
                marca=marca, modelo=result['modelo'],
                version_raw=version_n, search_text=text_all)
            if ver_result:
                result['version'] = ver_result['value']
                result['version_method'] = ver_result.get('method')
                result['version_score'] = ver_result.get('score', 0)
                # Copiar metadata extraída durante descomposición
                if 'extracted' in ver_result:
                    for k, v in ver_result['extracted'].items():
                        if v is not None:
                            result['extracted_data'][k] = v

        # ═══ PASO 4.5: METADATA ADICIONAL ═══
        gnc = _detect_gnc(text_all)
        if gnc is not None:
            result['extracted_data']['tiene_gnc'] = gnc
        result['extracted_data']['es_0km'] = _detect_0km(km_raw, text_all)

        # ═══ PASO 5: POST-VALIDACIÓN ═══
        if result['from_catalog'] and result['modelo']:
            self._post_validate(result, marca, año)

        # ═══ PASO 6: CONFIDENCE ═══
        self._calculate_confidence(result)

        # ═══ PASO 7: STATUS ═══
        self._set_final_status(result)
        return result

    # ─────────────────────────────────────────────
    # MARCA
    # ─────────────────────────────────────────────
    def _find_marca(self, marca_raw, text):
        if not self.catalog.is_loaded:
            return {'value': marca_raw, 'from_catalog': False, 'method': 'fallback'} if marca_raw else None
        if marca_raw:
            resolved = self.catalog.resolve_brand(marca_raw)
            if self.catalog.has_brand(resolved):
                return {'value': resolved, 'from_catalog': True, 'method': 'exact'}
        for brand in self.catalog.get_brands():
            if find_exact_in_text(brand, text):
                return {'value': brand, 'from_catalog': True, 'method': 'text_search'}
        if CONFIG.enable_fuzzy and marca_raw:
            match = fuzzy_match(marca_raw, self.catalog.get_brands(), CONFIG.fuzzy_threshold_marca)
            if match: return {'value': match[0], 'from_catalog': True, 'method': 'fuzzy'}
        return {'value': marca_raw, 'from_catalog': False, 'method': 'fallback'} if marca_raw else None

    # ─────────────────────────────────────────────
    # MOTORIZACIÓN FILTER
    # ─────────────────────────────────────────────
    def _is_motorization(self, text, marca):
        tn = normalize_key(text)
        bl = self.catalog.motorization_blacklist
        if bl:
            if tn in bl.get('exact', []): return True
            for p in bl.get('regex', []):
                try:
                    if re.match(p, tn): return True
                except re.error: pass
        if tn in self.catalog.known_motorizations.get(normalize_key(marca), {}): return True
        return is_motorization_pattern(tn)

    # ─────────────────────────────────────────────
    # MODELO (7 estrategias — sin cambios)
    # ─────────────────────────────────────────────
    def _find_modelo(self, marca, titulo, modelo_raw, version_raw, descripcion, text_high, text_medium, text_all):
        mn = normalize_key(marca)
        modelos = self.catalog.get_models(marca)
        if not modelos: return self._fallback_modelo(modelo_raw, version_raw, titulo, marca)
        combined = f"{version_raw} {modelo_raw} {titulo} {text_all}"

        # 1. Exacto
        for m in modelos:
            if find_exact_in_text(m, combined):
                return {'value': m, 'from_catalog': True, 'method': 'exact'}
        # 2. Alias
        aliases = self.catalog.model_aliases.get(mn, {})
        for alias, real in aliases.items():
            if find_exact_in_text(alias, combined):
                if self.catalog.has_model(mn, real):
                    return {'value': real, 'from_catalog': True, 'method': 'alias'}
        # 3. Códigos
        codes = self.catalog.code_mappings.get(mn, {})
        if codes:
            for code in sorted(codes.keys(), key=len, reverse=True):
                if find_exact_in_text(code, combined):
                    mapped = codes[code]
                    if isinstance(mapped, dict):
                        if mapped.get('ambiguo'): mapped_model = mapped.get('modelos', [None])[0]
                        else: continue
                    else: mapped_model = mapped
                    if mapped_model and self.catalog.has_model(marca, mapped_model):
                        return {'value': mapped_model, 'from_catalog': True, 'method': 'code_mapping'}
        # 4. Nombre base
        for src in [version_raw, modelo_raw, titulo]:
            if src:
                for w in src.split():
                    if len(w) >= 3 and not re.match(r'^\d+$', w):
                        wc = re.sub(r'\d+', '', w).strip()
                        if wc and len(wc) >= 3:
                            for m in modelos:
                                if wc == m or find_exact_in_text(wc, m):
                                    return {'value': m, 'from_catalog': True, 'method': 'base_name'}
        # 5. Descripción
        if descripcion:
            md = extract_model_from_description(descripcion, marca, modelos)
            if md:
                STATS.model_from_description += 1
                return {'value': md, 'from_catalog': True, 'method': 'description'}
        # 6. Version split
        if version_raw:
            sp = split_version_into_model_and_trim(version_raw, modelos, marca)
            if sp:
                STATS.model_from_version_split += 1
                return {'value': sp['model'], 'from_catalog': True, 'method': 'version_split'}
        # 7. Fuzzy
        if CONFIG.enable_fuzzy and modelos:
            fc = [m for m in modelos if not is_numeric_model(m)]
            if not fc: STATS.numeric_exact_only += 1
            else:
                for w in combined.split():
                    if len(w) >= CONFIG.fuzzy_min_word_length and w.lower() not in FUZZY_BLACKLIST and not re.match(r'^(19|20)\d{2}$', w) and not re.match(r'^\d+$', w):
                        match = fuzzy_match(w, fc, CONFIG.fuzzy_threshold_modelo)
                        if match: return {'value': match[0], 'from_catalog': True, 'method': 'fuzzy'}
        # 8. Fallback
        return self._fallback_modelo(modelo_raw, version_raw, titulo, marca)

    def _fallback_modelo(self, modelo_raw, version_raw, titulo, marca):
        combined = f"{version_raw} {modelo_raw} {titulo}"
        if marca:
            combined = re.sub(r'\b' + re.escape(marca) + r'\b', '', combined, flags=re.IGNORECASE)
            combined = re.sub(r'\b' + re.escape(marca.replace('-',' ')) + r'\b', '', combined, flags=re.IGNORECASE)
        for w in combined.split():
            w = w.strip()
            if len(w) >= 2 and not re.match(r'^(19|20)\d{2}$', w) and w.lower() not in FUZZY_BLACKLIST and not is_motorization_pattern(w):
                return {'value': w, 'from_catalog': False, 'method': 'fallback'}
        return None

    # ─────────────────────────────────────────────────
    # VERSIÓN — REESCRITO v5.0 (matching por componentes)
    # ─────────────────────────────────────────────────
    def _find_version(self, marca, modelo, version_raw, search_text) -> Optional[Dict]:
        versiones = self.catalog.get_versions(marca, modelo)
        if not versiones:
            return None

        key = make_key(marca, modelo)
        cat_components = self.catalog.version_components.get(key, {})
        search_norm = normalize_text(search_text)
        versiones_sorted = sorted(versiones, key=len, reverse=True)

        # ── 1. Match exacto completo ──
        for v in versiones_sorted:
            if find_exact_in_text(v, search_norm):
                STATS.version_exact_match += 1
                return {'value': v, 'from_catalog': True, 'method': 'exact', 'score': 100}

        # ── 2. Descomponer input de usuario ──
        clean_input = self._clean_version_input(version_raw, marca, modelo)
        user_comp = self._decompose_and_normalize(clean_input, search_norm)

        # ── 3. Scoring por componentes ──
        if cat_components and (user_comp.get('trim') or user_comp.get('motor_canonical')):
            best_match, best_score, best_detail = None, 0, None
            for version, cc in cat_components.items():
                score, detail = self._score_version_components(user_comp, cc)
                if score > best_score:
                    best_score, best_match, best_detail = score, version, detail

            threshold = self._adaptive_threshold(user_comp)
            if best_match and best_score >= threshold:
                STATS.version_component_match += 1
                return {
                    'value': best_match, 'from_catalog': True,
                    'method': 'component_match', 'score': best_score,
                    'extracted': {
                        'puertas': user_comp.get('puertas'),
                        'traccion': user_comp.get('traccion'),
                        'carroceria': user_comp.get('carroceria'),
                        'tiene_gnc': user_comp.get('tiene_gnc'),
                    },
                }

        # ── 4. Trim en search_text (descripción, título) ──
        known_trims = self.catalog.known_trims_by_model.get(key, [])
        if known_trims:
            for trim in sorted(known_trims, key=len, reverse=True):
                if len(trim) >= 2 and find_exact_in_text(trim, search_norm):
                    # Buscar versión con ese trim
                    for version, cc in cat_components.items():
                        if cc.get('trim') == trim:
                            STATS.version_trim_match += 1
                            return {
                                'value': version, 'from_catalog': True,
                                'method': 'trim_from_text', 'score': 60,
                                'extracted': {
                                    'puertas': user_comp.get('puertas'),
                                    'traccion': user_comp.get('traccion'),
                                    'carroceria': user_comp.get('carroceria'),
                                    'tiene_gnc': user_comp.get('tiene_gnc'),
                                },
                            }

        # ── 5. Trim en trim_index de la marca ──
        if user_comp.get('trim'):
            brand_trims = self.catalog.trim_index.get(normalize_key(marca), {})
            modelo_norm = normalize_key(modelo)
            trim_val = user_comp['trim']
            if trim_val in brand_trims and modelo_norm in brand_trims[trim_val]:
                for version, cc in cat_components.items():
                    if cc.get('trim') == trim_val:
                        STATS.version_trim_match += 1
                        return {
                            'value': version, 'from_catalog': True,
                            'method': 'trim_index', 'score': 55,
                            'extracted': {
                                'puertas': user_comp.get('puertas'),
                                'traccion': user_comp.get('traccion'),
                                'carroceria': user_comp.get('carroceria'),
                                'tiene_gnc': user_comp.get('tiene_gnc'),
                            },
                        }

        # ── 6. Fuzzy sobre versiones (restrictivo) ──
        if CONFIG.enable_fuzzy:
            for w in search_norm.split():
                if len(w) >= 3 and w not in FUZZY_BLACKLIST:
                    m = fuzzy_match(w, versiones, CONFIG.fuzzy_threshold_version)
                    if m: return {'value': m[0], 'from_catalog': True, 'method': 'fuzzy', 'score': m[1]}

        return None

    def _clean_version_input(self, version_raw, marca, modelo):
        """Limpia version_raw quitando marca y modelo."""
        text = normalize_text(version_raw) if version_raw else ""
        if marca:
            text = re.sub(r'\b' + re.escape(normalize_key(marca)) + r'\b', '', text).strip()
            text = re.sub(r'\b' + re.escape(normalize_key(marca).replace('-',' ')) + r'\b', '', text).strip()
        if modelo:
            text = re.sub(r'\b' + re.escape(normalize_key(modelo)) + r'\b', '', text).strip()
        return ' '.join(text.split())

    def _decompose_and_normalize(self, clean_input, search_text=""):
        """Descompone y normaliza aliases de motor/transmisión."""
        comp = decompose_user_input(clean_input or "")

        # Si no se encontró trim/motor en version_raw, intentar en search_text
        if not comp.get('trim') and not comp.get('motor') and search_text:
            comp2 = decompose_user_input(search_text[:200])
            if comp2.get('trim') and not comp.get('trim'):
                comp['trim'] = comp2['trim']
            if comp2.get('motor') and not comp.get('motor'):
                comp['motor'] = comp2['motor']
            if comp2.get('trans') and not comp.get('trans'):
                comp['trans'] = comp2['trans']

        # Normalizar motor via aliases
        if comp.get('motor'):
            canon = self.catalog.motor_aliases_lookup.get(comp['motor'])
            comp['motor_canonical'] = canon if canon else comp['motor']
        else:
            comp['motor_canonical'] = None

        # Normalizar trans via aliases
        if comp.get('trans'):
            canon = self.catalog.trans_aliases_lookup.get(comp['trans'])
            comp['trans_canonical'] = canon if canon else comp['trans']
        else:
            comp['trans_canonical'] = None

        # Normalizar traccion via aliases
        if comp.get('traccion'):
            canon = self.catalog.drive_aliases_lookup.get(comp['traccion'])
            if canon: comp['traccion'] = canon

        return comp

    def _score_version_components(self, user_comp, cat_comp):
        """Puntúa match por componentes con pesos adaptativos."""
        score = 0.0
        detail = {}
        has_trim = bool(user_comp.get('trim'))
        has_motor = bool(user_comp.get('motor_canonical'))
        has_trans = bool(user_comp.get('trans_canonical'))

        count = sum([has_trim, has_motor, has_trans])
        if count == 0: return 0, {}

        # Pesos adaptativos
        w = {'trim': 0, 'motor': 0, 'trans': 0}
        if has_trim and not has_motor and not has_trans:
            w['trim'] = 100
        elif has_trim and has_motor and not has_trans:
            w['trim'] = 55; w['motor'] = 45
        elif has_trim and not has_motor and has_trans:
            w['trim'] = 60; w['trans'] = 40
        elif has_trim and has_motor and has_trans:
            w['trim'] = 45; w['motor'] = 30; w['trans'] = 25
        elif not has_trim and has_motor and has_trans:
            w['motor'] = 60; w['trans'] = 40
        elif not has_trim and has_motor:
            w['motor'] = 100
        else:
            per = 100 // count
            if has_trim: w['trim'] = per
            if has_motor: w['motor'] = per
            if has_trans: w['trans'] = per

        # Trim
        if has_trim and w['trim'] > 0:
            ct = cat_comp.get('trim', '') or ''
            ut = user_comp.get('trim', '') or ''
            if ct and ut:
                if ct == ut:
                    score += w['trim']; detail['trim'] = 'exact'
                elif ut in ct or ct in ut:
                    score += w['trim'] * 0.75; detail['trim'] = 'partial'
                else:
                    # Multi-word: check word overlap
                    ct_words = set(ct.split())
                    ut_words = set(ut.split())
                    overlap = ct_words & ut_words
                    if overlap:
                        score += w['trim'] * (len(overlap) / max(len(ct_words), len(ut_words)))
                        detail['trim'] = 'word_overlap'
                    else:
                        detail['trim'] = 'miss'

        # Motor
        if has_motor and w['motor'] > 0:
            cm = cat_comp.get('motor', '') or ''
            um = user_comp.get('motor_canonical', '') or ''
            if cm and um:
                if cm == um:
                    score += w['motor']; detail['motor'] = 'exact'
                elif _motors_compatible(cm, um):
                    score += w['motor'] * 0.8; detail['motor'] = 'compatible'
                else:
                    detail['motor'] = 'miss'

        # Trans
        if has_trans and w['trans'] > 0:
            ct = cat_comp.get('trans', '') or ''
            ut = user_comp.get('trans_canonical', '') or ''
            if ct and ut:
                if ct == ut:
                    score += w['trans']; detail['trans'] = 'exact'
                elif _trans_compatible(ct, ut):
                    score += w['trans'] * 0.8; detail['trans'] = 'compatible'
                else:
                    detail['trans'] = 'miss'

        return round(score, 1), detail

    def _adaptive_threshold(self, user_comp):
        has_trim = bool(user_comp.get('trim'))
        has_motor = bool(user_comp.get('motor_canonical'))
        has_trans = bool(user_comp.get('trans_canonical'))
        count = sum([has_trim, has_motor, has_trans])
        if count >= 3: return 65
        if count == 2: return 55
        if count == 1:
            return 45 if has_trim else 40
        return 80

    # ─────────────────────────────────────────────
    # POST-VALIDACIÓN (con tolerancia ±2 años)
    # ─────────────────────────────────────────────
    def _post_validate(self, result, marca, año):
        modelo = result['modelo']
        tolerance = CONFIG.year_tolerance
        if año and not result.get('año_sospechoso'):
            yr = self.catalog.get_year_range(marca, modelo)
            if yr:
                desde, hasta = yr.get('desde', 0), yr.get('hasta', 9999)
                if desde - tolerance <= año <= hasta + tolerance:
                    pass
                else:
                    dist = max(0, desde - año) if año < desde else max(0, año - hasta)
                    if dist > CONFIG.year_garbage_threshold:
                        result['warnings'].append(
                            f"Año {año} a {dist} años de '{modelo}' ({desde}-{hasta}) — ignorado")
                    else:
                        self._try_year_correction(result, marca, modelo, año, desde, hasta)

        confusions = self.catalog.get_confusions(marca, result['modelo'])
        if confusions:
            names = [c.get('modelo_confundido','?') for c in confusions]
            result['warnings'].append(f"'{result['modelo']}' se confunde con: {', '.join(names)}")
            STATS.confusion_detected += 1

        pk = make_key(marca, result['modelo'])
        parent = self.catalog.model_parent_map.get(pk)
        if parent:
            result['warnings'].append(f"'{result['modelo']}' es variante de '{parent}'")

    def _try_year_correction(self, result, marca, modelo, año, desde, hasta):
        sug = self.catalog.get_year_suggestion(marca, modelo, año)
        if sug and self.catalog.has_model(marca, sug):
            syr = self.catalog.get_year_range(marca, sug)
            if syr:
                sd, sh = syr.get('desde',0), syr.get('hasta',9999)
                if sd <= año <= sh:
                    self._apply_year_correction(result, modelo, sug, desde, hasta, sd, sh, año)
                    return
        if año > hasta:
            self._walk_chain(result, marca, modelo, año, desde, hasta, 'sucesor')
        elif año < desde:
            self._walk_chain(result, marca, modelo, año, desde, hasta, 'predecesor')

    def _walk_chain(self, result, marca, modelo, año, desde, hasta, direction):
        current, visited = modelo, {modelo}
        for _ in range(5):
            nxt = (self.catalog.get_successor if direction == 'sucesor' else self.catalog.get_predecessor)(marca, current)
            if not nxt or nxt in visited: break
            visited.add(nxt)
            if self.catalog.has_model(marca, nxt):
                nyr = self.catalog.get_year_range(marca, nxt)
                if nyr:
                    nd, nh = nyr.get('desde',0), nyr.get('hasta',9999)
                    if nd <= año <= nh:
                        self._apply_year_correction(result, modelo, nxt, desde, hasta, nd, nh, año)
                        return
            current = nxt

    def _apply_year_correction(self, result, old, new, od, oh, nd, nh, año):
        result['corrections'].append({
            'tipo': 'year_mismatch', 'original': old, 'corregido': new,
            'razon': f"'{old}' {od}-{oh}, año {año} → '{new}' {nd}-{nh}"
        })
        result['modelo'] = new
        STATS.year_corrected += 1

    # ─────────────────────────────────────────────
    # CONFIDENCE
    # ─────────────────────────────────────────────
    def _calculate_confidence(self, result):
        method = result.get('match_method', 'fallback')
        scores = {'exact':90,'alias':85,'code_mapping':80,'description':78,
                  'version_split':76,'base_name':70,'fuzzy':55,'fallback':15}
        score = scores.get(method, 10)

        # Bonus por versión
        vm = result.get('version_method')
        if vm and result.get('from_catalog'):
            ver_bonus = {'exact':8,'component_match':6,'trim_from_text':4,
                         'trim_index':3,'fuzzy':1}.get(vm, 0)
            score += ver_bonus

        if result.get('año_detectado') and result.get('from_catalog') and result.get('modelo') and not result.get('año_sospechoso'):
            yr = self.catalog.get_year_range(result['marca'], result['modelo'])
            if yr and yr.get('desde',0) <= result['año_detectado'] <= yr.get('hasta',9999):
                score += 5
        if result.get('año_sospechoso'): score -= 5
        if result.get('corrections'): score -= 10
        if any('confunde' in w for w in result.get('warnings',[])): score -= 5
        if method == 'fuzzy' and result.get('modelo') and is_numeric_model(result['modelo']): score -= 20
        score = max(0, min(100, score))
        result['confidence'] = score

        level = 'very_low'
        if score >= 80: level = 'high'
        elif score >= 60: level = 'medium'
        elif score >= 40: level = 'low'
        result['confidence_level'] = level
        STATS.by_confidence[level] = STATS.by_confidence.get(level, 0) + 1

    # ─────────────────────────────────────────────
    # STATUS FINAL
    # ─────────────────────────────────────────────
    def _set_final_status(self, result):
        if result['from_catalog']:
            if result['version']:
                result['norm_status'] = 'full_match'; STATS.full_match += 1
            elif result['modelo']:
                result['norm_status'] = 'partial_match'; STATS.partial_match += 1
            else:
                result['norm_status'] = 'marca_only'; STATS.marca_only += 1
        else:
            result['norm_status'] = 'fallback' if result.get('modelo') else 'no_modelo'
            STATS.fallback_used += 1

NORMALIZER = VehicleNormalizerV4()

# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIONES PÚBLICAS (API)
# ═══════════════════════════════════════════════════════════════
def init_normalizer(dicts_path=None, brand_aliases_path=None) -> bool:
    global CATALOG, NORMALIZER, STATS
    STATS = NormalizationStats()
    dicts_path = dicts_path or CONFIG.dicts_path
    brand_aliases_path = brand_aliases_path or CONFIG.brand_aliases_path
    loaded = CATALOG.load(dicts_path, brand_aliases_path)
    NORMALIZER = VehicleNormalizerV4(CATALOG)
    return loaded

def normalize_vehicle(titulo="", descripcion="", marca_raw="", modelo_raw="",
                      version_raw="", año_raw=None, precio_raw=None, km_raw=None) -> Dict:
    return NORMALIZER.normalize(titulo=titulo, descripcion=descripcion,
        marca_raw=marca_raw, modelo_raw=modelo_raw, version_raw=version_raw,
        año_raw=año_raw, precio_raw=precio_raw, km_raw=km_raw)

def get_normalization_stats(): return STATS
def get_stats(): return STATS
def get_catalog(): return CATALOG

# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print("=" * 70)
    print("🧪 PRUEBA DEL NORMALIZADOR v5.0")
    print("=" * 70)

    loaded = init_normalizer("config/normalizer_dicts.json")
    print(f"\n📚 Catálogo cargado: {loaded}")
    if not loaded:
        print("❌ No se pudo cargar el catálogo"); exit(1)

    test_cases = [
        {
            "name": "Cruze LT 1.4 Turbo AT → debe matchear 1.4t lt at",
            "marca_raw": "Chevrolet", "version_raw": "Cruze LT 1.4 Turbo Automático",
            "expected": {"marca": "chevrolet", "modelo": "cruze", "version": "1.4t lt at"},
        },
        {
            "name": "Solo 'LT' → debe matchear alguna versión LT del Cruze",
            "marca_raw": "Chevrolet", "modelo_raw": "Cruze", "version_raw": "LT",
            "expected": {"marca": "chevrolet", "modelo": "cruze"},
            "note": "Sin motor ni trans, match parcial por trim",
        },
        {
            "name": "Onix 1.0T Premier AT → match exacto",
            "marca_raw": "Chevrolet", "version_raw": "Onix 1.0T Premier AT",
            "expected": {"marca": "chevrolet", "modelo": "onix", "version": "1.0t premier at"},
        },
        {
            "name": "S-10 High Country 4x4 → match con tracción",
            "marca_raw": "Chevrolet", "version_raw": "S10 High Country 4x4 Automática",
            "expected": {"marca": "chevrolet", "modelo": "s-10"},
        },
        {
            "name": "Tracker LTZ automática → match componentes",
            "marca_raw": "Chevrolet", "version_raw": "Tracker LTZ Automática",
            "expected": {"marca": "chevrolet", "modelo": "tracker"},
        },
        {
            "name": "Peugeot 206 año 2011 (dentro de holgura)",
            "titulo": "Peugeot 206 2011", "marca_raw": "Peugeot",
            "modelo_raw": "206", "año_raw": 2011,
            "expected": {"marca": "peugeot", "modelo": "206"},
        },
        {
            "name": "VW Amarok Highline 4x4",
            "marca_raw": "VW", "version_raw": "Amarok Highline 4x4",
            "expected": {"marca": "volkswagen", "modelo": "amarok"},
        },
        {
            "name": "Fiat año basura con desc 'mod 80'",
            "marca_raw": "Fiat", "version_raw": "Europa", "año_raw": 2026,
            "km_raw": 111111, "descripcion": "Fiat europa mod 80",
            "expected": {"marca": "fiat"},
        },
    ]

    passed = failed = 0
    for test in test_cases:
        print(f"\n📋 {test['name']}")
        r = normalize_vehicle(
            titulo=test.get('titulo',''), descripcion=test.get('descripcion',''),
            marca_raw=test.get('marca_raw',''), modelo_raw=test.get('modelo_raw',''),
            version_raw=test.get('version_raw',''), año_raw=test.get('año_raw'),
            km_raw=test.get('km_raw'))
        ex = test.get('expected', {})
        ok = all(r.get(k) == v for k, v in ex.items())
        status = "✅" if ok else "❌"
        if ok: passed += 1
        else: failed += 1
        print(f"   {status} marca={r['marca']} modelo={r['modelo']} version={r['version']}")
        print(f"      method={r['match_method']} ver_method={r['version_method']} "
              f"score={r['version_score']} conf={r['confidence']} ({r['confidence_level']})")
        print(f"      año={r.get('año_detectado')} (src={r.get('año_fuente')})")
        print(f"      extracted={r.get('extracted_data')}")
        if r.get('warnings'):
            for w in r['warnings']: print(f"      ⚠️ {w}")
        if r.get('corrections'):
            for c in r['corrections']: print(f"      🔧 {c.get('razon','')}")

    print(f"\n{'='*70}")
    print(f"📊 {passed} passed, {failed} failed")
    print(f"📈 {STATS.summary()}")
    print(f"{'='*70}")
