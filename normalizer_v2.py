"""
🔧 Módulo de Normalización de Vehículos v4.1
=============================================
Normaliza marca, modelo y versión usando diccionarios enriquecidos
generados desde catálogo JSON (normalizer_dicts.json).

MEJORAS v4.1 RESPECTO A v4.0:
─────────────────────────────
  - Holgura ±2 años antes de cambiar modelo (YEAR_TOLERANCE)
  - Detección de año basura (futuro, absurdo, contradice descripción)
  - Minería de descripción ampliada (año "mod XX", modelo en texto libre)
  - Validación cruzada año×km (111.000km en auto "2026" = imposible)
  - Split de version_raw en modelo+versión ("Amarok Highline" → modelo+trim)
  - Patrones de motorización ampliados (1300cc, 1.4, etc.)
  - Mejor fallback: busca modelo en TODOS los campos antes de rendirse
  - Urgency signals integrado

ESTRATEGIA DE BÚSQUEDA (en orden):
  1. Match exacto en catálogo (modelo directo)
  2. Match por alias de modelo
  3. Mapeo de códigos (C200→Clase C, 320i→Serie 3)
  4. Match de nombre base (sin números/códigos)
  5. Modelo encontrado en descripción ampliada
  6. Split de version_raw (primera palabra = modelo)
  7. Fuzzy matching (SOLO para modelos no numéricos, restrictivo)
  8. Fallback a datos originales
  + Post-validación con tolerancia de año y detección de basura

Autor: Sistema de Normalización
Versión: 4.1
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
        replacements = {
            'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
            'ä': 'a', 'ë': 'e', 'ï': 'i', 'ö': 'o', 'ü': 'u',
            'ñ': 'n', 'ç': 'c',
            'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U',
            'Ñ': 'N', 'Ü': 'U',
        }
        for old, new in replacements.items():
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
    """Configuración del normalizador v4.1."""
    # Fuzzy
    fuzzy_threshold_marca: int = 85
    fuzzy_threshold_modelo: int = 88
    fuzzy_threshold_version: int = 80
    fuzzy_min_word_length: int = 4
    enable_fuzzy: bool = True

    # ── NUEVO v4.1: Tolerancia de año ──
    # Años de holgura antes de cambiar modelo por año
    year_tolerance: int = 2
    # Si el año está >N años fuera del rango, es basura (no usar para nada)
    year_garbage_threshold: int = 10
    # Año máximo razonable
    year_max_valid: int = CURRENT_YEAR + 1
    # Año mínimo razonable para este marketplace
    year_min_valid: int = 1940
    # Km máximos para un auto del año actual (detectar año basura)
    km_max_for_current_year: int = 50000

    # Confianza mínima
    min_confidence_accept: int = 60

    # Rutas
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

    by_method: Dict[str, int] = field(default_factory=lambda: {
        'exact': 0, 'alias': 0, 'code_mapping': 0,
        'base_name': 0, 'description': 0, 'version_split': 0,
        'fuzzy': 0, 'fallback': 0
    })
    by_confidence: Dict[str, int] = field(default_factory=lambda: {
        'high': 0, 'medium': 0, 'low': 0, 'very_low': 0
    })

    def summary(self) -> str:
        if self.total == 0:
            return "Sin datos procesados"
        p = lambda x: f"{(x/self.total*100):.1f}%"
        return (
            f"Total: {self.total} | "
            f"Full: {self.full_match} ({p(self.full_match)}) | "
            f"Parcial: {self.partial_match} ({p(self.partial_match)}) | "
            f"Fallback: {self.fallback_used} ({p(self.fallback_used)}) | "
            f"Año corregido: {self.year_corrected} | "
            f"Año basura: {self.year_garbage_detected} | "
            f"Año de desc: {self.year_from_description} | "
            f"Modelo de desc: {self.model_from_description}"
        )


STATS = NormalizationStats()

# ═══════════════════════════════════════════════════════════════
# 🏷️ ALIASES DE MARCAS
# ═══════════════════════════════════════════════════════════════

DEFAULT_BRAND_ALIASES = {
    'vw': 'volkswagen', 'volks': 'volkswagen', 'volkswagon': 'volkswagen',
    'mercedes': 'mercedes-benz', 'mercedes benz': 'mercedes-benz',
    'mercedesbenz': 'mercedes-benz', 'mb': 'mercedes-benz',
    'benz': 'mercedes-benz',
    'chevy': 'chevrolet', 'gm': 'chevrolet',
    'alfa': 'alfa romeo',
    'landrover': 'land rover',
    'citroen': 'citroën', 'citröen': 'citroën',
}

# ═══════════════════════════════════════════════════════════════
# 🚫 BLACKLISTS
# ═══════════════════════════════════════════════════════════════

FUZZY_BLACKLIST = {
    # Años / tiempo
    'ano', 'año', 'años', 'anio',
    # Combustibles
    'nafta', 'naftero', 'diesel', 'gasoil', 'gnc', 'gas',
    'hibrido', 'electrico',
    # Transmisión
    'manual', 'automatico', 'automatica', 'secuencial', 'cvt', 'dsg',
    # Estados
    'muy', 'buen', 'bueno', 'buena', 'mal', 'estado', 'impecable',
    'excelente', 'perfecto', 'perfecta', 'inmaculado',
    'nuevo', 'nueva', 'usado',
    # Propiedad
    'unico', 'unica', 'dueno', 'dueño', 'titular', 'particular',
    # Extras
    'full', 'tope', 'gama', 'equipo', 'equipado', 'completo',
    'cuero', 'techo',
    # Transacciones
    'permuto', 'permuta', 'financio', 'vendo', 'venta', 'contado',
    # Genéricos
    'auto', 'autos', 'coche', 'vehiculo', 'carro',
    'motor', 'caja', 'cambios',
    # Documentos
    'vtv', 'papeles', 'patente', 'seguro', 'service',
    # Otros
    'consultar', 'precio', 'oferta', 'urgente', 'oportunidad',
    'original', 'funcionando', 'kilometros', 'kms', 'litros',
    'puertas', 'modelo', 'version', 'serie', 'linea', 'tipo',
    # ── NUEVO v4.1: Más palabras problemáticas ──
    'funciona', 'todo', 'anda', 'tiene', 'soy', 'directo',
    'transferencia', 'transfiere', 'conexion', 'luces',
    'alarma', 'cierre', 'centralizado', 'enganche', 'trailer',
    'instrumental', 'calefaccion', 'limpias', 'parabrisas',
    'electrónico', 'electronico', 'encendido', 'leva',
    'telefono', 'celular', 'whatsapp', 'llamar',
    'excelente', 'impecable', 'hermoso', 'joya', 'reliquia',
    'clasico', 'antiguo', 'restaurado', 'original',
    'pesos', 'dolares', 'usd', 'efectivo',
}

# ── Patrones de motorización expandidos ──
MOTORIZATION_PATTERNS = [
    r'^\d+\.\d+$',                                 # "2.0", "1.6"
    r'^\d+\.\d+\s*[lt]?$',                         # "2.0l", "1.6t"
    r'^\d+\.\d+\s*(tdi|tfsi|hdi|vti|thp|jtd|cdti|dci|tsi|fsi|mpi|ts|jts|jtdm|crdi)i?$',
    r'^\d+\.\d+\s*(turbo|diesel|nafta|naftero|multijet|bluehdi|ecoboost)$',
    r'^\d+\.\d+\s*l$',                             # "2.0 l"
    r'^\d+(v|cv|hp|bhp)$',                         # "150cv", "200hp"
    r'^(v6|v8|v10|v12|i4|i6|l4|l6|w12|w16)$',     # Configuraciones de motor
    r'^\d{3,4}\s*(cc)?$',                          # "1300", "1600cc"
    r'^\d{3,4}\s*(cm3|cilindrada)$',               # "1300 cm3"
    r'^motor\s+\d',                                 # "motor 1300"
    r'^\d+\.\d+\s+\d+v$',                          # "2.0 16v"
    r'^\d+v$',                                     # "16v", "8v"
]

# ═══════════════════════════════════════════════════════════════
# 🔥 URGENCY KEYWORDS
# ═══════════════════════════════════════════════════════════════

URGENCY_KEYWORDS = {
    'urgente': 30, 'urge': 30, 'urgencia': 30,
    'viajo': 25, 'viaje': 25, 'me voy': 25, 'mudanza': 25,
    'oportunidad': 20,
    'negociable': 15, 'escucho': 15, 'escucho ofertas': 20,
    'acepto oferta': 20, 'ofertas': 15,
    'vendo ya': 20, 'venta rapida': 20, 'liquido': 20,
    'financio': 10, 'permuto': 10, 'contado': 10,
    'rebajado': 15, 'rebaja': 15,
    'precio final': -20, 'no negociable': -25, 'firme': -15,
}



# ═══════════════════════════════════════════════════════════════
# 🛠️ FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower().strip()
    text = unidecode(text)
    text = re.sub(r'[^\w\s\-]', ' ', text)
    text = ' '.join(text.split())
    return text


def normalize_key(text: str) -> str:
    if not text:
        return ""
    return normalize_text(text)


def make_key(marca: str, modelo: str) -> str:
    return f"{normalize_key(marca)}|{normalize_key(modelo)}"


def find_exact_in_text(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    pattern = r'\b' + re.escape(needle) + r'\b'
    return bool(re.search(pattern, haystack, re.IGNORECASE))


def is_numeric_model(modelo: str) -> bool:
    """Modelo puramente numérico o alfanumérico corto (206, 208, A4)."""
    m = normalize_key(modelo)
    return bool(
        re.match(r'^\d{2,5}$', m) or
        re.match(r'^[a-z]\d{1,3}$', m)
    )


def is_motorization_pattern(text: str) -> bool:
    """Verifica si un texto matchea patrones de motorización."""
    if not text:
        return False
    t = normalize_key(text)
    for pattern in MOTORIZATION_PATTERNS:
        if re.match(pattern, t):
            return True
    return False


def fuzzy_match(needle: str, candidates: List[str],
                threshold: int = 85) -> Optional[Tuple[str, int]]:
    if not HAS_RAPIDFUZZ or not needle or not candidates:
        return None
    if len(needle) < CONFIG.fuzzy_min_word_length:
        return None
    if needle.lower() in FUZZY_BLACKLIST:
        return None
    try:
        result = process.extractOne(
            needle, candidates, scorer=fuzz.ratio,
            score_cutoff=threshold
        )
        return (result[0], result[1]) if result else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 📅 FUNCIONES DE AÑO (NUEVO v4.1)
# ═══════════════════════════════════════════════════════════════

def extract_year_from_text(text: str) -> Optional[int]:
    """Extrae año de 4 dígitos del texto (el más reciente razonable)."""
    if not text:
        return None
    matches = re.findall(r'\b(19[4-9]\d|20[0-2]\d)\b', text)
    if not matches:
        return None
    years = [int(y) for y in matches if int(y) <= CONFIG.year_max_valid]
    return max(years) if years else None


def extract_year_from_description(text: str) -> Optional[int]:
    """
    Extrae año de texto libre de descripción.
    Busca patrones como "mod 80", "modelo 97", "del 80", "año 80".

    IMPORTANTE: Solo matchea 2 dígitos (para décadas) o 4 dígitos
    (años completos). NO matchea 3 dígitos (podrían ser modelos: 206, 208).
    """
    if not text:
        return None

    text_lower = text.lower()

    # ── Patrones de 2 dígitos (décadas) ──
    patterns_2d = [
        r'\bmod(?:elo)?\.?\s+(\d{2})\b',      # "mod 80", "modelo 97"
        r'\bdel\s+(?:año\s+)?(\d{2})\b',       # "del 80", "del año 80"
        r'\baño\s+(\d{2})\b',                   # "año 80"
    ]

    for pattern in patterns_2d:
        m = re.search(pattern, text_lower)
        if m:
            y = int(m.group(1))
            # Interpretar: 40-99 → 1940-1999, 00-35 → 2000-2035
            if 40 <= y <= 99:
                return 1900 + y
            elif 0 <= y <= 35:
                return 2000 + y

    # ── Patrones de 4 dígitos ──
    patterns_4d = [
        r'\bmod(?:elo)?\.?\s+((?:19|20)\d{2})\b',    # "mod 2005"
        r'\baño\s+((?:19|20)\d{2})\b',                 # "año 2005"
        r'\bdel\s+((?:19|20)\d{2})\b',                 # "del 2005"
    ]

    for pattern in patterns_4d:
        m = re.search(pattern, text_lower)
        if m:
            y = int(m.group(1))
            if CONFIG.year_min_valid <= y <= CONFIG.year_max_valid:
                return y

    return None


def assess_year_quality(
    año_structured: Optional[int],
    año_description: Optional[int] = None,
    km: Optional[int] = None,
    model_year_range: Optional[Dict] = None,
) -> Dict:
    """
    Evalúa la calidad del año estructurado.

    Returns:
        Dict con:
        - quality: 'valid' | 'suspicious' | 'garbage'
        - best_year: el año más confiable a usar
        - reason: explicación
    """
    result = {
        'quality': 'valid',
        'best_year': año_structured,
        'year_source': 'structured',
        'reason': None,
    }

    if not año_structured:
        if año_description:
            result['best_year'] = año_description
            result['year_source'] = 'description'
            result['reason'] = 'Año extraído de descripción (no hay año estructurado)'
        else:
            result['quality'] = 'missing'
            result['reason'] = 'Sin año disponible'
        return result

    # ── Regla 1: Año futuro ──
    if año_structured > CONFIG.year_max_valid:
        result['quality'] = 'garbage'
        result['reason'] = (
            f"Año {año_structured} es futuro "
            f"(máx válido: {CONFIG.year_max_valid})"
        )
        if año_description and CONFIG.year_min_valid <= año_description <= CONFIG.year_max_valid:
            result['best_year'] = año_description
            result['year_source'] = 'description'
            result['reason'] += f" → usando año de descripción: {año_description}"
        else:
            result['best_year'] = None
        return result

    # ── Regla 2: Año demasiado antiguo ──
    if año_structured < CONFIG.year_min_valid:
        result['quality'] = 'garbage'
        result['reason'] = f"Año {año_structured} < {CONFIG.year_min_valid}"
        if año_description and CONFIG.year_min_valid <= año_description <= CONFIG.year_max_valid:
            result['best_year'] = año_description
            result['year_source'] = 'description'
        else:
            result['best_year'] = None
        return result

    # ── Regla 3: Km altos + año muy reciente → sospechoso ──
    if km and km > CONFIG.km_max_for_current_year and año_structured >= CURRENT_YEAR:
        result['quality'] = 'suspicious'
        result['reason'] = (
            f"Año {año_structured} con {km:,} km es sospechoso"
        )
        if año_description and año_description < año_structured:
            result['best_year'] = año_description
            result['year_source'] = 'description'
            result['reason'] += (
                f" → descripción sugiere {año_description}"
            )

    # ── Regla 4: Año contradice enormemente el rango del modelo ──
    if model_year_range:
        desde = model_year_range.get('desde', 0)
        hasta = model_year_range.get('hasta', 9999)
        # Distancia del año al rango más cercano
        if año_structured < desde:
            distance = desde - año_structured
        elif año_structured > hasta:
            distance = año_structured - hasta
        else:
            distance = 0

        if distance > CONFIG.year_garbage_threshold:
            result['quality'] = 'garbage'
            result['reason'] = (
                f"Año {año_structured} está a {distance} años "
                f"del rango {desde}-{hasta} (umbral: "
                f"{CONFIG.year_garbage_threshold})"
            )
            if (año_description
                    and CONFIG.year_min_valid <= año_description <= CONFIG.year_max_valid):
                result['best_year'] = año_description
                result['year_source'] = 'description'
                result['reason'] += (
                    f" → usando año de descripción: {año_description}"
                )
            else:
                result['best_year'] = None

    # ── Regla 5: Descripción contradice mucho al estructurado ──
    if (result['quality'] == 'valid'
            and año_description
            and año_structured
            and abs(año_structured - año_description) > 20):
        result['quality'] = 'suspicious'
        result['reason'] = (
            f"Año estructurado ({año_structured}) difiere mucho "
            f"del año en descripción ({año_description})"
        )

    return result


# ═══════════════════════════════════════════════════════════════
# 📝 MINERÍA DE DESCRIPCIÓN (NUEVO v4.1)
# ═══════════════════════════════════════════════════════════════

def extract_model_from_description(
    descripcion: str,
    marca: str,
    catalog_models: List[str],
) -> Optional[str]:
    """
    Busca nombres de modelos del catálogo en la descripción ampliada.
    Prioriza modelos más largos (Grand Cherokee antes que Cherokee).

    La descripción es texto libre del usuario, es MUY confiable para
    saber qué auto realmente es.
    """
    if not descripcion or not catalog_models:
        return None

    desc_norm = normalize_text(descripcion)

    # Remover marca para evitar falsos positivos
    if marca:
        desc_norm = re.sub(
            r'\b' + re.escape(normalize_key(marca)) + r'\b',
            ' ', desc_norm
        )
        # También variante sin guiones
        desc_norm = re.sub(
            r'\b' + re.escape(normalize_key(marca).replace('-', ' ')) + r'\b',
            ' ', desc_norm
        )

    # Buscar modelos del catálogo en la descripción
    # (ya vienen ordenados por longitud desc)
    for modelo in catalog_models:
        if find_exact_in_text(modelo, desc_norm):
            # Verificar que no sea parte de una motorización
            # Ej: "motor 1300" no debería matchear modelo "1300" de Fiat
            # Verificar contexto: la palabra antes no debe ser "motor"
            pattern = r'(?:^|\s)(?!motor\s)' + re.escape(modelo) + r'\b'
            if re.search(pattern, desc_norm):
                return modelo

    return None


def split_version_into_model_and_trim(
    version_raw: str,
    catalog_models: List[str],
    marca: str = "",
) -> Optional[Dict]:
    """
    Intenta separar version_raw en modelo + versión/trim.

    Ejemplos:
      "Amarok Highline 4x4" + models=["amarok"] → {model:"amarok", trim:"highline 4x4"}
      "Europa" + models=["europa"] → {model:"europa", trim:""}
      "C200 Avantgarde" → None (necesita code mapping, no split)

    Solo retorna resultado si la primera palabra significativa
    de version_raw es un modelo conocido.
    """
    if not version_raw or not catalog_models:
        return None

    version_norm = normalize_text(version_raw)

    # Remover marca si está al inicio
    if marca:
        marca_norm = normalize_key(marca)
        if version_norm.startswith(marca_norm):
            version_norm = version_norm[len(marca_norm):].strip()
        marca_nohyphen = marca_norm.replace('-', ' ')
        if version_norm.startswith(marca_nohyphen):
            version_norm = version_norm[len(marca_nohyphen):].strip()

    if not version_norm:
        return None

    # Buscar si algún modelo del catálogo está al inicio de version_raw
    for modelo in catalog_models:
        if version_norm == modelo:
            return {'model': modelo, 'trim': ''}

        if version_norm.startswith(modelo + ' '):
            trim = version_norm[len(modelo):].strip()
            # Verificar que el trim no sea solo motorización
            if trim and not is_motorization_pattern(trim):
                return {'model': modelo, 'trim': trim}
            elif trim:
                return {'model': modelo, 'trim': ''}
            else:
                return {'model': modelo, 'trim': ''}

    # Buscar modelo en cualquier posición (no solo inicio)
    for modelo in catalog_models:
        if find_exact_in_text(modelo, version_norm):
            # Extraer el trim removiendo el modelo
            trim = re.sub(
                r'\b' + re.escape(modelo) + r'\b',
                '', version_norm
            ).strip()
            if trim and not is_motorization_pattern(trim):
                return {'model': modelo, 'trim': trim}
            return {'model': modelo, 'trim': ''}

    return None


# ═══════════════════════════════════════════════════════════════
# 🔥 DETECCIÓN DE URGENCIA
# ═══════════════════════════════════════════════════════════════

def extract_urgency_signals(text: str) -> Dict:
    """
    Detecta señales de urgencia en el texto.
    Returns: Dict con 'has_urgency', 'score', 'keywords_found'
    """
    if not text:
        return {'has_urgency': False, 'score': 0, 'keywords_found': []}

    text_lower = text.lower()
    text_norm = normalize_text(text)

    score = 0
    keywords_found = []

    for keyword, points in URGENCY_KEYWORDS.items():
        if find_exact_in_text(keyword, text_norm) or keyword in text_lower:
            score += points
            if points > 0:
                keywords_found.append(keyword)

    score = max(0, min(100, score))

    return {
        'has_urgency': score >= 15,
        'score': score,
        'keywords_found': keywords_found
    }


# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO ENRIQUECIDO
# ═══════════════════════════════════════════════════════════════

class EnrichedCatalog:
    """Catálogo cargado desde normalizer_dicts.json."""

    def __init__(self):
        self._data: Dict = {}
        self._brand_aliases: Dict[str, str] = {}
        self._loaded = False

    def load(self, dicts_path: str,
             brand_aliases_path: str = None) -> bool:
        if not os.path.exists(dicts_path):
            logger.warning(f"⚠️ Archivo no encontrado: {dicts_path}")
            return False
        try:
            with open(dicts_path, 'r', encoding='utf-8') as f:
                self._data = json.load(f)
            self._loaded = True

            for alias, brand in DEFAULT_BRAND_ALIASES.items():
                self._brand_aliases[normalize_key(alias)] = (
                    normalize_key(brand)
                )
            if brand_aliases_path and os.path.exists(brand_aliases_path):
                with open(brand_aliases_path, 'r', encoding='utf-8') as f:
                    extra = json.load(f)
                for alias, brand in extra.get('brand_aliases', {}).items():
                    self._brand_aliases[normalize_key(alias)] = (
                        normalize_key(brand)
                    )

            meta = self._data.get('_metadata', {})
            logger.info(
                f"✅ Catálogo: {meta.get('total_brands', 0)} marcas, "
                f"{meta.get('total_models', 0)} modelos, "
                f"{meta.get('total_versions', 0)} versiones"
            )
            return True
        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ── Acceso a diccionarios ──

    @property
    def catalog_index(self) -> Dict:
        return self._data.get('catalog_index', {})

    @property
    def code_mappings(self) -> Dict:
        return self._data.get('model_code_to_catalog', {})

    @property
    def model_aliases(self) -> Dict:
        return self._data.get('model_aliases', {})

    @property
    def year_ranges(self) -> Dict:
        return self._data.get('year_ranges', {})

    @property
    def known_confusions(self) -> Dict:
        return self._data.get('known_confusions', {})

    @property
    def succession_map(self) -> Dict:
        return self._data.get('succession_map', {})

    @property
    def known_motorizations(self) -> Dict:
        return self._data.get('known_motorizations', {})

    @property
    def motorization_blacklist(self) -> Dict:
        return self._data.get('motorization_blacklist', {})

    @property
    def valid_versions(self) -> Dict:
        return self._data.get('valid_versions', {})

    @property
    def validation_rules(self) -> Dict:
        return self._data.get('validation_rules', {})

    @property
    def vehicle_classification(self) -> Dict:
        return self._data.get('vehicle_classification', {})

    @property
    def model_parent_map(self) -> Dict:
        return self._data.get('model_parent_map', {})

    # ── Métodos de consulta ──

    def resolve_brand(self, brand_raw: str) -> str:
        b = normalize_key(brand_raw)
        if b in self._brand_aliases:
            return self._brand_aliases[b]
        compact = b.replace(' ', '').replace('-', '')
        if compact in self._brand_aliases:
            return self._brand_aliases[compact]
        return b

    def has_brand(self, brand: str) -> bool:
        return normalize_key(brand) in self.catalog_index

    def get_brands(self) -> List[str]:
        return sorted(self.catalog_index.keys(), key=len, reverse=True)

    def get_models(self, brand: str) -> List[str]:
        b = normalize_key(brand)
        models = self.catalog_index.get(b, {})
        return sorted(models.keys(), key=len, reverse=True)

    def has_model(self, brand: str, model: str) -> bool:
        b = normalize_key(brand)
        m = normalize_key(model)
        return m in self.catalog_index.get(b, {})

    def get_versions(self, brand: str, model: str) -> List[str]:
        key = make_key(brand, model)
        return self.valid_versions.get(key, [])

    def get_year_range(self, brand: str, model: str) -> Optional[Dict]:
        key = make_key(brand, model)
        return self.year_ranges.get(key)

    def get_year_suggestion(self, brand: str, model: str,
                            year: int) -> Optional[str]:
        key = make_key(brand, model)
        suggestion = self.validation_rules.get(
            'year_mismatch_suggestions', {}
        ).get(key)
        if not suggestion:
            return None
        rango = suggestion.get('rango_valido', {})
        if year > rango.get('hasta', 9999):
            return suggestion.get('si_año_posterior')
        elif year < rango.get('desde', 0):
            return suggestion.get('si_año_anterior')
        return None

    def resolve_model_alias(self, brand: str,
                            alias: str) -> Optional[str]:
        b = normalize_key(brand)
        a = normalize_key(alias)
        return self.model_aliases.get(b, {}).get(a)

    def get_confusions(self, brand: str, model: str) -> List[Dict]:
        key = make_key(brand, model)
        return self.known_confusions.get(key, [])

    def get_successor(self, brand: str, model: str) -> Optional[str]:
        b = normalize_key(brand)
        m = normalize_key(model)
        return self.succession_map.get(b, {}).get(m, {}).get('sucesor')

    def get_predecessor(self, brand: str, model: str) -> Optional[str]:
        b = normalize_key(brand)
        m = normalize_key(model)
        return self.succession_map.get(b, {}).get(m, {}).get('predecesor')


# Instancia global
CATALOG = EnrichedCatalog()


# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR v4.1
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# 🔧 FUNCIONES AUXILIARES PARA MATCH DE VERSIONES (v4.2)
# ═══════════════════════════════════════════════════════════════

# ── Trims conocidos por marca ──
KNOWN_TRIMS = {
    'chevrolet': [
        'lt', 'ls', 'ltz', 'ltz+', 'premier', 'rs',
        'midnight', 'high country', 'highcountry',
        'joy', 'effect', 'spirit', 'activ',
        'wt', 'z71', 'base',
        'gl', 'gls', 'cd', 'gsi', 'dlx', 'custom',
        'deluxe', 'conquest', 'avantage',
        'black edition',
    ],
    'ford': [
        's', 'se', 'sel', 'titanium', 'trend',
        'xls', 'xlt', 'limited', 'wildtrak',
        'freestyle', 'kinetic',
    ],
    'fiat': [
        'attractive', 'drive', 'like', 'way',
        'trekking', 'freedom', 'precision',
        'ranch', 'sporting', 'hlx', 'elx',
        'fire', 'pack', 'base',
    ],
    'volkswagen': [
        'trendline', 'comfortline', 'highline',
        'sportline', 'hero', 'extreme',
    ],
    'renault': [
        'life', 'zen', 'intens', 'iconic',
        'outsider', 'expression', 'authentique',
        'privilege', 'luxe', 'pack', 'confort',
    ],
    'toyota': [
        'xli', 'xei', 'sr', 'srv', 'srx',
        'sx', 'dx', 'dl', 'limited',
        'gr-sport', 'gr sport',
    ],
    'peugeot': [
        'allure', 'feline', 'active', 'gt',
        'gt line', 'roadtrip',
    ],
}

# Trims genéricos que aplican a muchas marcas
GENERIC_TRIMS = {
    'base', 'full', 'pack',
    '4x2', '4x4', 'awd', 'fwd',
    'mt', 'at', 'cvt', 'dsg',
    'mt5', 'mt6', 'at6', 'at8', 'at9',
}

# ── Patrones de motorización en versiones ──
RE_MOTOR_IN_VERSION = re.compile(
    r'(\d+\.\d+)\s*([lt]|turbo|tdi|tfsi|hdi|thp|mpi|ts|'
    r'vti|jtd|cdti|dci|tsi|mjet|multijet|ecoboost)?',
    re.IGNORECASE
)

# Mapeo de abreviaciones de motorización
MOTOR_ALIASES = {
    '1.4t': ['1.4 turbo', '1.4 t', '1.4turbo', '14t'],
    '1.0t': ['1.0 turbo', '1.0 t', '1.0turbo', '10t'],
    '1.2t': ['1.2 turbo', '1.2 t', '1.2turbo', '12t'],
    '2.8 td': ['2.8 turbo diesel', '2.8td', '2.8 diesel'],
}


def _normalize_version_text(text: str) -> str:
    """
    Normaliza texto de versión para comparación flexible.
    "1.4T LT AT" → "14t lt at"
    "1.4 Turbo LT" → "14 turbo lt"
    """
    if not text:
        return ""
    t = normalize_text(text)
    # Quitar puntos entre dígitos: 1.4 → 14
    t = re.sub(r'(\d)\.(\d)', r'\1\2', t)
    return t


def _extract_version_components(version: str) -> Dict:
    """
    Descompone una versión en sus componentes.
    "1.4t lt at" → {
        'motor': '1.4', 'motor_suffix': 't',
        'trim': 'lt', 'trans': 'at',
        'all_words': ['1.4t', 'lt', 'at']
    }
    """
    result = {
        'motor': None,
        'motor_suffix': None,
        'trim': None,
        'trans': None,
        'drive': None,
        'all_words': [],
        'trim_words': [],
    }

    if not version:
        return result

    v = normalize_text(version)
    words = v.split()
    result['all_words'] = words

    trans_words = {'mt', 'at', 'cvt', 'dsg', 'mt5', 'mt6',
                   'at6', 'at8', 'at9'}
    drive_words = {'4x2', '4x4', 'awd', 'fwd', '2wd'}

    for word in words:
        # Motor: 1.4t, 2.0, 2.8
        motor_match = re.match(
            r'^(\d+\.?\d*)\s*([a-z]*)$', word
        )
        if motor_match and '.' in word or len(word) <= 4:
            num = motor_match.group(1)
            suffix = motor_match.group(2)
            if '.' in num or (
                len(num) <= 2 and suffix
            ):
                result['motor'] = num
                result['motor_suffix'] = suffix or None
                continue

        # Transmisión
        if word in trans_words:
            result['trans'] = word
            continue

        # Tracción
        if word in drive_words:
            result['drive'] = word
            continue

        # El resto es trim
        result['trim_words'].append(word)

    if result['trim_words']:
        result['trim'] = ' '.join(result['trim_words'])

    return result


def _score_version_match(
    catalog_version: str,
    search_text: str,
) -> int:
    """
    Puntúa qué tan bien matchea una versión del catálogo
    contra el texto de búsqueda.
    
    Retorna 0-100.
    """
    components = _extract_version_components(catalog_version)
    search_norm = normalize_text(search_text)
    score = 0
    max_score = 0

    # ── Trim (lo más importante: 50 pts) ──
    if components['trim']:
        max_score += 50
        trim = components['trim']
        if find_exact_in_text(trim, search_norm):
            score += 50
        else:
            # Intentar palabras individuales del trim
            trim_words = trim.split()
            if trim_words:
                found = sum(
                    1 for w in trim_words
                    if find_exact_in_text(w, search_norm)
                )
                score += int(50 * found / len(trim_words))

    # ── Motor (20 pts) ──
    if components['motor']:
        max_score += 20
        motor = components['motor']
        motor_with_suffix = (
            f"{motor}{components['motor_suffix']}"
            if components['motor_suffix'] else motor
        )

        if find_exact_in_text(motor_with_suffix, search_norm):
            score += 20
        elif find_exact_in_text(motor, search_norm):
            score += 15
        else:
            # Buscar variantes: "1.4t" vs "1.4 turbo"
            motor_compact = motor.replace('.', '')
            if motor_compact in search_norm.replace('.', ''):
                score += 10

    # ── Transmisión (15 pts) ──
    if components['trans']:
        max_score += 15
        if find_exact_in_text(
            components['trans'], search_norm
        ):
            score += 15

    # ── Tracción (15 pts) ──
    if components['drive']:
        max_score += 15
        if find_exact_in_text(
            components['drive'], search_norm
        ):
            score += 15

    # Normalizar a 0-100
    if max_score == 0:
        return 0

    return int(score * 100 / max_score)


def _match_trim_only(
    versiones: List[str],
    search_text: str,
) -> Optional[str]:
    """
    Busca solo el trim (sin motorización ni transmisión)
    en el texto.
    Si hay un único trim que matchea → lo retorna.
    Si hay varios → retorna el más largo (más específico).
    """
    matches = []

    for version in versiones:
        components = _extract_version_components(version)
        trim = components.get('trim')
        if not trim or len(trim) < 2:
            continue

        if find_exact_in_text(trim, search_text):
            matches.append((version, len(trim)))

    if not matches:
        return None

    # Retornar el match con trim más largo (más específico)
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[0][0]

class VehicleNormalizerV4:
    """
    Pipeline de normalización:

    PASADA 1:
      0. Validar/sanear año (detectar basura, extraer de descripción)
      1. Encontrar marca
      2. Filtrar motorizaciones del input
      3. Encontrar modelo (7 estrategias en cascada)
      4. Encontrar versión

    PASADA 2:
      5. Post-validar año vs modelo (con tolerancia ±2)
      6. Verificar confusiones conocidas
      7. Calcular confidence score
    """

    def __init__(self, catalog: EnrichedCatalog = None):
        self.catalog = catalog or CATALOG

    def normalize(
        self,
        titulo: str = "",
        descripcion: str = "",
        marca_raw: str = "",
        modelo_raw: str = "",
        version_raw: str = "",
        año_raw: Optional[int] = None,
        precio_raw: Optional[float] = None,
        km_raw: Optional[int] = None,
    ) -> Dict:

        global STATS
        STATS.total += 1

        # ── Normalizar inputs ──
        titulo_n = normalize_text(titulo)
        desc_n = normalize_text(
            descripcion[:1000] if descripcion else ""
        )
        marca_n = normalize_text(marca_raw)
        modelo_n = normalize_text(modelo_raw)
        version_n = normalize_text(version_raw)

        text_high = f"{titulo_n} {marca_n}"
        text_medium = f"{version_n} {modelo_n}"
        text_low = desc_n
        text_all = f"{text_high} {text_medium} {text_low}"

        result = {
            'marca': None,
            'modelo': None,
            'version': None,
            'año_detectado': None,
            'año_fuente': None,
            'año_sospechoso': False,
            'norm_status': 'pending',
            'from_catalog': False,
            'match_method': None,
            'confidence': 0,
            'confidence_level': 'very_low',
            'warnings': [],
            'corrections': [],
        }

        # ═════════════════════════════════════════════
        # PASO 0: VALIDAR Y SANEAR AÑO
        # ═════════════════════════════════════════════

        año_desc = extract_year_from_description(descripcion or "")
        if not año_desc:
            año_desc = extract_year_from_description(titulo or "")

        año_quality = assess_year_quality(
            año_structured=año_raw,
            año_description=año_desc,
            km=km_raw,
        )

        año = año_quality['best_year']
        result['año_detectado'] = año
        result['año_fuente'] = año_quality['year_source']

        if año_quality['quality'] == 'garbage':
            result['año_sospechoso'] = True
            result['warnings'].append(
                f"Año basura detectado: {año_quality['reason']}"
            )
            STATS.year_garbage_detected += 1
            if año_quality['year_source'] == 'description':
                STATS.year_from_description += 1

        elif año_quality['quality'] == 'suspicious':
            result['año_sospechoso'] = True
            result['warnings'].append(
                f"Año sospechoso: {año_quality['reason']}"
            )

        # Si no tenemos año de ninguna fuente, intentar del texto
        if not año:
            año = extract_year_from_text(titulo_n)
            if año:
                result['año_detectado'] = año
                result['año_fuente'] = 'title'
            else:
                año = extract_year_from_text(text_all)
                if año:
                    result['año_detectado'] = año
                    result['año_fuente'] = 'text'

        # ═════════════════════════════════════════════
        # PASO 1: ENCONTRAR MARCA
        # ═════════════════════════════════════════════

        marca_result = self._find_marca(marca_n, text_all)
        if not marca_result:
            STATS.fallback_used += 1
            result['norm_status'] = 'no_marca'
            result['confidence'] = 5
            result['confidence_level'] = 'very_low'
            return result

        result['marca'] = marca_result['value']
        result['from_catalog'] = marca_result['from_catalog']
        marca = result['marca']

        # ═════════════════════════════════════════════
        # PASO 2: FILTRAR MOTORIZACIONES
        # ═════════════════════════════════════════════

        for candidate in [modelo_n, version_n]:
            if candidate and self._is_motorization(candidate, marca):
                STATS.motorization_filtered += 1
                result['warnings'].append(
                    f"'{candidate}' filtrado como motorización"
                )

        # ═════════════════════════════════════════════
        # PASO 3: ENCONTRAR MODELO (7 estrategias)
        # ═════════════════════════════════════════════

        modelo_result = self._find_modelo(
            marca=marca,
            titulo=titulo_n,
            modelo_raw=modelo_n,
            version_raw=version_n,
            descripcion=desc_n,
            text_high=text_high,
            text_medium=text_medium,
            text_all=text_all,
        )

        if modelo_result:
            result['modelo'] = modelo_result['value']
            result['match_method'] = modelo_result['method']
            if modelo_result['from_catalog']:
                result['from_catalog'] = True
            STATS.by_method[modelo_result['method']] = (
                STATS.by_method.get(modelo_result['method'], 0) + 1
            )

        # ═════════════════════════════════════════════
        # PASO 4: ENCONTRAR VERSIÓN
        # ═════════════════════════════════════════════

        if result['from_catalog'] and result['modelo']:
            version_result = self._find_version(
                marca=marca,
                modelo=result['modelo'],
                search_text=text_all,
            )
            if version_result:
                result['version'] = version_result['value']

        # ═════════════════════════════════════════════
        # PASO 5: POST-VALIDACIÓN (con tolerancia)
        # ═════════════════════════════════════════════

        if result['from_catalog'] and result['modelo']:
            self._post_validate(result, marca, año)

        # ═════════════════════════════════════════════
        # PASO 6: CONFIDENCE
        # ═════════════════════════════════════════════

        self._calculate_confidence(result)

        # ═════════════════════════════════════════════
        # PASO 7: STATUS FINAL
        # ═════════════════════════════════════════════

        self._set_final_status(result)

        return result

    # ───────────────────────────────────────────────
    # MARCA
    # ───────────────────────────────────────────────

    def _find_marca(self, marca_raw: str,
                    text: str) -> Optional[Dict]:
        if not self.catalog.is_loaded:
            if marca_raw:
                return {
                    'value': marca_raw,
                    'from_catalog': False,
                    'method': 'fallback'
                }
            return None

        if marca_raw:
            resolved = self.catalog.resolve_brand(marca_raw)
            if self.catalog.has_brand(resolved):
                return {
                    'value': resolved,
                    'from_catalog': True,
                    'method': 'exact'
                }

        for brand in self.catalog.get_brands():
            if find_exact_in_text(brand, text):
                return {
                    'value': brand,
                    'from_catalog': True,
                    'method': 'text_search'
                }

        if CONFIG.enable_fuzzy and marca_raw:
            match = fuzzy_match(
                marca_raw, self.catalog.get_brands(),
                CONFIG.fuzzy_threshold_marca
            )
            if match:
                return {
                    'value': match[0],
                    'from_catalog': True,
                    'method': 'fuzzy'
                }

        if marca_raw:
            return {
                'value': marca_raw,
                'from_catalog': False,
                'method': 'fallback'
            }
        return None

    # ───────────────────────────────────────────────
    # MOTORIZACIÓN FILTER
    # ───────────────────────────────────────────────

    def _is_motorization(self, text: str, marca: str) -> bool:
        text_norm = normalize_key(text)

        # Blacklist global del catálogo
        bl = self.catalog.motorization_blacklist
        if bl:
            exact = bl.get('exact', [])
            if text_norm in exact:
                return True
            for pattern in bl.get('regex', []):
                try:
                    if re.match(pattern, text_norm):
                        return True
                except re.error:
                    pass

        # Motorizaciones conocidas de la marca
        marca_motors = self.catalog.known_motorizations.get(
            normalize_key(marca), {}
        )
        if text_norm in marca_motors:
            return True

        # Patrones genéricos
        return is_motorization_pattern(text_norm)

    # ───────────────────────────────────────────────
    # MODELO (7 estrategias)
    # ───────────────────────────────────────────────

    def _find_modelo(
        self, marca, titulo, modelo_raw, version_raw,
        descripcion, text_high, text_medium, text_all,
    ) -> Optional[Dict]:

        marca_norm = normalize_key(marca)
        modelos_catalogo = self.catalog.get_models(marca)

        if not modelos_catalogo:
            return self._fallback_modelo(
                modelo_raw, version_raw, titulo, marca
            )

        combined = f"{version_raw} {modelo_raw} {titulo} {text_all}"

        # ── 1. Match exacto ──
        r = self._try_exact_match(modelos_catalogo, combined)
        if r:
            return r

        # ── 2. Match por alias ──
        r = self._try_alias_match(marca_norm, combined)
        if r:
            return r

        # ── 3. Mapeo de códigos ──
        r = self._try_code_mapping(
            marca_norm, marca, modelos_catalogo, combined
        )
        if r:
            return r

        # ── 4. Match de nombre base ──
        r = self._try_base_name_match(
            modelos_catalogo,
            [version_raw, modelo_raw, titulo]
        )
        if r:
            return r

        # ── 5. NUEVO: Modelo en descripción ──
        r = self._try_description_match(
            marca, descripcion, modelos_catalogo
        )
        if r:
            return r

        # ── 6. NUEVO: Split version_raw ──
        r = self._try_version_split(
            marca, version_raw, modelos_catalogo
        )
        if r:
            return r

        # ── 7. Fuzzy (solo no-numéricos) ──
        r = self._try_fuzzy_match(marca, modelos_catalogo, combined)
        if r:
            return r

        # ── 8. Fallback ──
        return self._fallback_modelo(
            modelo_raw, version_raw, titulo, marca
        )

    def _try_exact_match(self, modelos: List[str],
                         text: str) -> Optional[Dict]:
        for modelo in modelos:
            if find_exact_in_text(modelo, text):
                return {
                    'value': modelo,
                    'from_catalog': True,
                    'method': 'exact'
                }
        return None

    def _try_alias_match(self, marca_norm: str,
                         text: str) -> Optional[Dict]:
        aliases = self.catalog.model_aliases.get(marca_norm, {})
        for alias, modelo_real in aliases.items():
            if find_exact_in_text(alias, text):
                if self.catalog.has_model(marca_norm, modelo_real):
                    return {
                        'value': modelo_real,
                        'from_catalog': True,
                        'method': 'alias'
                    }
        return None

    def _try_code_mapping(
        self, marca_norm, marca, modelos, text,
    ) -> Optional[Dict]:
        codes = self.catalog.code_mappings.get(marca_norm, {})
        if not codes:
            return None

        sorted_codes = sorted(codes.keys(), key=len, reverse=True)

        for code in sorted_codes:
            if find_exact_in_text(code, text):
                mapped = codes[code]
                if isinstance(mapped, dict):
                    if mapped.get('ambiguo'):
                        modelo_real = mapped.get('modelos', [None])[0]
                    else:
                        continue
                else:
                    modelo_real = mapped

                if modelo_real and self.catalog.has_model(
                    marca, modelo_real
                ):
                    return {
                        'value': modelo_real,
                        'from_catalog': True,
                        'method': 'code_mapping'
                    }
        return None

    def _try_base_name_match(
        self, modelos: List[str], text_sources: List[str],
    ) -> Optional[Dict]:
        words_to_check = []
        for text in text_sources:
            if text:
                words = [
                    w for w in text.split()
                    if len(w) >= 3 and not re.match(r'^\d+$', w)
                ]
                words_to_check.extend(words)

        for word in words_to_check:
            word_clean = re.sub(r'\d+', '', word).strip()
            if word_clean and len(word_clean) >= 3:
                for modelo in modelos:
                    if (word_clean == modelo
                            or find_exact_in_text(word_clean, modelo)):
                        return {
                            'value': modelo,
                            'from_catalog': True,
                            'method': 'base_name'
                        }
        return None

    def _try_description_match(
        self, marca: str, descripcion: str,
        modelos_catalogo: List[str],
    ) -> Optional[Dict]:
        """NUEVO v4.1: Busca modelo en la descripción ampliada."""
        if not descripcion:
            return None

        modelo = extract_model_from_description(
            descripcion, marca, modelos_catalogo
        )
        if modelo:
            STATS.model_from_description += 1
            return {
                'value': modelo,
                'from_catalog': True,
                'method': 'description'
            }
        return None

    def _try_version_split(
        self, marca: str, version_raw: str,
        modelos_catalogo: List[str],
    ) -> Optional[Dict]:
        """NUEVO v4.1: Separa version_raw en modelo+trim."""
        if not version_raw:
            return None

        split = split_version_into_model_and_trim(
            version_raw, modelos_catalogo, marca
        )
        if split:
            STATS.model_from_version_split += 1
            return {
                'value': split['model'],
                'from_catalog': True,
                'method': 'version_split'
            }
        return None

    def _try_fuzzy_match(
        self, marca: str, modelos: List[str], text: str,
    ) -> Optional[Dict]:
        if not CONFIG.enable_fuzzy or not modelos:
            return None

        fuzzy_candidates = [
            m for m in modelos if not is_numeric_model(m)
        ]
        if not fuzzy_candidates:
            STATS.numeric_exact_only += 1
            return None

        valid_words = [
            w for w in text.split()
            if (len(w) >= CONFIG.fuzzy_min_word_length
                and w.lower() not in FUZZY_BLACKLIST
                and not re.match(r'^(19|20)\d{2}$', w)
                and not re.match(r'^\d+$', w))
        ]

        for word in valid_words:
            match = fuzzy_match(
                word, fuzzy_candidates,
                CONFIG.fuzzy_threshold_modelo
            )
            if match:
                return {
                    'value': match[0],
                    'from_catalog': True,
                    'method': 'fuzzy'
                }
        return None

    def _fallback_modelo(
        self, modelo_raw, version_raw, titulo, marca,
    ) -> Optional[Dict]:
        combined = f"{version_raw} {modelo_raw} {titulo}"
        if marca:
            combined = re.sub(
                r'\b' + re.escape(marca) + r'\b',
                '', combined, flags=re.IGNORECASE
            )
            combined = re.sub(
                r'\b' + re.escape(marca.replace('-', ' ')) + r'\b',
                '', combined, flags=re.IGNORECASE
            )

        for word in combined.split():
            w = word.strip()
            if (len(w) >= 2
                    and not re.match(r'^(19|20)\d{2}$', w)
                    and w.lower() not in FUZZY_BLACKLIST
                    and not is_motorization_pattern(w)):
                return {
                    'value': w,
                    'from_catalog': False,
                    'method': 'fallback'
                }
        return None

    # ───────────────────────────────────────────────
    # VERSIÓN
    # ───────────────────────────────────────────────

    # ─────────────────────────────────────────────────
# VERSIÓN (MEJORADO v4.2 - match flexible)
# ─────────────────────────────────────────────────
def _find_version(self, marca, modelo, search_text) -> Optional[Dict]:
    versiones = self.catalog.get_versions(marca, modelo)
    if not versiones:
        return None

    search_norm = normalize_text(search_text)
    versiones_sorted = sorted(versiones, key=len, reverse=True)

    # ── 1. Match exacto completo (ideal) ──
    for version in versiones_sorted:
        if find_exact_in_text(version, search_norm):
            return {
                'value': version,
                'from_catalog': True,
                'method': 'exact'
            }

    # ── 2. Match normalizado (quitar puntos, guiones) ──
    for version in versiones_sorted:
        ver_compact = _normalize_version_text(version)
        search_compact = _normalize_version_text(search_norm)
        if ver_compact and find_exact_in_text(
            ver_compact, search_compact
        ):
            return {
                'value': version,
                'from_catalog': True,
                'method': 'exact_normalized'
            }

    # ── 3. Match por componentes (trim + motorización) ──
    best_match = None
    best_score = 0

    for version in versiones_sorted:
        score = _score_version_match(version, search_norm)
        if score > best_score:
            best_score = score
            best_match = version

    if best_match and best_score >= 60:
        return {
            'value': best_match,
            'from_catalog': True,
            'method': 'component_match'
        }

    # ── 4. Match solo por trim (LT, LTZ, etc.) ──
    trim_match = _match_trim_only(
        versiones_sorted, search_norm
    )
    if trim_match:
        return {
            'value': trim_match,
            'from_catalog': True,
            'method': 'trim_match'
        }

    # ── 5. Fuzzy (solo no-numéricos, restrictivo) ──
    if CONFIG.enable_fuzzy:
        valid_words = [
            w for w in search_norm.split()
            if (len(w) >= 3
                and w.lower() not in FUZZY_BLACKLIST)
        ]
        for word in valid_words:
            match = fuzzy_match(
                word, versiones,
                CONFIG.fuzzy_threshold_version
            )
            if match:
                return {
                    'value': match[0],
                    'from_catalog': True,
                    'method': 'fuzzy'
                }

    return None

    # ───────────────────────────────────────────────
    # POST-VALIDACIÓN (con tolerancia ±2 años)
    # ───────────────────────────────────────────────

    def _post_validate(self, result: Dict, marca: str,
                       año: Optional[int]):
        """
        Validación con TOLERANCIA:

        1. Si año dentro de (desde-TOLERANCE, hasta+TOLERANCE) → NO tocar
        2. Si año fuera pero <GARBAGE_THRESHOLD → buscar sucesor/predecesor
        3. Si año fuera y >GARBAGE_THRESHOLD → es basura, NO usar para
           cambiar modelo
        """
        modelo = result['modelo']
        tolerance = CONFIG.year_tolerance

        # ── Validación de año ──
        if año and not result.get('año_sospechoso'):
            yr = self.catalog.get_year_range(marca, modelo)
            if yr:
                desde = yr.get('desde', 0)
                hasta = yr.get('hasta', 9999)

                # Rango con tolerancia
                desde_tolerant = desde - tolerance
                hasta_tolerant = hasta + tolerance

                if desde_tolerant <= año <= hasta_tolerant:
                    # ✅ Dentro de tolerancia → NO tocar
                    pass

                else:
                    # Fuera de tolerancia → verificar si es basura
                    if año < desde:
                        distance = desde - año
                    else:
                        distance = año - hasta

                    if distance > CONFIG.year_garbage_threshold:
                        # 🗑️ Es basura → no cambiar modelo
                        result['warnings'].append(
                            f"Año {año} a {distance} años del "
                            f"rango de '{modelo}' ({desde}-{hasta})"
                            f" — año ignorado por posible error"
                        )
                    else:
                        # 🔄 Buscar sucesor/predecesor
                        self._try_year_correction(
                            result, marca, modelo, año,
                            desde, hasta
                        )

        # ── Verificar confusiones ──
        confusions = self.catalog.get_confusions(marca, result['modelo'])
        if confusions:
            names = [c.get('modelo_confundido', '?') for c in confusions]
            result['warnings'].append(
                f"'{result['modelo']}' se confunde con: "
                f"{', '.join(names)}"
            )
            STATS.confusion_detected += 1

        # ── Modelo padre ──
        parent_key = make_key(marca, result['modelo'])
        parent = self.catalog.model_parent_map.get(parent_key)
        if parent:
            result['warnings'].append(
                f"'{result['modelo']}' es variante de '{parent}'"
            )

    def _try_year_correction(
        self, result: Dict, marca: str, modelo: str,
        año: int, desde: int, hasta: int,
    ):
        """
        Busca sucesor/predecesor caminando la cadena.
        El modelo sugerido debe cubrir el año dentro de su
        rango REAL (sin tolerancia).
        """
        # Primero intentar suggestion del catálogo
        suggestion = self.catalog.get_year_suggestion(
            marca, modelo, año
        )
        if suggestion and self.catalog.has_model(marca, suggestion):
            sug_yr = self.catalog.get_year_range(marca, suggestion)
            if sug_yr:
                sd = sug_yr.get('desde', 0)
                sh = sug_yr.get('hasta', 9999)
                if sd <= año <= sh:
                    self._apply_year_correction(
                        result, modelo, suggestion,
                        desde, hasta, sd, sh, año
                    )
                    return

        # Caminar cadena de sucesión
        if año > hasta:
            self._walk_successors(
                result, marca, modelo, año, desde, hasta
            )
        elif año < desde:
            self._walk_predecessors(
                result, marca, modelo, año, desde, hasta
            )

    def _walk_successors(
        self, result, marca, modelo, año, desde, hasta,
    ):
        """Camina sucesores buscando uno cuyo rango cubra el año."""
        current = modelo
        visited = {current}
        max_depth = 5

        for _ in range(max_depth):
            sucesor = self.catalog.get_successor(marca, current)
            if not sucesor or sucesor in visited:
                break
            visited.add(sucesor)

            if self.catalog.has_model(marca, sucesor):
                syr = self.catalog.get_year_range(marca, sucesor)
                if syr:
                    sd = syr.get('desde', 0)
                    sh = syr.get('hasta', 9999)
                    if sd <= año <= sh:
                        self._apply_year_correction(
                            result, modelo, sucesor,
                            desde, hasta, sd, sh, año
                        )
                        return
            current = sucesor

    def _walk_predecessors(
        self, result, marca, modelo, año, desde, hasta,
    ):
        """Camina predecesores buscando uno cuyo rango cubra el año."""
        current = modelo
        visited = {current}
        max_depth = 5

        for _ in range(max_depth):
            predecesor = self.catalog.get_predecessor(marca, current)
            if not predecesor or predecesor in visited:
                break
            visited.add(predecesor)

            if self.catalog.has_model(marca, predecesor):
                pyr = self.catalog.get_year_range(marca, predecesor)
                if pyr:
                    pd = pyr.get('desde', 0)
                    ph = pyr.get('hasta', 9999)
                    if pd <= año <= ph:
                        self._apply_year_correction(
                            result, modelo, predecesor,
                            desde, hasta, pd, ph, año
                        )
                        return
            current = predecesor

    def _apply_year_correction(
        self, result, old_modelo, new_modelo,
        old_desde, old_hasta, new_desde, new_hasta, año,
    ):
        """Aplica una corrección de modelo por año."""
        result['corrections'].append({
            'tipo': 'year_mismatch',
            'original': old_modelo,
            'corregido': new_modelo,
            'razon': (
                f"'{old_modelo}' existió {old_desde}-{old_hasta}, "
                f"año {año} corresponde a '{new_modelo}' "
                f"({new_desde}-{new_hasta})"
            )
        })
        result['modelo'] = new_modelo
        STATS.year_corrected += 1

    # ───────────────────────────────────────────────
    # CONFIDENCE SCORE
    # ───────────────────────────────────────────────

    def _calculate_confidence(self, result: Dict):
        method = result.get('match_method', 'fallback')

        method_scores = {
            'exact': 90,
            'alias': 85,
            'code_mapping': 80,
            'description': 78,
            'version_split': 76,
            'base_name': 70,
            'fuzzy': 55,
            'fallback': 15,
        }
        score = method_scores.get(method, 10)

        # Bonus: versión del catálogo
        if result.get('version') and result.get('from_catalog'):
            score += 5

        # Bonus: año confirmado dentro de rango
        if (result.get('año_detectado')
                and result.get('from_catalog')
                and result.get('modelo')
                and not result.get('año_sospechoso')):
            yr = self.catalog.get_year_range(
                result['marca'], result['modelo']
            )
            if yr:
                d = yr.get('desde', 0)
                h = yr.get('hasta', 9999)
                if d <= result['año_detectado'] <= h:
                    score += 5

        # Penalización: año sospechoso
        if result.get('año_sospechoso'):
            score -= 5

        # Penalización: correcciones por año
        if result.get('corrections'):
            score -= 10

        # Penalización: confusión
        if any('confunde' in w for w in result.get('warnings', [])):
            score -= 5

        # Penalización: numérico + fuzzy
        if (method == 'fuzzy'
                and result.get('modelo')
                and is_numeric_model(result['modelo'])):
            score -= 20

        score = max(0, min(100, score))
        result['confidence'] = score

        if score >= 80:
            result['confidence_level'] = 'high'
            STATS.by_confidence['high'] = (
                STATS.by_confidence.get('high', 0) + 1
            )
        elif score >= 60:
            result['confidence_level'] = 'medium'
            STATS.by_confidence['medium'] = (
                STATS.by_confidence.get('medium', 0) + 1
            )
        elif score >= 40:
            result['confidence_level'] = 'low'
            STATS.by_confidence['low'] = (
                STATS.by_confidence.get('low', 0) + 1
            )
        else:
            result['confidence_level'] = 'very_low'
            STATS.by_confidence['very_low'] = (
                STATS.by_confidence.get('very_low', 0) + 1
            )

    # ───────────────────────────────────────────────
    # STATUS FINAL
    # ───────────────────────────────────────────────

    def _set_final_status(self, result: Dict):
        if result['from_catalog']:
            if result['version']:
                result['norm_status'] = 'full_match'
                STATS.full_match += 1
            elif result['modelo']:
                result['norm_status'] = 'partial_match'
                STATS.partial_match += 1
            else:
                result['norm_status'] = 'marca_only'
                STATS.marca_only += 1
        else:
            if result.get('modelo'):
                result['norm_status'] = 'fallback'
            else:
                result['norm_status'] = 'no_modelo'
            STATS.fallback_used += 1


# Instancia global
NORMALIZER = VehicleNormalizerV4()


# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIONES PÚBLICAS (API)
# ═══════════════════════════════════════════════════════════════

def init_normalizer(
    dicts_path: str = None,
    brand_aliases_path: str = None,
) -> bool:
    global CATALOG, NORMALIZER, STATS
    STATS = NormalizationStats()
    dicts_path = dicts_path or CONFIG.dicts_path
    brand_aliases_path = brand_aliases_path or CONFIG.brand_aliases_path
    loaded = CATALOG.load(dicts_path, brand_aliases_path)
    NORMALIZER = VehicleNormalizerV4(CATALOG)
    return loaded


def normalize_vehicle(
    titulo: str = "",
    descripcion: str = "",
    marca_raw: str = "",
    modelo_raw: str = "",
    version_raw: str = "",
    año_raw: Optional[int] = None,
    precio_raw: Optional[float] = None,
    km_raw: Optional[int] = None,
) -> Dict:
    return NORMALIZER.normalize(
        titulo=titulo,
        descripcion=descripcion,
        marca_raw=marca_raw,
        modelo_raw=modelo_raw,
        version_raw=version_raw,
        año_raw=año_raw,
        precio_raw=precio_raw,
        km_raw=km_raw,
    )


def get_normalization_stats() -> NormalizationStats:
    return STATS


def get_stats() -> NormalizationStats:
    return STATS


def get_catalog() -> EnrichedCatalog:
    return CATALOG


# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format='%(levelname)s: %(message)s'
    )

    print("=" * 70)
    print("🧪 PRUEBA DEL NORMALIZADOR v4.1")
    print("=" * 70)

    loaded = init_normalizer("config/normalizer_dicts.json")
    print(f"\n📚 Catálogo cargado: {loaded}")
    if not loaded:
        print("❌ No se pudo cargar el catálogo")
        exit(1)

    test_cases = [
        # ── Holgura de año: NO cambiar si está dentro de ±2 ──
        {
            "name": "✅ Peugeot 206 año 2011 (dentro de holgura +1)",
            "titulo": "Peugeot 206 2011",
            "marca_raw": "Peugeot",
            "modelo_raw": "206",
            "año_raw": 2011,
            "expected": {"marca": "peugeot", "modelo": "206"},
            "note": "206 terminó en 2010, pero 2011 está dentro de ±2"
        },
        {
            "name": "✅ Peugeot 206 año 2012 (borde de holgura +2)",
            "titulo": "Peugeot 206 2012",
            "marca_raw": "Peugeot",
            "modelo_raw": "206",
            "año_raw": 2012,
            "expected": {"marca": "peugeot", "modelo": "206"},
            "note": "2012 = 2010+2, justo en el borde"
        },
        {
            "name": "🔄 Peugeot 206 año 2015 → debe corregir a 208",
            "titulo": "Peugeot 206 2015",
            "marca_raw": "Peugeot",
            "modelo_raw": "206",
            "año_raw": 2015,
            "expected": {"marca": "peugeot", "modelo": "208"},
            "note": "2015 está 3 años fuera → buscar sucesor"
        },

        # ── Año basura ──
        {
            "name": "🗑️ Fiat Europa año 2026 (basura, desc dice mod 80)",
            "titulo": "Fiat Europa",
            "marca_raw": "Fiat",
            "version_raw": "Europa",
            "año_raw": 2026,
            "km_raw": 111111,
            "descripcion": "Fiat europa mod 80. Motor 1300 con leva del 1.4.",
            "expected": {"marca": "fiat", "modelo": "europa"},
            "note": "Año 2026 es futuro → basura. Desc dice mod 80 → 1980"
        },
        {
            "name": "🗑️ Auto año 2026 con 111k km (imposible)",
            "titulo": "Ford Focus 2026",
            "marca_raw": "Ford",
            "modelo_raw": "Focus",
            "año_raw": 2026,
            "km_raw": 111111,
            "expected": {"marca": "ford", "modelo": "focus"},
            "note": "2026 + 111k km = año basura"
        },

        # ── Modelo numérico: NO fuzzy ──
        {
            "name": "✅ Peugeot 208 (no confundir con 206 por fuzzy)",
            "titulo": "Peugeot 208 Allure 2015",
            "marca_raw": "Peugeot",
            "modelo_raw": "208",
            "expected": {"marca": "peugeot", "modelo": "208"}
        },

        # ── Código mapeado ──
        {
            "name": "Mercedes C200 → Clase C",
            "titulo": "Mercedes Benz C200 Año 2000",
            "marca_raw": "Mercedes Benz",
            "version_raw": "C200",
            "expected": {"marca": "mercedes-benz", "modelo": "clase c"}
        },

        # ── BMW código ──
        {
            "name": "BMW 320i → Serie 3",
            "titulo": "BMW 320i Sport 2019",
            "marca_raw": "BMW",
            "version_raw": "320i Sport",
            "expected": {"marca": "bmw", "modelo": "serie 3"}
        },

        # ── Motorización no es modelo ──
        {
            "name": "'2.0 TDI' es motorización, no modelo",
            "titulo": "Volkswagen Amarok 2.0 TDI Highline",
            "marca_raw": "Volkswagen",
            "modelo_raw": "2.0 TDI",
            "version_raw": "Highline",
            "expected": {"marca": "volkswagen", "modelo": "amarok"}
        },

        # ── VW alias ──
        {
            "name": "VW Amarok (alias vw→volkswagen)",
            "titulo": "VW Amarok Highline 4x4",
            "marca_raw": "VW",
            "version_raw": "Amarok Highline",
            "expected": {"marca": "volkswagen", "modelo": "amarok"}
        },

        # ── Modelo de descripción ──
        {
            "name": "Modelo encontrado en descripción ampliada",
            "titulo": "Fiat",
            "marca_raw": "Fiat",
            "version_raw": "1.6 16v",
            "descripcion": "Vendo Fiat Palio Weekend 1.6 16v, excelente estado",
            "expected": {"marca": "fiat", "modelo": "palio"},
            "note": "version_raw es motorización, modelo está en descripción"
        },

        # ── 1300cc no es modelo ──
        {
            "name": "'1300' es cilindrada, no modelo",
            "titulo": "Fiat 600",
            "marca_raw": "Fiat",
            "version_raw": "600",
            "descripcion": "Motor 1300 con leva del 1.4",
            "expected": {"marca": "fiat", "modelo": "600"},
            "note": "1300 en descripción es motor, no modelo"
        },
    ]

    print("\n" + "─" * 70)
    passed = 0
    failed = 0

    for test in test_cases:
        print(f"\n📋 {test['name']}")
        if test.get('note'):
            print(f"   💡 {test['note']}")
        print(f"   Título: {test.get('titulo', '')}")
        print(f"   Marca: {test.get('marca_raw', '')}")
        print(f"   Modelo: {test.get('modelo_raw', '')}")
        print(f"   Version: {test.get('version_raw', '')}")
        if test.get('año_raw'):
            print(f"   Año raw: {test['año_raw']}")
        if test.get('km_raw'):
            print(f"   Km raw: {test['km_raw']}")
        if test.get('descripcion'):
            desc_short = test['descripcion'][:60]
            print(f"   Desc: {desc_short}...")

        result = normalize_vehicle(
            titulo=test.get('titulo', ''),
            descripcion=test.get('descripcion', ''),
            marca_raw=test.get('marca_raw', ''),
            modelo_raw=test.get('modelo_raw', ''),
            version_raw=test.get('version_raw', ''),
            año_raw=test.get('año_raw'),
            km_raw=test.get('km_raw'),
        )

        expected = test.get('expected', {})
        marca_ok = result['marca'] == expected.get('marca')
        modelo_ok = result['modelo'] == expected.get('modelo')
        version_ok = (
            result['version'] == expected.get('version')
            or 'version' not in expected
        )
        all_ok = marca_ok and modelo_ok and version_ok

        if all_ok:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1

        print(f"\n   {status}")
        print(
            f"   marca: {result['marca']}",
            "✓" if marca_ok
            else f"✗ (esperado: {expected.get('marca')})"
        )
        print(
            f"   modelo: {result['modelo']}",
            "✓" if modelo_ok
            else f"✗ (esperado: {expected.get('modelo')})"
        )
        print(f"   version: {result['version']}")
        print(f"   method: {result.get('match_method')}")
        print(
            f"   confidence: {result['confidence']} "
            f"({result['confidence_level']})"
        )
        print(
            f"   año: {result.get('año_detectado')} "
            f"(fuente: {result.get('año_fuente')})"
        )
        if result.get('año_sospechoso'):
            print(f"   ⚠️ AÑO SOSPECHOSO")
        if result.get('warnings'):
            for w in result['warnings']:
                print(f"   ⚠️ {w}")
        if result.get('corrections'):
            for c in result['corrections']:
                print(f"   🔧 {c.get('razon', c)}")

    print("\n" + "═" * 70)
    print(f"📊 RESUMEN: {passed} passed, {failed} failed")
    print(f"📈 Stats: {STATS.summary()}")
    print(f"📊 Por método: {dict(STATS.by_method)}")
    print(f"📊 Por confianza: {dict(STATS.by_confidence)}")
    print("=" * 70)
